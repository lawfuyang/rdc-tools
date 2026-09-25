"""The file-side detectors (`rdc_detect_stream.py`): the ones that read the capture's own chunk
stream, so they need a real fixture capture rather than a bundle."""

from __future__ import annotations

import os
import sys
import unittest
from typing import Dict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src', 'py'))
sys.path.insert(0, HERE)

import rdc_analysis as R          # noqa: E402
import rdc_chunkmap as chunkmap   # noqa: E402
import rdc_fixtures as F          # noqa: E402
from rdc_report_fixtures import *  # noqa: F401,F403  (the shared bundle builders)
from rdc_testcase import CmdCase as _CmdCase   # noqa: E402


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

    def format_id(self, name: str) -> int:
        """The `DXGI_FORMAT` id the bundled table gives a name: a fixture writes ids, a reader reads names."""
        return {value: key for key, value in R.load_format_names().items()}[name]

    def texture(self, rid: int, fmt: int, width: int = 64, height: int = 64) -> bytes:
        """A committed texture, whose *declared* format is what the sRGB rule compares a view against."""
        return self.chunk('Device_CreateCommittedResource',
                          F.pl_committed_resource(
                              rid, F.pl_resource_desc(2, width=width, height=height, fmt=fmt)))

    def srv(self, rid: int, fmt: int, heap: int = 1, index: int = 0) -> bytes:
        """An SRV write over a resource -- the only place a *view* format is recorded in a capture."""
        return self.chunk('Device_CreateShaderResourceView',
                          F.pl_descriptor_write(rid, heap=heap, index=index, view_format=fmt))

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

    def test_a_linear_texture_read_through_an_srgb_view_is_a_question(self):
        """The format rule's other half, and one a bundle cannot answer: the resource table says the bits are
        `R8G8B8A8_UNORM` and a view written over the same resource declares `R8G8B8A8_UNORM_SRGB`, so the
        same bits go through the transfer function on one path and not on the other."""
        path = self.rdc(self.texture(2207, self.format_id('R8G8B8A8_UNORM')),
                        self.srv(2207, self.format_id('R8G8B8A8_UNORM_SRGB')))
        flags = R.detect_srgb_view_mismatch(path)
        assert flags is not None
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['detector'], 'srgb-view-mismatch')
        self.assertEqual(flags[0]['certainty'], 'question')
        self.assertIn('res2207: declared R8G8B8A8_UNORM, and a view over it declares R8G8B8A8_UNORM_SRGB',
                      flags[0]['evidence'][0])

    def test_a_view_of_a_different_format_is_not_this_story(self):
        """Two ways the rule must stay quiet. A resource that *is* sRGB has its transfer function on purpose,
        and a view of a *different* format over one resource (a typeless read, an integer read as float) is
        a different story with a different cause -- claiming it from the names would be the noisy kind."""
        intended = self.rdc(self.texture(2208, self.format_id('R8G8B8A8_UNORM_SRGB')),
                            self.srv(2208, self.format_id('R8G8B8A8_UNORM_SRGB')))
        self.assertEqual(R.detect_srgb_view_mismatch(intended), [])

        other = self.rdc(self.texture(2209, self.format_id('R8G8B8A8_TYPELESS')),
                         self.srv(2209, self.format_id('R8G8B8A8_UNORM_SRGB')))
        self.assertEqual(R.detect_srgb_view_mismatch(other), [])

    def test_a_declared_texture_nobody_binds_a_view_of_is_not_claimed(self):
        """A resource with no view over it has nothing to compare: the rule reads the *pair*, and half of a
        pair is not an observation."""
        path = self.rdc(self.texture(2210, self.format_id('R8G8B8A8_UNORM')))
        self.assertEqual(R.detect_srgb_view_mismatch(path), [])

    def aliased_frame(self, with_barrier: bool = False) -> str:
        """Two placed resources sharing bytes while both are live, one written while the other read.

        res101 sits at 0 (2 MB), res102 at 0x100000 (1 MB): 1 MB of overlap. 101 is written by the
        first draw, 102 by the second, and 101 is read again after that -- the write the pair
        conflicts on. The optional aliasing barrier sits between 102's write and 101's read.
        """
        chunks = [
            self.chunk('Device_CreateHeap', F.pl_create_heap(77, 4194304)),
            self.chunk('Device_CreatePlacedResource',
                       F.pl_placed_resource(101, F.pl_resource_desc(1, width=2097152), heap=77,
                                            heap_offset=0)),
            self.chunk('Device_CreatePlacedResource',
                       F.pl_placed_resource(102, F.pl_resource_desc(1, width=1048576), heap=77,
                                            heap_offset=0x100000)),
            self.chunk('List_OMSetRenderTargets', F.pl_omset(7, [101])),
            self.chunk('List_DrawInstanced',
                       F.u64b(7) + F.u32b(3) + F.u32b(1) + F.u32b(0) + F.u32b(0)),
            self.chunk('List_OMSetRenderTargets', F.pl_omset(8, [102])),
            self.chunk('List_DrawInstanced',
                       F.u64b(8) + F.u32b(3) + F.u32b(1) + F.u32b(0) + F.u32b(0)),
        ]
        if with_barrier:
            chunks.append(self.chunk('List_ResourceBarrier',
                                     F.pl_resources_barrier(7, [('aliasing', 101, 102)])))
        chunks += [
            self.chunk('List_SetGraphicsRootShaderResourceView', F.pl_root_view(7, 0, 101, 0)),
            self.chunk('List_DrawInstanced',
                       F.u64b(7) + F.u32b(3) + F.u32b(1) + F.u32b(0) + F.u32b(0)),
        ]
        return self.rdc(*chunks)

    def test_an_aliased_pair_written_while_the_other_is_read_fires(self):
        flags = R.detect_aliased_writes(self.aliased_frame())
        assert flags is not None
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['detector'], 'aliased-write')
        self.assertEqual(flags[0]['certainty'], 'question')
        self.assertIn('1 pair(s) of placed resources share bytes while both are live', flags[0]['what'])
        self.assertIn('res101/res102 in heap77: res102 written at chunk #7 (rtv) while res101 read '
                      'at chunk #9 (srv); no aliasing barrier declares the handover',
                      flags[0]['evidence'][0])

    def test_a_pair_an_aliasing_barrier_declares_is_not_a_finding(self):
        """Deliberate aliasing is `memory`'s story, not this detector's: the barrier between the write
        and the read is the frame declaring the handover, and the rule stays quiet about it."""
        self.assertEqual(R.detect_aliased_writes(self.aliased_frame(with_barrier=True)), [])

    def test_overlaps_that_never_collide_in_time_are_not_findings(self):
        """The two shapes that must stay quiet: ranges that do not overlap at all, and an overlap
        whose uses never meet -- the packing candidates `memory` prints from the other side."""
        disjoint = self.rdc(
            self.chunk('Device_CreateHeap', F.pl_create_heap(77, 4194304)),
            self.chunk('Device_CreatePlacedResource',
                       F.pl_placed_resource(101, F.pl_resource_desc(1, width=1048576), heap=77,
                                            heap_offset=0)),
            self.chunk('Device_CreatePlacedResource',
                       F.pl_placed_resource(102, F.pl_resource_desc(1, width=1048576), heap=77,
                                            heap_offset=1048576)),
            self.chunk('List_OMSetRenderTargets', F.pl_omset(7, [101])),
            self.chunk('List_DrawInstanced',
                       F.u64b(7) + F.u32b(3) + F.u32b(1) + F.u32b(0) + F.u32b(0)),
            self.chunk('List_OMSetRenderTargets', F.pl_omset(8, [102])),
            self.chunk('List_DrawInstanced',
                       F.u64b(8) + F.u32b(3) + F.u32b(1) + F.u32b(0) + F.u32b(0)))
        self.assertEqual(R.detect_aliased_writes(disjoint), [])

    def test_a_read_before_write_finding_gains_the_stream_verdict(self):
        """The verdict is an added evidence line, one per named resource: it says what the chunk
        stream's chain behind the read ends at, in the file's own numbering."""
        path = self.rdc(
            self.chunk('List_SetGraphicsRootShaderResourceView', F.pl_root_view(7, 0, 401, 0)),
            self.chunk('List_DrawInstanced',
                       F.u64b(7) + F.u32b(3) + F.u32b(1) + F.u32b(0) + F.u32b(0)),
            self.chunk('List_CopyBufferRegion', F.pl_copy_buffer(7, 401, 0, 400, 0, 128)))
        flags: list[R.RedFlag] = [{'detector': 'read-before-write',
                                   'what': '1 buffer(s) whose first use is a read that nothing writes first',
                                   'evidence': ['res401 (buffer, 0.00 MB): read at eid 3, first write eid 5',
                                                'res9999 (buffer, 0.00 MB): read at eid 3, first write never'],
                                   'certainty': 'question', 'unproven': True}]
        R.add_provenance_verdicts(path, flags)
        self.assertEqual(len(flags[0]['evidence']), 3, 'one verdict per resource the ledger knows')
        self.assertIn('provenance (chunk stream): res401 -- the chunk stream\'s chain behind that '
                      'read ends at no writer in the frame before the read (the producer is outside '
                      'this capture)', flags[0]['evidence'][2])
        self.assertNotIn('res9999', flags[0]['evidence'][2], 'an unknown resource gets no verdict')

        unchanged: list[R.RedFlag] = [{'detector': 'read-before-write', 'what': 'x',
                                       'evidence': ['res401'], 'certainty': 'question',
                                       'unproven': True}]
        R.add_provenance_verdicts(self.path('nowhere.rdc'), unchanged)
        self.assertEqual(unchanged[0]['evidence'], ['res401'], 'an unreadable capture adds nothing')
        other: list[R.RedFlag] = [{'detector': 'write-never-read', 'what': 'x', 'evidence': ['res401'],
                                   'certainty': 'question', 'unproven': True}]
        R.add_provenance_verdicts(path, other)
        self.assertEqual(other[0]['evidence'], ['res401'], 'only the read-before-write rows grow')

    def test_a_detector_that_could_not_look_says_so(self):
        """Two families of detectors can be blocked, each with its own reason: the .rdc-side rules need a
        capture path, and the two binding rules need a bundle whose driver resolved descriptor tables."""
        bundle = self.path('b')
        write_bundle(bundle, events=[event(1, targets=['11 64x64x1 R8G8B8A8_UNORM'])])
        _flags, runs = R.detect_all(R.load_bundle(bundle))
        skipped = {run['detector']: run['why'] for run in runs if not run['ran']}
        for detector in ('marker-imbalance', 'unattributed-draws', 'zero-work', 'srgb-view-mismatch',
                         'aliased-write'):
            self.assertIn('no capture path given', skipped[detector])
        for detector in ('unbound-table-slot', 'binding-kind-mismatch'):
            self.assertIn('no resolved descriptor tables', skipped[detector])

        missing = self.path('nowhere.rdc')
        _flags, runs = R.detect_all(R.load_bundle(bundle), missing)
        skipped = {run['detector']: run['why'] for run in runs if not run['ran']}
        for detector in ('marker-imbalance', 'unattributed-draws', 'zero-work', 'srgb-view-mismatch',
                         'aliased-write'):
            self.assertIn('could not be read', skipped[detector])
        for detector in ('unbound-table-slot', 'binding-kind-mismatch'):
            self.assertIn('no resolved descriptor tables', skipped[detector])

# =========================================================================== refusals

if __name__ == '__main__':
    unittest.main()
