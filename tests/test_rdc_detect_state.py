"""The pipeline-state detectors (`rdc_detect_state.py`), from bundles built by `rdc_report_fixtures`."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src', 'py'))
sys.path.insert(0, HERE)

from rdc_report_fixtures import *  # noqa: F401,F403  (the shared bundle builders)


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

if __name__ == '__main__':
    unittest.main()
