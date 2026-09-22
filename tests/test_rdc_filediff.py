"""Tests for `diff`: two captures compared by what their streams recorded (rdc_filediff.py).

The captures here are built from the payload builders in `rdc_fixtures`, so a layout change breaks
these tests rather than passing quietly, and the chunk-name map is the fake `renderdoc-src` tree --
nothing here needs a real capture, a GPU or a device.

What the tests pin, in the order the module answers them: what one stream's records are (`call_records`,
including the per-command-list setters and the fact that a binding is keyed by what it *is*), how two
path lists are paired (`align_paths`), how two call lists are compared (`align_calls`), and what the
command prints and returns (`cmd_filediff`, including the csv shape and the exit codes).

Run directly, via unittest, or through the tool:

    python tests/test_rdc_filediff.py
    python rdc_analysis.py selftest -k Filediff
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402
import rdc_chunkmap               # noqa: E402  (patched: the loader is called from inside functions)
import rdc_fixtures as F          # noqa: E402
from rdc_testcase import CmdCase as _CmdCase   # noqa: E402

#: The root signature every fixture creates: `rp0` is four root constants, `rp1` a CBV at b1 s0 --
#: the second one is what makes "the binding is keyed by what it is, not by its index" testable.
PARAMS: Sequence[Any] = (('32bit', 0, 0, 0, 4, []), ('cbv', 0, 1, 0, 0, []))


class DiffCase(_CmdCase):
    """`CmdCase` (fake source tree, capture builder) plus the fixture and output helpers."""

    def out(self, fn: Callable[..., object], *args: Any, **kwargs: Any) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args, **kwargs)
        return buf.getvalue()

    def streams(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Tuple[str, str, Any]:
        """`(stdout, stderr, return value)` -- the notes of a `--format` run go to stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(*args, **kwargs)
        return out.getvalue(), err.getvalue(), code

    def dispatch(self, argv: Sequence[str]) -> Tuple[str, Any]:
        """Run the real entry point with `argv` and return `(stdout, exit code)`."""
        buf = io.StringIO()
        with mock.patch.object(sys, 'argv', ['rdc_analysis.py'] + list(argv)):
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as caught:
                    R.main()
        return buf.getvalue(), caught.exception.code

    # --- fixtures -------------------------------------------------------------------------------
    def marker(self, name: str) -> bytes:
        return self.ch('PushMarker', name.encode('utf-8') + b'\x00')

    def pop(self) -> bytes:
        return self.ch('PopMarker')

    def rootsig(self, rid: int = 700, params: Sequence[Any] = PARAMS) -> bytes:
        return self.ch('Device_CreateRootSignature',
                       F.pl_create_root_sig(rid, F.root_signature(list(params))))

    def flat(self, *parts: Any) -> List[bytes]:
        """Chunks with the list-returning helpers (`buffer`) flattened in, in order."""
        out: List[bytes] = []
        for part in parts:
            out.extend(part if isinstance(part, list) else [part])
        return out

    def buffer(self, rid: int, name: str = '', size: int = 4096) -> List[bytes]:
        out = [self.ch('Device_CreateCommittedResource',
                       F.pl_committed_resource(rid, F.pl_resource_desc(1, width=size)))]
        if name:
            out.append(self.ch('SetName', F.pl_set_name(rid, name)))
        return out

    def scene(self, *, name: str = 'SceneUniformBuffer', index_count: int = 3, target: int = 200,
              target_name: str = 'SceneColor', pso: bool = True,
              root_view: bool = True) -> List[bytes]:
        """One pass, one draw: a signature, two buffers, a target binding and a root CBV."""
        chunks = [self.rootsig()]
        chunks += self.buffer(100, name)
        chunks += self.buffer(target, target_name)
        chunks += [self.marker('Pass'),
                   self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 700))]
        if pso:
            chunks += [self.ch('List_SetPipelineState', F.pl_pso(7, 55))]
        chunks += [self.ch('List_OMSetRenderTargets', F.pl_omset(7, [target]))]
        if root_view:
            chunks += [self.ch('List_SetGraphicsRootConstantBufferView',
                               F.pl_root_view(7, 1, 100, 0))]
        chunks += [self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, index_count, 1, 0, 0, 0)),
                   self.pop()]
        return chunks

    def two(self, a: Sequence[bytes], b: Sequence[bytes]) -> Tuple[str, str]:
        return (self.path('a.rdc', F.capture(list(a))), self.path('b.rdc', F.capture(list(b))))

    def rec(self, path: str = 'Pass', call: str = 'DrawIndexedInstanced', ordinal: int = 1,
            args: str = 'idx=3 inst=1', setters: Sequence[str] = (),
            bindings: Optional[Dict[str, str]] = None) -> R.CallRecord:
        return R.CallRecord(path=path, call=call, ordinal=ordinal, args=args,
                            setters=tuple(setters), bindings=dict(bindings or {}))


# =========================================================================== one stream's records
class TestCallRecords(DiffCase):
    def test_a_draw_is_recorded_with_its_path_call_and_arguments(self):
        path = self.cap(*self.scene(index_count=48))
        records = R.call_records(path)
        assert records is not None
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].path, 'Pass')
        self.assertEqual(records[0].call, 'DrawIndexedInstanced')
        self.assertEqual(records[0].args, 'idx=48 inst=1')
        self.assertEqual(records[0].ordinal, 1)

    def test_a_call_outside_every_marker_says_so_rather_than_having_no_path(self):
        path = self.cap(self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        records = R.call_records(path)
        assert records is not None
        self.assertEqual(records[0].path, R.NO_MARKER)

    def test_the_nested_path_is_the_whole_chain(self):
        chunks = [self.marker('Scene'), self.marker('BasePass'),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)),
                  self.pop(), self.pop()]
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].path, 'Scene > BasePass')

    def test_setters_are_the_chunks_since_the_previous_call_of_that_list(self):
        chunks = self.flat(self.marker('Pass'),
                           self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 700)),
                           self.ch('List_SetPipelineState', F.pl_pso(7, 55)),
                           self.ch('List_OMSetRenderTargets', F.pl_omset(7, [200])),
                           self.ch('List_SetGraphicsRootConstantBufferView',
                                   F.pl_root_view(7, 1, 100, 0)),
                           self.ch('List_DrawIndexedInstanced',
                                   F.pl_draw_indexed(7, 3, 1, 0, 0, 0)),
                           self.ch('List_SetGraphicsRootConstantBufferView',
                                   F.pl_root_view(7, 1, 100, 64)),
                           self.ch('List_DrawIndexedInstanced',
                                   F.pl_draw_indexed(7, 9, 1, 0, 0, 0)),
                           self.pop())
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].setters,
                         ('SetGraphicsRootSignature', 'SetPipelineState', 'OMSetRenderTargets',
                          'SetGraphicsRootConstantBufferView'))
        self.assertEqual(records[1].setters, ('SetGraphicsRootConstantBufferView',))
        self.assertEqual(records[1].ordinal, 2)

    def test_a_second_command_list_keeps_its_own_setters(self):
        chunks = [self.ch('List_SetPipelineState', F.pl_pso(7, 55)),
                  self.ch('List_SetPipelineState', F.pl_pso(8, 66)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(8, 3, 1, 0, 0, 0)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].setters, ('SetPipelineState',))
        self.assertEqual(records[1].setters, ('SetPipelineState',))

    def test_a_dispatch_is_a_call_too_and_reports_compute_bindings(self):
        chunks = self.flat(self.rootsig(), self.buffer(100, 'LightGrid'),
                           self.ch('List_SetComputeRootSignature', F.pl_root_signature(7, 700)),
                           self.ch('List_SetComputeRootConstantBufferView',
                                   F.pl_root_view(7, 1, 100, 0)),
                           self.ch('List_Dispatch', F.pl_dispatch(7, 12, 7, 1)))
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].call, 'Dispatch')
        self.assertEqual(records[0].args, 'x=12 y=7 z=1')
        self.assertEqual(records[0].bindings['cbv b1 s0'], 'LightGrid')
        self.assertNotIn('RTV', records[0].bindings)      # targets are graphics state

    def test_no_chunk_names_means_not_looked_at(self):
        path = self.cap(*self.scene())
        with mock.patch.object(rdc_chunkmap, 'load_chunk_names', lambda *a, **kw: {}):
            self.assertIsNone(R.call_records(path))

    def test_the_byte_offset_of_a_binding_is_not_part_of_its_value(self):
        # A sub-allocation's offset inside a UE page is the recording's own layout (REFERENCE 4.9).
        chunks = self.flat(self.rootsig(), self.buffer(100, 'Resource Allocator Underlying Buffer'),
                           self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 700)),
                           self.ch('List_SetGraphicsRootConstantBufferView',
                                   F.pl_root_view(7, 1, 100, 64)),
                           self.ch('List_DrawIndexedInstanced',
                                   F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].bindings['cbv b1 s0'], 'Resource Allocator Underlying Buffer')


# =========================================================================== bindings and descriptions
class TestCallBindings(DiffCase):
    def test_bindings_are_keyed_by_what_the_slot_is_not_by_its_index(self):
        chunks = self.flat(self.rootsig(), self.buffer(100, 'SceneUniformBuffer'),
                           self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 700)),
                           self.ch('List_SetGraphicsRootConstantBufferView',
                                   F.pl_root_view(7, 1, 100, 0)),
                           self.ch('List_SetPipelineState', F.pl_pso(7, 55)),
                           self.ch('List_OMSetRenderTargets', F.pl_omset(7, [200])),
                           self.ch('List_DrawIndexedInstanced',
                                   F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        bindings = records[0].bindings
        self.assertEqual(bindings['cbv b1 s0'], 'SceneUniformBuffer')   # rp1, and no `rp1` in the key
        self.assertIn('rootsig', bindings)
        self.assertIn('RTV', bindings)
        self.assertEqual(bindings['PSO'], 'bound')

    def test_a_stage_visible_parameter_carries_its_stage(self):
        params = (('cbv', 1, 0, 0, 0, []),)      # visibility 1 = vs (`rdc_resources.VISIBILITIES`)
        chunks = self.flat(self.rootsig(params=params), self.buffer(100, 'PerView'),
                           self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 700)),
                           self.ch('List_SetGraphicsRootConstantBufferView',
                                   F.pl_root_view(7, 0, 100, 0)),
                           self.ch('List_DrawIndexedInstanced',
                                   F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].bindings['vs cbv b0 s0'], 'PerView')

    def test_a_table_binding_is_named_by_its_ranges_and_the_slot_it_resolves_to(self):
        params = (('table', 0, 0, 0, 0, [('srv', 0, 5, 0, 0)]),)
        chunks = self.flat(self.rootsig(params=params),
                           self.ch('Device_CreateDescriptorHeap', F.pl_descriptor_heap(300)),
                           self.buffer(100, 'SkyViewLut'),
                           self.ch('Device_CreateShaderResourceView',
                                   F.pl_descriptor_write(100, 300, 7)),
                           self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 700)),
                           self.ch('List_SetGraphicsRootDescriptorTable',
                                   F.pl_root_table(7, 0, 300, 7)),
                           self.ch('List_DrawIndexedInstanced',
                                   F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].bindings['table t0 n5 s0'], 'srv SkyViewLut')

    def test_a_table_slot_the_capture_never_wrote_says_so(self):
        params = (('table', 0, 0, 0, 0, [('srv', 0, 5, 0, 0)]),)
        chunks = [self.rootsig(params=params),
                  self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 700)),
                  self.ch('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 0, 300, 7)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].bindings['table t0 n5 s0'], 'unwritten slot')

    def test_a_draw_whose_list_never_set_state_has_nothing_to_report(self):
        # The empty `RTV`/`DSV` keys belong to a *state* that exists: with no setter chunk at all there
        # is nothing to say about this list, and inventing "nothing was bound" would be a claim.
        chunks = [self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].bindings, {})

    def test_a_reset_makes_the_empty_slots_explicit(self):
        chunks = self.flat(self.ch('List_Reset', F.pl_reset(7)),
                           self.buffer(200, 'SceneColor'),
                           self.ch('List_DrawIndexedInstanced',
                                   F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].bindings, {'RTV': '', 'DSV': ''})
        self.assertEqual(records[0].setters, ('Reset',))

    def test_vertex_streams_and_the_index_buffer_carry_their_shape(self):
        chunks = self.flat(self.buffer(100, 'Verts'), self.buffer(101, 'Indices'),
                           self.ch('List_IASetVertexBuffers',
                                   F.pl_vertex_buffers(7, 0, [(100, 0, 65536, 24)])),
                           self.ch('List_IASetIndexBuffer', F.pl_index_buffer(7, 101, 0, 36, 42)),
                           self.ch('List_DrawIndexedInstanced',
                                   F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        records = R.call_records(self.cap(*chunks))
        assert records is not None
        self.assertEqual(records[0].bindings['VB0'], 'Verts (size 65536, stride 24)')
        self.assertEqual(records[0].bindings['IB'], 'Indices')

    def test_a_resource_the_capture_did_not_create_is_described_but_not_invented(self):
        self.assertEqual(R._describe_resource({}, 100), 'not in the resource table')
        table = {5: R.ResourceInfo(kind='buffer', name='', size=4096, width=4096, height=1, depth=1,
                                  mips=1, format=0, gpuAddress=0)}
        self.assertEqual(R._describe_resource(table, 5), 'buffer 4096 B')
        texture = {6: R.ResourceInfo(kind='texture2d', name='', size=0, width=1024, height=1024,
                                     depth=1, mips=1, format=28, gpuAddress=0)}
        self.assertEqual(R._describe_resource(texture, 6), 'texture2d 1024x1024x1')
        named = {7: R.ResourceInfo(kind='texture2d', name='SceneColor', size=0, width=64, height=64,
                                   depth=1, mips=1, format=28, gpuAddress=0)}
        self.assertEqual(R._describe_resource(named, 7), 'SceneColor')


# =========================================================================== alignment
class TestAlignPaths(DiffCase):
    def test_the_same_paths_pair_one_to_one(self):
        paired, renamed = R.align_paths(['Scene', 'Scene > BasePass'], ['Scene', 'Scene > BasePass'])
        self.assertEqual(paired, {0: 0, 1: 1})
        self.assertEqual(renamed, [])

    def test_a_dynamic_path_pairs_by_its_innermost_name_and_is_reported(self):
        # The real shape: the dynamic text is a *prefix* (`CullLights 22x14x8` against `CullLights
        # 32x20x8`, the same pass with a different light grid), and the leaf is the stable half.
        paired, renamed = R.align_paths(['CullLights 22x14x8 > Shadow', 'Scene'],
                                        ['Scene', 'CullLights 32x20x8 > Shadow'])
        self.assertEqual(paired, {0: 1, 1: 0})
        self.assertEqual(renamed, [('CullLights 22x14x8 > Shadow', 'CullLights 32x20x8 > Shadow')])

    def test_a_marker_the_file_could_not_name_pairs_by_its_path_text(self):
        # A deliberate consequence of reusing `rdc_passdiff`'s rules rather than writing a second set:
        # `UNNAMED` is a path *text* like any other, so two of them pair, where a path on one side only
        # would not. Two commands that aligned the same pair of frames differently would be worse.
        paired, _renamed = R.align_paths([R.UNNAMED], [R.UNNAMED])
        self.assertEqual(paired, {0: 0})


class TestAlignCalls(DiffCase):
    def test_identical_calls_count_as_same_and_produce_no_rows(self):
        rows, counts = R.align_calls([self.rec(bindings={'RTV': 'SceneColor'})],
                                     [self.rec(bindings={'RTV': 'SceneColor'})])
        self.assertEqual(rows, [])
        self.assertEqual(counts, {'same': 1, 'changed': 0, 'only A': 0, 'only B': 0})

    def test_a_changed_argument_is_one_row(self):
        rows, counts = R.align_calls([self.rec(args='idx=3 inst=1')], [self.rec(args='idx=9 inst=1')])
        self.assertEqual((rows[0].status, rows[0].field, rows[0].a, rows[0].b),
                         ('changed', 'args', 'idx=3 inst=1', 'idx=9 inst=1'))
        self.assertEqual(counts['changed'], 1)

    def test_a_binding_that_left_the_table_is_a_row_of_its_own(self):
        rows, _counts = R.align_calls([self.rec(bindings={'table t0 n5 s0': 'srv SkyViewLut'})],
                                      [self.rec(bindings={})])
        self.assertEqual(rows[0].field, 'table t0 n5 s0')
        self.assertEqual(rows[0].a, 'srv SkyViewLut')
        self.assertEqual(rows[0].b, '(not bound)')

    def test_the_setters_since_the_previous_call_are_compared(self):
        rows, _counts = R.align_calls([self.rec(setters=('SetPipelineState', 'OMSetRenderTargets'))],
                                      [self.rec(setters=('SetPipelineState',))])
        self.assertEqual(rows[0].field, 'setters')
        self.assertEqual(rows[0].a, 'SetPipelineState + OMSetRenderTargets')
        self.assertEqual(rows[0].b, 'SetPipelineState')

    def test_a_call_only_one_side_ran_is_one_sided(self):
        rows, counts = R.align_calls([self.rec()], [self.rec(), self.rec(ordinal=2)])
        self.assertEqual([row.status for row in rows], ['only B'])
        self.assertEqual(counts['only B'], 1)
        self.assertEqual(counts['same'], 1)

    def test_a_pass_only_one_side_has_makes_every_call_in_it_one_sided(self):
        rows, counts = R.align_calls([self.rec(path='Shadow')], [self.rec(path='Scene')])
        self.assertEqual([row.status for row in rows], ['only A', 'only B'])
        self.assertEqual((counts['only A'], counts['only B']), (1, 1))

    def test_calls_pair_by_occurrence_under_a_paired_path(self):
        a = [self.rec(args='idx=1 inst=1'), self.rec(ordinal=2, args='idx=1 inst=1')]
        b = [self.rec(args='idx=1 inst=1'), self.rec(ordinal=2, args='idx=2 inst=1')]
        rows, counts = R.align_calls(a, b)
        self.assertEqual(counts['same'], 1)
        self.assertEqual([row.b for row in rows], ['idx=2 inst=1'])


# =========================================================================== the command
class TestCmdFilediff(DiffCase):
    def test_two_identical_captures_differ_in_nothing(self):
        chunks = self.scene()
        a, b = self.two(chunks, chunks)
        text, _err, code = self.streams(R.cmd_filediff, a, b)
        self.assertEqual(code, 0)
        self.assertIn('verdicts  : 1 in both and identical, 0 changed, 0 only in A, 0 only in B', text)
        self.assertNotIn('--- changed', text)

    def test_a_different_argument_prints_one_changed_row(self):
        a, b = self.two(self.scene(index_count=3), self.scene(index_count=9))
        text = self.out(R.cmd_filediff, a, b)
        self.assertIn('--- changed (1) ---', text)
        self.assertIn('idx=3 inst=1', text)
        self.assertIn('idx=9 inst=1', text)

    def test_a_binding_only_one_side_has_is_printed_as_not_bound(self):
        a, b = self.two(self.scene(), self.scene(root_view=False))
        text = self.out(R.cmd_filediff, a, b)
        self.assertIn('cbv b1 s0', text)
        self.assertIn('(not bound)', text)

    def test_the_limits_and_the_notes_are_stated(self):
        a, b = self.two(self.scene(index_count=3), self.scene(index_count=9))
        text = self.out(R.cmd_filediff, a, b)
        self.assertIn('calls     : 1 in A, 1 in B', text)
        self.assertIn('a difference is not a defect', text)
        self.assertIn('capture-local', text)

    def test_the_csv_holds_every_row_the_terminal_groups(self):
        a, b = self.two(self.scene(index_count=3), self.scene(index_count=9))
        out, err, code = self.streams(R.cmd_filediff, a, b, True, 'csv')
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], 'status,path,call,field,a,b')
        self.assertEqual(len(lines[0].split(',')), 6)
        self.assertTrue(any(line.startswith('changed,') for line in lines[1:]))
        for line in lines[1:]:
            self.assertEqual(len(line.split(',')), 6, line)
        self.assertIn('calls     : 1 in A', err)          # the prose goes to stderr, not stdout
        self.assertNotIn('calls     :', out)

    def test_a_markdown_run_prints_a_table(self):
        a, b = self.two(self.scene(index_count=3), self.scene(index_count=9))
        out, _err, _code = self.streams(R.cmd_filediff, a, b, False, 'markdown')
        self.assertTrue(out.startswith('| status | path | call | field | a | b |'))
        self.assertIn('| changed |', out)

    def test_a_capture_whose_chunks_cannot_be_named_is_not_looked_at(self):
        a, b = self.two(self.scene(), self.scene())
        with mock.patch.object(rdc_chunkmap, 'load_chunk_names', lambda *args, **kw: {}):
            out, _err, code = self.streams(R.cmd_filediff, a, b)
        self.assertEqual(code, 1)
        self.assertIn('no chunk-name map', out)

    def test_a_missing_second_path_is_a_usage_error(self):
        out, code = self.dispatch(['diff', self.cap(*self.scene())])
        self.assertEqual(code, 2)
        self.assertIn('usage: rdc_analysis.py diff', out)

    def test_an_unknown_format_is_a_usage_error(self):
        out, code = self.dispatch(['diff', 'a.rdc', 'b.rdc', '--format', 'yaml'])
        self.assertEqual(code, 2)
        self.assertIn('--format table|csv|markdown', out)

    def test_the_entry_point_dispatches_diff(self):
        a, b = self.two(self.scene(index_count=3), self.scene(index_count=9))
        out, code = self.dispatch(['diff', a, b])
        self.assertEqual(code, 0)
        self.assertIn('verdicts  :', out)

    def test_the_displayed_values_are_clipped_and_say_so(self):
        self.assertEqual(R._clip('abc', 5), 'abc')
        self.assertEqual(R._clip('abcdefg', 5), 'ab...')
        self.assertEqual(R._tail('abcdefg', 5), '...fg')
        self.assertEqual(R._tail('abc', 5), 'abc')


if __name__ == '__main__':
    unittest.main()
