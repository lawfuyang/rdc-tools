"""The engine schema table: recognition, the concepts a frame's names claim, and what happens without a match.

The tests build bundles whose *names* are the fixture (`tests/test_rdc_report.py`'s writers), point
`RDC_ENGINE_SCHEMAS` at a scratch table so the shipped one cannot leak into a synthetic case, and check the
report that comes out. The one test that reads the repository's own table is the one that pins it: a table that
does not parse, or that names a match kind the interpreter does not know, would otherwise fail silently at run
time and simply claim fewer concepts.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R            # noqa: E402
import rdc_engine_schema as E       # noqa: E402
from rdc_testcase import remove_tree   # noqa: E402
from test_rdc_report import cbuffer, event, resource, write_bundle   # noqa: E402

RDC = 'C:\\captures\\test.rdc'

#: A table whose names are deliberately unlike Unreal's, so a test can say "this bundle speaks *this* engine".
TABLE = {
    'schemaVersion': 1,
    'engine': 'Test Engine',
    'aliases': ['TE'],
    'notes': ['a table for the tests'],
    'detect': {'constantBlocks': ['Scene', 'Material'], 'shaderEntries': ['MainPS', 'MainVS']},
    'concepts': [
        {'concept': 'base pass', 'kind': 'pass', 'group': 'lighting',
         'match': {'constantBlocks': ['Scene', 'Material'], 'shaderEntries': ['MainPS']},
         'members': ['SceneColour'],
         'note': 'the pass that writes the scene'},
        {'concept': 'scene constants', 'kind': 'constant-block',
         'match': {'constantBlocks': ['Scene']}, 'members': ['SceneColour']},
        {'concept': 'marker-only concept', 'kind': 'pass', 'match': {'markers': ['BasePass']}},
    ],
    'questions': [{'id': 'lighting', 'title': 'where the light comes from', 'group': 'lighting',
                   'ask': 'which block?'}],
}


def shaders(eid: int, stage: str = 'ps', entry: str = 'MainPS',
            blocks: tuple = ('Scene', 'Material')) -> dict:
    """One `states/<eid>.shaders.json`, with the constant-block rows in the driver's own format."""
    return {'eid': eid, 'stages': [{
        'stage': stage, 'resource': '2348', 'entry': entry,
        'constantBlocks': ['cbuffer[%d] %-28s b%d s0 64 bytes, 2 variables' % (i, name, i)
                           for i, name in enumerate(blocks)],
    }]}


class EngineCase(unittest.TestCase):
    """A scratch directory, a scratch schema folder, and the two writers the report tests already use."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='rdc_engine_')
        self.addCleanup(remove_tree, self.tmp)
        self.schemas = os.path.join(self.tmp, 'engine-schemas')
        os.makedirs(self.schemas)
        self._env = os.environ.get('RDC_ENGINE_SCHEMAS')
        os.environ['RDC_ENGINE_SCHEMAS'] = self.schemas

    def tearDown(self) -> None:
        if self._env is None:
            os.environ.pop('RDC_ENGINE_SCHEMAS', None)
        else:
            os.environ['RDC_ENGINE_SCHEMAS'] = self._env

    def table(self, name: str = 'test.json', document: dict = TABLE) -> None:
        with io.open(os.path.join(self.schemas, name), 'w', encoding='utf-8') as fh:
            json.dump(document, fh)

    def bundle(self, name: str = 'bundle', events: Optional[list] = None,
               resources: Optional[list] = None, states: Optional[dict] = None,
               cbuffers: Optional[dict] = None) -> str:
        """A bundle with one pass, one `Scene`/`Material` stage pair and one tagged member, unless overridden.

        Spelled out rather than passed through `**kwargs`: the writers take typed arguments, and a dictionary
        that has to be widened to build the call is how a fixture ends up building something else than it says.
        """
        root = os.path.join(self.tmp, name)
        write_bundle(
            root,
            events=[event(10), event(20)] if events is None else events,
            resources=[resource('300', name='SceneColour')] if resources is None else resources,
            manifest={'capture': RDC},
            states={10: {'shaders': shaders(10)}} if states is None else states,
            cbuffers={'10_ps_0.json': cbuffer(10, 'ps', 0, buffer='res300+0x0',
                                              variables=['SceneColour = 1, 0, 0, 1', 'Padding4 = 0'])}
            if cbuffers is None else cbuffers)
        return root

    def interpret(self, bundle: str) -> Dict[str, Any]:
        """The interpretation as a plain mapping: the rows are read by key, which is how the report reads them."""
        return dict(E.interpret_frame(R.load_bundle(bundle), R.reconstruct_passes(*self._passes(bundle))))

    @staticmethod
    def _passes(bundle: str) -> tuple:
        loaded = R.load_bundle(bundle)
        return (loaded['events'], loaded['resources'])


class TestEngineRecognition(EngineCase):
    def test_names_the_table_lists_identify_the_engine(self):
        self.table()
        bundle = self.bundle()
        engine = self.interpret(bundle)
        self.assertEqual(engine['engine'], 'Test Engine')
        self.assertEqual(engine['schema'], 'test.json')
        self.assertIn('name-based', engine['basis'])
        names = [item['name'] for item in engine['detected']]
        self.assertIn('Scene', names)
        self.assertIn('MainPS', names)

    def test_one_shared_name_is_not_an_identification(self):
        # One name the table happens to list -- `Scene`, with an entry point it does not know -- is not a
        # vocabulary: a frame that shares a single common name gets no interpretation rather than a confident
        # wrong one. (`MIN_DETECT_MATCHES` is the rule; this pins it at the point a user would notice.)
        self.table()
        bundle = self.bundle(states={10: {'shaders': shaders(10, entry='OtherEntry', blocks=('Scene',))}})
        engine = self.interpret(bundle)
        self.assertEqual(engine['engine'], '')
        self.assertEqual(engine['concepts'], [])
        self.assertTrue(any('match none' in reason for reason in engine['notInterpreted']))

    def test_no_table_at_all_is_still_an_answer(self):
        bundle = self.bundle()
        engine = self.interpret(bundle)
        self.assertEqual(engine['engine'], '')
        self.assertTrue(any('no engine-schema folder' in reason or 'no engine schema table' in reason
                            for reason in engine['notInterpreted']))

    def test_a_broken_table_is_a_problem_not_an_absence(self):
        # A table that cannot be read must never turn into "this engine is unknown": those are different
        # statements, and only one of them is true.
        broken = dict(TABLE, schemaVersion=99)
        self.table('broken.json', broken)
        tables, problems = E.load_engine_schemas()
        self.assertEqual(tables, [])
        self.assertTrue(any('schemaVersion 99' in problem for problem in problems))
        bundle = self.bundle()
        engine = self.interpret(bundle)
        self.assertEqual(engine['engine'], '')
        self.assertTrue(any('broken.json' in reason for reason in engine['notInterpreted']))


class TestConcepts(EngineCase):
    def test_a_pass_concept_needs_every_kind_it_asks_for(self):
        # The block is bound in the pass and the entry point runs in it: claimed. Either alone is not the
        # concept -- a block that is merely bound is a leftover from an earlier call as often as it is a fact
        # about this pass, and the table asks for both on purpose.
        self.table()
        bundle = self.bundle(states={10: {'shaders': shaders(10)}})
        engine = self.interpret(bundle)
        concepts = [row['concept'] for row in engine['concepts']]
        self.assertIn('base pass', concepts)

    def test_a_pass_is_not_named_by_a_block_alone(self):
        self.table()
        # `OtherEntry` runs with the two blocks bound: the block half matches, the entry half does not, and a
        # conjunction that is half-true is false.
        bundle = self.bundle(states={10: {'shaders': shaders(10, entry='OtherEntry')}})
        engine = self.interpret(bundle)
        self.assertNotIn('base pass', [row['concept'] for row in engine['concepts']])

    def test_every_claim_names_the_document_it_came_from(self):
        self.table()
        bundle = self.bundle()
        engine = self.interpret(bundle)
        for row in engine['concepts']:
            for item in row['evidence']:
                self.assertTrue(item['name'])
                self.assertTrue(item['where'], 'evidence with no place is not evidence')
        base = [row for row in engine['concepts'] if row['concept'] == 'base pass'][0]
        self.assertIn('pass 1 (eid 10)', base['evidence'][0]['where'])
        self.assertEqual((base['passIndex'], base['firstEid']), (1, 10))
        # A concept claimed for the whole frame says so with zeros rather than with a missing key.
        frame = [row for row in engine['concepts'] if row['concept'] == 'scene constants'][0]
        self.assertEqual((frame['passIndex'], frame['firstEid']), (0, 0))

    def test_marker_evidence_is_reported_as_unavailable(self):
        # The table lists a marker name, this bundle carries none, and the report has to say which of those
        # two facts is why the concept is missing -- and say what would fix it.
        self.table()
        bundle = self.bundle()
        engine = self.interpret(bundle)
        self.assertNotIn('marker-only concept', [row['concept'] for row in engine['concepts']])
        self.assertTrue(any('marker-based concept' in reason for reason in engine['notInterpreted']))

    def test_a_marker_claims_a_concept(self):
        # The driver's marker path is evidence like any other name: the *segment* matches, so `A > BasePass`
        # answers for `BasePass`, which is how a table can list a marker without the dynamic text its path may
        # carry.
        self.table()
        bundle = self.bundle(states={10: {'shaders': shaders(10)}},
                             events=[event(10, marker='Scene > BasePass'), event(20)])
        engine = self.interpret(bundle)
        rows = [row for row in engine['concepts'] if row['concept'] == 'marker-only concept']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['passIndex'], rows[0]['firstEid']), (1, 10))
        self.assertEqual(rows[0]['evidence'][0]['kind'], 'marker')
        self.assertEqual(rows[0]['evidence'][0]['name'], 'BasePass')
        self.assertFalse(any('marker-based concept' in reason for reason in engine['notInterpreted']))


class TestValues(EngineCase):
    def test_a_tagged_member_is_read_with_its_event_and_pass(self):
        self.table()
        bundle = self.bundle()
        engine = self.interpret(bundle)
        values = [item for item in engine['questions'][0]['values'] if item['block'] == 'Scene']
        self.assertTrue(values, 'the question has no values to show')
        self.assertEqual(values[0]['member'], 'SceneColour')
        self.assertEqual(values[0]['value'], '1, 0, 0, 1')
        self.assertEqual(values[0]['firstEid'], 10)
        self.assertEqual(values[0]['passIndex'], 1)
        self.assertTrue(values[0]['bound'])

    def test_a_member_read_with_nothing_bound_says_so(self):
        # The engine returns every member's default when nothing is bound, so the zeros are a fact about the
        # *binding*: a report that printed them as values would claim the block is zero at that event.
        self.table()
        bundle = self.bundle(cbuffers={'10_ps_0.json': cbuffer(
            10, 'ps', 0, buffer='(no root descriptor or table slot binds this block: values will be zero)',
            variables=['SceneColour = 0, 0, 0, 0'])})
        engine = self.interpret(bundle)
        values = [item for item in engine['questions'][0]['values'] if item['member'] == 'SceneColour']
        self.assertTrue(values)
        self.assertFalse(values[0]['bound'])

    def test_only_the_members_the_table_tags_are_read(self):
        # `Padding4` sits in the same document; a report that dumped the buffer would print it.
        self.table()
        bundle = self.bundle()
        engine = self.interpret(bundle)
        members = [item['member'] for item in engine['questions'][0]['values']]
        self.assertIn('SceneColour', members)
        self.assertNotIn('Padding4', members)

    def test_the_same_value_in_one_pass_is_one_row(self):
        self.table()
        documents = {'10_ps_0.json': cbuffer(10, 'ps', 0, buffer='res300+0x0',
                                             variables=['SceneColour = 1, 0, 0, 1']),
                     '11_ps_0.json': cbuffer(11, 'ps', 0, buffer='res300+0x0',
                                             variables=['SceneColour = 1, 0, 0, 1'])}
        bundle = self.bundle(events=[event(10), event(11)], states={10: {'shaders': shaders(10)}},
                             cbuffers=documents)
        engine = self.interpret(bundle)
        values = [item for item in engine['questions'][0]['values'] if item['member'] == 'SceneColour']
        self.assertEqual(len(values), 1, 'the same value twice in one pass is one fact')


class TestReportSection(EngineCase):
    def markdown(self, bundle: str) -> str:
        out = os.path.join(self.tmp, 'out')
        self.assertEqual(R.cmd_report(RDC, bundle, out), 0)
        with io.open(os.path.join(out, 'report.md'), encoding='utf-8') as fh:
            return fh.read()

    def test_the_section_says_what_the_claim_rests_on(self):
        self.table()
        text = self.markdown(self.bundle())
        self.assertIn("## The engine's vocabulary", text)
        self.assertIn('**Test Engine**', text)
        self.assertIn('name-based', text)
        self.assertIn('| concept | kind | claimed for | because |', text)
        self.assertIn('`SceneColour`', text)

    def test_a_member_read_with_nothing_bound_says_so_in_the_report(self):
        # The rendering half of the same rule: a reader of the Markdown must not see a zero where the truth is
        # that nothing was bound.
        self.table()
        bundle = self.bundle(cbuffers={'10_ps_0.json': cbuffer(
            10, 'ps', 0, buffer='(no root descriptor or table slot binds this block: values will be zero)',
            variables=['SceneColour = 0, 0, 0, 0'])})
        self.assertIn('*not bound*', self.markdown(bundle))

    def test_without_a_match_the_report_says_so(self):
        text = self.markdown(self.bundle())
        self.assertIn('No engine was recognised by name in this bundle', text)
        self.assertNotIn('| concept | kind | claimed for | because |', text)

    def test_two_runs_over_one_bundle_are_identical(self):
        self.table()
        bundle = self.bundle()
        self.assertEqual(self.markdown(bundle), self.markdown(bundle))


class TestShippedTable(unittest.TestCase):
    """The repository's own table, read the way the report reads it."""

    def setUp(self) -> None:
        self._env = os.environ.pop('RDC_ENGINE_SCHEMAS', None)

    def tearDown(self) -> None:
        if self._env is not None:
            os.environ['RDC_ENGINE_SCHEMAS'] = self._env

    def test_it_loads_without_a_problem(self):
        tables, problems = E.load_engine_schemas()
        self.assertEqual(problems, [])
        self.assertTrue(tables)
        self.assertEqual(tables[0]['engine'], 'Unreal Engine')

    def test_every_concept_matches_on_a_kind_that_exists(self):
        tables, _problems = E.load_engine_schemas()
        for table in tables:
            for concept in table['concepts']:
                for key in concept['match']:
                    self.assertIn(key, ('constantBlocks', 'shaderEntries', 'resources', 'markers', 'members',
                                        'structure'),
                                  '%s matches on %r' % (concept['concept'], key))
                self.assertTrue(concept['match'].get('constantBlocks') or
                                concept['match'].get('shaderEntries') or
                                concept['match'].get('members') or
                                concept['match'].get('structure') or
                                concept['match'].get('markers'))

    def test_every_question_names_a_group_a_concept_uses(self):
        tables, _problems = E.load_engine_schemas()
        for table in tables:
            groups = {str(concept.get('group', '')) for concept in table['concepts']}
            for question in table['questions']:
                self.assertIn(str(question['group']), groups,
                              '%s asks about a group no concept is tagged with' % question['id'])

    def test_the_mobile_and_the_pc_concepts_are_different(self):
        # The pilot in one assertion: the two captures' vocabularies are told apart by the
        # table, so a report over one of them cannot read like a report over the other.
        tables, _problems = E.load_engine_schemas()
        names = {concept['concept']: concept for concept in tables[0]['concepts']}
        self.assertIn('MobileBasePass', names['mobile base pass']['match']['constantBlocks'])
        self.assertIn('IndirectLightingCache',
                      names['indirect lighting cache']['match']['constantBlocks'])
        self.assertNotIn('IndirectLightingCache', names['base pass']['match']['constantBlocks'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
