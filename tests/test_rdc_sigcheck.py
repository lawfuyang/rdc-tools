"""Tests for `rootsig-check`: the signature's declaration against the stream's bindings (rdc_sigcheck.py).

Two halves, tested separately: the *file* half walks a hand-built stream built from the payload
builders in `rdc_fixtures` (so a layout change breaks these tests), and the *bundle* half writes a real
bundle with `rdc_report_fixtures.write_bundle` and compares the engine's rows with the same stream's facts.

What the tests pin, beyond each check firing at all: that a descriptor table is resolved through
`D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND` rather than through the sentinel as if it were an offset, that a
slot is resolved *as it stood when the table was bound* (the heaps are rebuilt chunk by chunk, not read
from the frame's final state), and that a heap the frame never writes produces a coverage note rather
than a finding -- UE fills its descriptor heaps at startup, so that is the normal case.

Run directly, via unittest, or through the tool:

    python tests/test_rdc_sigcheck.py
    python rdc_analysis.py selftest -k Sigcheck
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
from rdc_report_fixtures import event, write_bundle   # noqa: E402  (the bundle fixture the report tests use)

#: A signature with one descriptor table: `rp0` serves `t0..t3` from an SRV range.
TABLE_PARAMS: Sequence[Any] = (('table', 0, 0, 0, 0, [('srv', 0, 4, 0, 0)]),)
#: A signature with a table and a root CBV at b1 s0.
TABLE_AND_CBV: Sequence[Any] = (('table', 0, 0, 0, 0, [('srv', 0, 4, 0, 0)]), ('cbv', 0, 1, 0, 0, []))


class SigCase(_CmdCase):
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
        buf = io.StringIO()
        with mock.patch.object(sys, 'argv', ['rdc_analysis.py'] + list(argv)):
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as caught:
                    R.main()
        return buf.getvalue(), caught.exception.code

    # --- fixtures -------------------------------------------------------------------------------
    def flat(self, *parts: Any) -> List[bytes]:
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

    def sig(self, rid: int = 700, params: Sequence[Any] = TABLE_PARAMS) -> bytes:
        return self.ch('Device_CreateRootSignature',
                       F.pl_create_root_sig(rid, F.root_signature(list(params))))

    def table(self, cmdlist: int = 7, rp: int = 0, heap: int = 300, index: int = 0,
              compute: bool = False) -> bytes:
        name = 'List_SetComputeRootDescriptorTable' if compute else 'List_SetGraphicsRootDescriptorTable'
        return self.ch(name, F.pl_root_table(cmdlist, rp, heap, index))

    def sigset(self, cmdlist: int = 7, rid: int = 700, compute: bool = False) -> bytes:
        name = 'List_SetComputeRootSignature' if compute else 'List_SetGraphicsRootSignature'
        return self.ch(name, F.pl_root_signature(cmdlist, rid))

    def draw(self, cmdlist: int = 7, index_count: int = 3) -> bytes:
        return self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(cmdlist, index_count, 1, 0, 0, 0))

    def findings(self, *parts: Any) -> Tuple[List[R.SigFinding], List[str]]:
        """The file findings over a capture built from `parts` (lists flattened, as `flat` does)."""
        check = R.file_check(self.cap(*self.flat(*parts)))
        assert check is not None
        return R.file_findings(check)

    def checks(self, findings: Sequence[R.SigFinding]) -> List[str]:
        return [finding.check for finding in findings]


# =========================================================================== the slot arithmetic
class TestTableSlots(SigCase):
    def test_append_offsets_continue_after_the_previous_range(self):
        params = (('table', 0, 0, 0, 0, [('srv', 0, 2, 0, 0xffffffff), ('uav', 0, 3, 0, 0xffffffff)]),)
        sig = R._parse_root_signature(F.root_signature(list(params)))
        assert sig is not None
        facts = [fact for fact in R._table_slots(sig['params'][0], 300, 10, {}) if fact is not None]
        self.assertEqual([(fact.letter, fact.reg, fact.slot) for fact in facts],
                         [('t', 0, 10), ('t', 1, 11), ('u', 0, 12), ('u', 1, 13), ('u', 2, 14)])
        self.assertEqual([fact.declared for fact in facts],
                         ['srv', 'srv', 'uav', 'uav', 'uav'])

    def test_a_declared_offset_is_used_as_it_stands(self):
        params = (('table', 0, 0, 0, 0, [('cbv', 0, 2, 0, 8)]),)
        sig = R._parse_root_signature(F.root_signature(list(params)))
        assert sig is not None
        facts = [fact for fact in R._table_slots(sig['params'][0], 300, 4, {}) if fact is not None]
        self.assertEqual([fact.slot for fact in facts], [12, 13])

    def test_an_unbounded_range_is_reported_as_not_checked(self):
        params = (('table', 0, 0, 0, 0, [('srv', 0, 0xffffffff, 0, 0)]),)
        sig = R._parse_root_signature(F.root_signature(list(params)))
        assert sig is not None
        self.assertEqual(R._table_slots(sig['params'][0], 300, 0, {}), [None])

    def test_a_written_slot_carries_what_the_capture_put_there(self):
        params = (('table', 0, 0, 0, 0, [('srv', 0, 1, 0, 0)]),)
        sig = R._parse_root_signature(F.root_signature(list(params)))
        assert sig is not None
        heaps = {300: {5: R.DescriptorInfo(kind='srv', resource=100, viewFormat=0)}}
        facts = [fact for fact in R._table_slots(sig['params'][0], 300, 5, heaps) if fact is not None]
        self.assertEqual((facts[0].kind, facts[0].resource, facts[0].declared), ('srv', 100, 'srv'))


# =========================================================================== the file half
class TestFileFindings(SigCase):
    def test_a_parameter_index_the_signature_does_not_have_is_certain(self):
        # The bindings have to come *before* the draw: a use is recorded at the call that has it in
        # force. `rp0` is bound too, so the only thing wrong here is the index that does not exist.
        chunks = self.flat(self.sig(), self.sigset(), self.table(),
                           self.ch('List_SetGraphicsRootDescriptorTable',
                                   F.pl_root_table(7, 3, 300, 0)),
                           self.draw())
        findings, _notes = self.findings(chunks)
        self.assertEqual(self.checks(findings), ['undeclared-parameter'])
        self.assertEqual(findings[0].certainty, 'certain')
        self.assertIn('rp3', findings[0].what)

    def test_a_heap_the_frame_never_writes_is_coverage_and_not_a_finding(self):
        findings, notes = self.findings(self.flat(self.sig(), self.sigset(), self.table(), self.draw()))
        self.assertEqual(findings, [])
        self.assertTrue(any('binds and never writes' in note for note in notes))
        self.assertTrue(any('4 descriptor slot(s) bound' in note for note in notes))

    def test_a_heap_the_frame_writes_but_not_at_the_bound_slot_is_a_question(self):
        chunks = self.flat(self.sig(), self.sigset(),
                           self.ch('Device_CreateShaderResourceView',
                                   F.pl_descriptor_write(100, 300, 3)),
                           self.table(index=8), self.draw())
        findings, _notes = self.findings(chunks)
        self.assertEqual(self.checks(findings), ['partial-heap'])
        self.assertEqual(findings[0].certainty, 'question')

    def test_a_slot_holding_another_kind_than_the_range_declares_is_certain(self):
        # All four slots the range covers are written, so the *only* thing wrong is the kind at slot 5:
        # a slot left unwritten would be the coverage question instead, and both findings at once would
        # make the test say less about either.
        chunks = self.flat(self.sig(), self.buffer(200, 'SceneColor'), self.buffer(100, 'Lut'),
                           self.sigset(),
                           self.ch('Device_CreateRenderTargetView',
                                   F.pl_descriptor_write(200, 300, 5)),
                           self.ch('Device_CreateShaderResourceView',
                                   F.pl_descriptor_write(100, 300, 6)),
                           self.ch('Device_CreateShaderResourceView',
                                   F.pl_descriptor_write(100, 300, 7)),
                           self.ch('Device_CreateShaderResourceView',
                                   F.pl_descriptor_write(100, 300, 8)),
                           self.table(index=5), self.draw())
        findings, _notes = self.findings(chunks)
        self.assertEqual(self.checks(findings), ['range-kind-mismatch'])
        self.assertEqual(findings[0].certainty, 'certain')
        self.assertIn('rtv descriptor while the range declares srv', findings[0].what)

    def test_a_slot_is_read_as_it_stood_when_the_table_was_bound(self):
        # The same slot holds an SRV for the first binding and a UAV for the second: the first is a
        # mismatch (the range declares uav), the second is not -- which is only true if the heap is
        # rebuilt in stream order rather than read from the frame's final state.
        params = (('table', 0, 0, 0, 0, [('uav', 0, 1, 0, 0)]),)
        chunks = self.flat(self.sig(params=params), self.buffer(100, 'Lut'), self.buffer(200, 'Grid'),
                           self.sigset(),
                           self.ch('Device_CreateShaderResourceView', F.pl_descriptor_write(100, 300, 5)),
                           self.table(index=5), self.draw(),
                           self.ch('Device_CreateUnorderedAccessView',
                                   F.pl_descriptor_write(200, 300, 5)),
                           self.table(index=5), self.draw(index_count=9))
        findings, _notes = self.findings(chunks)
        self.assertEqual(self.checks(findings), ['range-kind-mismatch'])
        self.assertIn('1 slot(s)', findings[0].what)

    def test_a_declared_parameter_no_call_sets_is_a_question(self):
        chunks = self.flat(self.sig(params=TABLE_AND_CBV), self.sigset(), self.table(), self.draw())
        findings, _notes = self.findings(chunks)
        self.assertEqual(self.checks(findings), ['never-set-parameter'])
        self.assertIn('rp1', findings[0].what)

    def test_a_signature_that_is_never_created_is_a_question(self):
        chunks = self.flat(self.sigset(rid=7000), self.table(), self.draw())
        findings, _notes = self.findings(chunks)
        self.assertEqual(self.checks(findings), ['unknown-signature'])

    def test_bindings_with_no_signature_at_all_are_a_question(self):
        findings, _notes = self.findings(self.table(), self.draw())
        self.assertEqual(self.checks(findings), ['no-signature'])

    def test_a_binding_that_names_a_resource_the_capture_never_creates_is_a_question(self):
        chunks = self.flat(self.sig(), self.sigset(),
                           self.ch('List_SetGraphicsRootConstantBufferView',
                                   F.pl_root_view(7, 0, 999, 0)),
                           self.draw())
        findings, _notes = self.findings(chunks)
        self.assertEqual(self.checks(findings), ['unknown-resource'])
        self.assertEqual(findings[0].certainty, 'question')

    def test_no_chunk_names_means_not_looked_at(self):
        path = self.cap(*self.flat(self.sig(), self.sigset(), self.table(), self.draw()))
        with mock.patch.object(rdc_chunkmap, 'load_chunk_names', lambda *args, **kw: {}):
            self.assertIsNone(R.file_check(path))


# =========================================================================== the bundle half
class TestBundleFindings(SigCase):
    def bundle(self, rows: Sequence[str], signature: str = '700', eid: int = 1003) -> str:
        root = os.path.join(self.tmp, 'bundle')
        # The event record is what makes the loader *look* for the state document: `load_bundle`
        # collects `states/<eid>.state.json` for the ids `events.json` names.
        write_bundle(root, events=[event(eid)],
                     states={eid: {'state': {'schemaVersion': 1, 'eid': eid,
                                             'rootSignature': signature,
                                             'rootParameters': list(rows)}}})
        return root

    def compare(self, rows: Sequence[str], signature: str = '700') -> List[R.SigFinding]:
        check = R.file_check(self.cap(*self.flat(self.sig(), self.sigset(), self.table(), self.draw())))
        assert check is not None
        findings, _notes = R.bundle_findings(check, self.bundle(rows, signature))
        return findings

    def test_the_engine_row_spelling_is_read_back(self):
        self.assertEqual(R._resource_id('res2233'), 2233)
        self.assertEqual(R._resource_id('2233'), 2233)      # the state document's own spelling
        self.assertIsNone(R._resource_id('0'))
        self.assertIsNone(R._resource_id('res0'))
        self.assertIsNone(R._resource_id(''))

    def test_a_parameter_row_is_classified_by_its_detail(self):
        self.assertEqual(R._engine_class('rp0   reg=0 space=0 vis=all heap298+0x40'), 'table')
        self.assertEqual(R._engine_class('rp1   reg=0 space=0 vis=all 16 words'), '32bit')
        self.assertEqual(R._engine_class('rp2   reg=1 space=0 vis=ps res2233'), 'descriptor')
        self.assertEqual(R._engine_class('rp3   reg=0 space=0 vis=all '), 'unset')

    def test_a_table_row_gives_its_heap_and_descriptor_offset(self):
        self.assertEqual(R._engine_table('rp0   reg=0 space=0 vis=all heap298+0x40'), (298, 64))
        self.assertEqual(R._engine_table('rp0   reg=0 space=0 vis=all res100'), (None, 0))
        self.assertEqual(R._engine_table('rp0   reg=0 space=0 vis=all'), (None, 0))

    def test_the_same_table_binding_on_both_sides_is_no_disagreement(self):
        self.assertEqual(self.compare(['rp0   reg=0 space=0 vis=all heap300+0x0']), [])

    def test_a_table_binding_the_file_never_records_is_certain(self):
        findings = self.compare(['rp0   reg=0 space=0 vis=all heap300+0x9'])
        self.assertEqual([finding.check for finding in findings], ['table-binding'])
        self.assertEqual(findings[0].certainty, 'certain')

    def test_a_parameter_count_the_two_sides_disagree_about_is_certain(self):
        findings = self.compare(['rp0   reg=0 space=0 vis=all heap300+0x0',
                                 'rp1   reg=1 space=0 vis=all res100'])
        self.assertIn('parameter-count', [finding.check for finding in findings])

    def test_a_register_the_two_sides_disagree_about_is_certain(self):
        findings = self.compare(['rp0   reg=5 space=0 vis=all heap300+0x0'])
        self.assertEqual([finding.check for finding in findings], ['parameter-shape'])
        self.assertIn('register 0', findings[0].what)

    def test_a_signature_the_file_never_creates_is_a_question(self):
        findings = self.compare(['rp0   reg=0 space=0 vis=all heap300+0x0'], signature='9999')
        self.assertEqual([finding.check for finding in findings], ['signature-not-in-file'])
        self.assertEqual(findings[0].certainty, 'question')

    def test_a_parameter_the_event_had_not_set_is_not_a_disagreement(self):
        self.assertEqual(self.compare(['rp0   reg=0 space=0 vis=all ']), [])


# =========================================================================== the command
class TestCmdRootsigCheck(SigCase):
    def test_a_certain_finding_is_exit_1(self):
        path = self.cap(*self.flat(self.sig(), self.sigset(), self.table(),
                                   self.ch('List_SetGraphicsRootDescriptorTable',
                                           F.pl_root_table(7, 3, 300, 0)),
                                   self.draw()))
        out, _err, code = self.streams(R.cmd_rootsig_check, path)
        self.assertEqual(code, 1)
        self.assertIn('findings  : 1 certain, 0 question', out)
        self.assertIn('undeclared-parameter', out)

    def test_a_clean_frame_is_exit_0_with_the_coverage_notes(self):
        path = self.cap(*self.flat(self.sig(params=TABLE_AND_CBV), self.sigset(),
                                   self.ch('List_SetGraphicsRootConstantBufferView',
                                           F.pl_root_view(7, 1, 100, 0)),
                                   self.buffer(100, 'SceneUniform'),
                                   self.table(), self.draw()))
        out, _err, code = self.streams(R.cmd_rootsig_check, path)
        self.assertEqual(code, 0)
        self.assertIn('findings  : 0 certain, 0 question', out)
        self.assertIn('parameters: 2 set, 0 declared and never set', out)

    def test_the_csv_holds_every_finding_as_a_row(self):
        path = self.cap(*self.flat(self.sig(), self.sigset(), self.table(),
                                   self.ch('List_SetGraphicsRootDescriptorTable',
                                           F.pl_root_table(7, 3, 300, 0)),
                                   self.draw()))
        out, err, code = self.streams(R.cmd_rootsig_check, path, None, 'csv')
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual(lines[0], 'certainty,check,what,evidence')
        self.assertEqual(len(lines[0].split(',')), 4)
        self.assertTrue(any(line.startswith('certain,undeclared-parameter,') for line in lines[1:]))
        self.assertIn('signatures:', err)

    def test_a_bundle_that_is_not_there_is_an_error_and_exit_1(self):
        path = self.cap(*self.flat(self.sig(), self.sigset(), self.table(), self.draw()))
        out, _err, code = self.streams(R.cmd_rootsig_check, path, self.path('nope'))
        self.assertEqual(code, 1)
        self.assertIn('error:', out)

    def test_the_entry_point_dispatches_rootsig_check(self):
        path = self.cap(*self.flat(self.sig(), self.sigset(), self.table(), self.draw()))
        out, code = self.dispatch(['rootsig-check', path])
        self.assertEqual(code, 0)
        self.assertIn('signatures:', out)


if __name__ == '__main__':
    unittest.main()
