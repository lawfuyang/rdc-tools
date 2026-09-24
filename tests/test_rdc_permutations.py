"""The permutation table: which shaders a frame binds, how much, and the join with the file side.

The two halves are joined on a *measured* key -- a bundle's `hash` is `sha256` of the shader's bytes, which is
one of the three identities `psos` keeps per container -- so the join is pinned here with a hand-written index
rather than assumed, and a hash that matches nothing is pinned as left empty rather than guessed at.

Run through the suite's entry points like the rest: `python -m unittest discover -s tests -t tests`, or
`src/py/rdc_analysis.py selftest`.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from typing import Any, Dict, List, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402
from rdc_testcase import TempDirCase, capture_text   # noqa: E402
from rdc_report_fixtures import event, write_bundle      # noqa: E402

HASH_A = 'aa' * 32
HASH_B = 'bb' * 32
HASH_C = 'cc' * 32


def stage(name: str, resource: str, hash_: str, entry: str = 'Main', cbuffers: int = 1,
          srvs: int = 2, uavs: int = 0) -> Dict[str, Any]:
    """One `stages[]` entry of a `states/<eid>.shaders.json`, with the arrays the reflection summary counts."""
    return {'stage': name, 'resource': resource, 'entry': entry, 'hash': hash_,
            'constantBlocks': ['cbuffer[%d]' % i for i in range(cbuffers)],
            'readOnlyResources': ['srv'] * srvs, 'readWriteResources': ['uav'] * uavs}


class TestPermutations(TempDirCase):
    """The counting: one row per `(stage, hash)`, over the events that bind it."""

    def bundle(self, events: Sequence[Dict[str, Any]],
               states: Dict[int, Dict[str, Any]]) -> Any:
        root = self.path('bundle')
        write_bundle(root, events=list(events), states=states)
        return R.load_bundle(root)

    def test_one_row_per_stage_and_hash_counted_over_the_events(self):
        states = {100: {'shaders': {'eid': 100, 'stages': [stage('vs', '2348', HASH_A),
                                                          stage('ps', '2349', HASH_B)]}},
                  200: {'shaders': {'eid': 200, 'stages': [stage('vs', '2348', HASH_A),
                                                           stage('ps', '2350', HASH_C)]}}}
        events = [event(100, shaders='vs=2348 ps=2349 '),
                  event(150, shaders='vs=2348 ps=2349 '),
                  event(200, shaders='vs=2348 ps=2350 ')]
        rows = R.permutations(self.bundle(events, states))
        by_hash = {row['hash'][:2]: row for row in rows}
        self.assertEqual(sorted(by_hash), ['aa', 'bb', 'cc'])
        self.assertEqual((by_hash['aa']['events'], by_hash['aa']['firstEid'], by_hash['aa']['lastEid']),
                         (3, 100, 200))
        self.assertEqual((by_hash['bb']['events'], by_hash['bb']['firstEid'], by_hash['bb']['lastEid']),
                         (2, 100, 150))
        self.assertEqual(by_hash['cc']['events'], 1)
        self.assertEqual((by_hash['aa']['stage'], by_hash['aa']['entry']), ('vs', 'Main'))
        self.assertEqual((by_hash['aa']['cbuffers'], by_hash['aa']['srvs'], by_hash['aa']['uavs']),
                         (1, 2, 0))
        self.assertEqual(by_hash['aa']['resources'], ['2348'])

    def test_two_resources_with_one_hash_are_one_row_and_both_are_named(self):
        """A shader uploaded twice (a reload, a second entry point) is one identity, and both ids are kept."""
        states = {100: {'shaders': {'eid': 100, 'stages': [stage('vs', '2348', HASH_A)]}},
                  200: {'shaders': {'eid': 200, 'stages': [stage('vs', '9000', HASH_A)]}}}
        rows = R.permutations(self.bundle([event(100, shaders='vs=2348 '),
                                           event(200, shaders='vs=9000 ')], states))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['events'], 2)
        self.assertEqual(rows[0]['resources'], ['2348', '9000'])

    def test_an_event_whose_stage_no_document_describes_is_counted_nowhere(self):
        """Not guessed at: a resource no state document names has no hash, so it makes no row."""
        states = {100: {'shaders': {'eid': 100, 'stages': [stage('vs', '2348', HASH_A)]}}}
        events = [event(100, shaders='vs=2348 '), event(200, shaders='vs=7777 ')]
        rows = R.permutations(self.bundle(events, states))
        self.assertEqual([row['hash'] for row in rows], [HASH_A])
        self.assertEqual(rows[0]['events'], 1)

    def test_a_frame_with_no_state_documents_has_no_rows_rather_than_an_error(self):
        rows = R.permutations(self.bundle([event(100)], {}))
        self.assertEqual(rows, [])

    def test_bound_shaders_reads_the_drivers_own_row(self):
        self.assertEqual(R.bound_shaders({'shaders': 'vs=2348 ps=res2349 '}),
                         [('vs', '2348'), ('ps', '2349')])
        self.assertEqual(R.bound_shaders({'shaders': ''}), [])
        self.assertEqual(R.bound_shaders({}), [])


class TestTheContainerJoin(TempDirCase):
    """`join_containers`: the key is the container's `sha256`, and a miss stays empty."""

    def row(self, hash_: str) -> Any:
        return {'hash': hash_, 'container': 0, 'size': 0, 'ilbd': None}

    def index(self, containers: List[Dict[str, Any]]) -> Any:
        return {'containers': containers, 'psos': [], 'undecoded': 0}

    def test_a_matching_container_gives_its_offset_size_and_debug_flag(self):
        rows = [self.row(HASH_A)]
        containers = [{'sha256': HASH_A, 'offset': 4096, 'size': 11096, 'ilbd': True}]
        self.assertEqual(R.join_containers(rows, self.index(containers)), 1)
        self.assertEqual((rows[0]['container'], rows[0]['size'], rows[0]['ilbd']), (4096, 11096, True))

    def test_a_hash_no_container_has_is_left_empty_and_counted_as_a_miss(self):
        rows = [self.row(HASH_A), self.row('zz' * 32)]
        containers = [{'sha256': HASH_A, 'offset': 8, 'size': 16, 'ilbd': False}]
        self.assertEqual(R.join_containers(rows, self.index(containers)), 1)
        self.assertEqual((rows[1]['container'], rows[1]['size'], rows[1]['ilbd']), (0, 0, None))

    def test_the_other_two_identities_a_container_carries_are_not_the_join_key(self):
        """The header hash and the `HASH` part's digest name *containers*, not the bytes a bundle hashes."""
        rows = [self.row(HASH_A)]
        containers = [{'sha256': HASH_B, 'hash': HASH_A, 'shaderHash': HASH_A, 'offset': 4, 'size': 8,
                       'ilbd': True}]
        self.assertEqual(R.join_containers(rows, self.index(containers)), 0)
        self.assertEqual(rows[0]['container'], 0)


class TestThePermutationsCli(TempDirCase):
    """The command: its table, the note about what it did not read, and the failed lookup."""

    def bundle(self, *events: Dict[str, Any]) -> str:
        root = self.path('bundle')
        write_bundle(root, events=list(events),
                     states={100: {'shaders': {'eid': 100, 'stages': [stage('vs', '2348', HASH_A),
                                                                      stage('ps', '2349', HASH_B)]}}})
        return root

    def test_the_table_names_the_stage_the_reflection_and_what_was_not_read(self):
        text = capture_text(R.cmd_permutations, self.bundle(event(100, shaders='vs=2348 ps=2349 ')),
                            40, 'table')
        self.assertIn('vs', text)
        self.assertIn('cb1 srv2 uav0', text)
        self.assertIn('permutations: 2 shader(s) bound by 2 event(s)', text)
        self.assertIn('containers: not read (pass `--capture <rdc>`', text)

    def test_a_hash_prefix_answers_one_shader_and_an_unknown_one_exits_one(self):
        bundle = self.bundle(event(100, shaders='vs=2348 ps=2349 '))
        text = capture_text(R.cmd_permutations, bundle, 40, 'table', HASH_A[:12])
        self.assertIn('hash %s (vs, 1 event(s) eid 100-100' % HASH_A, text)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = R.cmd_permutations(bundle, 40, 'table', 'deadbeefdeadbeef')
        self.assertEqual(code, 1)
        self.assertIn('not bound by any event of this frame', err.getvalue())

    def test_the_csv_is_one_row_per_shader(self):
        text = capture_text(R.cmd_permutations, self.bundle(event(100, shaders='vs=2348 ps=2349 ')),
                            40, 'csv')
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3, lines)          # a header and two shaders
        self.assertTrue(lines[0].startswith('stage'), lines[0])


if __name__ == '__main__':
    unittest.main()
