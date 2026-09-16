"""Tests for the frame report (`report`): a bundle in, deterministic Markdown out.

The bundles here are written by hand from the shapes `replay_dump dump` produces (README §9), so these
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
        skipped = [run for run in document['detectors'] if not run['ran']]
        self.assertEqual([run['detector'] for run in skipped], ['dead-allocation'])
        self.assertIn('--no-usage', skipped[0]['why'])
        self.assertEqual([f for f in document['flags'] if f['detector'] == 'dead-allocation'], [],
                         'a detector that could not look must not report clean')
        self.assertIn('Skipped: dead-allocation', self.markdown(bundle))

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
                         ['debug-message', 'all-zero-constant-block', 'dead-allocation'])


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
