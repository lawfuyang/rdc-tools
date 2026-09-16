"""Tests for the frame report (`report`): a bundle in, deterministic Markdown out.

The bundles here are written by hand from the shapes `replay_dump dump` produces (REFERENCE §9), so these
tests need no capture, no GPU, no replay and no `renderdoc-src`: the report reads files, and a file
written by a test is a bundle like any other. That is also why the bundle writer lives here rather
than in `rdc_fixtures` (which builds `.rdc` payloads).

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_report.py
    python -m unittest tests.test_rdc_report
    python rdc_analysis.py selftest -k Report
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402
import rdc_fixtures as F          # noqa: E402
from test_rdc_analysis import CmdCase as _CmdCase   # noqa: E402

RDC = 'fixture.rdc'


def capture_text(func: Callable[..., object], *args: Any, **kwargs: Any) -> str:
    """Run `func` and return everything it printed on stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


def event(eid: int, kind: str = 'graphics', targets: Sequence[str] = (), depth: str = '0',
          pso: str = '100', shaders: str = 'vs=2348 ') -> Dict[str, Any]:
    """One `events.json` record, with the fields the report reads spelled out."""
    return {'eid': eid, 'pso': pso, 'psoKind': kind, 'shaders': shaders,
            'targets': list(targets), 'depth': depth, 'rootParameters': 1, 'state': 'deadbeef'}


def resource(resid: str, kind: str = 'texture', first: int = 0, name: str = '',
             **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {'resource': resid, 'name': name, 'kind': kind,
                             'usage': [{'eid': first, 'usage': 1}], 'usageCount': 1,
                             'firstEvent': first, 'lastEvent': first}
    entry.update(extra)
    return entry


def write_json(root: str, name: str, doc: Any) -> None:
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)


def write_bundle(root: str, events: Optional[List[Dict[str, Any]]] = None,
                 resources: Optional[List[Dict[str, Any]]] = None,
                 messages: Optional[List[Any]] = None,
                 manifest: Optional[Dict[str, Any]] = None,
                 capture: Optional[Dict[str, Any]] = None,
                 states: Optional[Dict[int, Dict[str, Any]]] = None,
                 cbuffers: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """Write a bundle with the files the report requires, plus whatever the test cares about."""
    os.makedirs(root, exist_ok=True)
    base_manifest: Dict[str, Any] = {
        'bundleVersion': R.BUNDLE_VERSION, 'driver': 'replay_dump', 'renderdoc': '1.46',
        'capture': RDC, 'captureSha256': 'ab' * 32, 'since': 1, 'until': 0, 'maxEvents': 0,
        'withImages': 0, 'withCounters': 0, 'withTextures': 0, 'resourceUsage': 'collected',
        'files': [], 'fileCount': 0,
    }
    base_manifest.update(manifest or {})
    base_capture: Dict[str, Any] = {'capture': RDC, 'renderdoc': '1.46', 'driver': 'D3D12',
                                    'chunks': 10, 'pipelineType': 1, 'localRenderer': 1, 'vendor': 1}
    base_capture.update(capture or {})

    write_json(root, 'manifest.json', base_manifest)
    write_json(root, 'capture.json', base_capture)
    write_json(root, 'events.json', {'capture': RDC, 'events': events or [], 'total': len(events or [])})
    write_json(root, 'resources.json', {'capture': RDC, 'resources': resources or [],
                                        'total': len(resources or [])})
    write_json(root, 'messages.json', {'capture': RDC, 'messages': messages or [],
                                       'total': len(messages or [])})
    for eid, documents in (states or {}).items():
        if 'state' in documents:
            write_json(root, os.path.join('states', '%d.state.json' % eid), documents['state'])
        if 'shaders' in documents:
            write_json(root, os.path.join('states', '%d.shaders.json' % eid), documents['shaders'])
    for name, document in (cbuffers or {}).items():
        write_json(root, os.path.join('cbuffers', name), document)


def cbuffer(eid: int, stage: str = 'ps', slot: int = 0, buffer: str = '300',
            variables: Optional[List[str]] = None) -> Dict[str, Any]:
    """One `cbuffers/<eid>_<stage>_<slot>.json`, with the header the driver writes."""
    return {'capture': RDC, 'renderdoc': '1.46', 'driver': 'D3D12', 'localReplay': 1,
            'machine': 'test', 'eid': eid, 'stage': stage, 'slot': slot, 'shader': '2348',
            'buffer': buffer, 'variables': variables or []}


class BundleCase(unittest.TestCase):
    """A scratch directory per test; bundles and reports are written inside it."""

    tmp: str

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='rdc_report_')
        self.addCleanup(self._remove_tmp)

    def _remove_tmp(self) -> None:
        for dirpath, _dirs, files in os.walk(self.tmp, topdown=False):
            for name in files:
                os.remove(os.path.join(dirpath, name))
            os.rmdir(dirpath)

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def report(self, bundle: str, out: Optional[str] = None) -> str:
        """Run `report` and return its stdout."""
        return capture_text(R.cmd_report, RDC, bundle, out)

    def markdown(self, bundle: str, out: Optional[str] = None) -> str:
        with open(os.path.join(out or bundle, 'report.md'), encoding='utf-8') as fh:
            return fh.read()

    def document(self, bundle: str, out: Optional[str] = None) -> Dict[str, Any]:
        with open(os.path.join(out or bundle, 'report.json'), encoding='utf-8') as fh:
            data: Dict[str, Any] = json.load(fh)
        return data

    def passes(self, bundle: str) -> List[R.ReportPass]:
        code = R.cmd_report(RDC, bundle, None)
        self.assertEqual(code, 0, 'report failed: %s' % self.markdown(bundle))
        return self.document(bundle)['passes']

    def reasons(self, bundle: str) -> List[str]:
        return [entry['reason'] for entry in self.passes(bundle)]


# =========================================================================== pass reconstruction
class TestReportPasses(BundleCase):
    def test_a_target_change_starts_a_new_pass(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(10, targets=['11 64x64x1 R8G8B8A8_UNORM']),
                                     event(11, targets=['11 64x64x1 R8G8B8A8_UNORM']),
                                     event(20, targets=['12 64x64x1 R8G8B8A8_UNORM']),
                                     event(21, targets=['12 64x64x1 R8G8B8A8_UNORM'])])
        passes = self.passes(bundle)
        self.assertEqual([(p['firstEid'], p['lastEid'], p['events']) for p in passes],
                         [(10, 11, 2), (20, 21, 2)])
        self.assertEqual(passes[0]['reason'], 'the first event with bound state in the bundle')
        self.assertIn('the render targets changed', passes[1]['reason'])
        self.assertIn('11 64x64x1 R8G8B8A8_UNORM', passes[1]['reason'])

    def test_a_call_kind_change_starts_a_new_pass(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(5, targets=['11 64x64x1 R8G8B8A8_UNORM']),
                                     event(6, kind='compute'),
                                     event(7, kind='compute')])
        passes = self.passes(bundle)
        self.assertEqual(len(passes), 2)
        self.assertEqual(passes[1]['kind'], 'compute')
        self.assertIn('the call kind changed: graphics -> compute', passes[1]['reason'])
        self.assertEqual(passes[1]['structure'], 'compute')
        self.assertEqual((passes[1]['graphics'], passes[1]['compute']), (0, 2))

    def test_a_depth_change_starts_a_new_pass(self):
        bundle = self.path('b')
        target = ['11 64x64x1 D32_FLOAT']
        write_bundle(bundle, events=[event(1, targets=target, depth='0'),
                                     event(2, targets=target, depth='99')])
        passes = self.passes(bundle)
        self.assertEqual(len(passes), 2)
        self.assertIn('the depth target changed: 0 -> 99', passes[1]['reason'])

    def test_one_pass_when_nothing_changes(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(eid, targets=['11 64x64x1 R8G8B8A8_UNORM'])
                                     for eid in (3, 4, 5)])
        passes = self.passes(bundle)
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0]['events'], 3)
        self.assertEqual(passes[0]['graphics'], 3)
        self.assertEqual(passes[0]['lastEid'], 5)

    def test_structure_is_read_from_state_alone(self):
        bundle = self.path('b')
        colour = '11 64x64x1 R8G8B8A8_UNORM'
        write_bundle(bundle, events=[
            event(1, targets=[colour], depth='13'),                              # single target + depth
            event(2, targets=[colour, '12 64x64x1 R8G8B8A8_UNORM'], depth='13'),  # two targets + depth
            event(3, targets=[], depth='13'),                                    # depth only
            event(4, targets=[colour]),                                          # colour, no depth
            event(5, kind='compute'),                                            # compute
        ])
        structures = [p['structure'] for p in self.passes(bundle)]
        self.assertEqual(structures, ['single colour target', 'multi-target',
                                      'depth only (no colour target)',
                                      'colour only (no depth target)', 'compute'])

    def test_dispatches_are_grouped_by_pipeline_not_by_inherited_targets(self):
        """A dispatch does not set the output-merge state, so the targets the engine reports at a
        compute event are leftovers from an earlier call. Grouping on them invented passes out of stale
        state (measured on a compute-only capture: five "passes" that were nothing of the kind), so a
        compute pass is grouped by its pipeline and shaders and its targets are reported as not
        applicable."""
        bundle = self.path('b')
        stale = ['11 64x64x1 R8G8B8A8_UNORM']
        write_bundle(bundle, events=[
            event(1, kind='compute', targets=stale, pso='300', shaders='cs=7 '),
            event(2, kind='compute', targets=[], pso='300', shaders='cs=7 '),
            event(3, kind='compute', targets=stale, pso='301', shaders='cs=8 '),
        ])
        passes = self.passes(bundle)
        self.assertEqual([(p['firstEid'], p['lastEid'], p['events']) for p in passes],
                         [(1, 2, 2), (3, 3, 1)])
        self.assertIn('the dispatch changed', passes[1]['reason'])
        text = self.markdown(bundle)
        self.assertIn('n/a — a dispatch does not set the output merger', text)
        self.assertIn('| 1 | 1–2 | compute | 2 | n/a | n/a | compute |', text)

    def test_shaders_a_pass_does_not_use_are_separated_from_the_ones_it_does(self):
        """The state document lists every bound stage: at a dispatch that includes the vertex and pixel
        shaders an earlier draw left bound, which must not read as part of the pass."""
        bundle = self.path('b')
        write_bundle(
            bundle, events=[event(96, kind='compute', pso='300', shaders='cs=2302 ')],
            states={96: {
                'state': {'eid': 96, 'shaders': ['vs  res2348   ', 'ps  res2349   ', 'cs  res2302   ']},
                'shaders': {'eid': 96, 'stages': [
                    {'stage': 'cs', 'resource': '2302', 'entry': 'Main',
                     'constantBlocks': ['cbuffer[0] LightGrid b0 s0 32 bytes']}]},
            }})
        self.passes(bundle)
        text = self.markdown(bundle)
        self.assertIn('- shaders (from states/96.state.json): cs 2302', text)
        self.assertIn('- also bound at that event, and not used by a dispatch: vs 2348, ps 2349', text)
        entry = self.document(bundle)['passes'][0]
        self.assertEqual(entry['shaders'], ['cs 2302'])
        self.assertEqual(entry['otherShaders'], ['vs 2348', 'ps 2349'])

    def test_an_empty_bundle_still_writes_a_report(self):
        bundle = self.path('b')
        write_bundle(bundle)
        out = self.report(bundle)
        self.assertIn('0 pass(es)', out)
        text = self.markdown(bundle)
        self.assertIn('| render targets seen | none |', text)
        self.assertIn('```mermaid\ngraph LR\n```', text)
        self.assertEqual(self.document(bundle)['passes'], [])


# =========================================================================== roll-ups
class TestReportRollups(BundleCase):
    def test_shaders_and_blocks_come_from_the_state_documents(self):
        bundle = self.path('b')
        write_bundle(
            bundle, events=[event(96, targets=['2207 2003x1254x1 R10G10B10A2_UNORM'])],
            states={96: {
                'state': {'eid': 96, 'shaders': ['vs  res2348   ', 'ps  res2349   '],
                          'renderTargets': ['slot 0  res2207'], 'depthTarget': '0'},
                'shaders': {'eid': 96, 'stages': [
                    {'stage': 'vs', 'resource': '2348', 'entry': 'Main',
                     'constantBlocks': ['cbuffer[0] $Globals                     b0 s0 80 bytes']},
                    {'stage': 'ps', 'resource': '2349', 'entry': 'Main',
                     'constantBlocks': ['cbuffer[1] MobileBasePass            b3 s0 64 bytes']},
                ]},
            }})
        self.passes(bundle)
        text = self.markdown(bundle)
        self.assertIn('shaders (from states/96.state.json): vs 2348, ps 2349', text)
        self.assertIn('constant blocks (from states/96.shaders.json):', text)
        # The driver's block rows are column-aligned text; a report line collapses the runs of spaces
        # (they are layout for a terminal, not information) and keeps everything else.
        self.assertIn('vs 2348: cbuffer[0] $Globals b0 s0 80 bytes', text)
        self.assertIn('ps 2349: cbuffer[1] MobileBasePass b3 s0 64 bytes', text)

    def test_resources_are_attributed_to_the_pass_that_first_uses_them(self):
        bundle = self.path('b')
        write_bundle(
            bundle,
            events=[event(10, targets=['11 64x64x1 R8G8B8A8_UNORM']),
                    event(20, targets=['12 64x64x1 R8G8B8A8_UNORM'])],
            resources=[resource('11', name='SceneColour', width=64, height=64, depth=1,
                                format='R8G8B8A8_UNORM', first=10),
                       resource('12', name='Shadow', width=64, height=64, depth=1,
                                format='D32_FLOAT', first=20),
                       resource('77', kind='buffer', first=20, bytes=4096)])
        self.passes(bundle)
        passes = self.document(bundle)['passes']
        self.assertEqual(len(passes[0]['firstTouched']), 1)
        self.assertIn('res11 "SceneColour" (texture, 64x64x1 R8G8B8A8_UNORM)', passes[0]['firstTouched'][0])
        self.assertEqual(passes[1]['firstTouched'],
                         ['res12 "Shadow" (texture, 64x64x1 D32_FLOAT)',
                          'res77 (buffer, 0.00 MB)'])

    def test_frame_facts_count_kinds_severities_and_formats(self):
        bundle = self.path('b')
        write_bundle(
            bundle,
            events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'], depth='13'),
                    event(2, kind='compute')],
            resources=[resource('11', first=1), resource('77', kind='buffer', first=1, bytes=2048),
                       resource('88', kind='other', first=0)],
            messages=[{'eid': 1, 'severity': 4, 'severityText': 'error', 'text': 'boom'}],
            capture={'chunks': 42})
        self.passes(bundle)
        frame = self.document(bundle)['frame']
        self.assertEqual(frame['events'], 2)
        self.assertEqual((frame['graphicsEvents'], frame['computeEvents']), (1, 1))
        self.assertEqual(frame['resourcesByKind'], {'buffer': 1, 'other': 1, 'texture': 1})
        self.assertEqual(frame['messagesBySeverity'], {'error': 1})
        self.assertEqual(frame['formatsSeen'], ['R8G8B8A8_UNORM'])
        self.assertEqual(frame['chunks'], 42)


# =========================================================================== the document itself
class TestReportDocument(BundleCase):
    def test_the_report_is_byte_identical_between_runs(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM']),
                                     event(2, targets=['12 64x64x1 D32_FLOAT'], depth='12')],
                     resources=[resource('11', name='A|B', first=1, width=8, height=8, depth=1,
                                         format='R8G8B8A8_UNORM')])
        self.report(bundle)
        first_md = self.markdown(bundle)
        first_json = open(os.path.join(bundle, 'report.json'), encoding='utf-8').read()
        self.report(bundle)
        self.assertEqual(self.markdown(bundle), first_md)
        self.assertEqual(open(os.path.join(bundle, 'report.json'), encoding='utf-8').read(), first_json)
        self.assertNotIn(self.tmp, first_md, 'the prose must not carry machine-specific paths')

    def test_every_pass_cites_its_events_and_the_way_to_reproduce_them(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(96, targets=['11 64x64x1 R8G8B8A8_UNORM']),
                                     event(200, targets=['12 64x64x1 R8G8B8A8_UNORM'])])
        self.passes(bundle)
        text = self.markdown(bundle)
        self.assertIn('### Pass 1 — eid 96–96 (graphics)', text)
        self.assertIn('### Pass 2 — eid 200–200 (graphics)', text)
        self.assertIn("`replay_dump state '%s' 96`" % RDC, text)
        self.assertIn("`replay_dump shaders '%s' 200`" % RDC, text)
        self.assertEqual(len(self.document(bundle)['caveats']), len(R.report_caveats()))

    def test_the_caveats_name_what_is_missing_and_why(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])])
        self.passes(bundle)
        text = self.markdown(bundle)
        for needle in ('ROADMAP §2', 'ROADMAP §1.4', 'ROADMAP §1.1', 'ROADMAP §1.2', 'ROADMAP §4'):
            self.assertIn(needle, text)

    def test_application_text_cannot_break_a_line_or_a_heading(self):
        """Resource names and capture paths come from outside the tool: a newline must not split a
        list item, and a `|` must not break the structure of a heading or a table row."""
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     resources=[resource('11', name='a|b\nsecond line', first=1, width=4, height=4,
                                         depth=1, format='R8G8B8A8_UNORM')],
                     manifest={'capture': 'a|b.rdc'})
        self.report(bundle)
        text = self.markdown(bundle)
        self.assertIn('# Frame report — a\\|b.rdc', text, 'a heading escapes what would break it')
        bullet = [line for line in text.splitlines() if line.strip().startswith('- res11')]
        self.assertEqual(len(bullet), 1)
        self.assertIn('"a|b second line"', bullet[0], 'a list item keeps the name on one line')

    def test_the_output_directory_can_be_elsewhere(self):
        bundle = self.path('b')
        out = self.path('out')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])])
        printed = self.report(bundle, out)
        self.assertTrue(os.path.isfile(os.path.join(out, 'report.md')))
        self.assertTrue(os.path.isfile(os.path.join(out, 'report.json')))
        self.assertIn(os.path.join(out, 'report.md'), printed)


# =========================================================================== detectors
class TestReportDetectors(BundleCase):
    def flags(self, bundle: str, detector: str) -> List[Dict[str, Any]]:
        self.passes(bundle)
        return [flag for flag in self.document(bundle)['flags'] if flag['detector'] == detector]

    def test_a_message_becomes_one_finding_per_complaint(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     messages=['eid 10     warning  D3D12 WARNING: heap is not shader visible',
                               'eid 40     warning  D3D12 WARNING: heap is not shader visible',
                               'eid 50     info     D3D12 INFO: driver version'])
        flags = self.flags(bundle, 'debug-message')
        self.assertEqual(len(flags), 2, 'the same complaint twice is one finding, and the info is its own')
        warning = [f for f in flags if f['what'].startswith('warning:')][0]
        self.assertIn('D3D12 WARNING: heap is not shader visible', warning['what'])
        self.assertEqual(warning['evidence'], ['2 message(s), eid 10..40'])
        self.assertEqual(warning['certainty'], 'certain')
        self.assertTrue(warning['unproven'], 'no detector has been checked against a labelled capture yet')

        clean = self.path('clean')
        write_bundle(clean, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])])
        self.assertEqual(self.flags(clean, 'debug-message'), [])

    def test_an_all_zero_constant_block_is_a_finding(self):
        bundle = self.path('b')
        zero = ['Atmosphere = {', '  MultiScatteringFactor = 0', '  RayleighScattering = 0, 0, 0, 0']
        write_bundle(bundle, events=[event(96, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     cbuffers={'96_ps_0.json': cbuffer(96, variables=zero),
                               '200_ps_0.json': cbuffer(200, variables=['Light = {', '  intensity = 2.5'])})
        flags = self.flags(bundle, 'all-zero-constant-block')
        self.assertEqual(len(flags), 1)
        self.assertIn('every value in this block is zero', flags[0]['what'])
        self.assertIn('zero at 1 of the 2 event(s)', flags[0]['what'])
        self.assertEqual(flags[0]['evidence'], ['ps stage, slot 0, buffer res300, eid 96'])

        clean = self.path('clean')
        write_bundle(clean, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     cbuffers={'1_ps_0.json': cbuffer(1, variables=['Light = {', '  intensity = 1'])})
        self.assertEqual(self.flags(clean, 'all-zero-constant-block'), [])

    def test_a_dead_allocation_is_a_finding(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     resources=[resource('11', name='SceneColour', first=1, bytes=1048576, width=64,
                                         height=64, depth=1, format='R8G8B8A8_UNORM'),
                                resource('22', name='DeadUAV', kind='buffer', first=0, bytes=4194304),
                                resource('33', kind='other', first=0)])
        # No usage bit anywhere: every record is usage 0.
        with open(os.path.join(bundle, 'resources.json'), encoding='utf-8') as fh:
            document = json.load(fh)
        for entry in document['resources']:
            for record in entry['usage']:
                record['usage'] = 0
        with open(os.path.join(bundle, 'resources.json'), 'w', encoding='utf-8') as fh:
            json.dump(document, fh)

        flags = self.flags(bundle, 'dead-allocation')
        self.assertEqual(len(flags), 2, 'the used texture and the `other` resource are not allocations')
        self.assertIn('res22 "DeadUAV" (buffer, buffer)', flags[0]['evidence'][0])
        self.assertIn('4.00 MB', flags[0]['what'])

    def test_more_dead_allocations_than_the_limit_are_counted(self):
        bundle = self.path('b')
        resources = [resource(str(100 + i), kind='buffer', first=0, bytes=1024) for i in range(23)]
        for entry in resources:
            entry['usage'] = [{'eid': 0, 'usage': 0}]
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])], resources=resources)
        flags = self.flags(bundle, 'dead-allocation')
        self.assertEqual(len(flags), R.DEAD_ALLOCATION_LIMIT + 1)
        self.assertIn('3 smaller unused resource(s) are not listed', flags[-1]['what'])
        self.assertEqual(flags[-1]['evidence'], ['23 unused in total'])

    def test_the_usage_detector_reports_itself_skipped_without_usage_lists(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     manifest={'resourceUsage': 'not collected'})
        self.passes(bundle)
        document = self.document(bundle)
        skipped = {run['detector']: run['why'] for run in document['detectors'] if not run['ran']}
        self.assertIn('dead-allocation', skipped)
        self.assertIn('--no-usage', skipped['dead-allocation'])
        self.assertEqual([f for f in document['flags'] if f['detector'] == 'dead-allocation'], [],
                         'a detector that could not look must not report clean')
        self.assertIn('Skipped: dead-allocation', self.markdown(bundle))

    def test_a_constant_block_at_an_unset_root_parameter_is_a_finding(self):
        bundle = self.path('b')
        write_bundle(
            bundle, events=[event(96, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
            states={96: {
                'state': {'eid': 96, 'shaders': ['cs  res2317   '], 'renderTargets': [],
                          # The middle row is what an unset root parameter looks like: register and space,
                          # and nothing after them.
                          'rootParameters': ['rp0   reg=0 space=0',
                                             'rp1   reg=1 space=0 res344',
                                             'rp3   reg=3 space=0 heap298+0x21cda']},
                'shaders': {'eid': 96, 'stages': [
                    {'stage': 'cs', 'resource': '2317', 'entry': 'Main',
                     'constantBlocks': ['cbuffer[0] $Globals      b0 s0 80 bytes, 3 variables',
                                        'cbuffer[1] Bound            b1 s0 64 bytes, 2 variables',
                                        'cbuffer[2] NotInTheState    b2 s0 64 bytes, 2 variables',
                                        'cbuffer[3] BehindATable     b3 s0 64 bytes, 2 variables']}]},
            }})
        flags = [f for f in self.flags(bundle, 'unbound-root-parameter')]
        self.assertEqual(len(flags), 1, 'b0 is unset, b1 is bound, b2 has no row, b3 is behind a table')
        self.assertIn('reads a constant block at b0 s0', flags[0]['what'])
        self.assertIn('root constants are the other way', flags[0]['what'])
        self.assertEqual(flags[0]['evidence'], ['cs stage, eid 96..96'])
        self.assertEqual(flags[0]['certainty'], 'question',
                         'root constants can serve the register too, and a bundle cannot say which')

    def test_the_measured_vertex_and_pixel_signature_pair_does_not_fire(self):
        """The real rows from `PC Renderer.rdc` at eid 700, and the same window's rows with their widths.

        The vertex shader emits five semantics and the pixel shader reads six; the extra one is
        `SV_IsFrontFace`, which the rasteriser supplies. That is why the rule ignores `SV_` on both sides --
        and why this pair, the only measured attribute pass available, must stay silent. Committed as a test
        rather than a note because it is the one case that could have made the rule fire on correct shaders.

        The second bundle is the same capture's rows as the driver writes them now, with the engine's
        component counts (`c4`, `c3`, `c1` -- measured at eid 678 in that window). Both spellings must stay
        silent: an older bundle has no counts, and a newer one has counts that agree.
        """
        bundle = self.path('b')
        write_bundle(
            bundle, events=[event(700, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
            states={700: {'shaders': {'eid': 700, 'stages': [
                {'stage': 'vs', 'resource': '1', 'entry': 'Main', 'constantBlocks': [],
                 'inputSignature': ['ATTRIBUTE0 reg0', 'ATTRIBUTE13 reg1', 'SV_InstanceID0 reg2',
                                    'SV_VertexID0 reg3'],
                 'outputSignature': ['TEXCOORD10_centroid0 reg0', 'TEXCOORD11_centroid0 reg1',
                                     'PRIMITIVE_ID0 reg2', 'TEXCOORD9 reg3', 'SV_Position0 reg4']},
                {'stage': 'ps', 'resource': '2', 'entry': 'MainPS', 'constantBlocks': [],
                 'outputSignature': ['SV_Target0 reg0'],
                 'inputSignature': ['TEXCOORD10_centroid0 reg0', 'TEXCOORD11_centroid0 reg1',
                                    'PRIMITIVE_ID0 reg2', 'SV_IsFrontFace0 reg2', 'TEXCOORD9 reg3',
                                    'SV_Position0 reg4']}]}}})
        self.assertEqual(self.flags(bundle, 'shader-io-mismatch'), [],
                         'a system input and an interpolation suffix are not mismatches')

        with_widths = self.path('bw')
        write_bundle(
            with_widths, events=[event(678, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
            states={678: {'shaders': {'eid': 678, 'stages': [
                {'stage': 'vs', 'resource': '1', 'entry': 'Main', 'constantBlocks': [],
                 'inputSignature': ['ATTRIBUTE0 reg0 c4', 'ATTRIBUTE13 reg1 c1', 'SV_InstanceID0 reg2 c1',
                                    'SV_VertexID0 reg3 c1'],
                 'outputSignature': ['TEXCOORD10_centroid0 reg0 c4', 'TEXCOORD11_centroid0 reg1 c4',
                                     'PRIMITIVE_ID0 reg2 c1', 'TEXCOORD9 reg3 c3', 'SV_Position0 reg4 c4']},
                {'stage': 'ps', 'resource': '2', 'entry': 'MainPS', 'constantBlocks': [],
                 'outputSignature': ['SV_Target0 reg0 c4'],
                 'inputSignature': ['TEXCOORD10_centroid0 reg0 c4', 'TEXCOORD11_centroid0 reg1 c4',
                                    'PRIMITIVE_ID0 reg2 c1', 'SV_IsFrontFace0 reg2 c1', 'TEXCOORD9 reg3 c3',
                                    'SV_Position0 reg4 c4']}]}}})
        self.assertEqual(self.flags(with_widths, 'shader-io-mismatch'), [],
                         'the measured widths agree on every semantic')

    def test_a_pixel_input_wider_than_the_vertex_output_is_a_finding(self):
        """The width half of *VS out is not PS in*: the evidence is the engine's own component count.

        Reading *fewer* components is a legal prefix subset and stays silent; reading *more* cannot be
        satisfied at pipeline creation. Rows without a count -- a bundle from an older driver -- are not
        compared at all rather than guessed at, which is the difference between "agrees" and "not looked at".
        """
        def pair(vs_out: List[str], ps_in: List[str], name: str) -> str:
            bundle = self.path(name)
            write_bundle(bundle, events=[event(96, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                         states={96: {'shaders': {'eid': 96, 'stages': [
                             {'stage': 'vs', 'resource': '1', 'entry': 'Main', 'constantBlocks': [],
                              'outputSignature': vs_out},
                             {'stage': 'ps', 'resource': '2', 'entry': 'MainPS', 'constantBlocks': [],
                              'inputSignature': ps_in}]}}})
            return bundle

        wider = self.flags(pair(['TEXCOORD0 reg0 c2', 'SV_Position0 reg1 c4'],
                                ['TEXCOORD0 reg0 c4', 'SV_Position0 reg1 c4'], 'wider'),
                           'shader-io-mismatch')
        self.assertEqual(len(wider), 1, 'SV_Position agrees, TEXCOORD0 does not')
        self.assertIn('reads TEXCOORD0 at width c4 and the vertex shader writes it at c2', wider[0]['what'])
        self.assertIn('may use fewer components than its producer writes, never more', wider[0]['what'])
        self.assertEqual(wider[0]['evidence'],
                         ['vs Main -> ps MainPS, eid 96..96',
                          'vs writes: TEXCOORD0 reg0 c2', 'ps reads: TEXCOORD0 reg0 c4'])
        self.assertEqual(wider[0]['certainty'], 'certain')

        narrower = self.flags(pair(['TEXCOORD0 reg0 c4'], ['TEXCOORD0 reg0 c2'], 'narrower'),
                              'shader-io-mismatch')
        self.assertEqual(narrower, [], 'a prefix subset is legal')
        unknown = self.flags(pair(['TEXCOORD0 reg0'], ['TEXCOORD0 reg0'], 'unknown'),
                             'shader-io-mismatch')
        self.assertEqual(unknown, [], 'an older bundle carries no component counts to compare')

    def test_a_pixel_input_the_vertex_shader_does_not_emit_is_a_finding(self):
        def pair(vs_out: List[str], ps_in: List[str], extra_stage: str = '') -> str:
            bundle = self.path('b%d' % hash((tuple(vs_out), tuple(ps_in), extra_stage)))
            stages = [
                {'stage': 'vs', 'resource': '1', 'entry': 'Main', 'constantBlocks': [],
                 'outputSignature': vs_out},
                {'stage': 'ps', 'resource': '2', 'entry': 'MainPS', 'constantBlocks': [],
                 'inputSignature': ps_in}]
            if extra_stage:
                stages.insert(1, {'stage': extra_stage, 'resource': '3', 'entry': 'MainGS',
                                  'constantBlocks': [], 'outputSignature': [], 'inputSignature': []})
            write_bundle(bundle, events=[event(96, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                         states={96: {'shaders': {'eid': 96, 'stages': stages}}})
            return bundle

        missing = self.flags(pair(['TEXCOORD0 reg0', 'SV_Position0 reg1'],
                                  ['TEXCOORD4 reg0', 'SV_Position0 reg1']), 'shader-io-mismatch')
        self.assertEqual(len(missing), 1)
        self.assertIn('reads TEXCOORD4 and the vertex shader does not emit it', missing[0]['what'])
        self.assertEqual(missing[0]['evidence'], ['vs Main -> ps MainPS, eid 96..96'])

        modifier = self.flags(pair(['TEXCOORD9 reg0', 'SV_Position0 reg1'],
                                   ['TEXCOORD9_centroid reg0', 'SV_Position0 reg1']),
                              'shader-io-mismatch')
        self.assertEqual(modifier, [], 'an interpolation suffix is the same semantic')

        between = self.flags(pair(['TEXCOORD0 reg0', 'SV_Position0 reg1'],
                                  ['TEXCOORD4 reg0', 'SV_Position0 reg1'], extra_stage='gs'),
                             'shader-io-mismatch')
        self.assertEqual(between, [], 'a geometry shader sits between them, so the rule does not apply')

    def test_the_red_flags_are_in_both_documents(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     messages=['eid 5      error    something the API disliked'])
        self.passes(bundle)
        text = self.markdown(bundle)
        self.assertIn('## Red flags', text)
        self.assertIn('| debug-message | error: something the API disliked |', text)
        self.assertIn('unproven', text)
        document = self.document(bundle)
        self.assertEqual([flag['detector'] for flag in document['flags']], ['debug-message'])
        self.assertEqual([run['detector'] for run in document['detectors']],
                         ['debug-message', 'all-zero-constant-block', 'unbound-root-parameter',
                          'shader-io-mismatch', 'dead-allocation', 'read-before-write',
                          'write-never-read', 'load-instead-of-clear', 'marker-imbalance',
                          'unattributed-draws', 'zero-work'],
                         'every detector is listed, whether it ran or was skipped')

    def test_a_read_with_nothing_writing_it_first_is_a_question(self):
        """The legitimate shapes and a real ordering bug are the same rows, so this reports the observation.

        An upload (`CPUWrite` before the first read) and a buffer written later in the frame are both quiet:
        the first has a write before its read, the second's finder is the later write. What fires is a
        resource whose only history is reads -- the static-asset shape -- and the evidence names where the
        first write is, or that there is none.
        """
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])], resources=[
            resource('326', name='ColoredTexture', usage=[{'eid': 282, 'usage': 17}]),
            resource('342', name='Uploaded', kind='buffer',
                     usage=[{'eid': 66, 'usage': 45}, {'eid': 124, 'usage': 8}]),
            resource('315', name='Opened', kind='buffer',
                     usage=[{'eid': 147, 'usage': 18}, {'eid': 200, 'usage': 27}]),
            resource('9999', name='NotTracked', usage=[{'eid': 0, 'usage': 0}]),
        ])
        flags = self.flags(bundle, 'read-before-write')
        self.assertEqual(len(flags), 2, 'one texture read and never written, one buffer written later')
        texture = [f for f in flags if 'texture(s)' in f['what']][0]
        self.assertIn('whose first use is a read (PS_Resource)', texture['what'])
        self.assertEqual(texture['evidence'],
                         ['res326 "ColoredTexture" (texture, 0x0x0 ?): read at eid 282, first write never in '
                          'the frame'])
        self.assertEqual(texture['certainty'], 'question')
        buffer = [f for f in flags if 'buffer(s)' in f['what']][0]
        self.assertIn('res315 "Opened" (buffer, 0.00 MB): read at eid 147, first write eid 200',
                      buffer['evidence'][0])
        self.assertNotIn('NotTracked', texture['what'] + str(texture['evidence']) + str(buffer['evidence']),
                         'a resource the engine did not track is never judged')

    def test_a_write_nothing_reads_later_is_a_question(self):
        """Grouped by the kind of last write, because each group is a different story: a resolve nobody
        reads is a readback that never happened, a dispatch output nobody reads is work that dies."""
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])], resources=[
            resource('2236', name='SkyViewLut', usage=[{'eid': 154, 'usage': 27}]),
            resource('2269', name='SceneColor',
                     usage=[{'eid': 257, 'usage': 32}, {'eid': 434, 'usage': 17}]),
            resource('2207', name='BufferedRT', usage=[{'eid': 444, 'usage': 32}]),
            resource('9002', name='Readback', kind='buffer', usage=[{'eid': 520, 'usage': 40}]),
        ])
        flags = self.flags(bundle, 'write-never-read')
        self.assertEqual(len(flags), 3, 'a target read later stays quiet; three write kinds fire')
        dispatch = [f for f in flags if 'CS_RWResource' in f['what']][0]
        self.assertIn('res2236 "SkyViewLut"', dispatch['evidence'][0])
        target = [f for f in flags if f['what'].startswith('1 resource(s) whose last write is ColorTarget')][0]
        self.assertIn('A swapchain image that exists only to be presented looks the same', target['what'],
                      'a present is not a usage row, so the target group says so rather than saying "dead"')
        self.assertIn('res2207 "BufferedRT"', target['evidence'][0])
        resolve = [f for f in flags if 'ResolveDst' in f['what']][0]
        self.assertIn('res9002 "Readback"', resolve['evidence'][0])

    def test_a_target_with_nothing_clearing_or_writing_it_first_is_a_question(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])], resources=[
            resource('2269', name='SceneColor',
                     usage=[{'eid': 90, 'usage': 36}, {'eid': 241, 'usage': 35}, {'eid': 257, 'usage': 32}]),
            resource('2270', name='SceneDepthZ',
                     usage=[{'eid': 87, 'usage': 44}, {'eid': 242, 'usage': 35}, {'eid': 257, 'usage': 33}]),
            resource('2207', name='BufferedRT',
                     usage=[{'eid': 233, 'usage': 44}, {'eid': 444, 'usage': 32}]),
            resource('3001', name='NeverATarget', usage=[{'eid': 10, 'usage': 17}]),
        ])
        flags = self.flags(bundle, 'load-instead-of-clear')
        self.assertEqual(len(flags), 1, 'cleared and discarded targets stay quiet; a barrier is neither')
        self.assertIn('first used as ColorTarget at eid 444', flags[0]['what'])
        self.assertEqual(flags[0]['evidence'],
                         ['res2207 "BufferedRT" (texture, 0x0x0 ?)', 'before that: 233:Barrier'])

    def test_the_usage_detectors_are_skipped_without_usage_lists(self):
        bundle = self.path('b')
        write_bundle(bundle, manifest={'resourceUsage': 'not collected'},
                     events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     resources=[resource('326', usage=[{'eid': 282, 'usage': 17}])])
        self.passes(bundle)
        document = self.document(bundle)
        runs = {run['detector']: run for run in document['detectors']}
        for detector in ('dead-allocation', 'read-before-write', 'write-never-read',
                         'load-instead-of-clear'):
            self.assertFalse(runs[detector]['ran'], detector)
            self.assertIn('--no-usage', runs[detector]['why'], detector)
        self.assertEqual([flag for flag in document['flags'] if flag['detector'].endswith('-read')], [])


# =========================================================================== the .rdc-side detectors
class StreamCase(_CmdCase):
    """The scratch directory and the fake `renderdoc-src` tree the chunk-name map needs.

    The three detectors below read the capture's chunk stream, so they need names for its chunks -- which is
    exactly what `CmdCase` provides (and why these tests are not `BundleCase`: they do not need a bundle at
    all, they call the detectors with a capture and nothing else).
    """

    def chunk_ids(self) -> Dict[str, int]:
        return {name: cid for cid, name in R.load_chunk_names().items()}

    def rdc(self, *chunks: bytes) -> str:
        return self.path('c.rdc', F.rdc([F.section('FrameCapture', F.stream(*chunks))]))

    def chunk(self, name: str, payload: bytes = b'') -> bytes:
        return F.chunk(self.chunk_ids()[name], payload)

    def draw_instanced(self, vertices: int, instances: int = 1) -> bytes:
        return self.chunk('List_DrawInstanced',
                          F.u64b(0) + F.u32b(vertices) + F.u32b(instances) + F.u32b(0) + F.u32b(0))

    def dispatch(self, x: int, y: int = 1, z: int = 1) -> bytes:
        return self.chunk('List_Dispatch', F.u64b(0) + F.u32b(x) + F.u32b(y) + F.u32b(z))


class TestStreamDetectors(StreamCase):
    def test_marker_imbalance_is_found_both_ways(self):
        unclosed = R.detect_marker_balance(self.rdc(self.chunk('PushMarker', b'BasePass\x00'),
                                                    self.draw_instanced(100)))
        self.assertIsNotNone(unclosed)
        assert unclosed is not None
        self.assertEqual(len(unclosed), 1)
        self.assertIn('1 marker(s) never popped', unclosed[0]['what'])
        self.assertEqual(unclosed[0]['evidence'], ['chunk 1'])

        extra = R.detect_marker_balance(self.rdc(self.chunk('PopMarker', b'BasePass\x00'),
                                                 self.chunk('PopMarker', b'Offscreen\x00')))
        assert extra is not None
        self.assertEqual(len(extra), 1)
        self.assertIn('2 marker pop(s) with nothing pushed', extra[0]['what'])

        balanced = R.detect_marker_balance(self.rdc(self.chunk('PushMarker', b'BasePass\x00'),
                                                    self.draw_instanced(100),
                                                    self.chunk('PopMarker', b'BasePass\x00')))
        self.assertEqual(balanced, [])

    def test_draws_outside_a_marker_are_counted(self):
        path = self.rdc(self.draw_instanced(100),
                        self.chunk('PushMarker', b'BasePass\x00'),
                        self.draw_instanced(200),
                        self.chunk('PopMarker', b'BasePass\x00'))
        flags = R.detect_unattributed_draws(path)
        assert flags is not None
        self.assertEqual(len(flags), 1)
        self.assertIn('1 draw(s) or dispatch(es) outside any marker', flags[0]['what'])
        self.assertEqual(flags[0]['evidence'], ['List_DrawInstanced: chunk 1'])

        inside = R.detect_unattributed_draws(
            self.rdc(self.chunk('PushMarker', b'BasePass\x00'), self.draw_instanced(100),
                     self.chunk('PopMarker', b'BasePass\x00')))
        self.assertEqual(inside, [])

    def test_zero_work_is_read_from_the_call_arguments(self):
        path = self.rdc(self.draw_instanced(0), self.dispatch(1, 1, 0), self.draw_instanced(384, 2))
        flags = R.detect_zero_work(path)
        assert flags is not None
        self.assertEqual(len(flags), 2)
        self.assertIn('0 vertices, 1 instance(s)', flags[0]['what'])
        self.assertEqual(flags[0]['evidence'], ['List_DrawInstanced at chunk 1'])
        self.assertIn('dispatch 1x1x0', flags[1]['what'])

    def test_a_detector_that_could_not_look_says_so(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])])
        _flags, runs = R.detect_all(R.load_bundle(bundle))
        skipped = {run['detector']: run['why'] for run in runs if not run['ran']}
        self.assertEqual(sorted(skipped), ['marker-imbalance', 'unattributed-draws', 'zero-work'])
        self.assertTrue(all('no capture path given' in why for why in skipped.values()))

        missing = self.path('nowhere.rdc')
        _flags, runs = R.detect_all(R.load_bundle(bundle), missing)
        skipped = {run['detector']: run['why'] for run in runs if not run['ran']}
        self.assertEqual(sorted(skipped), ['marker-imbalance', 'unattributed-draws', 'zero-work'])
        self.assertTrue(all('could not be read' in why for why in skipped.values()))


# =========================================================================== refusals
class TestReportRefusals(BundleCase):
    def refused(self, bundle: str) -> str:
        printed = self.report(bundle)
        self.assertIn('error:', printed)
        self.assertFalse(os.path.isfile(os.path.join(bundle, 'report.md')))
        return printed

    def test_a_missing_bundle_directory_is_named(self):
        self.assertIn('no such bundle directory', self.refused(self.path('nope')))

    def test_a_missing_events_file_says_how_to_make_one(self):
        bundle = self.path('b')
        write_bundle(bundle)
        os.remove(os.path.join(bundle, 'events.json'))
        printed = self.refused(bundle)
        self.assertIn('events.json is missing', printed)
        self.assertIn('replay_dump dump', printed)

    def test_a_newer_bundle_version_is_refused_not_half_read(self):
        bundle = self.path('b')
        write_bundle(bundle, manifest={'bundleVersion': R.BUNDLE_VERSION + 1})
        printed = self.refused(bundle)
        self.assertIn('bundle version', printed)
        self.assertIn('this tool reads %d' % R.BUNDLE_VERSION, printed)

    def test_a_damaged_document_is_refused(self):
        bundle = self.path('b')
        write_bundle(bundle)
        with open(os.path.join(bundle, 'events.json'), 'w', encoding='utf-8') as fh:
            fh.write('{ not json')
        self.assertIn('is not valid JSON', self.refused(bundle))

    def test_a_capture_mismatch_warns_and_still_writes(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     manifest={'capture': 'other.rdc'})
        printed = self.report(bundle)
        self.assertIn('warning: the bundle was written for other.rdc', printed)
        self.assertTrue(os.path.isfile(os.path.join(bundle, 'report.md')))


if __name__ == '__main__':
    unittest.main()
