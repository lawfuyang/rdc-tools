"""The report document: its passes, its rollups, its recommendations, the evidence it states, and
what it refuses.

The fixture half of this file now lives in `rdc_report_fixtures.py`; the detector suites moved to
`test_rdc_notable.py`, `test_rdc_report_detectors.py`, `test_rdc_detect_state.py` and
`test_rdc_detect_stream.py` when this file passed 1,700 lines."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src', 'py'))
sys.path.insert(0, HERE)

import rdc_analysis as R          # noqa: E402
import rdc_report                 # noqa: E402
from rdc_report_fixtures import *  # noqa: F401,F403  (the shared bundle builders)


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
        self.assertIn('| 1 | 1–2 | — | compute | 2 | n/a | n/a | n/a | compute |', text)

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
        # ranking's inputs a bundle may not carry (the driver's section) and the counters (the driver's section
        # again). The work volumes and the marker path are no longer gaps -- the driver writes both -- so those
        # caveats speak of the *bundle's* age instead, which is what the last needle checks.
        for needle in ('REFERENCE §4.17', 'REFERENCE §9', '2026-09-22'):
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
