"""The payload decoders: what the D3D12 chunks actually contain -- draw state, pipeline state, constant buffers, vertex buffers."""

from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_resources import *  # noqa: F401,F403
import rdc_resources  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_profile

from typing import Dict, List, Optional, Tuple

@rdc_profile.timed('chunk payload decode')
def decode_chunk(name: Optional[str], blob: Buffer) -> List[str]:
    """Decode the known D3D12 chunk payload layouts (each element is raw, ResourceId = u64)."""
    out: List[str] = []
    # The broad catch is deliberate: `blob` comes straight from a stream we only heuristically
    # validate, so any struct/Unicode/index error must surface as a printed `decode error: ...`
    # line rather than aborting the whole command (`chunk N` fed a str must not raise).
    try:
        if name == 'List_SetPipelineState' and len(blob) >= 16:
            out.append('cmdList=%d pso=%d' % (u64(blob, 0), u64(blob, 8)))
        elif name in ('List_DrawIndexedInstanced',) and len(blob) >= 28:
            out.append('cmdList=%d idx=%d inst=%d startIdx=%d baseVtx=%d startInst=%d'
                       % (u64(blob, 0), u32(blob, 8), u32(blob, 12), u32(blob, 16),
                          u32(blob, 20), u32(blob, 24)))
        elif name in ('List_DrawInstanced',) and len(blob) >= 24:
            out.append('cmdList=%d verts=%d inst=%d startVtx=%d startInst=%d'
                       % (u64(blob, 0), u32(blob, 8), u32(blob, 12), u32(blob, 16),
                          u32(blob, 20)))
        elif name == 'List_Dispatch' and len(blob) >= 20:
            out.append('cmdList=%d x=%d y=%d z=%d' % (u64(blob, 0), u32(blob, 8), u32(blob, 12),
                                                      u32(blob, 16)))
        elif name in ('List_SetGraphicsRootConstantBufferView',
                      'List_SetGraphicsRootShaderResourceView',
                      'List_SetGraphicsRootUnorderedAccessView',
                      'List_SetComputeRootConstantBufferView') and len(blob) >= 28:
            # [u64 cmdList][u32 rootParam][u64 resourceId][u64 byteOffset] -- D3D12BufferLocation
            # serialises as Id + Offset (d3d12_serialise.cpp), so this is the same pair `draws` uses.
            out.append('cmdList=%d rootParam=%d res=%d+0x%x'
                       % (u64(blob, 0), u32(blob, 8), u64(blob, 12), u64(blob, 20)))
        elif name in ('List_SetGraphicsRootDescriptorTable',
                      'List_SetComputeRootDescriptorTable') and len(blob) >= 24:
            # [u64 cmdList][u32 rootParam][PortableHandle: u64 heapId, u32 descriptorIndex]:
            # a D3D12_GPU_DESCRIPTOR_HANDLE is serialised as (heap resource, index) rather than as
            # a raw pointer (d3d12_serialise.cpp DoSerialise + PortableHandle in d3d12_manager.h),
            # so the payload is 24 bytes and there is no pointer to print
            out.append('cmdList=%d rootParam=%d heap=%d index=%d'
                       % (u64(blob, 0), u32(blob, 8), u64(blob, 12), u32(blob, 20)))
        elif name in ('List_SetGraphicsRootSignature',
                      'List_SetComputeRootSignature') and len(blob) >= 16:
            out.append('cmdList=%d rootSig=%d' % (u64(blob, 0), u64(blob, 8)))
        elif name == 'List_Reset' and len(blob) >= 56:
            # 64-byte payload; the command-list id at +40 is the one the other List_* chunks carry
            # at +0 (see `_apply_state_chunk`), and +48 is the optional initial PSO
            out.append('cmdList=%d initialPso=%d' % (u64(blob, 40), u64(blob, 48)))
        elif name == 'List_IASetVertexBuffers' and len(blob) >= 24:
            # [u64 cmdList][u32 startSlot][u32 numViews][u64 arrayCount] then 24 bytes per view:
            # [u64 resourceId][u64 VA][u32 sizeInBytes][u32 strideInBytes]
            start_slot, num_views = u32(blob, 8), u32(blob, 12)
            out.append('cmdList=%d startSlot=%d numViews=%d' % (u64(blob, 0), start_slot, num_views))
            for i in range(min(num_views, 16)):
                o = 24 + i * 24
                if o + 24 > len(blob):
                    break
                out.append('    view[%d] res=%d VA=0x%x size=%d stride=%d'
                           % (start_slot + i, u64(blob, o), u64(blob, o + 8), u32(blob, o + 16),
                              u32(blob, o + 20)))
        elif name == 'List_IASetIndexBuffer' and len(blob) >= 9:
            # [u64 cmdList][u8 present][u64 resourceId][u64 byteOffset][u32 size][u32 format]
            # The view goes through SERIALISE_ELEMENT_OPT, which writes a "present" bool first
            # (serialiser.h), so the whole payload is 33 bytes and every field is one byte later
            # than the struct alone would suggest. `size` is indexCount * formatWidth.
            if not blob[8]:
                out.append('cmdList=%d (null index buffer view)' % u64(blob, 0))
            elif len(blob) >= 33:
                out.append('cmdList=%d res=%d+0x%x size=%d fmt=%d'
                           % (u64(blob, 0), u64(blob, 9), u64(blob, 17), u32(blob, 25),
                              u32(blob, 29)))
        elif name == 'Device_CreatePipelineState' and len(blob) >= 8:
            out.append('payload %d bytes; tail=%s' % (len(blob), blob[-24:].hex()))
        elif name in ('InitialContents', 'InitialContentsList') and len(blob) >= 32:
            out.append('id=%d hdr=%s' % (u64(blob, 0), blob[8:40].hex()))
    except Exception as exc:  # noqa: BLE001 - see the comment above
        out.append('decode error: %s' % exc)
    return out

def _draw_state() -> DrawState:
    """A fresh command-list state: nothing bound, no PSO, no root signature."""
    return DrawState(pso=None, gfxSig=None, compSig=None, gfxCbv={}, compCbv={}, gfxTable={},
                     compTable={}, vbs={}, ib=None)

def _apply_state_chunk(name: str, blob: Buffer, states: Dict[int, DrawState]) -> bool:
    """Apply one state-changing `List_*` chunk; True when `name` is one of the tracked setters.

    Every command-list payload starts with the `u64` resource id of its command list, so the state
    is tracked *per command list*: two lists recorded in one capture cannot leak into each other.
    `List_Reset` is the exception -- its 64-byte payload carries that same id at +40 and the
    optional initial PSO at +48 (measured on both captures in this repo: +40 matches the id the
    other `List_*` chunks carry for all 170 setter chunks of the PC capture).
    """
    if name == 'List_Reset':
        # Reset() is what clears a command list in D3D12 -- not SetPipelineState, not the draws
        if len(blob) >= 56:
            fresh = _draw_state()
            fresh['pso'] = u64(blob, 48) or None
            states[u64(blob, 40)] = fresh
        else:
            # an unknown Reset layout: assume it is the only list, so nothing stale can survive
            states.clear()
        return True
    if name not in STATE_SETTERS or len(blob) < 8:
        return False
    st = states.setdefault(u64(blob, 0), _draw_state())
    if name == 'List_SetPipelineState' and len(blob) >= 16:
        st['pso'] = u64(blob, 8)
    elif name in ('List_SetGraphicsRootSignature',
                  'List_SetComputeRootSignature') and len(blob) >= 16:
        sig = u64(blob, 8)
        if name == 'List_SetGraphicsRootSignature':
            # a *changed* signature makes every root argument stale; setting the same one again
            # keeps them (D3D12 command-list semantics, and what RenderDoc's replay implements)
            if st['gfxSig'] != sig:
                st['gfxSig'], st['gfxCbv'], st['gfxTable'] = sig, {}, {}
        elif st['compSig'] != sig:
            st['compSig'], st['compCbv'], st['compTable'] = sig, {}, {}
    elif name in ('List_SetGraphicsRootConstantBufferView',
                  'List_SetComputeRootConstantBufferView') and len(blob) >= 28:
        # [u64 cmdList][u32 rootParam][u64 resourceId][u64 byteOffset] for both pipelines
        target = st['gfxCbv'] if name == 'List_SetGraphicsRootConstantBufferView' else st['compCbv']
        target[u32(blob, 8)] = (u64(blob, 12), u64(blob, 20))
    elif name in ('List_SetGraphicsRootDescriptorTable',
                  'List_SetComputeRootDescriptorTable') and len(blob) >= 24:
        # [u64 cmdList][u32 rootParam][PortableHandle: u64 heapId, u32 descriptorIndex]
        tables = (st['gfxTable'] if name == 'List_SetGraphicsRootDescriptorTable'
                  else st['compTable'])
        tables[u32(blob, 8)] = (u64(blob, 12), u32(blob, 20))
    elif name == 'List_IASetVertexBuffers' and len(blob) >= 24:
        # [u64 cmdList][u32 startSlot][u32 numViews][u64 arrayCount] then 24 bytes per view; the
        # slots outside [startSlot, startSlot + numViews) keep whatever they had bound
        start, count = u32(blob, 8), u32(blob, 12)
        for i in range(min(count, 16)):
            o = 24 + i * 24
            if o + 24 > len(blob):
                break
            st['vbs'][start + i] = (u64(blob, o), u64(blob, o + 8), u32(blob, o + 16),
                                    u32(blob, o + 20))
    elif name == 'List_IASetIndexBuffer' and len(blob) >= 9:
        # the view goes through SERIALISE_ELEMENT_OPT: [u8 present] then (resourceId, byteOffset).
        # A null view (9-byte payload) *clears* the binding -- leaving the previous one in place
        # would report an index buffer the draw does not have.
        if not blob[8]:
            st['ib'] = None
        elif len(blob) >= 33:
            st['ib'] = (u64(blob, 9), u64(blob, 17))
    return True

def _print_draw_state(state: Optional[DrawState], compute: bool, resources: Dict[int, ResourceInfo],
                      heaps: Dict[int, Dict[int, DescriptorInfo]], sigs: Dict[int, RootSignature],
                      binds: Dict[str, Dict[Tuple[str, int, int], str]]) -> None:
    """Print the bindings in effect for one draw or dispatch (the indented lines under its row).

    `compute` selects the namespace: a dispatch uses the compute root parameters, a draw the
    graphics ones. Vertex streams and the index buffer are graphics-only state. Every resource id
    that the capture named gets its name appended (`_name_suffix`); a descriptor-table binding gets
    the descriptor written into the slot it names (`_descriptor_label`), or the heap's name when the
    slot was never written. `rpN` is annotated with what the root signature says it holds
    (`_root_param_label`), which is what keeps an index from being read as something it is not.
    """
    if state is None:
        return
    sig_id = state['compSig'] if compute else state['gfxSig']
    sig = sigs.get(sig_id) if sig_id is not None else None

    def label(rp: int) -> str:
        return _root_param_label(sig, binds, rp)

    cbvs = state['compCbv'] if compute else state['gfxCbv']
    tables = state['compTable'] if compute else state['gfxTable']
    if cbvs:
        print('        CBV: ' + '  '.join(
            '%s=res%d+0x%x%s' % (label(rp), res, off, rdc_resources._name_suffix(resources, res))
            for rp, (res, off) in sorted(cbvs.items())))
    if tables:
        print('        Table: ' + '  '.join(
            '%s=heap%d[%d]%s' % (label(rp), heap, idx, _descriptor_label(heaps, resources, heap, idx)
                                 or rdc_resources._name_suffix(resources, heap))
            for rp, (heap, idx) in sorted(tables.items())))
    if compute:
        return
    if state['vbs']:
        print('        VB : ' + '  '.join(
            'res%d+0x%x(sz%d,st%d)%s' % (view + (rdc_resources._name_suffix(resources, view[0]),))
            for _, view in sorted(state['vbs'].items())))
    if state['ib']:
        ib_res, ib_off = state['ib']
        print('        IB : res%d+0x%x%s' % (ib_res, ib_off, rdc_resources._name_suffix(resources, ib_res)))

__all__ = [
    '_apply_state_chunk',
    '_draw_state',
    '_print_draw_state',
    'decode_chunk',
]
