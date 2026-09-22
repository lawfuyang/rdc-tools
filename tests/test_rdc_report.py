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
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402
import rdc_chunkmap as chunkmap   # noqa: E402  (the detectors name chunks through this module, so the
import rdc_report                 # noqa: E402  (patched by name: the corpus lookup is its global, not R's)
import rdc_fixtures as F          # noqa: E402   #   capture fixture must be built with the same map)
from rdc_testcase import CmdCase as _CmdCase   # noqa: E402

RDC = 'fixture.rdc'


def capture_text(func: Callable[..., object], *args: Any, **kwargs: Any) -> str:
    """Run `func` and return everything it printed on stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


def event(eid: int, kind: str = 'graphics', targets: Sequence[str] = (), depth: str = '0',
          pso: str = '100', shaders: str = 'vs=2348 ', marker: str = '') -> Dict[str, Any]:
    """One `events.json` record, with the fields the report reads spelled out."""
    return {'eid': eid, 'marker': marker, 'pso': pso, 'psoKind': kind, 'shaders': shaders,
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
                 cbuffers: Optional[Dict[str, Dict[str, Any]]] = None,
                 counters: Optional[List[str]] = None) -> None:
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
    if counters is not None:
        write_json(root, 'counters.json', {'capture': RDC, 'counters': counters,
                                          'total': len(counters)})
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

    def flags(self, bundle: str, detector: str) -> List[Dict[str, Any]]:
        """One detector's findings out of the report document -- every class of detector test needs this."""
        self.passes(bundle)
        return [flag for flag in self.document(bundle)['flags'] if flag['detector'] == detector]

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
        self.assertIn('| 1 | 1–2 | — | compute | 2 | n/a | n/a | compute |', text)

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

    def test_the_json_twin_is_schema_valid(self):
        """The report's own schema against the document it describes (the acceptance gate, REFERENCE §4.12).

        The schema lives in `rdc_schemas.py` rather than in `schema/`, because that folder is what the driver
        publishes and the driver does not write this document. It is exhaustive and closed, so a member a
        change dropped or renamed fails here rather than going unnoticed.
        """
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])])
        self.passes(bundle)                       # writes report.json, which is what gets validated
        document = self.document(bundle)
        self.assertEqual(R.validate_document(document, R.REPORT_SCHEMA), [])

    def test_the_report_schema_notices_a_member_that_went_missing(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])])
        self.passes(bundle)
        document = self.document(bundle)
        del document['engine']
        problems = R.validate_document(document, R.REPORT_SCHEMA)
        self.assertTrue(problems, 'a missing member is not a valid document')
        self.assertTrue(any('engine' in problem for problem in problems))

    def test_the_caveats_name_what_is_missing_and_why(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])])
        self.passes(bundle)
        text = self.markdown(bundle)
        # Each gap points at where its work is tracked: the unproven detectors (the verification section), the
        # ranking's unavailable inputs (the action list) and the counters (the frame's pictures and counters).
        # The marker path is no longer one of the gaps -- the driver records it -- so the caveat speaks of the
        # *bundle's* age instead, which is what the last needle checks.
        for needle in ('REFERENCE §4.17', 'REFERENCE §9', 'ROADMAP §4'):
            self.assertIn(needle, text)
        self.assertIn('as of 2026-09-17', text)
        # The vocabulary's own two limits: what a name can say, and which names exist at all.
        self.assertIn('name-based', text)
        self.assertIn('engine-schemas/', text)

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
# =========================================================================== notables (REFERENCE §4.11)
class TestReportNotables(BundleCase):
    """The ranking, the rules that ignore it, and the two claims the notable lists must not make."""

    def two_passes(self) -> str:
        """Two passes: two draws on a 1000x1000 target, then one draw on a 10x10 one. Enough for the ranking
        to have an order and for two of the oddity rules to have something to catch."""
        bundle = self.path('b')
        write_bundle(bundle,
                     events=[event(100, targets=['10 SceneColour 1000x1000 B8G8R8A8_UNORM']),
                             event(101, targets=['10 SceneColour 1000x1000 B8G8R8A8_UNORM']),
                             event(200, targets=['11 Tiny 10x10 B8G8R8A8_UNORM'])],
                     resources=[resource('10', first=100, name='SceneColour', width=1000, height=1000,
                                         depth=1, samples=1, format='B8G8R8A8_UNORM'),
                                resource('11', first=200, name='Tiny', width=10, height=10, depth=1,
                                         samples=1, format='B8G8R8A8_UNORM')])
        self.report(bundle)
        return bundle

    def notable_resource(self, bundle: str, resid: str) -> Dict[str, Any]:
        rows = [row for row in self.document(bundle)['notables']['resources']
                if row['resource'] == 'res%s' % resid]
        self.assertTrue(rows, 'res%s is not in the notable resources' % resid)
        return rows[0]

    def test_the_ranking_orders_by_the_stated_inputs(self):
        doc = self.document(self.two_passes())
        ranked = [row['passIndex'] for row in doc['notables']['passes'] if row['rank']]
        self.assertEqual(ranked[:2], [1, 2])
        first = [row for row in doc['notables']['passes'] if row['passIndex'] == 1][0]
        self.assertEqual(first['values'][0], '2 call(s)')
        self.assertIn('Mpixel', first['values'][1], 'the footprint is in pixels, not bytes')

    def test_a_single_call_pass_is_listed_by_a_rule_not_by_its_rank(self):
        doc = self.document(self.two_passes())
        single = [row for row in doc['notables']['passes'] if row['passIndex'] == 2][0]
        self.assertTrue(any('single event' in reason for reason in single['why']))

    def test_an_input_the_bundle_cannot_answer_stays_in_the_table_with_its_reason(self):
        inputs = {entry['input']: entry for entry in self.document(self.two_passes())['notables']['passInputs']}
        primitives = [entry for entry in inputs.values() if entry['input'].startswith('primitives')][0]
        self.assertFalse(primitives['available'], 'a bundle carries no action list')
        self.assertIn('action list', primitives['why'])
        self.assertFalse(inputs['counter cost']['available'])
        self.assertTrue(inputs['calls']['available'])

    def test_counter_cost_joins_the_ranking_when_the_bundle_has_counters(self):
        bundle = self.path('c')
        write_bundle(bundle, events=[event(100), event(200)], resources=[resource('10', first=100)],
                     counters=['eid 200  <counter> = 12.5'])
        self.report(bundle)
        doc = self.document(bundle)
        entry = [entry for entry in doc['notables']['passInputs'] if entry['input'] == 'counter cost'][0]
        self.assertTrue(entry['available'], 'the bundle holds counter results')
        self.assertEqual(entry['why'], '', 'a reason for an input that *is* available would be a contradiction')
        costs = [value for row in doc['notables']['passes'] for value in row['values']
                 if value.startswith('counter cost')]
        self.assertEqual(costs, ['counter cost 12.500 over 1 row(s)'])

    def test_a_resource_the_engine_recorded_nothing_for_is_not_called_unread(self):
        bundle = self.path('d')
        write_bundle(bundle, events=[event(100, targets=['10 C 100x100 B8G8R8A8_UNORM'])],
                     resources=[resource('10', first=100, name='Unrecorded', width=100, height=100, depth=1,
                                         samples=1, format='B8G8R8A8_UNORM', usage=[])])
        self.report(bundle)
        row = self.notable_resource(bundle, '10')
        self.assertTrue(any('recorded no usage row' in value for value in row['values']))
        self.assertFalse(any(value.startswith('read by 0') for value in row['values']),
                         '"nobody reads it" is not something an empty chain can say')
        self.assertFalse(any(reason.startswith('never read at all') for reason in row['why']))

    def test_the_unused_marker_is_reported_as_not_tracked(self):
        bundle = self.path('e')
        write_bundle(bundle, events=[event(100, targets=['10 C 100x100 B8G8R8A8_UNORM'])],
                     resources=[resource('10', first=0, name='Untracked', width=100, height=100, depth=1,
                                         samples=1, format='B8G8R8A8_UNORM',
                                         usage=[{'eid': 0, 'usage': 0}])])
        self.report(bundle)
        row = self.notable_resource(bundle, '10')
        self.assertTrue(any('did not track it' in value for value in row['values']))
        self.assertFalse(any(reason.startswith('never read at all') for reason in row['why']))

    def test_a_write_only_resource_is_listed_as_never_read(self):
        bundle = self.path('f')
        write_bundle(bundle, events=[event(100, targets=['10 C 100x100 B8G8R8A8_UNORM'])],
                     resources=[resource('10', first=100, name='BufferedRT', width=100, height=100, depth=1,
                                         samples=1, format='B8G8R8A8_UNORM',
                                         usage=[{'eid': 100, 'usage': 32}])])
        self.report(bundle)
        row = self.notable_resource(bundle, '10')
        self.assertTrue(any(reason.startswith('never read at all') for reason in row['why']))
        self.assertTrue(any(value == 'read by 0 pass(es)' for value in row['values']))

    def test_a_texture_is_measured_in_pixels_and_a_buffer_in_bytes(self):
        bundle = self.path('g')
        write_bundle(bundle, events=[event(100, targets=['10 C 1000x1000 B8G8R8A8_UNORM'])],
                     resources=[resource('10', first=100, name='SceneColour', width=1000, height=1000,
                                         depth=1, samples=1, format='B8G8R8A8_UNORM'),
                                resource('11', kind='buffer', first=100, name='Big', bytes=1048576)])
        self.report(bundle)
        self.assertTrue(any('Mpixel' in value and 'bytes/pixel' in value
                            for value in self.notable_resource(bundle, '10')['values']))
        self.assertEqual(self.notable_resource(bundle, '11')['values'][0], '1.00 MB')

    def test_a_buffer_is_never_called_undecodable(self):
        # The driver writes a format for a texture and none for a buffer, so "no format" is a fact about
        # textures: calling every buffer undecodable was wrong on a real capture (149 of them).
        bundle = self.path('h')
        write_bundle(bundle, events=[event(100, targets=['11 C 100x100 B8G8R8A8_UNORM'])],
                     resources=[resource('10', kind='buffer', first=100, name='Plain', bytes=1024),
                                resource('11', first=100, name='NoFormat', width=100, height=100, depth=1,
                                         samples=1)])
        self.report(bundle)
        self.assertFalse(any('could not decode' in reason for reason in self.notable_resource(bundle, '10')['why']))
        self.assertTrue(any('could not decode' in reason for reason in self.notable_resource(bundle, '11')['why']))

    def test_a_capped_list_says_what_it_left_out(self):
        bundle = self.path('i')
        write_bundle(bundle, events=[event(100)],
                     resources=[resource(str(100 + index), kind='buffer', first=100, bytes=1024,
                                         usage=[{'eid': 100, 'usage': 32}]) for index in range(25)])
        self.report(bundle)
        doc = self.document(bundle)
        self.assertEqual(len(doc['notables']['resources']), doc['notables']['limit'] + doc['notables']['oddityLimit'])
        self.assertTrue(any('not listed below' in note for note in doc['notables']['notes']))


# =========================================================================== recommendations (REFERENCE §4.11)
class TestReportRecommendations(BundleCase):
    """What to look at first: one row per thing to check, each with the command that shows its evidence."""

    def dead_allocation_bundle(self) -> str:
        bundle = self.path('b')
        write_bundle(bundle, events=[event(100)],
                     resources=[resource('10', kind='buffer', first=0, bytes=1024,
                                         usage=[{'eid': 0, 'usage': 0}])])
        self.report(bundle)
        return bundle

    def test_a_finding_becomes_one_row_whose_command_aims_at_its_own_evidence(self):
        doc = self.document(self.dead_allocation_bundle())
        rows = [row for row in doc['recommendations']['rows'] if row['kind'] == 'finding']
        self.assertEqual(len(rows), 1, 'one row per detector, not one per finding')
        self.assertIn('dead-allocation', rows[0]['do'])
        self.assertEqual(rows[0]['command'], "replay_dump usage 'fixture.rdc' 10")
        self.assertEqual(rows[0]['resource'], 'res10')
        self.assertEqual(rows[0]['severity'], 'medium')

    def test_severity_and_kind_decide_the_order(self):
        doc = self.document(self.dead_allocation_bundle())
        rows = doc['recommendations']['rows']
        self.assertEqual([row['rank'] for row in rows], list(range(1, len(rows) + 1)))
        keys = [(R.SEVERITY_ORDER.index(row['severity']), R.KIND_ORDER.index(row['kind'])) for row in rows]
        self.assertEqual(keys, sorted(keys), 'the rows are not in the declared order: %s' % rows)

    def test_a_command_falls_back_to_the_action_tree_when_the_evidence_names_nothing(self):
        self.assertEqual(R._command('unbound-table-slot', {}, 'cap.rdc'), "replay_dump draws 'cap.rdc'")
        self.assertEqual(R._command('unbound-table-slot', {'eid': '42'}, 'cap.rdc'),
                         "replay_dump state 'cap.rdc' 42")
        self.assertEqual(R._command('all-zero-constant-block',
                                    {'eid': '12', 'stage': 'ps', 'slot': '3'}, 'cap.rdc'),
                         "replay_dump cb 'cap.rdc' 12 ps 3")

    def test_the_references_come_out_of_the_evidence_the_driver_wrote(self):
        flag = R.RedFlag(detector='all-zero-constant-block', what='', certainty='certain', unproven=True,
                         evidence=['ps stage, slot 3, buffer res30, eid 12..40'])
        self.assertEqual(R._refs(flag), {'eid': '12', 'resId': '30', 'stage': 'ps', 'slot': '3'})

    def test_a_skipped_detector_becomes_a_gap_with_the_command_that_closes_it(self):
        bundle = self.path('c')
        write_bundle(bundle, events=[event(100)], resources=[resource('10', first=100)],
                     manifest={'resourceUsage': 'not collected'})
        self.report(bundle)
        doc = self.document(bundle)
        gaps = [row for row in doc['recommendations']['rows'] if row['kind'] == 'gap']
        self.assertTrue(any('dead-allocation' in row['do'] for row in gaps),
                        'a skipped detector is a gap: %s' % gaps)
        self.assertTrue(any(row['command'].startswith('replay_dump dump') for row in gaps),
                        'a bundle gap is fixed by writing a bundle: %s' % gaps)
        self.assertTrue(any('--with-counters' in row['do'] for row in gaps))
        self.assertTrue(all(row['command'].startswith('replay_dump') for row in gaps),
                        'every recommendation carries a command, not a sentence')

    def test_the_severity_table_covers_every_detector(self):
        doc = self.document(self.dead_allocation_bundle())
        table = {item['detector'] for row in doc['severityTable'] for item in row['members']}
        self.assertEqual(len(table), len({run['detector'] for run in doc['detectors']}),
                         'the table and the run list disagree about which detectors exist')
        for run in doc['detectors']:
            self.assertIn(run['detector'], table)

    def test_a_finding_is_grouped_under_its_detectors_group(self):
        bundle = self.path('d')
        write_bundle(bundle, events=[event(100)], resources=[resource('10', first=100)],
                     messages=['eid 100  error  something went wrong'])
        self.report(bundle)
        doc = self.document(bundle)
        group = [row['severity'] for row in doc['severityTable'] for item in row['members']
                 if item['detector'] == 'debug-message'][0]
        self.assertEqual(group, 'medium')
        text = self.markdown(bundle)
        heading = '### %s — ' % group
        self.assertIn(heading, text)
        self.assertLess(text.index(heading), text.index('something went wrong'),
                        'the finding is not under its group heading')


class TestReportEvidence(BundleCase):
    """AGENTS.md: no claim without evidence -- every row says which event or resource it is about."""

    def test_every_row_of_every_section_cites_an_event_or_a_resource(self):
        bundle = self.path('b')
        write_bundle(bundle,
                     events=[event(100, targets=['10 SceneColour 100x100 B8G8R8A8_UNORM']),
                             event(200, kind='compute')],
                     resources=[resource('10', first=100, name='SceneColour', width=100, height=100,
                                         depth=1, samples=1, format='B8G8R8A8_UNORM')],
                     messages=['eid 200  error  a complaint'])
        self.report(bundle)
        doc = self.document(bundle)
        for entry in doc['passes']:
            self.assertGreater(entry['firstEid'], 0, 'a pass without an eid range: %s' % entry)
            self.assertGreaterEqual(entry['lastEid'], entry['firstEid'])
        for row in doc['notables']['passes']:
            self.assertGreater(row['firstEid'], 0, 'a notable pass with no eid: %s' % row)
        for row in doc['notables']['resources']:
            self.assertRegex(row['resource'], r'^res\d+$', 'a notable resource with no id: %s' % row)
        for row in doc['recommendations']['rows']:
            self.assertTrue(row['eid'] > 0 or row['resource'] or row['command'],
                            'a recommendation that says nothing about where to look: %s' % row)
        for flag in doc['flags']:
            self.assertTrue(flag['evidence'], 'a finding without evidence: %s' % flag)
        self.assertTrue(doc['flags'], 'the fixture should produce at least one finding')


class TestReportDetectors(BundleCase):
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
        markdown = self.markdown(bundle)
        self.assertIn('Skipped:', markdown)
        self.assertIn('dead-allocation (', markdown)

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

    def test_nothing_bound_through_a_table_is_a_finding(self):
        """The table half of the flagship row: the engine resolved the slot and it holds nothing.

        `certain`, unlike the root-descriptor half above it, because there is no second explanation to weigh:
        the row is the engine's own answer for that slot. Only rows from a parameter *visible* to the reading
        stage are matched -- the fixture proves that with a ps-visible table and a cs shader.
        """
        def with_binding(name: str, parameter: str, slot: str, stage: str = 'cs') -> str:
            bundle = self.path(name)
            write_bundle(bundle, events=[event(96, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                         states={96: {
                             'state': {'eid': 96, 'shaders': [], 'rootParameters': [parameter, slot]},
                             'shaders': {'eid': 96, 'stages': [
                                 {'stage': stage, 'resource': '2317', 'entry': 'Main',
                                  'constantBlocks': [], 'readOnlyResources': ['MyTexture t0 s0 n1']}]}}})
            return bundle

        empty = self.flags(with_binding('empty', 'rp0   reg=0 space=0 vis=cs heap298+0x10',
                                        'rp0   t0  s0   cat(3) none'), 'unbound-table-slot')
        self.assertEqual(len(empty), 1)
        self.assertIn('reads MyTexture at t0 s0 and the descriptor table bound there resolves the slot to '
                      'nothing', empty[0]['what'])
        self.assertEqual(empty[0]['evidence'], ['eid 96..96', 'rp0   t0  s0   cat(3) none'])
        self.assertEqual(empty[0]['certainty'], 'certain')

        filled = self.flags(with_binding('filled', 'rp0   reg=0 space=0 vis=cs heap298+0x10',
                                         'rp0   t0  s0   cat(3) res2233'), 'unbound-table-slot')
        self.assertEqual(filled, [], 'a populated slot is not a finding')

        other_stage = self.flags(with_binding('ps', 'rp0   reg=0 space=0 vis=ps heap298+0x10',
                                              'rp0   t0  s0   cat(3) none'), 'unbound-table-slot')
        self.assertEqual(other_stage, [], 'the table is visible to ps and the shader reading it is cs')

    def test_the_root_signature_and_the_heap_disagreeing_is_a_finding(self):
        """The mismatch is between the range the signature declares and what the heap actually holds.

        `cat(N)` (the range's category) and `type(N)` (the slot's own descriptor type) are the two sides, and
        the engine's own `CategoryForDescriptorType` relates them. The first draft of this rule compared the
        *reflection's* letter against a row of a *different* letter instead -- and a real capture produced
        ~60 false positives with it, because `b0` and `t0` are separate register spaces: a `t` row says
        nothing about `b`. That case is pinned here so it cannot come back.
        """
        def with_binding(name: str, slot: str, binding: str, key: str = 'readOnlyResources') -> str:
            bundle = self.path(name)
            stage = {'stage': 'ps', 'resource': '2317', 'entry': 'Main', 'constantBlocks': [],
                     'readOnlyResources': [], 'readWriteResources': []}
            stage[key] = [binding]
            write_bundle(bundle, events=[event(96, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                         states={96: {
                             'state': {'eid': 96, 'shaders': [],
                                       'rootParameters': ['rp0   reg=0 space=0 vis=ps heap298+0x10', slot]},
                             'shaders': {'eid': 96, 'stages': [stage]}}})
            return bundle

        mismatch = self.flags(with_binding('mismatch', 'rp0   b0  s0   cat(1) type(4) res342',
                                           'cbuffer[0] $Globals b0 s0 80 bytes, 1 variables',
                                           key='constantBlocks'), 'binding-kind-mismatch')
        self.assertEqual(len(mismatch), 1, 'cat(1) is a CBV range and type(4) is an image')
        self.assertIn('reads $Globals at b0 s0, and the range bound there is declared a constant block while '
                      'the heap holds an image', mismatch[0]['what'])
        self.assertEqual(mismatch[0]['evidence'], ['eid 96..96', 'rp0   b0  s0   cat(1) type(4) res342'])
        self.assertEqual(mismatch[0]['certainty'], 'certain')

        agree = self.flags(with_binding('agree', 'rp0   t0  s0   cat(3) type(4) res2233',
                                        'MyTexture t0 s0 n1'), 'binding-kind-mismatch')
        self.assertEqual(agree, [], 'an SRV range holding an image is the engine\'s own consistent pair')

        space = self.flags(with_binding('space', 'rp0   t0  s0   cat(3) type(4) res2233',
                                        'cbuffer[0] $Globals b0 s0 80 bytes, 1 variables',
                                        key='constantBlocks'), 'binding-kind-mismatch')
        self.assertEqual(space, [], 'a t row says nothing about b0: the register spaces are separate')

        old_rows = self.flags(with_binding('old', 'rp0   t0  s0   cat(3) res2233', 'MyTexture t0 s0 n1'),
                              'binding-kind-mismatch')
        self.assertEqual(old_rows, [], 'a bundle without the type token has nothing to compare')

        empty = self.flags(with_binding('empty', 'rp0   t0  s0   cat(3) type(0) none', 'MyTexture t0 s0 n1'),
                           'binding-kind-mismatch')
        self.assertEqual(empty, [], 'an empty slot is the other detector\'s finding, not a mismatch')

    def test_the_measured_vertex_and_pixel_signature_pair_does_not_fire(self):
        """The real rows from `desktop-1` at eid 700, and the same window's rows with their widths.

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
                          'unbound-table-slot', 'binding-kind-mismatch', 'shader-io-mismatch',
                          'dead-allocation', 'read-before-write', 'write-never-read',
                          'load-instead-of-clear', 'dead-compute', 'depth-logic', 'empty-scissor',
                          'stencil-without-writer', 'blend-in-opaque-pass', 'format-units-suspicion',
                          'mismatched-msaa', 'marker-imbalance', 'unattributed-draws', 'zero-work'],
                         'every detector is listed, whether it ran or was skipped')
        runs = {run['detector']: run for run in self.document(bundle)['detectors']}
        for detector in ('unbound-table-slot', 'binding-kind-mismatch'):
            self.assertFalse(runs[detector]['ran'], detector)
            self.assertIn('no resolved descriptor tables', runs[detector]['why'], detector)

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
                         'load-instead-of-clear', 'dead-compute'):
            self.assertFalse(runs[detector]['ran'], detector)
            self.assertIn('--no-usage', runs[detector]['why'], detector)
        self.assertEqual([flag for flag in document['flags'] if flag['detector'].endswith('-read')], [])

    def test_a_compute_pass_binding_uavs_nothing_reads_is_a_finding(self):
        """Per *pass*, because that is what a bundle can attribute work to: the usage chain says which event
        bound a resource as a compute UAV, and a pass is a run of dispatches agreeing on pipeline and
        shaders. A UAV read later stays quiet, and so does a UAV in a *graphics* pass -- this rule is about
        compute passes, and the graphics case is `write-never-read`'s.
        """
        bundle = self.path('b')
        write_bundle(bundle, events=[
            event(96, kind='compute', pso='2300', shaders='cs=2302 '),
            event(97, kind='compute', pso='2300', shaders='cs=2302 '),
            event(200, targets=['11 64x64x1 R8G8B8A8_UNORM']),
        ], resources=[
            resource('2266', kind='buffer', name='NumCulledLightsGrid',
                     usage=[{'eid': 97, 'usage': 27}]),
            resource('2234', name='MultiScatteredLuminanceLut',
                     usage=[{'eid': 97, 'usage': 27}, {'eid': 120, 'usage': 18}]),
            resource('9001', kind='buffer', name='GraphicsUAV',
                     usage=[{'eid': 200, 'usage': 27}]),
            resource('9999', name='NotTracked', usage=[{'eid': 0, 'usage': 0}]),
        ])
        flags = self.flags(bundle, 'dead-compute')
        self.assertEqual(len(flags), 1, 'one compute pass with one unread UAV')
        self.assertIn('the compute pass at eid 96..97 binds 1 resource(s) as UAVs that nothing after eid 97 '
                      'reads', flags[0]['what'])
        self.assertEqual(flags[0]['evidence'],
                         ['res2266 "NumCulledLightsGrid" (buffer, 0.00 MB): bound as a UAV at eid 97, last '
                          'use eid 97 (CS_RWResource)'])
        self.assertEqual(flags[0]['certainty'], 'question')


# =========================================================================== pipeline state
def pipeline_state(depth_enable: bool = True, depth_writes: bool = True, depth_function: str = 'LessEqual',
                   stencil_enable: bool = False, stencil_read_only: bool = False, pass_op: str = 'Keep',
                   write_mask: int = 255, blends: Optional[List[Dict[str, Any]]] = None,
                   viewports: Optional[List[Dict[str, Any]]] = None,
                   scissors: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The `outputMerger`/`viewports`/`scissors` blocks as the driver writes them, in one call so a test's
    fixture is about the rule rather than about the JSON. `pass_op` is the pass operation of both faces: the
    op that decides whether a state *writes* stencil, which is what the writer rule turns on."""
    face = {'fail': 'Keep', 'depthFail': 'Keep', 'pass': pass_op, 'function': 'AlwaysTrue',
            'compareMask': 255, 'writeMask': write_mask}
    return {
        'outputMerger': {
            'depthEnable': depth_enable, 'depthWrites': depth_writes, 'depthFunction': depth_function,
            'stencilEnable': stencil_enable, 'stencilReadOnly': stencil_read_only,
            'alphaToCoverage': False, 'independentBlend': False,
            'frontFace': dict(face), 'backFace': dict(face),
            'blends': blends if blends is not None else [],
        },
        'viewports': viewports if viewports is not None else [],
        'scissors': scissors if scissors is not None else [],
    }


def graphics_state(eid: int, depth_target: str = '2270', render_targets: Optional[List[str]] = None,
                   **rest: Any) -> Dict[str, Any]:
    """One draw's state document: the fields every rule reads, then whatever the test cares about."""
    document: Dict[str, Any] = {'eid': eid, 'depthTarget': depth_target,
                                'renderTargets': render_targets or [], 'shaders': [],
                                'rootParameters': []}
    blocks = dict(rest.pop('blocks', {}))
    document.update(pipeline_state(**blocks))
    document.update(rest)
    return document


class TestReportPipelineState(BundleCase):
    """The five rules that read the state blocks the driver records per state *change* (`outputMerger`,
    `viewports`, `scissors`), and the one that reads `samples` from the resource table."""

    def test_depth_writes_with_the_test_off_are_certain(self):
        """Two facts in one fixture: the range runs to the *next state document* (a state is bound until it
        changes), and a dispatch is never judged on the output-merger state at all -- the mistake this pins
        is blaming the previous pass's state on a compute range that merely inherited it."""
        bundle = self.path('b')
        write_bundle(bundle, events=[
            event(96, targets=['2207 2003x1254x1 R8G8B8A8_UNORM']),
            event(150, kind='compute', pso='300', shaders='cs=2302 '),
        ], states={
            96: {'state': graphics_state(96, blocks={'depth_enable': False, 'depth_writes': True})},
            150: {'state': graphics_state(150, depth_target='0',
                                          blocks={'depth_enable': False, 'depth_writes': True})},
        })
        flags = self.flags(bundle, 'depth-logic')
        self.assertEqual(len(flags), 1, 'the compute range is skipped: it inherits the last draw\'s state')
        self.assertEqual(flags[0]['evidence'],
                         ['eid 96..149: depthWrites on with depthEnable off (depthFunction LessEqual), '
                          'on res2270'])
        self.assertEqual(flags[0]['certainty'], 'certain')

    def test_a_depth_test_with_no_target_is_its_own_finding(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(96, targets=['2207 2003x1254x1 R8G8B8A8_UNORM'])],
                     states={96: {'state': graphics_state(96, depth_target='0',
                                                          blocks={'depth_enable': True,
                                                                  'depth_writes': False})}})
        flags = self.flags(bundle, 'depth-logic')
        self.assertEqual(len(flags), 1)
        self.assertIn('depth testing is on with no depth target bound', flags[0]['what'])
        self.assertIn('eid 96..96: depthEnable on (depthFunction LessEqual) with no depth target bound',
                      flags[0]['evidence'])

    def test_a_zero_extent_rectangle_is_certain_and_a_disabled_one_is_not(self):
        bundle = self.path('b')
        empty = {'x': 0.0, 'y': 0.0, 'width': 0.0, 'height': 856.0, 'minDepth': 0.0, 'maxDepth': 1.0,
                 'enabled': True}
        disabled = {'x': 0, 'y': 0, 'width': 0, 'height': 0, 'enabled': False}
        write_bundle(bundle, events=[event(96, targets=['2207 2003x1254x1 R8G8B8A8_UNORM'])],
                     states={96: {'state': graphics_state(
                         96, blocks={'viewports': [empty], 'scissors': [disabled]})}})
        flags = self.flags(bundle, 'empty-scissor')
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['evidence'], ['eid 96..96: viewport x 0 y 0 width 0 height 856'])
        self.assertEqual(flags[0]['certainty'], 'certain')

    def test_stencil_with_nothing_writing_it_first_is_a_question(self):
        """The writer can be an earlier range on the same target *or* the range's own write-as-it-tests,
        which is why the third range -- a different target -- is the one that fires."""
        bundle = self.path('b')
        write_bundle(bundle, events=[
            event(96, targets=['2207 2003x1254x1 R8G8B8A8_UNORM']),
            event(120, targets=['2207 2003x1254x1 R8G8B8A8_UNORM']),
            event(140, targets=['2207 2003x1254x1 R8G8B8A8_UNORM']),
        ], states={
            96: {'state': graphics_state(96, blocks={'stencil_enable': True, 'pass_op': 'Replace'})},
            120: {'state': graphics_state(120, blocks={'stencil_enable': True})},
            140: {'state': graphics_state(140, depth_target='2271', blocks={'stencil_enable': True})},
        })
        flags = self.flags(bundle, 'stencil-without-writer')
        self.assertEqual(len(flags), 1, 'the range that writes as it tests makes the next one quiet')
        self.assertIn('eid 140..140: stencil tests res2271', flags[0]['evidence'][0])
        self.assertEqual(flags[0]['certainty'], 'question')

    def test_a_multisampled_colour_target_without_a_resolve_is_a_question(self):
        bundle = self.path('b')
        write_bundle(bundle, events=[event(96, targets=['2207 2003x1254x1 R8G8B8A8_UNORM'])], resources=[
            resource('2207', name='SceneColorMS', format='R8G8B8A8_UNORM', samples=4,
                     usage=[{'eid': 96, 'usage': 32}]),
            resource('2210', name='Resolved', format='R8G8B8A8_UNORM', samples=4,
                     usage=[{'eid': 96, 'usage': 32}, {'eid': 120, 'usage': 40}]),
            resource('2270', name='SceneDepthMS', format='D32_FLOAT', samples=4,
                     usage=[{'eid': 96, 'usage': 33}]),
        ])
        flags = self.flags(bundle, 'mismatched-msaa')
        self.assertEqual(len(flags), 1, 'a resolved target and a depth target are both silent')
        self.assertIn('4 samples, used as a colour target, nothing reads it either',
                      flags[0]['evidence'][0])

    def test_blending_on_a_gbuffer_named_target_is_a_heuristic(self):
        bundle = self.path('b')
        blend_on = {'enabled': True, 'writeMask': 15, 'colorOperation': 'Add', 'alphaOperation': 'Add',
                    'srcColor': 'SrcAlpha', 'dstColor': 'InvSrcAlpha', 'srcAlpha': 'One', 'dstAlpha': 'Zero'}
        write_bundle(bundle, events=[
            event(96, targets=['2207 2003x1254x1 R8G8B8A8_UNORM']),
            event(120, targets=['2208 2003x1254x1 R8G8B8A8_UNORM']),
        ], resources=[
            resource('2207', name='GBufferA', format='R8G8B8A8_UNORM', usage=[{'eid': 96, 'usage': 32}]),
            resource('2208', name='SceneColor', format='R8G8B8A8_UNORM', usage=[{'eid': 120, 'usage': 32}]),
        ], states={
            96: {'state': graphics_state(96, render_targets=['slot 0  res2207'],
                                         blocks={'blends': [blend_on]})},
            120: {'state': graphics_state(120, render_targets=['slot 0  res2208'],
                                          blocks={'blends': [blend_on]})},
        })
        flags = self.flags(bundle, 'blend-in-opaque-pass')
        self.assertEqual(len(flags), 1, 'SceneColor may legitimately be blended; a GBuffer may not')
        self.assertIn('res2207 "GBufferA" with blending on (src SrcAlpha, dst InvSrcAlpha, op Add, '
                      'writeMask 15)', flags[0]['evidence'][0])
        self.assertEqual(flags[0]['certainty'], 'heuristic')

    def test_a_float_output_into_a_narrow_linear_target_is_a_heuristic(self):
        """Three targets and three float outputs: only the plain 8-bit UNORM one fires. A float format has
        the range already, and an sRGB one carries its transfer function -- the half of the row this rule
        deliberately does not claim."""
        bundle = self.path('b')
        write_bundle(bundle, events=[event(96, targets=['2207 2003x1254x1 R8G8B8A8_UNORM'])], resources=[
            resource('2207', name='LdrTarget', format='R8G8B8A8_UNORM', usage=[{'eid': 96, 'usage': 32}]),
            resource('2208', name='HdrTarget', format='R16G16B16A16_FLOAT', usage=[{'eid': 96, 'usage': 32}]),
            resource('2209', name='SrgbTarget', format='R8G8B8A8_UNORM_SRGB',
                     usage=[{'eid': 96, 'usage': 32}]),
        ], states={96: {
            'state': graphics_state(96, render_targets=['slot 0  res2207', 'slot 1  res2208',
                                                        'slot 2  res2209']),
            'shaders': {'eid': 96, 'stages': [{'stage': 'ps', 'resource': '2349', 'entry': 'Main',
                                               'outputSignature': ['SV_TARGET0 reg0 c4 float',
                                                                   'SV_TARGET1 reg1 c4 float',
                                                                   'SV_TARGET2 reg2 c4 float']}]},
        }})
        flags = self.flags(bundle, 'format-units-suspicion')
        self.assertEqual(len(flags), 1)
        self.assertIn('res2207 "LdrTarget" (R8G8B8A8_UNORM, 8 bits) and the pixel shader writes '
                      'SV_TARGET0 reg0 c4 float', flags[0]['evidence'][0])
        self.assertEqual(flags[0]['certainty'], 'heuristic')

    def test_the_state_rules_are_skipped_without_the_state_blocks(self):
        """A bundle from an older driver has no `outputMerger`: "the state is fine" and "I could not look at
        the state" are different answers, and only one of them is worth anything."""
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                     states={1: {'state': {'eid': 1, 'depthTarget': '0', 'renderTargets': [],
                                           'shaders': [], 'rootParameters': []}}})
        self.passes(bundle)
        document = self.document(bundle)
        runs = {run['detector']: run for run in document['detectors']}
        for detector in ('depth-logic', 'empty-scissor', 'stencil-without-writer', 'blend-in-opaque-pass',
                         'format-units-suspicion'):
            self.assertFalse(runs[detector]['ran'], detector)
            self.assertIn('older driver', runs[detector]['why'], detector)
        self.assertTrue(runs['mismatched-msaa']['ran'],
                        'the samples rule reads the resource table, which every bundle has')


# =========================================================================== the .rdc-side detectors
class StreamCase(_CmdCase):
    """The scratch directory and the fake `renderdoc-src` tree the chunk-name map needs.

    The three detectors below read the capture's chunk stream, so they need names for its chunks -- which is
    exactly what `CmdCase` provides (and why these tests are not `BundleCase`: they do not need a bundle at
    all, they call the detectors with a capture and nothing else).
    """

    def chunk_ids(self) -> Dict[str, int]:
        # Through the module the detectors themselves call: the test fixture patches it with a fake
        # `renderdoc-src` tree, and a capture built from the *unpatched* map would use ids the detector then
        # cannot name -- which is exactly how these two tests failed once the loader moved out of the entry
        # module.
        return {name: cid for cid, name in chunkmap.load_chunk_names().items()}

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
        """Two families of detectors can be blocked, each with its own reason: the .rdc-side three need a
        capture path, and the two binding rules need a bundle whose driver resolved descriptor tables."""
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])])
        _flags, runs = R.detect_all(R.load_bundle(bundle))
        skipped = {run['detector']: run['why'] for run in runs if not run['ran']}
        for detector in ('marker-imbalance', 'unattributed-draws', 'zero-work'):
            self.assertIn('no capture path given', skipped[detector])
        for detector in ('unbound-table-slot', 'binding-kind-mismatch'):
            self.assertIn('no resolved descriptor tables', skipped[detector])

        missing = self.path('nowhere.rdc')
        _flags, runs = R.detect_all(R.load_bundle(bundle), missing)
        skipped = {run['detector']: run['why'] for run in runs if not run['ran']}
        for detector in ('marker-imbalance', 'unattributed-draws', 'zero-work'):
            self.assertIn('could not be read', skipped[detector])
        for detector in ('unbound-table-slot', 'binding-kind-mismatch'):
            self.assertIn('no resolved descriptor tables', skipped[detector])


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


class TestKnownCauses(BundleCase):
    """`apply_known`: the one path by which a finding stops being unproven (REFERENCE §4.17)."""

    def flag(self, **extra: Any) -> Any:
        """One finding as `detect_all` hands it over: `Any`, so a test can bend any member of it."""
        flag = {'detector': 'dead-allocation', 'what': 'created and never used by any call',
                'evidence': ['res80125 "Nanite.VisibleClustersSWHW" (buffer, buffer)'],
                'certainty': 'certain', 'unproven': True}
        flag.update(extra)
        return flag

    def cause(self, **extra: Any) -> Any:
        entry = {'detector': 'dead-allocation', 'what': 'created and never used',
                 'evidence': 'Nanite.VisibleClustersSWHW', 'cause': 'allocated for a path this frame '
                 'does not run', 'verdict': 'confirmed'}
        entry.update(extra)
        return entry

    def test_a_match_turns_the_flag_off_and_writes_the_cause_on_it(self):
        flags = [self.flag()]
        self.assertEqual(R.apply_known(flags, [self.cause()]), [])
        self.assertFalse(flags[0]['unproven'])
        self.assertEqual(flags[0]['cause'], 'allocated for a path this frame does not run')
        self.assertEqual(flags[0]['verdict'], 'confirmed')

    def test_the_detector_the_substring_and_the_evidence_all_have_to_agree(self):
        # Each of the three is narrowed, and a non-match comes back as *stale* -- described by the
        # corpus entry's own detector and `what`, because that is what a reader has to go and fix.
        for entry, stale in ((self.cause(detector='other'), 'other: created and never used'),
                             (self.cause(what='nothing like it'),
                              'dead-allocation: nothing like it'),
                             (self.cause(evidence='res999'),
                              'dead-allocation: created and never used')):
            flags = [self.flag()]
            self.assertEqual(R.apply_known(flags, [entry]), [stale])
            self.assertTrue(flags[0]['unproven'])
            self.assertNotIn('cause', flags[0])

    def test_an_empty_evidence_matches_any_finding_of_that_detector(self):
        flags = [self.flag(evidence=['res1 (buffer, buffer)'])]
        self.assertEqual(R.apply_known(flags, [self.cause(evidence='')]), [])
        self.assertFalse(flags[0]['unproven'])

    def test_a_note_is_not_a_cause_and_never_matches(self):
        flags = [self.flag()]
        self.assertEqual(R.apply_known(flags, ['its markers balance']), [])
        self.assertTrue(flags[0]['unproven'])

    def test_one_cause_can_cover_several_findings_of_the_same_detector(self):
        flags = [self.flag(), self.flag(what='created and never used by any call: 48.00 MB',
                                       evidence=['res2 "Other" (buffer, buffer)'])]
        self.assertEqual(R.apply_known(flags, [self.cause(evidence='')]), [])
        self.assertEqual([flag['unproven'] for flag in flags], [False, False])

    def test_a_cause_that_no_finding_matches_comes_back_as_stale(self):
        self.assertEqual(R.apply_known([self.flag()], [self.cause(what='a rule that stopped firing')]),
                         ['dead-allocation: a rule that stopped firing'])

    def test_the_report_counts_what_was_proven_and_says_which_corpus_it_read(self):
        bundle = self.path('bundle')
        write_bundle(bundle, events=[event(1003)])
        with mock.patch.object(rdc_report, 'known_for_capture', lambda path: [self.cause()]):
            printed = capture_text(R.cmd_report, RDC, bundle)
        self.assertIn('known    : 1 entr(y/ies) for this capture in the corpus, 1 of them a cause', printed)

    def test_a_stale_cause_is_printed_rather_than_left_to_be_found(self):
        bundle = self.path('bundle')
        write_bundle(bundle, events=[event(1003)])
        with mock.patch.object(rdc_report, 'known_for_capture',
                               lambda path: [self.cause(what='a rule that stopped firing')]):
            printed = capture_text(R.cmd_report, RDC, bundle)
        self.assertIn('stale    : the corpus knows a cause for dead-allocation: a rule that stopped '
                      'firing that no finding here matches any more', printed)


if __name__ == '__main__':
    unittest.main()
