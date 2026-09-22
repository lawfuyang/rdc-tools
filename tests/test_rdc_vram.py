"""Tests for `vram`: the frame's memory by role, the widest pass, and the what-if arithmetic.

Built from `rdc_fixtures` like the rest of the suite, so a payload layout change breaks these tests
rather than passing quietly, and nothing here needs a capture, a GPU or a device.

What the tests pin: that a resource's *role* comes from how the frame uses it (a texture written as a
render target is a render target, not a texture), that a texture is counted at 4 bytes a pixel and the
figure is marked as the estimate it is, that "the widest pass" is the largest *peak live* inside a
pass's own range rather than the largest total it touches, and that the two what-if cases are the
arithmetic they claim to be (area for the halved one) and nothing more.

Run directly, via unittest, or through the tool:

    python tests/test_rdc_vram.py
    python rdc_analysis.py selftest -k Vram
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from typing import Any, Callable, List, Sequence, Tuple
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


class VramCase(_CmdCase):
    """`CmdCase` (fake source tree, capture builder) plus the fixture and output helpers."""

    def out(self, fn: Callable[..., object], *args: Any, **kwargs: Any) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args, **kwargs)
        return buf.getvalue()

    def streams(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Tuple[str, str, Any]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(*args, **kwargs)
        return out.getvalue(), err.getvalue(), code

    def dispatch(self, argv: Sequence[str]) -> Tuple[str, Any]:
        """`(stdout, exit code)` from the real entry point; a command that does not exit is 0.

        `vram` is one of the commands that print and return (`draws`, `memory`), unlike the ones that
        gate a script (`verify`, `rootsig-check`), so the helper has to accept both.
        """
        buf = io.StringIO()
        code: Any = 0
        with mock.patch.object(sys, 'argv', ['rdc_analysis.py'] + list(argv)):
            with contextlib.redirect_stdout(buf):
                try:
                    R.main()
                except SystemExit as exc:
                    code = exc.code
        return buf.getvalue(), code

    def flat(self, *parts: Any) -> List[bytes]:
        out: List[bytes] = []
        for part in parts:
            out.extend(part if isinstance(part, list) else [part])
        return out

    def marker(self, name: str) -> bytes:
        return self.ch('PushMarker', name.encode('utf-8') + b'\x00')

    def pop(self) -> bytes:
        return self.ch('PopMarker')

    def buffer(self, rid: int, name: str = '', size: int = 4096) -> List[bytes]:
        out = [self.ch('Device_CreateCommittedResource',
                       F.pl_committed_resource(rid, F.pl_resource_desc(1, width=size)))]
        if name:
            out.append(self.ch('SetName', F.pl_set_name(rid, name)))
        return out

    def texture(self, rid: int, name: str = '', width: int = 1024, height: int = 1024,
                fmt: int = 28) -> List[bytes]:
        out = [self.ch('Device_CreateCommittedResource',
                       F.pl_committed_resource(rid, F.pl_resource_desc(3, width=width, height=height,
                                                                      fmt=fmt)))]
        if name:
            out.append(self.ch('SetName', F.pl_set_name(rid, name)))
        return out

    def draw(self, cmdlist: int = 7) -> bytes:
        return self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(cmdlist, 3, 1, 0, 0, 0))

    def ledger(self, *parts: Any) -> Tuple[R.UseLedger, Any]:
        """The use ledger of a capture built from `parts`, and its resource table."""
        check = R._deps_ledger(self.cap(*self.flat(*parts)))
        return check[0], check[1]


# =========================================================================== roles
class TestRoles(VramCase):
    def scene(self) -> List[bytes]:
        """One pass: a 1024x1024 target, a vertex buffer and a 256-byte constant buffer."""
        return self.flat(
            self.texture(200, 'SceneColor'), self.buffer(100, 'Verts'), self.buffer(101, 'CbData', 256),
            self.marker('Pass'),
            self.ch('List_OMSetRenderTargets', F.pl_omset(7, [200])),
            self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [(100, 0, 4096, 24)])),
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 0, 101, 0)),
            self.draw(), self.pop())

    def test_a_texture_written_as_a_target_is_a_render_target_not_a_texture(self):
        ledger, resources = self.ledger(self.scene())
        rows = {rid: R.tally(rid, record) for rid, record in ledger['resources'].items()}
        grouped = {entry.role: entry for entry in R.roles(ledger, resources, rows)}
        self.assertEqual(sorted(grouped), ['buffer', 'render target'])
        self.assertEqual(grouped['render target'].resources, 1)
        self.assertEqual(grouped['render target'].bytes, 1024 * 1024 * 4)
        self.assertTrue(grouped['render target'].estimated)

    def test_a_buffer_is_counted_at_its_own_size_and_not_estimated(self):
        ledger, resources = self.ledger(self.scene())
        rows = {rid: R.tally(rid, record) for rid, record in ledger['resources'].items()}
        grouped = {entry.role: entry for entry in R.roles(ledger, resources, rows)}
        self.assertEqual(grouped['buffer'].resources, 2)
        self.assertEqual(grouped['buffer'].bytes, 4096 + 256)
        self.assertFalse(grouped['buffer'].estimated)

    def test_a_resource_nothing_uses_has_no_role(self):
        # 300 is created and never referenced by a decoded chunk: `memory` owns that question, and a
        # budget that counted it would charge the frame for an allocation it does not use.
        chunks = self.flat(self.scene(), self.buffer(300, 'Unused', 8192))
        ledger, resources = self.ledger(chunks)
        rows = {rid: R.tally(rid, record) for rid, record in ledger['resources'].items()}
        grouped = {entry.role: entry for entry in R.roles(ledger, resources, rows)}
        self.assertEqual(grouped['buffer'].resources, 2)
        self.assertEqual(grouped['buffer'].bytes, 4096 + 256)

    def test_a_sampled_texture_that_is_not_a_target_is_a_texture(self):
        chunks = self.flat(self.texture(400, 'SkyViewLut', 64, 64), self.marker('Pass'),
                           self.ch('List_SetGraphicsRootDescriptorTable',
                                   F.pl_root_table(7, 0, 300, 4)),
                           self.ch('Device_CreateDescriptorHeap', F.pl_descriptor_heap(300)),
                           self.ch('Device_CreateShaderResourceView',
                                   F.pl_descriptor_write(400, 300, 4)),
                           self.draw(), self.pop())
        ledger, resources = self.ledger(chunks)
        rows = {rid: R.tally(rid, record) for rid, record in ledger['resources'].items()}
        grouped = {entry.role: entry for entry in R.roles(ledger, resources, rows)}
        self.assertEqual(grouped['texture'].resources, 1)
        self.assertEqual(grouped['texture'].bytes, 64 * 64 * 4)


# =========================================================================== the widest pass
class TestPassPeaks(VramCase):
    def test_the_widest_pass_is_the_one_with_the_largest_peak_inside_it(self):
        # A 1024x1024 target in pass `Small`, a second one in `Big`: `Big` is wider even though both
        # passes touch the same *kind* of memory.
        chunks = self.flat(self.texture(200, 'Small'), self.texture(201, 'Big'), self.buffer(100, 'V'),
                           self.marker('Small'),
                           self.ch('List_OMSetRenderTargets', F.pl_omset(7, [200])),
                           self.draw(), self.pop(),
                           self.marker('Big'),
                           self.ch('List_OMSetRenderTargets', F.pl_omset(7, [201])),
                           self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [(100, 0, 4096, 24)])),
                           self.draw(), self.pop())
        ledger, resources = self.ledger(chunks)
        rows = {rid: R.tally(rid, record) for rid, record in ledger['resources'].items()}
        passes = R.marker_passes(self.cap(*self.flat(chunks)))
        assert passes is not None
        peaks = R.pass_peaks(ledger, resources, rows, passes)
        self.assertEqual([peak.path for peak in peaks], ['Big', 'Small'])
        self.assertEqual(peaks[0].peak, 1024 * 1024 * 4 + 4096)
        self.assertEqual(peaks[1].peak, 1024 * 1024 * 4)
        self.assertEqual(peaks[0].resources, 2)

    def test_a_resource_whose_use_outlives_the_pass_is_clipped_to_it(self):
        # The target is bound before the pass and read after it: the peak inside the pass counts it, and
        # counts it once.
        chunks = self.flat(self.texture(200, 'Target'), self.buffer(100, 'V'),
                           self.ch('List_OMSetRenderTargets', F.pl_omset(7, [200])),
                           self.marker('Pass'),
                           self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [(100, 0, 4096, 24)])),
                           self.draw(), self.pop(),
                           self.ch('List_CopyTextureRegion', F.pl_copy_texture(7, 200, 200)))
        ledger, resources = self.ledger(chunks)
        rows = {rid: R.tally(rid, record) for rid, record in ledger['resources'].items()}
        passes = R.marker_passes(self.cap(*self.flat(chunks)))
        assert passes is not None
        peaks = R.pass_peaks(ledger, resources, rows, passes)
        self.assertEqual(len(peaks), 1)
        self.assertEqual(peaks[0].resources, 2)      # the target (bound before) and the vertex buffer
        self.assertEqual(peaks[0].peak, 1024 * 1024 * 4 + 4096)

    def test_a_pass_that_touches_nothing_sized_is_left_out(self):
        chunks = self.flat(self.marker('Empty'), self.pop(), self.buffer(100, 'V'))
        ledger, resources = self.ledger(chunks)
        rows = {rid: R.tally(rid, record) for rid, record in ledger['resources'].items()}
        passes = R.marker_passes(self.cap(*self.flat(chunks)))
        assert passes is not None
        self.assertEqual(R.pass_peaks(ledger, resources, rows, passes), [])


# =========================================================================== what-if
class TestWhatIf(VramCase):
    def rows(self) -> Tuple[List[R.Role], Any]:
        return [
            R.Role(role='render target', resources=1, bytes=1024 * 1024 * 4, estimated=True),
            R.Role(role='texture', resources=1, bytes=64 * 64 * 4, estimated=True),
            R.Role(role='buffer', resources=1, bytes=4096, estimated=False),
        ], None

    def test_half_resolution_quarters_the_area_counted_roles(self):
        grouped, _ = self.rows()
        scenarios = {name: (size, note) for name, size, _estimated, note in R.what_if(grouped, {}, [])}
        self.assertEqual(scenarios['now'][0], 1024 * 1024 * 4 + 64 * 64 * 4 + 4096)
        self.assertEqual(scenarios['at half resolution'][0],
                         (1024 * 1024 * 4) // 4 + (64 * 64 * 4) // 4 + 4096)
        self.assertIn('area', scenarios['at half resolution'][1])

    def test_a_dropped_name_is_a_subtraction_over_the_matching_resources(self):
        grouped, _ = self.rows()
        resources = {200: R.ResourceInfo(kind='texture2d', name='SceneColor', size=0, width=1024,
                                         height=1024, depth=1, mips=1, format=28, gpuAddress=0)}
        scenarios = {name: (size, note) for name, size, _estimated, note in R.what_if(grouped, resources,
                                                                                      ['scenecolor'])}
        self.assertEqual(scenarios['with "scenecolor" dropped'][0],
                         1024 * 1024 * 4 + 64 * 64 * 4 + 4096 - 1024 * 1024 * 4)
        self.assertIn('1 resource(s) match', scenarios['with "scenecolor" dropped'][1])

    def test_a_filter_that_matches_nothing_changes_nothing(self):
        grouped, _ = self.rows()
        scenarios = {name: size for name, size, _e, _n in R.what_if(grouped, {}, [])}
        empty = R.what_if(grouped, {}, ['nothing-matches-this'])[0][1]
        self.assertEqual(empty, scenarios['now'])


# =========================================================================== the command
class TestCmdVram(VramCase):
    def scene(self) -> List[bytes]:
        return self.flat(
            self.texture(200, 'SceneColor'), self.buffer(100, 'Verts'),
            self.marker('Pass'),
            self.ch('List_OMSetRenderTargets', F.pl_omset(7, [200])),
            self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [(100, 0, 4096, 24)])),
            self.draw(), self.pop())

    def test_the_budget_the_widest_pass_and_the_what_if_are_all_printed(self):
        text = self.out(R.cmd_vram, self.cap(*self.scene()))
        self.assertIn('by role', text)
        self.assertIn('render target', text)
        self.assertIn('widest pass', text)
        self.assertIn('what-if', text)
        self.assertIn('at half resolution', text)
        self.assertIn('what this cannot say', text)

    def test_the_roles_and_the_scenarios_come_out_as_rows(self):
        out, err, _code = self.streams(R.cmd_vram, self.cap(*self.scene()), 8, (), 'csv')
        self.assertEqual(out.splitlines()[0], 'section,name,value')
        self.assertTrue(any(line.startswith('role,') for line in out.splitlines()))
        self.assertTrue(any(line.startswith('pass,') for line in out.splitlines()))
        self.assertTrue(any(line.startswith('what-if,') for line in out.splitlines()))
        self.assertIn('budget:', err)

    def test_a_drop_is_taken_as_a_filter_rather_than_as_the_pass_limit(self):
        path = self.cap(*self.scene())
        out, _code = self.dispatch(['vram', path, '--drop', 'SceneColor'])
        self.assertIn('with "SceneColor" dropped', out)
        self.assertIn('1 resource(s) match', out)

    def test_a_drop_with_no_filter_is_a_usage_error(self):
        out, code = self.dispatch(['vram', self.cap(*self.scene()), '--drop'])
        self.assertEqual(code, 2)
        self.assertIn('--drop <nameFilter>', out)

    def test_the_entry_point_dispatches_vram_with_a_pass_limit(self):
        out, code = self.dispatch(['vram', self.cap(*self.scene()), '1'])
        self.assertEqual(code, 0)
        self.assertIn('widest pass', out)

    def test_no_chunk_names_says_the_pass_half_was_not_read(self):
        path = self.cap(*self.scene())
        with mock.patch.object(rdc_chunkmap, 'load_chunk_names', lambda *args, **kw: {}):
            out, _err, _code = self.streams(R.cmd_vram, path)
        self.assertIn('no chunk-name map', out)


if __name__ == '__main__':
    unittest.main()
