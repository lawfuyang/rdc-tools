"""The report's detectors over the bundle side, from bundles built by `rdc_report_fixtures`: what
each one fires on, and the fixture that makes it stay quiet."""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src', 'py'))
sys.path.insert(0, HERE)

import rdc_analysis as R          # noqa: E402
import rdc_detect_bundle          # noqa: E402
from rdc_report_fixtures import *  # noqa: F401,F403  (the shared bundle builders)


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

    def test_the_zero_test_agrees_with_the_float_comparison_it_replaced(self):
        """The text rule and the `float()`-per-token rule answer every awkward shape the same way.

        That equivalence *is* the justification for not converting (0.234 s of `desktop-1`'s 0.277 s of
        detector time was this conversion and nothing else), so it is pinned here rather than assumed:
        the reference below is the rule the detector used to run, written out.
        """
        number = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
        cases = [
            (['Scalar = 0'], True),
            (['Vec = 0, 0, 0, 0', 'Neg = -0.0', 'Exp = 0.000e-9', 'Point = .0'], True),
            (['Atmosphere = {', '  Factor = 0', '}', 'Name = SceneCB'], True),
            (['Tiny = 1e-320'], False),          # subnormal, and not zero to a float either
            (['Small = 0.0001'], False),
            (['One = 1'], False),
            (['Hex = 0x0'], True),               # both findall tokens are `0` -- a hex case, and honest
            (['Hex = 0x1F'], False),
            (['Nan = nan', 'True = true'], False),      # no number at all: not evidence either way
            (['Mixed = 0, 1', 'Zero = 0'], False),
            (['Blank = '], False),
            ([], False),
        ]
        for rows, expected in cases:
            floats = [float(token) for row in rows if '=' in row
                      for token in number.findall(row.split('=', 1)[1])]
            reference = bool(floats) and all(value == 0.0 for value in floats)
            self.assertEqual(reference, expected, rows)         # the fixture says what the rule says
            sides, all_zero = rdc_detect_bundle._zero_sides(rows)
            self.assertEqual(all_zero, reference, rows)
            if reference:                                       # the count is only read for a zero block
                self.assertEqual(sum(len(number.findall(side)) for side in sides),
                                 len(floats), rows)

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
                          'mismatched-msaa', 'marker-imbalance', 'unattributed-draws', 'zero-work',
                          'srgb-view-mismatch'],
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

if __name__ == '__main__':
    unittest.main()
