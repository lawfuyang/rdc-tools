"""The resource table, descriptor heaps and the enum parsing.

Split out of `test_rdc_analysis.py`, which held the parsers and decoders for the whole tool and
passed 2200 lines. `rdc_testcase`. The shared fixtures and case classes live there, because every file that
exercises the tool needs them.

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_resources.py
    python -m unittest tests.test_rdc_resources
    python src/py/rdc_analysis.py selftest -k <Class>

Payload layouts used by the fixtures follow REFERENCE section 3.4; where a decoder reads a chunk
differently from another consumer of the same chunk, the test pins the behaviour actually
implemented and says so in a comment.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from typing import Dict, Optional, Sequence
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R            # noqa: E402
import rdc_fixtures as F            # noqa: E402

from rdc_testcase import *          # noqa: E402,F401,F403

# =========================================================================== resource table
class TestParseResourceTable(CmdCase):
    """`parse_resource_table`: the descriptor from the creation chunks, the names from `SetName`."""

    def table(self, *chunks: bytes) -> Dict[int, R.ResourceInfo]:
        path = self.cap(*chunks)
        _info, stream, _how = R.load_stream(path)
        return R.parse_resource_table(stream, self.names)

    def test_buffer_from_a_committed_resource(self):
        table = self.table(self.ch('Device_CreateCommittedResource',
                                   F.pl_committed_resource(271, F.pl_resource_desc(1, 2097168))))
        self.assertEqual(table[271]['kind'], 'buffer')
        self.assertEqual(table[271]['size'], 2097168)
        self.assertEqual(table[271]['gpuAddress'], 0)

    def test_texture_from_a_placed_resource(self):
        desc = F.pl_resource_desc(3, 256, 64, 6, mips=8, fmt=10, alignment=4096)
        table = self.table(self.ch('Device_CreatePlacedResource',
                                   F.pl_placed_resource(2233, desc, heap=298, heap_offset=0x1000)))
        self.assertEqual(table[2233]['kind'], 'texture2d')
        self.assertEqual((table[2233]['width'], table[2233]['height']), (256, 64))
        self.assertEqual(table[2233]['depth'], 6)          # depth doubles as array size
        self.assertEqual(table[2233]['mips'], 8)
        self.assertEqual(table[2233]['format'], 10)
        self.assertEqual(table[2233]['size'], 0)           # textures have no byte size

    def test_reserved_resource(self):
        table = self.table(self.ch('Device_CreateReservedResource',
                                   F.pl_reserved_resource(9, F.pl_resource_desc(4, 64, 64, 64))))
        self.assertEqual(table[9]['kind'], 'texture3d')

    def test_the_id_is_the_second_to_last_field(self):
        # committed(117), placed(109) and reserved(93) payloads all end with id | gpuAddress, so the
        # id is always at len-16 whatever the optional clear value does to the rest of the tail
        # (the real captures carry 117/145 and 109/118/137)
        desc = F.pl_resource_desc(1, 16)
        cases = [('Device_CreateCommittedResource', F.pl_committed_resource(4242, desc), 117, 24),
                 ('Device_CreatePlacedResource', F.pl_placed_resource(4242, desc), 109, 16),
                 ('Device_CreateReservedResource', F.pl_reserved_resource(4242, desc), 93, 0)]
        for name, blob, length, desc_off in cases:
            with self.subTest(chunk=name):
                self.assertEqual(len(blob), length)
                self.assertEqual(R.RESOURCE_CHUNKS[name], desc_off)
                self.assertIsNotNone(R._parse_resource(blob, desc_off))
                self.assertEqual(self.table(self.ch(name, blob))[4242]['size'], 16)

    def test_gpu_address_is_the_last_field(self):
        table = self.table(self.ch('Device_CreateCommittedResource',
                                   F.pl_committed_resource(7, F.pl_resource_desc(1, 64),
                                                           gpu_address=0x7FF6A000)))
        self.assertEqual(table[7]['gpuAddress'], 0x7FF6A000)

    def test_a_name_after_the_creation(self):
        chunks = [self.ch('Device_CreateCommittedResource',
                          F.pl_committed_resource(271, F.pl_resource_desc(1, 4096))),
                  self.ch('SetName', F.pl_set_name(271, 'SkyAtmosphere.SkyViewLut'))]
        self.assertEqual(self.table(*chunks)[271]['name'], 'SkyAtmosphere.SkyViewLut')

    def test_a_name_before_the_creation(self):
        # the table is order-independent: names are merged in after the whole walk
        chunks = [self.ch('SetName', F.pl_set_name(271, 'SceneUniformBuffer')),
                  self.ch('Device_CreateCommittedResource',
                          F.pl_committed_resource(271, F.pl_resource_desc(1, 4096)))]
        table = self.table(*chunks)
        self.assertEqual(table[271]['name'], 'SceneUniformBuffer')
        self.assertEqual(table[271]['kind'], 'buffer')

    def test_an_id_that_is_only_named(self):
        # heaps, queues and fences have no descriptor: kind 'unknown', no size
        table = self.table(self.ch('SetName', F.pl_set_name(298, 'GlobalResourceHeap')))
        self.assertEqual(table[298]['kind'], 'unknown')
        self.assertEqual(table[298]['name'], 'GlobalResourceHeap')
        self.assertEqual(table[298]['size'], 0)

    def test_a_name_with_a_length_past_the_payload_is_ignored(self):
        blob = F.u64b(5) + F.u32b(999) + b'short'
        table = self.table(self.ch('SetName', blob))
        self.assertEqual(table, {})

    def test_a_name_payload_that_is_too_short_is_ignored(self):
        self.assertEqual(self.table(self.ch('SetName', F.u64b(5) + b'\x01\x00')), {})

    def test_an_invalid_dimension_is_not_a_resource(self):
        # dimension 0 (unknown) and 9 are not D3D10_RESOURCE_DIMENSION values: skip, do not guess
        for dim in (0, 9):
            with self.subTest(dim=dim):
                table = self.table(self.ch('Device_CreateCommittedResource',
                                           F.pl_committed_resource(3, F.pl_resource_desc(dim, 64))))
                self.assertEqual(table, {})

    def test_a_payload_too_short_for_a_descriptor_is_skipped(self):
        blob = F.pl_committed_resource(3, F.pl_resource_desc(1, 64))[:90]
        self.assertEqual(self.table(self.ch('Device_CreateCommittedResource', blob)), {})

    def test_an_empty_stream_is_an_empty_table(self):
        self.assertEqual(self.table(), {})

    def test_the_newer_creation_variants_use_the_same_offsets(self):
        # a hobby-renderer capture creates 5252 resources with `...CommittedResource3` (149-byte
        # payloads, `D3D12_RESOURCE_DESC1`) and 148 with `...PlacedResource2`: the extra fields come
        # *after* the descriptor and its first 48 bytes are the same struct, so one offset each
        desc3 = F.pl_committed_resource3(4001, F.pl_resource_desc(3, 1920, 1080, fmt=45))
        desc2 = F.pl_placed_resource(4002, F.pl_resource_desc(1, 512), heap=2)
        table = self.table(self.ch('Device_CreateCommittedResource3', desc3),
                           self.ch('Device_CreatePlacedResource2', desc2))
        self.assertEqual(len(desc3), 149)
        self.assertEqual(table[4001]['kind'], 'texture2d')
        self.assertEqual((table[4001]['width'], table[4001]['height']), (1920, 1080))
        self.assertEqual(table[4001]['format'], 45)
        self.assertEqual(table[4002]['size'], 512)

    def test_an_acceleration_structure_is_recorded_by_its_own_id(self):
        # `CreateAS` names a sub-range of a buffer: the id is the AS id (what the frame references),
        # the size is the AS's own byte size, and the type is TOP_LEVEL = 0 / BOTTOM_LEVEL = 1
        table = self.table(self.ch('CreateAS', F.pl_create_as(47055, buffer=11157, as_type=0,
                                                              byte_size=904832)),
                           self.ch('CreateAS', F.pl_create_as(393, buffer=390, as_type=1,
                                                              byte_size=62336)))
        self.assertEqual(table[47055]['kind'], 'tlas')
        self.assertEqual(table[47055]['size'], 904832)
        self.assertEqual(table[393]['kind'], 'blas')
        self.assertEqual(table[393]['size'], 62336)
        self.assertNotIn(11157, table)          # the buffer is not created by this chunk

    def test_an_acceleration_structure_keeps_its_name(self):
        table = self.table(self.ch('CreateAS', F.pl_create_as(7, buffer=1, as_type=1)),
                           self.ch('SetName', F.pl_set_name(7, 'UnitSphereBLAS')))
        self.assertEqual(table[7]['name'], 'UnitSphereBLAS')
        self.assertEqual(table[7]['kind'], 'blas')

    def test_an_acceleration_structure_with_an_unknown_type_is_skipped(self):
        self.assertEqual(self.table(self.ch('CreateAS', F.pl_create_as(7, buffer=1, as_type=2))), {})

    def test_a_short_acceleration_structure_payload_is_skipped(self):
        self.assertEqual(self.table(self.ch('CreateAS', F.pl_create_as(7, buffer=1)[:35])), {})

class TestParseDescriptorHeaps(CmdCase):
    """`parse_descriptor_heaps`: the writes and copies that fill a descriptor heap, in stream order."""

    def heaps(self, *chunks: bytes) -> Dict[int, Dict[int, R.DescriptorInfo]]:
        path = self.cap(*chunks)
        _info, stream, _how = R.load_stream(path)
        return R.parse_descriptor_heaps(stream, self.names)

    def test_a_view_write_records_its_slot_and_resource(self):
        heaps = self.heaps(self.ch('Device_CreateShaderResourceView',
                                   F.pl_descriptor_write(2233, heap=298, index=138456)))
        self.assertEqual(heaps, {298: {138456: {'kind': 'srv', 'resource': 2233}}})

    def test_the_kind_comes_from_the_chunk_name(self):
        cases = [('Device_CreateConstantBufferView', 'cbv'), ('Device_CreateShaderResourceView', 'srv'),
                 ('Device_CreateUnorderedAccessView', 'uav'), ('Device_CreateRenderTargetView', 'rtv'),
                 ('Device_CreateDepthStencilView', 'dsv')]
        for name, kind in cases:
            with self.subTest(chunk=name):
                heaps = self.heaps(self.ch(name, F.pl_descriptor_write(7, heap=1, index=2)))
                self.assertEqual(heaps[1][2], {'kind': kind, 'resource': 7})

    def test_a_sampler_records_no_resource(self):
        heaps = self.heaps(self.ch('Device_CreateSampler', F.pl_descriptor_write(0, heap=299, index=0)))
        self.assertEqual(heaps, {299: {0: {'kind': 'sampler', 'resource': 0}}})

    def test_a_copy_moves_the_descriptor_into_the_destination_slot(self):
        # the real captures do exactly this: write into one heap, copy into the heap the frame binds
        chunks = [self.ch('Device_CreateUnorderedAccessView',
                          F.pl_descriptor_write(2266, heap=300, index=1047)),
                  self.ch('Device_CopyDescriptorsSimple',
                          F.pl_copy_descriptors([(298, 138455, 300, 1047)]))]
        heaps = self.heaps(*chunks)
        self.assertEqual(heaps[300][1047], {'kind': 'uav', 'resource': 2266})
        self.assertEqual(heaps[298][138455], {'kind': 'uav', 'resource': 2266})

    def test_a_copy_of_an_unwritten_slot_records_nothing(self):
        heaps = self.heaps(self.ch('Device_CopyDescriptors',
                                   F.pl_copy_descriptors([(298, 5, 300, 9)])))
        self.assertEqual(heaps, {})

    def test_stream_order_decides_which_write_wins(self):
        # a write after the copy must survive it: that is the order D3D12 applies them in
        copy = self.ch('Device_CopyDescriptors', F.pl_copy_descriptors([(298, 5, 300, 9)]))
        chunks = [self.ch('Device_CreateShaderResourceView',
                          F.pl_descriptor_write(1, heap=300, index=9)),
                  copy,
                  self.ch('Device_CreateUnorderedAccessView',
                          F.pl_descriptor_write(2, heap=298, index=5))]
        heaps = self.heaps(*chunks)
        self.assertEqual(heaps[298][5], {'kind': 'uav', 'resource': 2})

    def test_a_copy_after_a_write_wins(self):
        chunks = [self.ch('Device_CreateShaderResourceView',
                          F.pl_descriptor_write(1, heap=298, index=5)),
                  self.ch('Device_CreateShaderResourceView',
                          F.pl_descriptor_write(2, heap=300, index=9)),
                  self.ch('Device_CopyDescriptors', F.pl_copy_descriptors([(298, 5, 300, 9)]))]
        self.assertEqual(self.heaps(*chunks)[298][5], {'kind': 'srv', 'resource': 2})

    def test_a_write_payload_too_short_for_the_handle_is_skipped(self):
        self.assertEqual(self.heaps(self.ch('Device_CreateShaderResourceView', b'\x00' * 20)), {})

    def test_a_truncated_copy_entry_stops_the_list(self):
        # the count claims two entries but only one fits: the second must not be invented
        blob = F.pl_copy_descriptors([(298, 5, 300, 9)]) + b'\x00' * 10
        heaps = self.heaps(self.ch('Device_CreateShaderResourceView',
                                   F.pl_descriptor_write(1, heap=300, index=9)),
                           self.ch('Device_CopyDescriptors', blob))
        self.assertEqual(heaps[298][5], {'kind': 'srv', 'resource': 1})
        self.assertEqual(len(heaps[298]), 1)

    def test_an_empty_stream_has_no_heaps(self):
        self.assertEqual(self.heaps(), {})

class TestParseRootSignature(unittest.TestCase):
    """`_parse_root_signature`: the `RTS0` layout (`DecodeRootSig`, d3d12_rootsig.cpp).

    Header(24) | param array (12 bytes each) | out-of-line data per parameter, all offsets from the
    start of the part. Nothing here names anything: the decode says what each `rpN` *is*.
    """

    def parse(self, sig: bytes) -> Optional[R.RootSignature]:
        return R._parse_root_signature(sig)

    def param(self, sig: Optional[R.RootSignature], i: int) -> R.RootParam:
        assert sig is not None
        return sig['params'][i]

    def test_a_root_descriptor(self):
        sig = self.parse(F.root_signature([('cbv', 5, 1, 2, 0, [])]))
        assert sig is not None
        self.assertEqual((sig['version'], sig['dwords'], sig['flags'], sig['samplers']),
                         ('1.1', 2, 0, 0))                 # a root descriptor costs 2 dwords
        self.assertEqual(self.param(sig, 0), {'kind': 'cbv', 'visibility': 'ps', 'register': 1,
                                             'space': 2, 'count': 0, 'ranges': []})

    def test_root_constants(self):
        sig = self.parse(F.root_signature([('32bit', 0, 3, 1, 4, [])]))
        self.assertEqual(self.param(sig, 0)['count'], 4)
        self.assertEqual(self.param(sig, 0)['register'], 3)
        assert sig is not None
        self.assertEqual(sig['dwords'], 4)                 # root constants cost their count

    def test_a_descriptor_table_and_its_ranges(self):
        sig = self.parse(F.root_signature([('table', 0, 0, 0, 0, [('srv', 0, 18, 0, 2),
                                                                 ('uav', 0, 7, 0, 2)])]))
        self.assertEqual(self.param(sig, 0)['ranges'],
                         [{'kind': 'srv', 'base': 0, 'count': 18, 'space': 0, 'offset': 2},
                          {'kind': 'uav', 'base': 0, 'count': 7, 'space': 0, 'offset': 2}])
        assert sig is not None
        self.assertEqual(sig['dwords'], 1)                 # a table costs 1 dword

    def test_a_version_1_0_signature_has_20_byte_ranges(self):
        # the one place the version matters: a 1.1 range carries a flags word, so walking a 1.0
        # signature with a 24-byte stride would read the second range out of the first one's tail
        sig = self.parse(F.root_signature([('table', 0, 0, 0, 0, [('srv', 4, 2, 1, 0),
                                                                 ('cbv', 8, 1, 3, 9)])], version=1))
        assert sig is not None
        self.assertEqual(sig['version'], '1.0')
        self.assertEqual([(r['kind'], r['base'], r['count'], r['space'], r['offset'])
                          for r in self.param(sig, 0)['ranges']],
                         [('srv', 4, 2, 1, 0), ('cbv', 8, 1, 3, 9)])

    def test_a_1_2_signature_and_static_samplers(self):
        sig = self.parse(F.root_signature([('cbv', 0, 0, 0, 0, [])], version=3, flags=0x332,
                                         samplers=6))
        assert sig is not None
        self.assertEqual(sig['version'], '1.2')
        self.assertEqual(sig['flags'], 0x332)
        self.assertEqual(sig['samplers'], 6)

    def test_an_unbounded_range_count_survives(self):
        sig = self.parse(F.root_signature([('table', 0, 0, 0, 0, [('srv', 0, 0xffffffff, 0, 0)])]))
        self.assertEqual(self.param(sig, 0)['ranges'][0]['count'], 0xffffffff)

    def test_several_parameters_keep_their_own_data(self):
        sig = self.parse(F.root_signature([('table', 0, 0, 0, 0, [('uav', 0, 16, 0, 0)]),
                                           ('cbv', 0, 0, 0, 0, []),
                                           ('32bit', 0, 2, 0, 4, [])]))
        assert sig is not None
        self.assertEqual([p['kind'] for p in sig['params']], ['table', 'cbv', '32bit'])
        self.assertEqual(self.param(sig, 2)['count'], 4)
        self.assertEqual(sig['dwords'], 1 + 2 + 4)

    def test_an_unknown_visibility_reads_as_all(self):
        sig = self.parse(F.root_signature([('cbv', 9, 0, 0, 0, [])]))
        self.assertEqual(self.param(sig, 0)['visibility'], 'all')

    def test_bad_input_is_none_not_garbage(self):
        cases = {
            'empty': b'',
            'header only': F.root_signature([])[:20],
            'unknown version': F.root_signature([], version=9),
            'param array past the end': (F.u32b(2) + F.u32b(4) + F.u32b(24) + F.u32b(0) + F.u32b(0)
                                        + F.u32b(0) + b'\x00' * 12),
            'param data past the end': (F.u32b(2) + F.u32b(1) + F.u32b(24) + F.u32b(0) + F.u32b(0)
                                        + F.u32b(0) + F.u32b(2) + F.u32b(0) + F.u32b(0x1000)),
            'unknown param kind': F.root_signature([('cbv', 0, 0, 0, 0, [])])[:24]
                                  + F.u32b(9) + F.u32b(0) + F.u32b(36),
            'ranges past the end': (F.u32b(2) + F.u32b(1) + F.u32b(24) + F.u32b(0) + F.u32b(0)
                                    + F.u32b(0) + F.u32b(0) + F.u32b(0) + F.u32b(36) + F.u32b(4)
                                    + F.u32b(0x1000)),
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                self.assertIsNone(self.parse(data))

    def test_an_unknown_range_kind_is_rejected(self):
        sig = F.root_signature([('table', 0, 0, 0, 0, [('srv', 0, 1, 0, 0)])])
        bad = sig[:-24] + F.u32b(9) + sig[-20:]          # the first range's type word
        self.assertIsNone(self.parse(bad))

class RootSigCase(CmdCase):
    """A capture with root signature chunks, plus the parse of its `Device_CreateRootSignature`s."""

    def sigs(self, *chunks: bytes) -> Dict[int, R.RootSignature]:
        path = self.cap(*chunks)
        _info, stream, _how = R.load_stream(path)
        return R.parse_root_signatures(stream, self.names)

    def sig(self, resid: int, params: Sequence[F.RootParamSpec], **kw: object) -> bytes:
        """A `Device_CreateRootSignature` chunk for `resid`."""
        return self.ch('Device_CreateRootSignature',
                       F.pl_create_root_sig(resid, F.root_signature(params, **kw)))  # type: ignore[arg-type]

class TestParseRootSignatures(RootSigCase):
    """`parse_root_signatures`: the blob is found by its magic and keyed by the id at `length - 8`."""

    def test_one_signature_keyed_by_its_id(self):
        sigs = self.sigs(self.sig(11365, [('cbv', 0, 0, 0, 0, [])]))
        self.assertEqual(list(sigs), [11365])
        self.assertEqual(sigs[11365]['params'][0]['kind'], 'cbv')

    def test_the_container_is_found_by_magic_not_by_offset(self):
        # the fields around the blob are the serialiser's own framing, and the gap is not fixed
        for gap in (0, 4, 16, 40):
            with self.subTest(gap=gap):
                payload = F.pl_create_root_sig(7, F.root_signature([('srv', 0, 3, 0, 0, [])]),
                                               gap=gap)
                sigs = self.sigs(self.ch('Device_CreateRootSignature', payload))
                self.assertEqual(sigs[7]['params'][0]['kind'], 'srv')

    def test_the_length_field_is_cross_checked(self):
        # the tool trusts the container's own size field, and requires the u64 at +4 to agree
        payload = bytearray(F.pl_create_root_sig(7, F.root_signature([('cbv', 0, 0, 0, 0, [])])))
        payload[4] = (payload[4] + 1) % 256
        self.assertEqual(self.sigs(self.ch('Device_CreateRootSignature', bytes(payload))), {})

    def test_a_container_that_is_not_a_root_signature_is_skipped(self):
        not_rts0 = F.dxbc([('RDAT', b'\x00' * 16)])
        two_parts = F.dxbc([('RTS0', F.root_signature([('cbv', 0, 0, 0, 0, [])])), ('STAT', b'x')])
        sigs = self.sigs(self.ch('Device_CreateRootSignature',
                                 F.u32b(0) + F.u64b(len(not_rts0)) + not_rts0 + F.u64b(7)),
                         self.ch('Device_CreateRootSignature',
                                 F.u32b(0) + F.u64b(len(two_parts)) + two_parts + F.u64b(8)))
        self.assertEqual(sigs, {})

    def test_a_corrupt_blob_is_skipped_rather_than_guessed_at(self):
        sigs = self.sigs(self.ch('Device_CreateRootSignature',
                                 F.pl_create_root_sig(7, b'\x02\x00' * 2)))
        self.assertEqual(sigs, {})

    def test_several_signatures_are_all_kept(self):
        sigs = self.sigs(self.sig(417, [('cbv', 0, 0, 1, 0, [])]),
                         self.sig(429, [('table', 0, 0, 0, 0, [('sampler', 0, 2, 1, 0)])]))
        self.assertEqual(sorted(sigs), [417, 429])
        self.assertEqual(sigs[429]['params'][0]['ranges'][0]['kind'], 'sampler')

    def test_no_root_signature_chunks_is_an_empty_table(self):
        self.assertEqual(self.sigs(self.ch('PushMarker', b'x\x00')), {})

    def test_an_unrelated_chunk_with_a_dxbc_blob_is_ignored(self):
        sigs = self.sigs(self.ch('Device_CreatePipelineState',
                                 F.dxbc([('RTS0', F.root_signature([('cbv', 0, 0, 0, 0, [])]))])))
        self.assertEqual(sigs, {})

class TestParseRdef(unittest.TestCase):
    """`parse_rdef`: the reflection's bindings, the one place a root parameter name can come from."""

    def test_names_kinds_registers_and_spaces(self):
        binds = R.parse_rdef(F.rdef([('SceneCB', 'cbv', 0, 1), ('Textures', 'srv', 2, 0),
                                     ('Output', 'uav', 3, 4), ('LinearClamp', 'sampler', 5, 6)]))
        self.assertEqual([(b['name'], b['kind'], b['register'], b['space']) for b in binds],
                         [('SceneCB', 'cbv', 0, 1), ('Textures', 'srv', 2, 0),
                          ('Output', 'uav', 3, 4), ('LinearClamp', 'sampler', 5, 6)])
        self.assertEqual(binds[0]['count'], 1)

    def test_a_tbuffer_reads_as_an_srv_and_every_uav_flavour_as_a_uav(self):
        self.assertEqual([b['kind'] for b in R.parse_rdef(F.rdef([('t', 'srv', 0, 0),
                                                                 ('u', 'uav', 1, 0)]))],
                         ['srv', 'uav'])

    def test_a_5_0_rdef_has_no_space_or_id_field(self):
        binds = R.parse_rdef(F.rdef([('SceneCB', 'cbv', 1, 7)], target_version=0x500))
        self.assertEqual((binds[0]['register'], binds[0]['space']), (1, 0))

    def test_bad_input_is_empty_not_garbage(self):
        self.assertEqual(R.parse_rdef(b''), [])
        self.assertEqual(R.parse_rdef(F.rdef([('x', 'cbv', 0, 0)])[:16]), [])
        huge = bytearray(F.rdef([('x', 'cbv', 0, 0)]))
        huge[8:12] = F.u32b(99999)
        self.assertEqual(R.parse_rdef(bytes(huge)), [])

    def test_a_name_offset_past_the_end_is_skipped(self):
        data = bytearray(F.rdef([('x', 'cbv', 0, 0)]))
        data[32:36] = F.u32b(0xFFFF)                      # the first entry's nameOffset
        self.assertEqual(R.parse_rdef(bytes(data)), [])

class TestShaderBindNames(RootSigCase):
    """`shader_bind_names`: `RDEF` parts of the shaders a capture still carries, by stage."""

    def stream_of(self, *chunks: bytes) -> bytes:
        path = self.cap(*chunks)
        _info, stream, _how = R.load_stream(path)
        return stream

    def test_bindings_are_keyed_by_stage_and_slot(self):
        stream = self.stream_of(self.ch('Device_CreatePipelineState',
                                        F.dxbc([('RDEF', F.rdef([('SceneCB', 'cbv', 0, 1)]))])))
        self.assertEqual(R.shader_bind_names(stream), {'cs': {('cbv', 0, 1): 'SceneCB'}})

    def test_a_capture_without_rdef_has_no_names(self):
        stream = self.stream_of(self.ch('Device_CreatePipelineState', F.dxbc([('STAT', b'x')])))
        self.assertEqual(R.shader_bind_names(stream), {})

class TestRootParamLabel(unittest.TestCase):
    """`_root_param_label`: what `draws` prints for a root parameter index."""

    def sig(self, *params: F.RootParamSpec) -> R.RootSignature:
        sig = R._parse_root_signature(F.root_signature(list(params)))
        assert sig is not None
        return sig

    def test_without_a_signature_the_index_stands_alone(self):
        self.assertEqual(R._root_param_label(None, {}, 7), 'rp7')

    def test_an_index_the_signature_does_not_have_stands_alone(self):
        self.assertEqual(R._root_param_label(self.sig(('cbv', 0, 0, 0, 0, [])), {}, 9), 'rp9')

    def test_a_root_descriptor(self):
        sig = self.sig(('cbv', 0, 1, 2, 0, []))
        self.assertEqual(R._root_param_label(sig, {}, 0), 'rp0(cbv b1 s2)')

    def test_root_constants(self):
        self.assertEqual(R._root_param_label(self.sig(('32bit', 0, 0, 0, 4, [])), {}, 0),
                         'rp0(32bit b0 s0 n4)')

    def test_a_table_lists_its_ranges(self):
        sig = self.sig(('table', 0, 0, 0, 0, [('srv', 0, 18, 0, 2), ('uav', 0, 7, 0, 2)]))
        self.assertEqual(R._root_param_label(sig, {}, 0), 'rp0(table t0 n18 s0, u0 n7 s0)')

    def test_an_unbounded_range_says_so(self):
        sig = self.sig(('table', 0, 0, 0, 0, [('srv', 0, 0xffffffff, 0, 0)]))
        self.assertEqual(R._root_param_label(sig, {}, 0), 'rp0(table t0 nunbounded s0)')

    def test_a_restricted_visibility_is_shown(self):
        self.assertEqual(R._root_param_label(self.sig(('cbv', 1, 0, 0, 0, [])), {}, 0),
                         'rp0(vs cbv b0 s0)')

    def test_a_name_from_the_reflection_is_appended(self):
        sig = self.sig(('cbv', 0, 1, 2, 0, []))
        binds = {'all': {('cbv', 1, 2): 'SceneCB'}}
        self.assertEqual(R._root_param_label(sig, binds, 0), 'rp0(cbv b1 s2) [SceneCB]')

    def test_a_stage_specific_name_is_used_for_a_restricted_parameter(self):
        sig = self.sig(('cbv', 5, 0, 0, 0, []))            # pixel-shader visible
        binds = {'ps': {('cbv', 0, 0): 'PixelCB'}, 'vs': {('cbv', 0, 0): 'VertexCB'}}
        self.assertEqual(R._root_param_label(sig, binds, 0), 'rp0(ps cbv b0 s0) [PixelCB]')

    def test_stages_that_disagree_leave_the_parameter_unnamed(self):
        sig = self.sig(('cbv', 0, 0, 0, 0, []))
        binds = {'ps': {('cbv', 0, 0): 'PixelCB'}, 'vs': {('cbv', 0, 0): 'VertexCB'}}
        self.assertEqual(R._root_param_label(sig, binds, 0), 'rp0(cbv b0 s0)')

    def test_stages_that_agree_name_it_once(self):
        sig = self.sig(('cbv', 0, 0, 0, 0, []))
        binds = {'ps': {('cbv', 0, 0): 'SceneCB'}, 'vs': {('cbv', 0, 0): 'SceneCB'}}
        self.assertEqual(R._root_param_label(sig, binds, 0), 'rp0(cbv b0 s0) [SceneCB]')

    def test_a_table_is_never_named(self):
        # a table holds several ranges, so one name would be a lie
        sig = self.sig(('table', 0, 0, 0, 0, [('srv', 0, 1, 0, 0)]))
        binds = {'all': {('srv', 0, 0): 'Textures'}}
        self.assertEqual(R._root_param_label(sig, binds, 0), 'rp0(table t0 n1 s0)')

class TestLoadFormatNames(TempDirCase):
    def test_parses_the_dxgi_format_enum(self):
        _root, _names = F.make_fake_src(self.tmp)
        formats = R.load_format_names(self.tmp)
        self.assertEqual(formats[28], 'R8G8B8A8_UNORM')       # the prefix is stripped
        self.assertEqual(formats[0], 'UNKNOWN')
        self.assertEqual(formats[90], 'BC4_UNORM')

    def test_missing_source_returns_no_names(self):
        self.assertEqual(R.load_format_names(os.path.join(self.tmp, 'nope')), {})

    def test_a_plain_enum_is_parsed_like_an_enum_class(self):
        # `parse_chunk_enum` handles both spellings (the chunk enums are enum class, DXGI_FORMAT
        # in RenderDoc is a plain enum)
        text = 'enum DXGI_FORMAT\n{\n  DXGI_FORMAT_UNKNOWN = 0,\n  DXGI_FORMAT_BC7_UNORM = 98,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'DXGI_FORMAT'),
                         {0: 'DXGI_FORMAT_UNKNOWN', 98: 'DXGI_FORMAT_BC7_UNORM'})
        self.assertEqual(R.parse_chunk_enum(text, 'NoSuchEnum'), {})

# =========================================================================== misc helpers
class TestAlignUp(unittest.TestCase):
    def test_default_alignment_is_64(self):
        self.assertEqual(R.CHUNK_ALIGN, 64)
        for value, expected in ((0, 0), (1, 64), (63, 64), (64, 64), (65, 128), (127, 128),
                                (128, 128), (129, 192), (1000, 1024)):
            with self.subTest(value=value):
                self.assertEqual(R.align_up(value), expected)

    def test_custom_alignment(self):
        self.assertEqual(R.align_up(1, 8), 8)
        self.assertEqual(R.align_up(8, 8), 8)
        self.assertEqual(R.align_up(9, 8), 16)
        self.assertEqual(R.align_up(3, 1), 3)

class TestChunkFlagConstants(unittest.TestCase):
    def test_flag_values_match_the_serialiser(self):
        self.assertEqual(R.CHUNK_CALLSTACK, 0x00010000)
        self.assertEqual(R.CHUNK_THREADID, 0x00020000)
        self.assertEqual(R.CHUNK_DURATION, 0x00040000)
        self.assertEqual(R.CHUNK_TIMESTAMP, 0x00080000)
        self.assertEqual(R.CHUNK_64BITSIZE, 0x00100000)
        self.assertEqual(R.CHUNK_ALIGN, 64)
        self.assertEqual(R.ZSTD_MAGIC, b'\x28\xb5\x2f\xfd')

    def test_chunk_name_sets(self):
        self.assertIn('PushMarker', R.MARKER_CHUNKS)
        self.assertIn('Queue_BeginEvent', R.MARKER_CHUNKS)
        self.assertIn('List_DrawIndexedInstanced', R.DRAW_CHUNKS)
        self.assertIn('List_Dispatch', R.DRAW_CHUNKS)

# =========================================================================== enum parsing
ENUM_TEXT = '''
enum class SystemChunk : uint32_t
{
  // 0 is reserved as a 'null' chunk that is only for debug
  DriverInit = 1,
  InitialContentsList,
  InitialContents,
  CaptureBegin,
  CaptureScope,
  CaptureEnd,

  FirstDriverChunk = 1000,
};
'''

class TestParseChunkEnum(unittest.TestCase):
    def test_explicit_implicit_and_sentinel(self):
        self.assertEqual(R.parse_chunk_enum(ENUM_TEXT, 'SystemChunk'),
                         {1: 'DriverInit', 2: 'InitialContentsList', 3: 'InitialContents',
                          4: 'CaptureBegin', 5: 'CaptureScope', 6: 'CaptureEnd',
                          1000: 'FirstDriverChunk'})

    def test_comments_and_blank_lines_are_skipped(self):
        self.assertNotIn('comment', R.parse_chunk_enum(ENUM_TEXT, 'SystemChunk').values())

    def test_commented_out_entry_is_ignored(self):
        text = 'enum class E : uint32_t\n{\n  // Hidden = 5,\n  Shown = 7,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {7: 'Shown'})

    def test_firstdriverchunk_expression_sets_1000(self):
        text = 'enum class E : uint32_t\n{\n  SetName = (uint32_t)SystemChunk::FirstDriverChunk,\n  Next,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {1000: 'SetName', 1001: 'Next'})

    def test_unknown_expression_increments(self):
        text = 'enum class E : uint32_t\n{\n  A = SOME_OTHER_ENUM,\n  B,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {1: 'A', 2: 'B'})

    def test_line_without_trailing_comma_is_skipped(self):
        text = 'enum class E : uint32_t\n{\n  A = 3,\n  B\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {3: 'A'})

    def test_non_matching_lines_are_skipped(self):
        text = 'enum class E : uint32_t\n{\n  int Fake = 1;\n  A = 2,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {2: 'A'})

    def test_missing_enum_returns_empty(self):
        self.assertEqual(R.parse_chunk_enum(ENUM_TEXT, 'NoSuchEnum'), {})
        self.assertEqual(R.parse_chunk_enum('', 'SystemChunk'), {})

    def test_a_plain_enum_is_parsed_too(self):
        # `DXGI_FORMAT` is a plain `enum` in RenderDoc (common/dds_readwrite.cpp), so the pattern
        # accepts both spellings -- with or without an explicit underlying type
        text = 'enum SystemChunk : uint32_t\n{\n  A = 1,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'SystemChunk'), {1: 'A'})
        text = 'enum DXGI_FORMAT\n{\n  DXGI_FORMAT_UNKNOWN = 0,\n  DXGI_FORMAT_BC7_UNORM = 98,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'DXGI_FORMAT'),
                         {0: 'DXGI_FORMAT_UNKNOWN', 98: 'DXGI_FORMAT_BC7_UNORM'})

    def test_only_the_named_enum_is_parsed(self):
        text = ('enum class A : uint32_t\n{\n  X = 1,\n};\n'
                'enum class B : uint32_t\n{\n  Y = 2,\n};\n')
        self.assertEqual(R.parse_chunk_enum(text, 'B'), {2: 'Y'})

class TestLoadChunkNames(TempDirCase):
    #: root of the fake `renderdoc-src` tree and the {id: name} map parsed from it.
    src: str
    names: Dict[int, str]

    def setUp(self) -> None:
        super().setUp()
        self.src, self.names = F.make_fake_src(os.path.join(self.tmp, 'renderdoc-src'))

    def test_system_and_driver_enums_are_merged(self):
        self.assertEqual(self.names[1], 'DriverInit')
        self.assertEqual(self.names[3], 'InitialContents')
        # the driver enum starts at FirstDriverChunk and overwrites the sentinel entry
        self.assertEqual(self.names[1000], 'SetName')
        self.assertEqual(self.names[1001], 'PushMarker')
        self.assertEqual(self.names[1002], 'SetMarker')
        self.assertEqual(self.names[1003], 'PopMarker')

    def test_explicit_values_resume_counting(self):
        self.assertEqual(self.names[1200], 'List_SetPipelineState')
        self.assertEqual(self.names[1201], 'List_SetGraphicsRootSignature')
        self.assertEqual(self.names[1212], 'List_DrawIndexedInstanced')
        self.assertEqual(self.names[1214], 'List_ExecuteIndirect')

    def test_driver_parameter_switches_the_header(self):
        names = R.load_chunk_names(self.src, 'D3D11')
        self.assertEqual(names[1], 'DriverInit')
        self.assertEqual(names[1001], 'PushMarker')
        self.assertEqual(names[1002], 'DrawIndexed')

    def test_missing_source_warns_once_on_stderr(self):
        with mock.patch.object(chunkmap, '_SRC_WARNED', False):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                names = R.load_chunk_names(os.path.join(self.tmp, 'does-not-exist'))
                self.assertEqual(names, {})
                first = err.getvalue()
            err2 = io.StringIO()
            with contextlib.redirect_stderr(err2):
                R.load_chunk_names(os.path.join(self.tmp, 'does-not-exist'))
            self.assertIn('warning: RenderDoc source not found', first)
            self.assertIn('renderdoc-src', first)
            self.assertEqual(err2.getvalue(), '')

    def test_missing_driver_header_still_returns_system_chunks(self):
        os.remove(os.path.join(self.src, 'renderdoc', 'driver', 'd3d12', 'd3d12_common.h'))
        with mock.patch.object(chunkmap, '_SRC_WARNED', False):
            names = R.load_chunk_names(self.src)
        self.assertEqual(names[1], 'DriverInit')
        self.assertEqual(names[1000], 'FirstDriverChunk')
        self.assertNotIn('PushMarker', names.values())

    def test_missing_core_header_still_returns_driver_chunks(self):
        os.remove(os.path.join(self.src, 'renderdoc', 'core', 'core.h'))
        with mock.patch.object(chunkmap, '_SRC_WARNED', False):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                names = R.load_chunk_names(self.src)
        self.assertIn('warning', err.getvalue())
        self.assertEqual(names[1001], 'PushMarker')
        self.assertNotIn('DriverInit', names.values())

class TestFindRenderdocSrc(unittest.TestCase):
    #: The tool folder (src/py), the folder above it, and the repository root -- the search walks up from the
    #: tool, because the tool used to sit at the root and now sits two levels below it.
    here: str
    tool_candidate: str
    parent_candidate: str
    root_candidate: str

    def setUp(self) -> None:
        self.here = os.path.dirname(os.path.abspath(R.__file__))
        self.tool_candidate = os.path.join(self.here, 'renderdoc-src')
        self.parent_candidate = os.path.join(os.path.dirname(self.here), 'renderdoc-src')
        self.root_candidate = os.path.join(os.path.dirname(os.path.dirname(self.here)), 'renderdoc-src')

    def core_of(self, root: str) -> str:
        return os.path.join(root, 'renderdoc', 'core', 'core.h')

    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {'RENDERDOC_SRC': r'D:\src\renderdoc'}):
            with mock.patch.object(os.path, 'isfile', lambda p: p == self.core_of(r'D:\src\renderdoc')):
                self.assertEqual(R._find_renderdoc_src(), r'D:\src\renderdoc')

    def test_falls_through_to_tool_folder(self):
        with mock.patch.dict(os.environ, {'RENDERDOC_SRC': r'D:\nope'}):
            with mock.patch.object(os.path, 'isfile', lambda p: p == self.core_of(self.tool_candidate)):
                self.assertEqual(R._find_renderdoc_src(), self.tool_candidate)

    def test_falls_through_to_parent_folder(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(os.path, 'isfile', lambda p: p == self.core_of(self.parent_candidate)):
                self.assertEqual(R._find_renderdoc_src(), self.parent_candidate)

    def test_documented_fallback_when_nothing_exists(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(os.path, 'isfile', lambda p: False):
                self.assertEqual(R._find_renderdoc_src(), self.root_candidate)

    def test_empty_env_var_is_ignored(self):
        with mock.patch.dict(os.environ, {'RENDERDOC_SRC': ''}, clear=True):
            with mock.patch.object(os.path, 'isfile', lambda p: False):
                self.assertEqual(R._find_renderdoc_src(), self.root_candidate)

if __name__ == '__main__':
    unittest.main(verbosity=2)
