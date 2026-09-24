"""The report's notables (`rdc_notable.py`), from bundles built by `rdc_report_fixtures`."""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src', 'py'))
sys.path.insert(0, HERE)

from rdc_report_fixtures import *  # noqa: F401,F403  (the shared bundle builders)


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
        """A bundle with no `volume` rows -- written by a driver from before 2026-09-22, or one whose frame has
        no calls in it -- keeps the input in the table with its reason, rather than ranking every pass by a hard
        zero that a reader would take for a measurement."""
        inputs = {entry['input']: entry for entry in self.document(self.two_passes())['notables']['passInputs']}
        primitives = [entry for entry in inputs.values() if entry['input'].startswith('primitives')][0]
        self.assertFalse(primitives['available'], 'this bundle carries no per-event volume')
        self.assertIn('no per-event volume', primitives['why'])
        self.assertFalse(inputs['counter cost']['available'])
        self.assertTrue(inputs['calls']['available'])

    def test_a_dispatches_targets_are_its_bound_uavs_when_the_state_document_has_them(self):
        """What a dispatch writes: not the leftover output-merge state the engine reports at a compute event,
        but the bound read-write resources -- and the same `n/a` where the document has none."""
        bundle = self.path('uavs')
        write_bundle(bundle, events=[event(1, kind='compute')],
                     states={1: {'state': {'eid': 1, 'marker': '', 'rootSignature': '99',
                                           'rootParameters': [], 'shaders': ['cs res10'],
                                           'renderTargets': ['3 C 100x100 B8G8R8A8_UNORM'],
                                           'depthTarget': '4',
                                           'uavs': ['cs u0 res6979', 'cs u3 res11']}}})
        self.report(bundle)
        doc = self.document(bundle)
        entry = doc['passes'][0]
        self.assertEqual(entry['writes'], ['cs u0 res6979', 'cs u3 res11'])
        self.assertEqual(entry['targets'], [],
                         'the leftover output-merge state is not this pass\'s targets -- it stays in the '
                         'state document, which is where the engine reported it')
        text = self.markdown(bundle)
        self.assertIn('- targets: res6979 (cs u0), res11 (cs u3)', text)
        self.assertIn('| res6979 (cs u0), res11 (cs u3) |', text)

    def test_a_dispatch_with_no_uav_rows_says_not_applicable_rather_than_writes_nothing(self):
        bundle = self.path('nouavs')
        write_bundle(bundle, events=[event(1, kind='compute')],
                     states={1: {'state': {'eid': 1, 'marker': '', 'rootSignature': '99',
                                           'rootParameters': [], 'shaders': ['cs res10'],
                                           'renderTargets': [], 'depthTarget': '0'}}})
        self.report(bundle)
        self.assertEqual(self.document(bundle)['passes'][0]['writes'], [])
        self.assertIn('n/a — a dispatch does not set the output merger', self.markdown(bundle))

    def test_the_work_volumes_are_summed_per_pass(self):
        """A pass's numbers are the sum over its calls, and they are what the pass-by-pass bullet prints: the
        bundle carries them per *event*, and the report's unit is the pass."""
        bundle = self.path('v')
        write_bundle(bundle,
                     events=[event(100, targets=['10 SceneColour 1000x1000 B8G8R8A8_UNORM'],
                                   volume=draw_volume(2880, triangles=960)),
                             event(101, targets=['10 SceneColour 1000x1000 B8G8R8A8_UNORM'],
                                   volume=draw_volume(2880, triangles=960)),
                             event(200, targets=['11 Tiny 10x10 B8G8R8A8_UNORM'],
                                   volume=draw_volume(300, triangles=100))],
                     resources=volume_resources())
        self.report(bundle)
        text = self.markdown(bundle)
        doc = self.document(bundle)
        first = [entry for entry in doc['passes'] if entry['firstEid'] == 100][0]
        self.assertEqual((first['triangles'], first['vertices'], first['instances'], first['volumeCalls']),
                         (1920, 5760, 2, 2))
        second = [entry for entry in doc['passes'] if entry['firstEid'] == 200][0]
        self.assertEqual((second['triangles'], second['volumeCalls']), (100, 1))
        self.assertIn('1.9 K triangle(s) over 2 call(s)', text)
        self.assertNotIn('the bundle carries no counts', text)
        inputs = {entry['input']: entry for entry in doc['notables']['passInputs']}
        self.assertTrue(inputs['primitives (vertices, triangles, threads)']['available'],
                        'a bundle with volume rows can answer the first input of the ranking')
        self.assertEqual(inputs['primitives (vertices, triangles, threads)']['why'], '',
                         'a reason on an available input would be a contradiction')

    def test_a_compute_pass_reports_threads_and_not_triangles(self):
        """One unit per pass: a dispatch's work is threads (groups x [numthreads]), and a graphics pass's is
        triangles. A pass is one kind or the other, so a reader is never comparing the two."""
        bundle = self.path('c')
        write_bundle(bundle, events=[event(100, kind='compute', pso='200', shaders='cs=500 ',
                                           volume=dispatch_volume([8, 5, 2], 5120, [4, 4, 4]))],
                     resources=volume_resources())
        self.report(bundle)
        doc = self.document(bundle)
        entry = doc['passes'][0]
        self.assertEqual((entry['threads'], entry['triangles'], entry['volumeCalls']), (5120, 0, 1))
        self.assertEqual(doc['notables']['passes'][0]['values'][0], '5.1 K thread(s) over 1 call(s)')

    def test_the_work_volume_outranks_the_call_count(self):
        """The ranking's first input doing the job it was added for: one heavy call beats three light ones.

        The second half is the same frame without the volumes -- the *old* behaviour, where the only work input
        a bundle had was the call count -- and the order flips. Without that half the test would pass for a
        ranking that ignored the volumes and happened to sort that way.
        """
        events = [event(100, targets=['10 SceneColour 1000x1000 B8G8R8A8_UNORM'],
                        volume=draw_volume(2880, triangles=960)),
                  event(200, targets=['11 Tiny 10x10 B8G8R8A8_UNORM'],
                        volume=draw_volume(300, triangles=100)),
                  event(201, targets=['11 Tiny 10x10 B8G8R8A8_UNORM'],
                        volume=draw_volume(300, triangles=100)),
                  event(202, targets=['11 Tiny 10x10 B8G8R8A8_UNORM'],
                        volume=draw_volume(300, triangles=100))]
        with_volume = self.path('w1')
        write_bundle(with_volume, events=events, resources=volume_resources())
        self.report(with_volume)
        ranked = [row['passIndex'] for row in self.document(with_volume)['notables']['passes'] if row['rank']]
        self.assertEqual(ranked[:2], [1, 2], 'the one heavy pass ranks above the three light ones')

        no_volume = self.path('w2')
        write_bundle(no_volume,
                     events=[{k: v for k, v in entry.items() if k != 'volume'} for entry in events],
                     resources=volume_resources())
        self.report(no_volume)
        old = [row['passIndex'] for row in self.document(no_volume)['notables']['passes'] if row['rank']]
        self.assertEqual(old[:2], [2, 1], 'without the volumes the call count decides, which is the old order')

    def test_counter_cost_joins_the_ranking_when_the_bundle_has_counters(self):
        bundle = self.path('c')
        write_bundle(bundle, events=[event(100), event(200)], resources=[resource('10', first=100)],
                     counters=[{'eid': 200, 'counter': 1, 'value': 12.5}])
        self.report(bundle)
        doc = self.document(bundle)
        entry = [entry for entry in doc['notables']['passInputs'] if entry['input'] == 'counter cost'][0]
        self.assertTrue(entry['available'], 'the bundle holds counter results')
        self.assertEqual(entry['why'], '', 'a reason for an input that *is* available would be a contradiction')
        costs = [value for row in doc['notables']['passes'] for value in row['values']
                 if value.startswith('counter cost')]
        self.assertEqual(costs, ['counter cost 12.500 over 1 row(s)'])

    def test_a_passes_cost_is_the_counters_sum_and_its_share_of_the_frame(self):
        """The frame's time per pass: one counter summed over the pass's own events, its share of the frame's
        total, and the dearest single event in it -- the three numbers a reader used to get by running
        `counters` beside the report and joining the two documents by hand."""
        bundle = self.path('cost')
        write_bundle(bundle,
                     events=[event(100, targets=['10 SceneColour 1000x1000 B8G8R8A8_UNORM']),
                             event(101, targets=['10 SceneColour 1000x1000 B8G8R8A8_UNORM']),
                             event(200, targets=['11 Tiny 10x10 B8G8R8A8_UNORM'])],
                     manifest={'withCounters': 1},
                     counters=[{'eid': 100, 'counter': 1, 'value': 1.0},
                               {'eid': 101, 'counter': 1, 'value': 3.0},
                               {'eid': 200, 'counter': 1, 'value': 4.0}],
                     cost_unit='ms')
        self.report(bundle)
        doc = self.document(bundle)
        first = [entry for entry in doc['passes'] if entry['firstEid'] == 100][0]
        self.assertEqual((first['cost'], first['share'], first['costRows'], first['dearestEid']),
                         (4.0, 0.5, 2, 101))
        second = [entry for entry in doc['passes'] if entry['firstEid'] == 200][0]
        self.assertEqual((second['cost'], second['share'], second['dearestEid']), (4.0, 0.5, 200))
        self.assertEqual((doc['frame']['costCounter'], doc['frame']['costUnit'],
                          doc['frame']['costMeasured']), ('EventGPUDuration', 'ms', 3))
        text = self.markdown(bundle)
        self.assertIn('Costs are `EventGPUDuration` (unit `ms`), measured at 3 of the frame\'s 3 event(s)',
                      text)
        self.assertIn('| 4.000ms (50.0%) |', text)
        self.assertIn('- cost: 4.000ms of `EventGPUDuration` (50.0% of the frame), dearest event 101', text)

    def test_a_pass_the_counter_skipped_is_unmeasured_not_free(self):
        """A counter that measured part of the frame: the pass with no row says `n/a` rather than 0, and the
        shares are against what was measured -- the frame is not scaled so that a missing half reads as free."""
        bundle = self.path('partial')
        write_bundle(bundle,
                     events=[event(100, targets=['10 SceneColour 1000x1000 B8G8R8A8_UNORM']),
                             event(200, targets=['11 Tiny 10x10 B8G8R8A8_UNORM'])],
                     manifest={'withCounters': 1},
                     counters=[{'eid': 200, 'counter': 1, 'value': 4.0}], cost_unit='ms')
        self.report(bundle)
        doc = self.document(bundle)
        skipped = [entry for entry in doc['passes'] if entry['firstEid'] == 100][0]
        measured = [entry for entry in doc['passes'] if entry['firstEid'] == 200][0]
        self.assertEqual((skipped['cost'], skipped['costRows'], skipped['dearestEid']), (0.0, 0, 0))
        self.assertEqual((measured['cost'], measured['share'], measured['dearestEid']), (4.0, 1.0, 200))
        text = self.markdown(bundle)
        self.assertIn('| 1 | 100–100 | — | graphics | 1 | n/a |', text)
        self.assertIn('- cost: 4.000ms of `EventGPUDuration` (100.0% of the frame), dearest event 200', text)
        self.assertNotIn('- cost:', text.split('### Pass 1')[1].split('### Pass 2')[0],
                         'the pass with no measured event says nothing about a cost')

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

if __name__ == '__main__':
    unittest.main()
