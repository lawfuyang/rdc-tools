"""Tests for the offline use ledger and its two commands: `deps` and `memory` (rdc_uses.py).

Everything here is built from the fixture builders in `rdc_fixtures`, which follow the payload layouts
the decoders were written against -- so a change to a layout breaks these tests rather than passing
quietly. The chunk-name map is the fake `renderdoc-src` tree, so nothing needs a capture or a GPU.

Run directly, via unittest, or through the tool:

    python tests/test_rdc_uses.py
    python rdc_analysis.py selftest -k Uses
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from typing import Any, Callable, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402
import rdc_fixtures as F          # noqa: E402
from rdc_testcase import CmdCase as _CmdCase, capture_text   # noqa: E402


class UsesCase(_CmdCase):
    """`CmdCase` (fake source tree, capture builder) plus the stream- and output-level helpers."""

    def out(self, fn: Callable[..., object], *args: Any, **kwargs: Any) -> str:
        return capture_text(fn, *args, **kwargs)

    def line_with(self, text: str, needle: str) -> str:
        for line in text.splitlines():
            if needle in line:
                return line
        raise AssertionError('no line containing %r in:\n%s' % (needle, text))

    def ledger(self, *chunks: bytes, **kw: Any) -> R.UseLedger:
        """The use ledger of a hand-built stream -- the section body on its own, no container."""
        return R.walk_uses(F.stream(*chunks), **kw)

    def uses_of(self, ledger: R.UseLedger, rid: int) -> Sequence[Any]:
        """`[(access, how)]` for one resource, so a test states the classification and nothing else."""
        return [(u['access'], u['how']) for u in ledger['resources'][rid]['uses']]

    def used(self, ledger: R.UseLedger) -> Sequence[int]:
        """The ids the walk recorded a use for -- a creation alone is a record with no uses."""
        return [rid for rid, record in ledger['resources'].items() if record['uses']]

    def buffer(self, rid: int, size: int = 4096, gpu_address: int = 0) -> bytes:
        """A committed buffer resource creation chunk."""
        return self.ch('Device_CreateCommittedResource',
                       F.pl_committed_resource(rid, F.pl_resource_desc(1, width=size),
                                               gpu_address=gpu_address))


# =========================================================================== payload walks
class TestBarrierPayloads(UsesCase):
    def test_a_resource_barrier_walks_its_three_arms(self):
        blob = F.pl_resources_barrier(7, [('transition', 100, 0, 64, 8), ('aliasing', 1, 2),
                                          ('uav', 3)])
        self.assertEqual(R.parse_barriers(blob), [
            {'kind': 'transition', 'resource': 100, 'resource2': 0, 'before': 64, 'after': 8,
             'subresource': 0},
            {'kind': 'aliasing', 'resource': 1, 'resource2': 2, 'before': 0, 'after': 0,
             'subresource': 0},
            {'kind': 'uav', 'resource': 3, 'resource2': 0, 'before': 0, 'after': 0,
             'subresource': 0}])

    def test_a_barrier_payload_that_does_not_land_at_its_end_is_refused(self):
        # the walk is exact by construction, so anything that does not land is not a barrier payload
        blob = F.pl_resources_barrier(7, [('transition', 100, 0, 64, 8)])
        self.assertIsNone(R.parse_barriers(blob + b'\x00' * 4))
        self.assertIsNone(R.parse_barriers(blob[:24]))
        self.assertIsNone(R.parse_barriers(blob[:19]))
        self.assertIsNone(R.parse_barriers(b'\x00' * 32))

    def test_the_count_is_written_twice_and_both_must_agree(self):
        blob = F.pl_resources_barrier(7, [('uav', 3)])
        self.assertIsNone(R.parse_barriers(blob[:8] + F.u32b(2) + blob[12:]))

    def test_barrier_groups_walk_the_three_kinds(self):
        blob = F.pl_barrier_groups(7, [('texture', [(100, 0x80, 6, 0)]),
                                       ('buffer', [(200, 0x400)]),
                                       ('global', [(0,)])])
        self.assertEqual(R.parse_barrier_groups(blob), [
            {'kind': 'texture', 'resource': 100, 'sync': 0, 'access': 0x80, 'layout': 6, 'flags': 0},
            {'kind': 'buffer', 'resource': 200, 'sync': 0, 'access': 0x400, 'layout': 0, 'flags': 0},
            {'kind': 'global', 'resource': 0, 'sync': 0, 'access': 0, 'layout': 0, 'flags': 0}])

    def test_a_group_whose_count_disagrees_or_that_runs_off_the_end_is_refused(self):
        blob = F.pl_barrier_groups(7, [('buffer', [(200, 0x400)])])
        # the array count sits after the group's own NumBarriers (at +8): making them disagree refuses
        self.assertIsNone(R.parse_barrier_groups(blob[:28] + F.u64b(9) + blob[36:]))
        self.assertIsNone(R.parse_barrier_groups(blob[:-8]))
        self.assertIsNone(R.parse_barrier_groups(b'\x00' * 24))

    def test_decode_chunk_names_the_states_and_the_access_words(self):
        out = R.decode_chunk('List_ResourceBarrier',
                             F.pl_resources_barrier(7, [('transition', 100, 0xffffffff, 0x40, 0x4)]))
        self.assertEqual(out, ['cmdList=7 barriers=1',
                               '    transition res100 sub=all NonPixelShaderResource -> RenderTarget'])
        out = R.decode_chunk('List_Barrier',
                             F.pl_barrier_groups(7, [('texture', [(100, 0x80, 6, 1)])]))
        self.assertEqual(out, ['cmdList=7 barriers=1',
                               '    texture res100 ShaderResource layout=ShaderResource discard'])

    def test_a_state_word_prints_its_names_and_keeps_an_unknown_bit(self):
        self.assertEqual(R.state_text(0), 'Common')
        self.assertEqual(R.state_text(0x4 | 0x80), 'RenderTarget|PixelShaderResource')
        self.assertEqual(R.state_text(0x40000000), '0x40000000')
        self.assertEqual(R.access_text(0x4), 'IndexBuffer')     # 0x4 is INDEX_BUFFER, not the CBV at 0x2


class TestTargetAndCopyPayloads(UsesCase):
    def test_omset_targets_and_its_depth_view(self):
        self.assertEqual(R.parse_targets(F.pl_omset(7, [100, 101], dsv=200)), ([100, 101], 200))

    def test_no_depth_target_is_zero(self):
        self.assertEqual(R.parse_targets(F.pl_omset(7, [100])), ([100], 0))
        self.assertEqual(R.parse_targets(F.pl_omset(7, [])), ([], 0))

    def test_the_walk_depends_on_each_entrys_view_dimension(self):
        # a BUFFER RTV's arm is 12 bytes where a TEXTURE2D one's is 8: one entry size cannot read both
        blob = (F.u64b(7) + F.u32b(2) + F.u64b(2) + F.pl_rtv_descriptor(1, dimension=1)
                + F.pl_rtv_descriptor(2, dimension=4) + bytes([0]))
        self.assertEqual(R.parse_targets(blob), ([1, 2], 0))

    def test_an_unknown_view_dimension_refuses_the_whole_walk(self):
        desc = (F.u32b(0x1003) + F.u64b(1) + F.u32b(0) + F.u64b(1) + F.u32b(0) + F.u32b(99))
        blob = F.u64b(7) + F.u32b(1) + F.u64b(1) + desc + bytes([0])
        self.assertIsNone(R.parse_targets(blob))

    def test_a_clear_names_its_resource_at_a_fixed_offset(self):
        self.assertEqual(R.clear_target(F.pl_clear(7, 100)), 100)
        self.assertEqual(R.clear_target(F.pl_clear(7, 200, kind='dsv')), 200)
        self.assertEqual(R.clear_target(F.pl_clear(7, 300, kind='uav')), 300)

    def test_a_payload_that_is_not_a_clear_names_no_resource(self):
        self.assertEqual(R.clear_target(b'\x00' * 40), 0)
        self.assertEqual(R.clear_target(F.pl_discard(7, 100)), 0)

    def test_a_discard_names_its_resource(self):
        self.assertEqual(R.discard_target(F.pl_discard(7, 100)), 100)
        self.assertEqual(R.discard_target(b'\x00' * 12), 0)

    def test_a_copy_pair_is_destination_then_source(self):
        self.assertEqual(R.copy_pair('List_CopyBufferRegion', F.pl_copy_buffer(7, 1, 0, 2, 16, 64)),
                         (1, 2))
        self.assertEqual(R.copy_pair('List_CopyTextureRegion', F.pl_copy_texture(7, 3, 4)), (3, 4))

    def test_a_copy_payload_that_does_not_fit_is_refused(self):
        self.assertIsNone(R.copy_pair('List_CopyBufferRegion', b'\x00' * 47))
        self.assertIsNone(R.copy_pair('List_CopyTextureRegion', F.pl_copy_texture(7, 3, 4)[:50]))
        self.assertIsNone(R.copy_pair('List_CopyTextureRegion',
                                      F.u64b(7) + F.u64b(1) + F.u32b(7) + b'\x00' * 40))

    def test_a_resolve_names_both_subresources(self):
        """The plain form: 36 bytes, the format last, and the two subresources are what the MSAA question
        needs -- *which* slice of the multisampled texture went where."""
        self.assertEqual(R.parse_resolve('List_ResolveSubresource',
                                         F.pl_resolve(7, 400, 401, dst_sub=1, src_sub=3, fmt=28)),
                         {'destination': 400, 'destinationSubresource': 1, 'source': 401,
                          'sourceSubresource': 3, 'format': 28, 'region': False})

    def test_a_resolve_region_walks_its_optional_rect(self):
        """The `Region` form puts the source subresource after a destination offset and the format after an
        optional rect, so reading it as the plain form would name the wrong subresource: both lengths are
        walked, and the flag is read rather than assumed."""
        for rect in (False, True):
            self.assertEqual(R.parse_resolve('List_ResolveSubresourceRegion',
                                             F.pl_resolve_region(7, 400, 401, src_sub=2, fmt=28,
                                                                 rect=rect)),
                             {'destination': 400, 'destinationSubresource': 0, 'source': 401,
                              'sourceSubresource': 2, 'format': 28, 'region': True})

    def test_a_resolve_that_does_not_fit_is_refused(self):
        """A payload of the wrong length names no resource at all: half a resolve read as a whole one would
        attribute a use to a resource the payload does not name."""
        self.assertIsNone(R.parse_resolve('List_ResolveSubresource', b'\x00' * 35))
        self.assertIsNone(R.parse_resolve('List_ResolveSubresource', b'\x00' * 37))
        self.assertIsNone(R.parse_resolve('List_ResolveSubresourceRegion',
                                          F.pl_resolve_region(7, 1, 2)[:48]))
        flag_without_rect = F.pl_resolve_region(7, 1, 2)[:40] + bytes([1]) + b'\x00' * 8
        self.assertIsNone(R.parse_resolve('List_ResolveSubresourceRegion', flag_without_rect))


# =========================================================================== the ledger
class TestWalkUses(UsesCase):
    def test_a_draws_bindings_are_classified_by_what_they_bind(self):
        ledger = self.ledger(
            self.buffer(100),
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 0, 100, 0)),
            self.ch('List_SetGraphicsRootUnorderedAccessView', F.pl_root_view(7, 1, 101, 0)),
            self.ch('List_OMSetRenderTargets', F.pl_omset(7, [300], dsv=301)),
            self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [(102, 0, 64, 12)])),
            self.ch('List_IASetIndexBuffer', F.pl_index_buffer(7, 103, 0, 64, 57)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        self.assertEqual(self.uses_of(ledger, 100), [('read', 'cbv')])
        self.assertEqual(self.uses_of(ledger, 101), [('read+write', 'uav')])
        self.assertEqual(self.uses_of(ledger, 102), [('read', 'vb')])
        self.assertEqual(self.uses_of(ledger, 103), [('read', 'ib')])
        self.assertEqual(self.uses_of(ledger, 300), [('write', 'rtv')])
        self.assertEqual(self.uses_of(ledger, 301), [('write', 'dsv')])
        self.assertEqual(ledger['events'], 1)

    def test_the_use_is_attributed_to_the_draw_not_to_the_binding(self):
        # OMSetRenderTargets and the root setter bind; the draw that uses them is where the use is
        ledger = self.ledger(
            self.ch('List_OMSetRenderTargets', F.pl_omset(7, [300])),
            self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [(102, 0, 64, 12)])),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        self.assertEqual(ledger['resources'][300]['uses'][0]['eid'], 3)
        self.assertEqual(ledger['resources'][300]['uses'][0]['call'], 'List_DrawIndexedInstanced')

    def test_a_dispatch_uses_the_compute_namespace_only(self):
        ledger = self.ledger(
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 0, 100, 0)),
            self.ch('List_SetComputeRootConstantBufferView', F.pl_root_view(7, 0, 200, 0)),
            self.ch('List_Dispatch', F.pl_dispatch(7, 8, 8, 1)))
        self.assertEqual(self.uses_of(ledger, 200), [('read', 'cbv')])
        self.assertNotIn(100, ledger['resources'])

    def test_an_execute_indirect_contributes_both_namespaces(self):
        # the stream does not say whether it is a draw or a dispatch, and `draws` prints both
        ledger = self.ledger(
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 0, 100, 0)),
            self.ch('List_SetComputeRootConstantBufferView', F.pl_root_view(7, 0, 200, 0)),
            self.ch('List_ExecuteIndirect', F.pl_dispatch(7, 1, 1, 1)))
        self.assertEqual(self.uses_of(ledger, 100), [('read', 'cbv')])
        self.assertEqual(self.uses_of(ledger, 200), [('read', 'cbv')])

    def test_a_table_binding_resolves_through_the_heap(self):
        ledger = self.ledger(
            self.ch('Device_CreateShaderResourceView', F.pl_descriptor_write(200, heap=298, index=5)),
            self.ch('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 0, 298, 5)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        self.assertEqual(self.uses_of(ledger, 200), [('read', 'srv')])
        self.assertEqual(ledger['unresolved'], 0)

    def test_a_table_slot_the_capture_never_wrote_is_counted_not_guessed(self):
        ledger = self.ledger(
            self.ch('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 0, 298, 5)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        self.assertEqual(ledger['unresolved'], 1)
        self.assertEqual(self.used(ledger), [])

    def test_a_cbv_table_slot_is_resolved_through_its_gpu_address(self):
        # a CBV descriptor carries a VA, not a resource id (D3D12Descriptor::Init stores none), so the
        # only way to name the resource is the resource table's own gpuAddress
        ledger = self.ledger(
            self.buffer(400, size=4096, gpu_address=0x10000000),
            self.ch('Device_CreateConstantBufferView',
                    F.pl_descriptor_write(0x10000040, heap=298, index=5)),
            self.ch('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 0, 298, 5)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        self.assertEqual(self.uses_of(ledger, 400), [('read', 'cbv')])

    def test_a_cbv_slot_pointing_outside_every_buffer_names_nothing(self):
        ledger = self.ledger(
            self.buffer(400, size=4096, gpu_address=0x10000000),
            self.ch('Device_CreateConstantBufferView',
                    F.pl_descriptor_write(0x20000000, heap=298, index=5)),
            self.ch('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 0, 298, 5)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        self.assertEqual(self.used(ledger), [])             # the slot resolves to no resource at all
        self.assertEqual(ledger['resources'][400]['uses'], [])   # the buffer is created, never used

    def test_a_clear_discard_and_copy_are_uses(self):
        ledger = self.ledger(
            self.ch('List_ClearRenderTargetView', F.pl_clear(7, 300)),
            self.ch('List_DiscardResource', F.pl_discard(7, 301)),
            self.ch('List_CopyBufferRegion', F.pl_copy_buffer(7, 400, 0, 401, 0, 144)))
        self.assertEqual(self.uses_of(ledger, 300), [('write', 'clear')])
        self.assertEqual(self.uses_of(ledger, 301), [('discard', 'discard')])
        self.assertEqual(self.uses_of(ledger, 400), [('write', 'copy-dst')])
        self.assertEqual(self.uses_of(ledger, 401), [('read', 'copy-src')])

    def test_a_resolve_is_a_read_and_a_write(self):
        """Both sides are uses, and each row names the *other* subresource -- which is the question a
        resolve raises. Before this decode the chunk was counted as unattributed, so a resource only ever
        resolved into read as unused in `deps`."""
        ledger = self.ledger(self.ch('List_ResolveSubresource', F.pl_resolve(7, 400, 401, src_sub=3)))
        self.assertEqual(self.uses_of(ledger, 400), [('write', 'resolve-dst')])
        self.assertEqual(self.uses_of(ledger, 401), [('read', 'resolve-src')])
        self.assertIn('subresource 3', ledger['resources'][400]['uses'][0]['detail'])
        self.assertIn('to res400 subresource 0', ledger['resources'][401]['uses'][0]['detail'])
        # and the chunk is no longer one of the kinds the walk only counts rather than attributes
        self.assertNotIn('List_ResolveSubresource', R.UNATTRIBUTED_CHUNKS)
        self.assertNotIn('List_ResolveSubresourceRegion', R.UNATTRIBUTED_CHUNKS)

    def test_a_transition_is_classified_by_the_state_it_enters(self):
        ledger = self.ledger(
            self.ch('List_ResourceBarrier', F.pl_resources_barrier(7, [
                ('transition', 100, 0, 0x40, 0x4),          # -> RenderTarget: a write
                ('transition', 101, 0, 0x4, 0x80),          # -> PixelShaderResource: a read
                ('transition', 102, 0, 0x80, 0x8),          # -> UnorderedAccess: both
                ('transition', 103, 0, 0x400, 0),           # -> Common: no use at all
                ('uav', 104)])))
        self.assertEqual(self.uses_of(ledger, 100), [('write', 'barrier')])
        self.assertEqual(self.uses_of(ledger, 101), [('read', 'barrier')])
        self.assertEqual(self.uses_of(ledger, 102), [('read+write', 'barrier')])
        self.assertNotIn(103, ledger['resources'])
        self.assertNotIn(104, ledger['resources'])
        self.assertEqual(ledger['resources'][100]['uses'][0]['detail'],
                         'NonPixelShaderResource -> RenderTarget')

    def test_a_modern_texture_barrier_names_an_access_and_can_discard(self):
        ledger = self.ledger(
            self.ch('List_Barrier', F.pl_barrier_groups(7, [
                ('texture', [(100, 0x80, 6, 1)]),           # ShaderResource, with DISCARD
                ('buffer', [(200, 0x10)]),                  # UnorderedAccess: both
                ('global', [(0,)])])))
        self.assertEqual(self.uses_of(ledger, 100), [('read', 'barrier'), ('discard', 'discard')])
        self.assertEqual(self.uses_of(ledger, 200), [('read+write', 'barrier')])

    def test_a_barrier_with_no_access_bits_falls_back_to_its_layout(self):
        ledger = self.ledger(self.ch('List_Barrier', F.pl_barrier_groups(7, [
            ('texture', [(100, 0, 2, 0)])])))               # layout RenderTarget, no access bits
        self.assertEqual(self.uses_of(ledger, 100), [('write', 'barrier')])

    def test_aliasing_barriers_are_recorded_in_order_and_are_not_uses(self):
        ledger = self.ledger(self.ch('List_ResourceBarrier', F.pl_resources_barrier(7, [
            ('aliasing', 100, 200), ('aliasing', 200, 300)])))
        self.assertEqual(ledger['aliases'], [(1, 100, 200), (1, 200, 300)])
        self.assertEqual(self.used(ledger), [])

    def test_placement_comes_from_the_creation_chunk(self):
        ledger = self.ledger(
            self.buffer(100),
            self.ch('Device_CreatePlacedResource',
                    F.pl_placed_resource(101, F.pl_resource_desc(1, width=4096), heap=77,
                                         heap_offset=4096)),
            self.ch('Device_CreateHeap', F.pl_create_heap(77, 1048576)),
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 0, 500, 0)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        self.assertEqual(ledger['resources'][100]['placement'], 'committed')
        self.assertEqual(ledger['resources'][101]['placement'], 'placed')
        self.assertEqual((ledger['resources'][101]['heap'], ledger['resources'][101]['offset']),
                         (77, 4096))
        self.assertEqual(ledger['resources'][500]['placement'], 'external')
        self.assertEqual(ledger['resources'][500]['created'], 0)
        self.assertEqual(ledger['heaps'], {77: 1048576})

    def test_a_creation_is_found_even_when_the_use_comes_first(self):
        ledger = self.ledger(
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 0, 100, 0)),
            self.buffer(100))
        self.assertEqual(ledger['resources'][100]['created'], 2)
        self.assertEqual(ledger['resources'][100]['placement'], 'committed')

    def test_an_unreadable_barrier_payload_is_counted_and_contributes_nothing(self):
        ledger = self.ledger(self.ch('List_ResourceBarrier', b'\x00' * 24))
        self.assertEqual(ledger['failed'], {'List_ResourceBarrier': 1})
        self.assertEqual(self.used(ledger), [])

    def test_an_unreadable_copy_payload_is_counted(self):
        ledger = self.ledger(self.ch('List_CopyTextureRegion', b'\x00' * 20))
        self.assertEqual(ledger['failed'], {'List_CopyTextureRegion': 1})

    def test_the_chunk_histogram_is_recorded(self):
        ledger = self.ledger(self.buffer(100), self.ch('List_ResolveQueryData', b'\x00' * 20))
        self.assertEqual(ledger['seen']['Device_CreateCommittedResource'], 1)
        self.assertEqual(ledger['seen']['List_ResolveQueryData'], 1)

    def test_a_reset_clears_the_targets_and_the_root_bindings(self):
        ledger = self.ledger(
            self.ch('List_OMSetRenderTargets', F.pl_omset(7, [300])),
            self.ch('List_SetGraphicsRootShaderResourceView', F.pl_root_view(7, 0, 100, 0)),
            self.ch('List_Reset', F.pl_reset(7)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        self.assertEqual(self.used(ledger), [])

    def test_a_changed_root_signature_clears_the_root_srv_and_uav(self):
        ledger = self.ledger(
            self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 5)),
            self.ch('List_SetGraphicsRootShaderResourceView', F.pl_root_view(7, 0, 100, 0)),
            self.ch('List_SetGraphicsRootUnorderedAccessView', F.pl_root_view(7, 1, 101, 0)),
            self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 6)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        self.assertEqual(self.used(ledger), [])

    def test_draws_prints_the_targets_and_the_root_views(self):
        out = self.out(R.cmd_draws, self.cap(
            self.ch('List_SetGraphicsRootShaderResourceView', F.pl_root_view(7, 0, 100, 0x20)),
            self.ch('List_SetGraphicsRootUnorderedAccessView', F.pl_root_view(7, 1, 101, 0)),
            self.ch('List_OMSetRenderTargets', F.pl_omset(7, [300, 301], dsv=302)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))))
        self.assertIn('SRV: rp0=res100+0x20', out)
        self.assertIn('UAV: rp1=res101+0x0', out)
        self.assertIn('RTV: res300  res301', out)
        self.assertIn('DSV: res302', out)


# =========================================================================== the findings
class TestFindings(UsesCase):
    def tally_of(self, *uses: Any) -> R.UseTally:
        """A tally built from `(eid, access)` pairs, for the flag rules on their own."""
        record = R.ResourceUse(created=1, placement='external', heap=0, offset=0,
                               uses=[R.UseInfo(eid=e, access=a, how='x', call='c', detail='')
                                     for e, a in uses])
        return R.tally(9, record)

    def test_read_before_write_needs_a_strictly_earlier_write(self):
        self.assertIsNone(R.read_before_write(self.tally_of((1, 'write'), (5, 'read'))))
        self.assertIsNotNone(R.read_before_write(self.tally_of((5, 'read'), (9, 'write'))))
        self.assertIsNone(R.read_before_write(self.tally_of((1, 'read'), (1, 'write'))))

    def test_read_before_write_reports_the_first_read_and_write(self):
        one = R.read_before_write(self.tally_of((5, 'read'), (9, 'write')))
        self.assertIsNotNone(one)
        assert one is not None            # for the type checker: the case above pins it
        self.assertEqual((one[0]['eid'], one[1]), (5, 9))

    def test_read_before_write_is_silent_without_a_read(self):
        self.assertIsNone(R.read_before_write(self.tally_of((1, 'write'))))
        self.assertIsNone(R.read_before_write(self.tally_of()))

    def test_write_never_read_needs_no_later_read(self):
        self.assertIsNone(R.write_never_read(self.tally_of((1, 'write'), (5, 'read'))))
        dead = R.write_never_read(self.tally_of((5, 'read'), (9, 'write')))
        self.assertIsNotNone(dead)
        assert dead is not None
        self.assertEqual(dead[0]['eid'], 9)
        self.assertIsNone(R.write_never_read(self.tally_of((1, 'read'))))

    def test_write_never_read_returns_the_discards_that_explain_it(self):
        dead = R.write_never_read(self.tally_of((1, 'write'), (7, 'discard')))
        self.assertIsNotNone(dead)
        assert dead is not None
        self.assertEqual([d['eid'] for d in dead[1]], [7])
        # a discard before the write explains nothing and is not returned
        dead = R.write_never_read(self.tally_of((1, 'discard'), (7, 'write')))
        assert dead is not None
        self.assertEqual(dead[1], [])

    def test_a_uav_counts_as_both_a_read_and_a_write(self):
        one = self.tally_of((1, 'read+write'))
        self.assertEqual(one['reads'][0]['access'], 'read+write')
        self.assertEqual(one['writes'][0]['access'], 'read+write')
        # the read is at the *same* event as the write, so it does not count as consuming it: only a
        # strictly later read does (the bundle detector's rule -- a UAV may read before it writes)
        self.assertEqual(R.dep_flags(one), ['write-never-read'])
        self.assertEqual(R.dep_flags(self.tally_of((1, 'read+write'), (5, 'read'))), [])

    def test_the_discarded_case_is_named_in_its_own_words(self):
        self.assertEqual(R.dep_flags(self.tally_of((1, 'write'))), ['write-never-read'])
        self.assertEqual(R.dep_flags(self.tally_of((1, 'write'), (2, 'discard'))), ['discarded'])

    def test_resource_bytes_are_the_size_of_a_buffer_and_an_estimate_for_a_texture(self):
        self.assertEqual(R.resource_bytes({'kind': 'buffer', 'name': '', 'size': 1024, 'width': 1024,
                                           'height': 1, 'depth': 1, 'mips': 1, 'format': 0,
                                           'gpuAddress': 0}), (1024, False))
        self.assertEqual(R.resource_bytes({'kind': 'texture2d', 'name': '', 'size': 0, 'width': 8,
                                           'height': 4, 'depth': 1, 'mips': 1, 'format': 28,
                                           'gpuAddress': 0}), (128, True))
        self.assertEqual(R.resource_bytes(None), (0, False))
        self.assertEqual(R.resource_bytes({'kind': 'unknown', 'name': 'heap298', 'size': 0,
                                           'width': 0, 'height': 0, 'depth': 0, 'mips': 0,
                                           'format': 0, 'gpuAddress': 0}), (0, False))

    def test_the_peak_of_a_set_of_windows_is_the_widest_moment(self):
        self.assertEqual(R._peak_live([(1, 10, 100), (5, 10, 50)], 50), (150, 5))   # both live at #5
        self.assertEqual(R._peak_live([(1, 10, 100), (30, 40, 200)], 50), (200, 30))
        # a window with no use counts to the end of the capture rather than not at all
        self.assertEqual(R._peak_live([(0, 0, 100), (1, 5, 200)], 50)[0], 300)


# =========================================================================== deps
class TestCmdDeps(UsesCase):
    def frame(self) -> Sequence[bytes]:
        return [
            self.buffer(100, size=1024),
            self.buffer(200, size=2048),
            self.ch('List_OMSetRenderTargets', F.pl_omset(7, [300])),
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 0, 100, 0)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)),
            self.ch('List_ResourceBarrier',
                    F.pl_resources_barrier(7, [('transition', 300, 0, 0x4, 0x80)])),
            self.ch('List_SetComputeRootConstantBufferView', F.pl_root_view(8, 0, 200, 0)),
            self.ch('List_Dispatch', F.pl_dispatch(8, 1, 1, 1)),
        ]

    def test_the_table_gives_writes_reads_and_the_span(self):
        out = self.out(R.cmd_deps, self.cap(*self.frame()), 0)
        self.assertIn('resources with a use the stream shows: 3', out)
        self.assertIn('2 draws/dispatches', out)
        row = self.line_with(out, 'res200')
        self.assertIn('#8', row)

    def test_a_resource_only_read_is_flagged_and_its_evidence_printed(self):
        out = self.out(R.cmd_deps, self.cap(*self.frame()), 0)
        # both constant buffers are read and never written: nothing in this frame produces them
        self.assertIn('read-before-write: 2 resource(s)', out)
        self.assertIn('res200: read #8 (cbv), no write in this capture', out)

    def test_a_transition_to_a_read_state_reads_the_resource(self):
        # the frame's own barrier is the evidence: after it, the target is in PixelShaderResource
        out = self.out(R.cmd_deps, self.cap(*self.frame()), 0)
        row = self.line_with(out, 'res300')
        self.assertNotIn('write-never-read', row)

    def test_a_target_nothing_ever_reads_is_flagged(self):
        out = self.out(R.cmd_deps, self.cap(
            self.ch('List_OMSetRenderTargets', F.pl_omset(7, [300])),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))), 0)
        row = self.line_with(out, 'res300')
        self.assertIn('write-never-read', row)
        self.assertIn('res300: last write #2 (rtv)', out)

    def test_the_limit_truncates_the_table_and_zero_shows_all(self):
        limited = self.out(R.cmd_deps, self.cap(*self.frame()), 1)
        self.assertIn('... 2 more resource(s) with uses (maxResources=0 shows all)', limited)
        self.assertNotIn('... 2 more', self.out(R.cmd_deps, self.cap(*self.frame()), 0))

    def test_the_graph_forms_name_the_events_and_the_resources(self):
        dot = self.out(R.cmd_deps, self.cap(*self.frame()), 5, 'dot')
        self.assertIn('digraph deps {', dot)
        # `?` for a resource the capture never described: the target here has no creation chunk
        self.assertIn('"res300" [label="res300\\n?"];', dot)
        self.assertIn('"w5" -> "res300" [label="rtv"];', dot)
        self.assertIn('"res300" -> "r6" [label="barrier"];', dot)
        mermaid = self.out(R.cmd_deps, self.cap(*self.frame()), 5, 'mermaid')
        self.assertIn('flowchart LR', mermaid)
        self.assertIn('res300["res300"]', mermaid)
        self.assertIn('w5["#5 List_DrawIndexedInstanced"]', mermaid)

    def test_a_resource_that_is_only_read_is_not_drawn(self):
        dot = self.out(R.cmd_deps, self.cap(*self.frame()), 5, 'dot')
        self.assertNotIn('"res200"', dot)

    def test_the_unattributed_chunks_are_named(self):
        out = self.out(R.cmd_deps, self.cap(*self.frame(),
                                            self.ch('List_ResolveQueryData', b'\x00' * 20)), 0)
        self.assertIn('not decoded as uses: List_ResolveQueryData x1', out)

    def test_an_unresolved_table_binding_is_counted(self):
        out = self.out(R.cmd_deps, self.cap(
            self.ch('List_SetComputeRootDescriptorTable', F.pl_root_table(8, 0, 298, 5)),
            self.ch('List_Dispatch', F.pl_dispatch(8, 1, 1, 1))), 0)
        self.assertIn('unresolved: 1 descriptor-table binding(s)', out)

    def test_an_unreadable_payload_is_reported_rather_than_guessed(self):
        out = self.out(R.cmd_deps, self.cap(self.ch('List_ResourceBarrier', b'\x00' * 24)), 0)
        self.assertIn('unreadable: List_ResourceBarrier x1', out)

    def test_the_format_word_is_checked_by_the_dispatch(self):
        import unittest.mock as mock
        path = self.cap(*self.frame())
        with mock.patch.object(sys, 'argv', ['rdc_analysis.py', 'deps', path, '5', 'bogus']):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as caught:
                    R.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('usage: rdc_analysis.py deps', buf.getvalue())


# =========================================================================== memory
class TestCmdMemory(UsesCase):
    def frame(self) -> Sequence[bytes]:
        return [
            self.buffer(100, size=1048576),                                    # 1 MB committed
            self.ch('Device_CreatePlacedResource',
                    F.pl_placed_resource(101, F.pl_resource_desc(1, width=2097152), heap=77,
                                         heap_offset=0)),
            self.ch('Device_CreatePlacedResource',
                    F.pl_placed_resource(102, F.pl_resource_desc(1, width=1048576), heap=77,
                                         heap_offset=2097152)),
            self.ch('Device_CreateHeap', F.pl_create_heap(77, 4194304)),
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 0, 101, 0)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)),
            # the second call is on *another* command list, so the first list's CBV does not carry
            # into it: 101 and 102 get windows that do not overlap, which is what the test is about
            self.ch('List_ResourceBarrier',
                    F.pl_resources_barrier(8, [('transition', 102, 0, 0x40, 0x4)])),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(8, 3, 1, 0, 0, 0)),
        ]

    def test_placement_and_size_come_from_the_creations(self):
        out = self.out(R.cmd_memory, self.cap(*self.frame()), 5)
        self.assertIn('memory: 3 resource(s) referenced, 3 created by this capture', out)
        self.assertIn('committed     1  1.0 MB', out)
        self.assertIn('placed        2  3.0 MB', out)
        self.assertIn('heap77', out)

    def test_a_resource_the_capture_never_touches_is_listed_as_unused(self):
        chunks = list(self.frame()) + [self.buffer(103, size=1048576)]
        out = self.out(R.cmd_memory, self.cap(*chunks), 5)
        # two: the extra buffer, and the committed one the frame never binds either
        self.assertIn('2 resource(s), 2.0 MB: created here and touched by no decoded chunk', out)
        self.assertIn('res103: created #9, committed', out)

    def test_a_write_nothing_reads_is_listed_with_its_last_write(self):
        out = self.out(R.cmd_memory, self.cap(*self.frame()), 5)
        self.assertIn('1 resource(s), 1.0 MB: written with nothing afterwards reading them', out)
        self.assertIn('res102: last write #7 (barrier)', out)

    def test_a_heap_reports_its_total_its_peak_and_what_sharing_could_save(self):
        out = self.out(R.cmd_memory, self.cap(*self.frame()), 5)
        # 101 is live #6..#6 (2 MB) and 102 #7..#7 (1 MB): they never overlap, so 1 MB could be shared
        line = self.line_with(out, 'heap77')
        self.assertIn('2 resource(s), 3.0 MB in it, 2.0 MB live at once (peak at #6)', line)
        self.assertIn('up to 1.0 MB of it could be shared', line)
        self.assertIn('res101 #6..#6 and res102 #7..#7: 1.0 MB', out)

    def test_an_aliasing_barrier_is_reported_in_order(self):
        chunks = list(self.frame()) + [
            self.ch('List_ResourceBarrier', F.pl_resources_barrier(7, [('aliasing', 101, 102)]))]
        out = self.out(R.cmd_memory, self.cap(*chunks), 5)
        self.assertIn('1 aliasing barrier(s) hand one piece of memory over, in order:', out)
        self.assertIn('#9 res101 -> res102', out)

    def test_the_caveats_are_printed_with_the_numbers(self):
        out = self.out(R.cmd_memory, self.cap(*self.frame()), 5)
        self.assertIn('destruction: D3D12 writes no release to the stream', out)
        self.assertIn("a texture's bytes are not recorded", out)
        self.assertIn('a buffer sub-allocated out of a UE page is named after the page', out)

    def test_a_heaps_one_resource_is_summarised_rather_than_listed(self):
        chunks = list(self.frame()) + [
            self.ch('Device_CreatePlacedResource',
                    F.pl_placed_resource(104, F.pl_resource_desc(1, width=512), heap=88, heap_offset=0)),
            self.ch('Device_CreateHeap', F.pl_create_heap(88, 1048576))]
        out = self.out(R.cmd_memory, self.cap(*chunks), 5)
        self.assertIn('1 heap(s) hold one resource each', out)
        self.assertNotIn('heap88:', out)


if __name__ == '__main__':
    unittest.main()
