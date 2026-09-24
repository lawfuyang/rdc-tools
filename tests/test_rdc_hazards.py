"""The offline `hazards` command: state conflicts, same-event loops, and what it refuses to claim.

Every frame here is built from `rdc_fixtures`, so nothing needs a capture or a GPU. The cases that matter
most are the *negative* ones -- a resource the frame never states a state for, and a depth view read rather
than written -- because this command's failure mode is a confident wrong answer, and each of those is a
measured way that used to happen.
"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'py'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import unittest

import rdc_fixtures as F                                                           # noqa: E402
import rdc_hazards as H                                                           # noqa: E402
from rdc_testcase import CmdCase, capture_text                                    # noqa: E402

RENDER_TARGET = 0x4
COPY_DEST = 0x400
DEPTH_READ = 0x20
DEPTH_WRITE = 0x10
UNORDERED_ACCESS = 0x8
PIXEL_SHADER_RESOURCE = 0x80


def out(func: Any, *args: Any) -> str:
    """Everything one command printed, the helper `test_rdc_uses` uses for the same job."""
    return capture_text(func, *args)


class TestHazardsCommand(CmdCase):
    """One frame per case, each short enough to read as the scenario it is."""

    def buffer(self, resid: int, size: int = 4096) -> bytes:
        return self.ch('Device_CreateCommittedResource',
                       F.pl_committed_resource(resid, F.pl_resource_desc(dimension=1, width=size)))

    def srv_signature(self, resid: int = 11365) -> bytes:
        """A root signature whose one table binds an SRV at t0."""
        return self.ch('Device_CreateRootSignature',
                       F.pl_create_root_sig(resid, F.root_signature(
                           [('table', 0, 0, 0, 0, [('srv', 0, 1, 0, 0)])])))

    def read_as_srv(self, resid: int, heap: int = 1, slot: int = 0, cmdlist: int = 7) -> list:
        """The chunks that bind `resid` as an SRV through that table and draw with it."""
        return [self.ch('Device_CreateShaderResourceView',
                        F.pl_descriptor_write(resid, heap, slot)),
                self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(cmdlist, 11365)),
                self.ch('List_SetGraphicsRootDescriptorTable',
                        F.pl_root_table(cmdlist, 0, heap, slot)),
                self.ch('List_DrawInstanced', F.pl_draw_instanced(cmdlist, 3, 1, 0, 0))]

    def test_a_read_while_the_resource_is_left_in_a_write_state_is_reported(self):
        path = self.cap(self.buffer(100), self.srv_signature(),
                        self.ch('List_ResourceBarrier',
                                F.pl_resources_barrier(7, [('transition', 100, 0, 0, COPY_DEST)])),
                        *self.read_as_srv(100))
        out_text = out(H.cmd_hazards, path, 8)
        self.assertIn('read as srv while in CopyDest', out_text)
        self.assertIn('1 distinct conflict(s)', out_text)
        self.assertEqual(H.cmd_hazards(path, 8), 1)          # a `certain` finding gates the exit code

    def test_a_resource_the_frame_never_states_is_not_claimed(self):
        path = self.cap(self.buffer(100), self.srv_signature(), *self.read_as_srv(100))
        out_text = out(H.cmd_hazards, path, 8)
        self.assertIn('0 finding(s)', out_text)
        self.assertIn('no state evidence', out_text)
        self.assertEqual(H.cmd_hazards(path, 8), 0)

    def test_a_transition_to_the_state_the_use_needs_is_not_a_finding(self):
        path = self.cap(self.buffer(100), self.srv_signature(),
                        self.ch('List_ResourceBarrier', F.pl_resources_barrier(
                            7, [('transition', 100, 0, 0, PIXEL_SHADER_RESOURCE)])),
                        *self.read_as_srv(100))
        self.assertIn('0 finding(s)', out(H.cmd_hazards, path, 8))

    def test_a_transition_in_another_command_list_is_not_evidence(self):
        path = self.cap(self.buffer(100), self.srv_signature(),
                        self.ch('List_ResourceBarrier', F.pl_resources_barrier(
                            9, [('transition', 100, 0, 0, PIXEL_SHADER_RESOURCE)])),
                        *self.read_as_srv(100))
        out_text = out(H.cmd_hazards, path, 8)
        self.assertIn('0 finding(s)', out_text)
        self.assertIn('no state evidence', out_text)

    def test_a_depth_view_read_through_is_not_a_write(self):
        """A `D3D12_DSV_FLAG_READ_ONLY_DEPTH` view binds a resource in `DepthRead`, which is legal."""
        path = self.cap(self.buffer(300),
                        self.ch('List_ResourceBarrier', F.pl_resources_barrier(
                            7, [('transition', 300, 0, DEPTH_WRITE, DEPTH_READ)])),
                        self.ch('List_OMSetRenderTargets', F.pl_omset(7, [], 300)),
                        self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 3, 1, 0, 0)))
        self.assertIn('0 finding(s)', out(H.cmd_hazards, path, 8))

    def test_a_target_also_read_through_the_same_signature_is_a_loop(self):
        path = self.cap(self.buffer(200), self.srv_signature(),
                        self.ch('Device_CreateShaderResourceView',
                                F.pl_descriptor_write(200, 1, 0)),
                        self.ch('List_OMSetRenderTargets', F.pl_omset(7, [200])),
                        self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 11365)),
                        self.ch('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 0, 1, 0)),
                        self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 3, 1, 0, 0)))
        out_text = out(H.cmd_hazards, path, 8)
        self.assertIn('loop', out_text)
        self.assertIn('bound as a render or depth target here', out_text)

    def test_an_srv_and_a_uav_at_one_event_is_a_likely_loop(self):
        # one table covering an SRV slot and a UAV slot, which is what makes the same resource both
        both = self.ch('Device_CreateRootSignature',
                       F.pl_create_root_sig(11365, F.root_signature(
                           [('table', 0, 0, 0, 0, [('srv', 0, 1, 0, 0), ('uav', 0, 2, 0, 1)])])))
        path = self.cap(self.buffer(400), both,
                        self.ch('Device_CreateShaderResourceView',
                                F.pl_descriptor_write(400, 1, 0)),
                        self.ch('Device_CreateUnorderedAccessView',
                                F.pl_descriptor_write(400, 1, 1, length=80)),
                        self.ch('List_SetComputeRootSignature', F.pl_root_signature(8, 11365)),
                        self.ch('List_SetComputeRootDescriptorTable', F.pl_root_table(8, 0, 1, 0)),
                        self.ch('List_Dispatch', F.pl_dispatch(8, 1, 1, 1)))
        out_text = out(H.cmd_hazards, path, 8)
        self.assertIn('likely', out_text)
        self.assertIn('both an SRV and a UAV at one event', out_text)

    def test_repeats_fold_into_one_conflict_with_their_count(self):
        frame = [self.buffer(100), self.srv_signature(),
                 self.ch('List_ResourceBarrier',
                         F.pl_resources_barrier(7, [('transition', 100, 0, 0, UNORDERED_ACCESS)]))]
        frame += self.read_as_srv(100) + self.read_as_srv(100)
        out_text = out(H.cmd_hazards, self.cap(*frame), 8)
        self.assertIn('2 finding(s)', out_text)
        self.assertIn('1 distinct conflict(s)', out_text)
        self.assertIn('x2', out_text)


if __name__ == '__main__':
    unittest.main()
