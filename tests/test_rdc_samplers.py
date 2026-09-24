"""The offline `samplers` command: the values behind `rootsig`'s count, and the mip arithmetic.

Everything here is built from `rdc_fixtures`, so nothing needs a capture or a GPU: a signature carries its
static samplers as a real `D3D12_STATIC_SAMPLER_DESC` array, a heap sampler arrives as a
`Device_CreateSampler` payload, and the arithmetic is asked of `sampler_verdict` directly as well as through
a run -- the command's two halves, so a failure says which one broke.
"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'py'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import unittest

import rdc_fixtures as F                                                           # noqa: E402
import rdc_samplers as S                                                          # noqa: E402
from rdc_testcase import CmdCase, capture_text                                    # noqa: E402

FLT_MAX = 3.4028234663852886e38


def out(func: Any, *args: Any) -> str:
    """Everything one command printed, the helper `test_rdc_uses` uses for the same job."""
    return capture_text(func, *args)


def sampler(min_lod: float = 0.0, max_lod: float = FLT_MAX, filter_: str = 'linear/linear/linear',
            **kw: object) -> dict:
    """A sampler record as the decode produces one, for the arithmetic to be asked about."""
    row: dict = {'filterText': filter_, 'minLod': min_lod, 'maxLod': max_lod, 'mipLodBias': 0.0,
                 'addressU': 'clamp', 'addressV': 'clamp', 'addressW': 'clamp', 'comparison': 'none',
                 'maxAnisotropy': 1}
    row.update(kw)
    return row


class TestSamplerVerdict(unittest.TestCase):
    """The four pieces of arithmetic, each on its own, plus the two cases that must stay silent."""

    def test_an_inverted_range_is_certain(self):
        verdict = S.sampler_verdict(sampler(min_lod=4.0, max_lod=1.0), mips=8, format_id=0, formats={})
        assert verdict is not None
        self.assertEqual(verdict[0], 'certain')
        self.assertIn('inverted', verdict[1])

    def test_a_floor_past_the_mip_count_is_certain(self):
        verdict = S.sampler_verdict(sampler(min_lod=3.0), mips=2, format_id=0, formats={})
        assert verdict is not None
        self.assertEqual(verdict[0], 'certain')
        self.assertIn('level >= 3', verdict[1])

    def test_a_floor_inside_the_chain_is_not_a_finding(self):
        self.assertIsNone(S.sampler_verdict(sampler(min_lod=1.0), mips=4, format_id=0, formats={}))

    def test_mip_mapping_off_for_a_texture_with_a_chain_is_likely(self):
        verdict = S.sampler_verdict(sampler(max_lod=0.0), mips=4, format_id=0, formats={})
        assert verdict is not None
        self.assertEqual(verdict[0], 'likely')
        self.assertIn('only the base level', verdict[1])

    def test_a_comparison_filter_on_a_colour_format_is_likely(self):
        verdict = S.sampler_verdict(sampler(filter_='comparison linear/linear/linear'), mips=1,
                                    format_id=28, formats={28: 'R8G8B8A8_UNORM'})
        assert verdict is not None
        self.assertEqual(verdict[0], 'likely')
        self.assertIn('R8G8B8A8_UNORM', verdict[1])

    def test_a_comparison_filter_on_depth_or_on_a_format_it_cannot_name_is_silent(self):
        comparison = sampler(filter_='comparison linear/linear/linear')
        self.assertIsNone(S.sampler_verdict(comparison, mips=1, format_id=40,
                                            formats={40: 'D32_FLOAT'}))
        self.assertIsNone(S.sampler_verdict(comparison, mips=1, format_id=41,
                                            formats={41: 'R32_TYPELESS'}))
        self.assertIsNone(S.sampler_verdict(comparison, mips=1, format_id=99, formats={}))


class TestSamplersCommand(CmdCase):
    """The command over a fixture capture: the static array, a heap slot, and the pair between them."""

    def sig(self, resid: int = 11365, samplers: Any = ()) -> bytes:
        """A `Device_CreateRootSignature` whose one table binds an SRV slot and a sampler slot."""
        return self.ch('Device_CreateRootSignature',
                       F.pl_create_root_sig(resid, F.root_signature(
                           [('table', 0, 0, 0, 0, [('srv', 0, 1, 0, 0), ('sampler', 0, 1, 0, 1)])],
                           samplers=samplers)))

    def test_a_static_sampler_is_decoded_from_the_signature(self):
        path = self.cap(self.sig(samplers=(F.static_sampler(filter_=0x0, address=(3, 3, 3)),
                                          F.static_sampler(filter_=0x15, address=(1, 1, 1)))))
        text = out(S.cmd_samplers, path, 10)
        self.assertIn('static res11365', text)
        self.assertIn('point/point/point', text)
        self.assertIn('linear/linear/linear', text)
        self.assertIn('wrap/wrap/wrap', text)
        self.assertIn('2 sampler(s) created', text)

    def test_a_heap_sampler_slot_is_decoded(self):
        path = self.cap(self.ch('Device_CreateSampler',
                                F.pl_create_sampler(7, 3, F.sampler_desc(filter_=0x0,
                                                                         address=(5, 5, 5)))))
        text = out(S.cmd_samplers, path, 10)
        self.assertIn('heap 7 slot 3', text)
        self.assertIn('point/point/point', text)
        self.assertIn('mirror-once', text)          # the column is truncated to fit the table

    def test_a_pair_is_checked_against_the_texture_it_is_bound_with(self):
        texture = F.pl_committed_resource(200, F.pl_resource_desc(dimension=3, width=64, height=64,
                                                                  mips=4, fmt=28))
        path = self.cap(self.sig(samplers=(F.static_sampler(min_lod=4.0),)),
                        self.ch('Device_CreateCommittedResource', texture),
                        self.ch('Device_CreateShaderResourceView',
                                F.pl_descriptor_write(200, 1, 0, view_format=28)),
                        self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 11365)),
                        self.ch('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 0, 1, 0)),
                        self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 3, 1, 0, 0)))
        text = out(S.cmd_samplers, path, 10)
        self.assertIn('res200', text)
        self.assertIn('level >= 4', text)
        self.assertIn('1 sampler(s) created', text)

    def test_a_frame_with_no_sampler_at_all_says_so(self):
        text = out(S.cmd_samplers, self.cap(self.sig()), 5)
        self.assertIn('0 sampler(s) created', text)
        self.assertIn('0 with an arithmetic verdict', text)


if __name__ == '__main__':
    unittest.main()
