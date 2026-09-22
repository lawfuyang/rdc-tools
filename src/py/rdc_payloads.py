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
        elif name == 'List_ResourceBarrier':
            entries = parse_barriers(blob)
            if entries is not None:
                out.append('cmdList=%d barriers=%d' % (u64(blob, 0), len(entries)))
                for e in entries:
                    if e['kind'] == 'transition':
                        out.append('    transition res%d sub=%s %s -> %s'
                                   % (e['resource'],
                                      'all' if e['subresource'] == 0xffffffff else str(e['subresource']),
                                      state_text(e['before']), state_text(e['after'])))
                    elif e['kind'] == 'aliasing':
                        out.append('    aliasing res%d -> res%d' % (e['resource'], e['resource2']))
                    else:
                        out.append('    uav res%d' % e['resource'])
        elif name == 'List_Barrier':
            groups = parse_barrier_groups(blob)
            if groups is not None:
                out.append('cmdList=%d barriers=%d' % (u64(blob, 0), len(groups)))
                for g in groups:
                    if g['kind'] == 'texture':
                        out.append('    texture res%d %s layout=%s%s'
                                   % (g['resource'], access_text(g['access']),
                                      BARRIER_LAYOUTS.get(g['layout'], '0x%x' % g['layout']),
                                      ' discard' if g['flags'] & 1 else ''))
                    elif g['kind'] == 'buffer':
                        out.append('    buffer res%d %s' % (g['resource'], access_text(g['access'])))
                    else:
                        out.append('    global %s' % access_text(g['access']))
        elif name == 'List_OMSetRenderTargets':
            targets = parse_targets(blob)
            if targets is not None:
                out.append('cmdList=%d rtv=[%s] dsv=%s'
                           % (u64(blob, 0), ', '.join('res%d' % r for r in targets[0]),
                              'res%d' % targets[1] if targets[1] else 'none'))
        elif name in CLEAR_CHUNKS and len(blob) >= 16:
            out.append('cmdList=%d res=%d' % (u64(blob, 0), clear_target(blob)))
        elif name in DISCARD_CHUNKS and len(blob) >= 16:
            out.append('cmdList=%d res=%d' % (u64(blob, 0), discard_target(blob)))
        elif name in COPY_CHUNKS:
            pair = copy_pair(name, blob)
            if pair is not None:
                out.append('cmdList=%d dst=%d src=%d%s'
                           % (u64(blob, 0), pair[0], pair[1],
                              ' bytes=%d' % u64(blob, 40) if name == 'List_CopyBufferRegion' else ''))
        elif name in HEAP_CHUNKS and len(blob) >= 16:
            out.append('size=%d heapId=%d' % (u64(blob, 0), u64(blob, len(blob) - 8)))
    except Exception as exc:  # noqa: BLE001 - see the comment above
        out.append('decode error: %s' % exc)
    return out

# ---------------------------------------------------------------------------
# The chunks a *use* ledger reads: barriers, the render-target binding, clears, discards and copies.
#
# These parsers return the structured records the ledger wants (`BarrierInfo`, `GroupBarrier`,
# `(resource, resource)` pairs) and **None** when a payload does not walk cleanly, because each walk
# is exact by construction -- every arm's length comes from the serialiser -- so a payload that does
# not land at its own end is refused rather than approximated. The measures are on every real payload
# of the two captures in this repo (see each docstring).
# ---------------------------------------------------------------------------
#: `D3D12DescriptorType` (d3d12_manager.h): what a serialised `D3D12Descriptor` holds. The values
#: start at 0x1000 so that the type field aliases with a sampler's filter field, which is why they are
#: not 0..5.
DESCRIPTOR_TYPES: Dict[int, str] = {0x1000: 'cbv', 0x1001: 'srv', 0x1002: 'uav', 0x1003: 'rtv',
                                    0x1004: 'dsv'}

#: `D3D12_RESOURCE_STATES` bits worth naming, in bit order (d3d12.h). `COMMON`/`PRESENT` are 0 and
#: carry no bit, so "this state says nothing" is expressible rather than being lost in a name.
RESOURCE_STATES: List[Tuple[int, str]] = [
    (0x1, 'VertexAndConstantBuffer'), (0x2, 'IndexBuffer'), (0x4, 'RenderTarget'),
    (0x8, 'UnorderedAccess'), (0x10, 'DepthWrite'), (0x20, 'DepthRead'),
    (0x40, 'NonPixelShaderResource'), (0x80, 'PixelShaderResource'), (0x100, 'StreamOut'),
    (0x200, 'IndirectArgument'), (0x400, 'CopyDest'), (0x800, 'CopySource'),
    (0x1000, 'ResolveDest'), (0x2000, 'ResolveSource'), (0x10000, 'VideoDecodeRead'),
    (0x20000, 'VideoDecodeWrite'), (0x40000, 'VideoProcessRead'), (0x80000, 'VideoProcessWrite'),
    (0x200000, 'VideoEncodeRead'), (0x800000, 'VideoEncodeWrite'), (0x1000000, 'ShadingRateSource'),
    (0x400000, 'RaytracingAccelerationStructure')]

#: `D3D12_BARRIER_ACCESS` bits (d3d12.h). The 1.7-era barrier names what a resource is about to be
#: used for rather than the state it is in, so this -- not a state word -- is what a `List_Barrier`
#: gives the ledger to classify. Note `CONSTANT_BUFFER` is 0x2 and `INDEX_BUFFER` 0x4: not the order
#: the resource states use.
BARRIER_ACCESS: List[Tuple[int, str]] = [
    (0x1, 'VertexBuffer'), (0x2, 'ConstantBuffer'), (0x4, 'IndexBuffer'), (0x8, 'RenderTarget'),
    (0x10, 'UnorderedAccess'), (0x20, 'DepthStencilWrite'), (0x40, 'DepthStencilRead'),
    (0x80, 'ShaderResource'), (0x100, 'StreamOutput'), (0x200, 'IndirectArgument'),
    (0x400, 'CopyDest'), (0x800, 'CopySource'), (0x1000, 'ResolveDest'), (0x2000, 'ResolveSource'),
    (0x4000, 'RaytracingAccelerationStructureRead'),
    (0x8000, 'RaytracingAccelerationStructureWrite'), (0x10000, 'ShadingRateSource'),
    (0x20000, 'VideoDecodeRead'), (0x40000, 'VideoDecodeWrite'), (0x80000, 'VideoProcessRead'),
    (0x100000, 'VideoProcessWrite'), (0x200000, 'VideoEncodeRead'), (0x400000, 'VideoEncodeWrite')]

#: `D3D12_BARRIER_LAYOUT` (d3d12.h), which a texture barrier carries alongside its access word. The
#: same values are reused per queue type; the direct-queue name is the one printed.
BARRIER_LAYOUTS: Dict[int, str] = {
    0: 'Common', 1: 'GenericRead', 2: 'RenderTarget', 3: 'UnorderedAccess', 4: 'DepthStencilWrite',
    5: 'DepthStencilRead', 6: 'ShaderResource', 7: 'CopySource', 8: 'CopyDest', 9: 'ResolveSource',
    10: 'ResolveDest', 11: 'ShadingRateSource', 12: 'VideoDecodeRead', 13: 'VideoDecodeWrite',
    14: 'VideoProcessRead', 15: 'VideoProcessWrite', 16: 'VideoEncodeRead', 17: 'VideoEncodeWrite'}

#: `D3D12_TEXTURE_BARRIER_FLAG_DISCARD`: the barrier drops the contents it is leaving behind.
TEXTURE_BARRIER_DISCARD = 0x1

def _names_of_bits(words: List[Tuple[int, str]], value: int) -> str:
    """`RenderTarget|PixelShaderResource` for a bitmask, with any unnamed bit shown as `0x...`."""
    parts = [name for bit, name in words if value & bit]
    known = 0
    for bit, _name in words:
        known |= bit
    if value & ~known:
        parts.append('0x%x' % (value & ~known))
    return '|'.join(parts)

def state_text(word: int) -> str:
    """The `D3D12_RESOURCE_STATES` names in `word`; 0 is `Common`, which names no use at all."""
    return 'Common' if word == 0 else _names_of_bits(RESOURCE_STATES, word)

def access_text(word: int) -> str:
    """The `D3D12_BARRIER_ACCESS` names in `word`; 0 is `Common` (`D3D12_BARRIER_ACCESS_COMMON`)."""
    return 'Common' if word == 0 else _names_of_bits(BARRIER_ACCESS, word)

def parse_barriers(blob: Buffer) -> Optional[List[BarrierInfo]]:
    """Every barrier of a `List_ResourceBarrier` payload, or None when it does not walk cleanly.

    `u64 cmdList | u32 NumBarriers | u64 arrayCount | entry*` -- the count written twice, u32 then
    u64, the same shape `IASetVertexBuffers` has. The entries are **not** one length: a transition
    serialises as 28 bytes (resource, subresource, state before, state after), an aliasing barrier as
    24 (the resource handing over and the one receiving) and a UAV barrier as 16, because the
    serialiser writes the active arm of the union member by member. So they are walked by their own
    type, and measured on every `List_ResourceBarrier` in both captures here (63 + 12 payloads): each
    walk lands exactly at the end of its payload, which is what makes None a real answer.
    """
    if len(blob) < 20:
        return None
    count = u32(blob, 8)
    if u64(blob, 12) != count:
        return None
    out: List[BarrierInfo] = []
    o = 20
    for _ in range(count):
        kind = u32(blob, o)
        if kind == 0 and o + 28 <= len(blob):
            out.append(BarrierInfo(kind='transition', resource=u64(blob, o + 8), resource2=0,
                                   before=u32(blob, o + 20), after=u32(blob, o + 24),
                                   subresource=u32(blob, o + 16)))
            o += 28
        elif kind == 1 and o + 24 <= len(blob):
            out.append(BarrierInfo(kind='aliasing', resource=u64(blob, o + 8),
                                   resource2=u64(blob, o + 16), before=0, after=0, subresource=0))
            o += 24
        elif kind == 2 and o + 16 <= len(blob):
            out.append(BarrierInfo(kind='uav', resource=u64(blob, o + 8), resource2=0, before=0,
                                   after=0, subresource=0))
            o += 16
        else:
            return None
    return out if len(out) == count and o == len(blob) else None

def parse_barrier_groups(blob: Buffer) -> Optional[List[GroupBarrier]]:
    """Every barrier of a `List_Barrier` payload (`ID3D12GraphicsCommandList7`), or None.

    `u64 cmdList | u32 NumBarrierGroups | u64 arrayCount | group*`, and a group is
    `u32 Type | u32 NumBarriers | u64 count | element*` with `D3D12_BARRIER_TYPE` 0/1/2 for
    global/texture/buffer. An element is 16 bytes for a global barrier; the other two are fixed
    records with the resource inside them -- 60 bytes for a texture barrier (`SyncBefore`,
    `SyncAfter`, `AccessBefore`, `AccessAfter`, `LayoutBefore`, `LayoutAfter`, the resource, a
    24-byte subresource range and the flags) and 40 for a buffer one (the same access words, the
    resource, the offset and the size). Measured on all 91 `List_Barrier` payloads of `desktop-2`:
    every walk lands at the end.
    """
    if len(blob) < 20:
        return None
    groups = u32(blob, 8)
    if u64(blob, 12) != groups:
        return None
    out: List[GroupBarrier] = []
    o = 20
    for _ in range(groups):
        if o + 16 > len(blob):
            return None
        kind, count = u32(blob, o), u32(blob, o + 4)
        arm = {0: 16, 1: 60, 2: 40}.get(kind)
        if arm is None or u64(blob, o + 8) != count or o + 16 + count * arm > len(blob):
            return None
        for i in range(count):
            e = o + 16 + i * arm
            if kind == 1:
                out.append(GroupBarrier(kind='texture', resource=u64(blob, e + 24),
                                        sync=u32(blob, e + 4), access=u32(blob, e + 12),
                                        layout=u32(blob, e + 20), flags=u32(blob, e + 56)))
            elif kind == 2:
                out.append(GroupBarrier(kind='buffer', resource=u64(blob, e + 16),
                                        sync=u32(blob, e + 4), access=u32(blob, e + 12),
                                        layout=0, flags=0))
            else:
                out.append(GroupBarrier(kind='global', resource=0, sync=u32(blob, e + 4),
                                        access=u32(blob, e + 12), layout=0, flags=0))
        o += 16 + count * arm
    return out if o == len(blob) else None

#: The serialised length of the RTV/DSV union arm, by `D3D12_RTV_DIMENSION` / `D3D12_DSV_DIMENSION`
#: (`D3D12_BUFFER_RTV` is 12 bytes, `D3D12_TEX2D_DSV` only the 4-byte mip slice, and so on). A
#: `D3D12Descriptor` is the 16-byte header (type, heap, index), the resource id, and then the view
#: description, which the serialiser writes *arm by arm* -- so this table is the only variable part
#: of an entry, and a dimension it does not know makes the walk refuse instead of stepping by a guess.
_RTV_ARM: Dict[int, int] = {0: 0, 1: 12, 2: 4, 3: 12, 4: 8, 5: 16, 6: 0, 7: 8, 8: 12}
_DSV_ARM: Dict[int, int] = {0: 0, 1: 4, 2: 12, 3: 4, 4: 12, 5: 0, 6: 8}

def _rtv_entry(blob: Buffer, o: int) -> Optional[Tuple[int, int]]:
    """`(end offset, resource)` of one serialised RTV descriptor at `o`, or None."""
    if o + 32 > len(blob) or u32(blob, o) != 0x1003:
        return None
    arm = _RTV_ARM.get(u32(blob, o + 28))
    return None if arm is None else (o + 32 + arm, u64(blob, o + 16))

def _dsv_entry(blob: Buffer, o: int) -> Optional[Tuple[int, int]]:
    """`(end offset, resource)` of one serialised DSV descriptor at `o`, or None.

    A DSV description carries `Flags` as well as the format and the dimension, so its arm starts
    four bytes later than an RTV's.
    """
    if o + 36 > len(blob) or u32(blob, o) != 0x1004:
        return None
    arm = _DSV_ARM.get(u32(blob, o + 32))
    return None if arm is None else (o + 36 + arm, u64(blob, o + 16))

def parse_targets(blob: Buffer) -> Optional[Tuple[List[int], int]]:
    """`(render targets, depth target)` of a `List_OMSetRenderTargets` payload, or None.

    `u64 cmdList | u32 NumRenderTargetDescriptors | u64 arrayCount | RTV descriptor* | u8 present |
    [DSV descriptor]`: the call serialises the descriptors themselves rather than handles, which is
    why this is a walk and not two field reads (`D3D12Descriptor` in d3d12_serialise.cpp). The depth
    target is 0 when the call bound none. Measured on the 25 payloads of the two captures here --
    16 in `desktop-1` and 9 in `desktop-2` -- every walk lands at its payload's end.
    """
    if len(blob) < 21:
        return None
    count = u32(blob, 8)
    if u64(blob, 12) != count:
        return None
    targets: List[int] = []
    o = 20
    for _ in range(count):
        entry = _rtv_entry(blob, o)
        if entry is None:
            return None
        o, resource = entry
        targets.append(resource)
    if o >= len(blob):
        return None
    present = blob[o]
    o += 1
    if not present:
        return (targets, 0) if o == len(blob) else None
    dsv = _dsv_entry(blob, o)
    if dsv is None or dsv[0] != len(blob):
        return None
    return targets, dsv[1]

def clear_target(blob: Buffer) -> int:
    """The resource a `List_Clear*View` writes, or 0 when the payload is not shaped as one.

    An RTV or DSV clear serialises its descriptor first (16-byte header, then the resource id), so
    the id is at +24; a UAV clear puts a 12-byte `PortableHandle` in front of the descriptor, so
    theirs is at +36. The descriptor *type* is checked rather than assumed, which is what turns an
    unexpected payload into 0 instead of into a made-up resource. Verified against the resource
    table on every clear and discard in both captures (100 payloads): every id names a resource the
    capture created.
    """
    if len(blob) >= 32 and u32(blob, 8) in (0x1003, 0x1004):
        return u64(blob, 24)
    if len(blob) >= 44 and u32(blob, 20) == 0x1002:
        return u64(blob, 36)
    return 0

def discard_target(blob: Buffer) -> int:
    """The resource a `List_DiscardResource` drops: `u64 cmdList | u64 resource | OPT(region)`."""
    return u64(blob, 8) if len(blob) >= 16 else 0

def _copy_location_size(blob: Buffer, o: int) -> Optional[int]:
    """Bytes of the serialised `D3D12_TEXTURE_COPY_LOCATION` at `o`, or None.

    `pResource(8) | Type(4) | the arm the type selects` -- and the two types are the other way round
    from the obvious guess: `D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX` is **0** (one `UINT`, so the
    location is 16 bytes) and `..._PLACED_FOOTPRINT` is 1 (offset u64, format u32, width/height/depth
    u32 and rowPitch u32 = 28, so 40). All five `List_CopyTextureRegion` payloads in the captures
    here carry type 0, which is what made the wrong reading of the enum show up as a payload that
    would not walk.
    """
    if o + 12 > len(blob):
        return None
    kind = u32(blob, o + 8)
    if kind == 0:
        return 16
    if kind == 1:
        return 40
    return None

def copy_pair(name: str, blob: Buffer) -> Optional[Tuple[int, int]]:
    """`(destination, source)` of a copy payload, or None when it does not fit its layout.

    `List_CopyBufferRegion` is `u64 cmdList, u64 dst, u64 dstOffset, u64 src, u64 srcOffset, u64
    numBytes` -- 48 bytes, which is in `EXPECTED_LENGTHS`. `List_CopyTextureRegion` is a
    `D3D12_TEXTURE_COPY_LOCATION` on each side with three u32 coordinates between them, then the
    optional source box (a flag byte, and 24 bytes of coordinates when it is set), so it is walked
    to its end rather than to its first two fields: a payload that stops early is refused, which is
    what keeps a truncated one from naming a resource that is not there.
    """
    if name == 'List_CopyBufferRegion':
        if len(blob) < 48:
            return None
        return u64(blob, 8), u64(blob, 24)
    dst_arm = _copy_location_size(blob, 8)
    if dst_arm is None:
        return None
    src = 8 + dst_arm + 12
    src_arm = _copy_location_size(blob, src)
    if src_arm is None:
        return None
    end = src + src_arm
    if end > len(blob):
        return None                             # the source location itself is cut short
    if end < len(blob) and len(blob) - end < 1 + (24 if blob[end] else 0):
        return None                             # the source box is cut short
    return u64(blob, 8), u64(blob, src)

def _draw_state() -> DrawState:
    """A fresh command-list state: nothing bound, no PSO, no root signature, no targets."""
    return DrawState(pso=None, gfxSig=None, compSig=None, gfxCbv={}, compCbv={}, gfxSrv={},
                     compSrv={}, gfxUav={}, compUav={}, gfxTable={}, compTable={}, vbs={},
                     ib=None, rtv=[], dsv=0)

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
                st['gfxSig'] = sig
                st['gfxCbv'], st['gfxSrv'], st['gfxUav'], st['gfxTable'] = {}, {}, {}, {}
        elif st['compSig'] != sig:
            st['compSig'] = sig
            st['compCbv'], st['compSrv'], st['compUav'], st['compTable'] = {}, {}, {}, {}
    elif name in ('List_SetGraphicsRootConstantBufferView',
                  'List_SetComputeRootConstantBufferView') and len(blob) >= 28:
        # [u64 cmdList][u32 rootParam][u64 resourceId][u64 byteOffset] for both pipelines
        target = st['gfxCbv'] if name == 'List_SetGraphicsRootConstantBufferView' else st['compCbv']
        target[u32(blob, 8)] = (u64(blob, 12), u64(blob, 20))
    elif name in ('List_SetGraphicsRootShaderResourceView',
                  'List_SetGraphicsRootUnorderedAccessView',
                  'List_SetComputeRootShaderResourceView',
                  'List_SetComputeRootUnorderedAccessView') and len(blob) >= 28:
        # a root SRV/UAV is the same (resourceId, byteOffset) pair a root CBV carries; which of the
        # four dictionaries it goes in is the only difference
        if name == 'List_SetGraphicsRootShaderResourceView':
            st['gfxSrv'][u32(blob, 8)] = (u64(blob, 12), u64(blob, 20))
        elif name == 'List_SetComputeRootShaderResourceView':
            st['compSrv'][u32(blob, 8)] = (u64(blob, 12), u64(blob, 20))
        elif name == 'List_SetGraphicsRootUnorderedAccessView':
            st['gfxUav'][u32(blob, 8)] = (u64(blob, 12), u64(blob, 20))
        else:
            st['compUav'][u32(blob, 8)] = (u64(blob, 12), u64(blob, 20))
    elif name in ('List_SetGraphicsRootDescriptorTable',
                  'List_SetComputeRootDescriptorTable') and len(blob) >= 24:
        # [u64 cmdList][u32 rootParam][PortableHandle: u64 heapId, u32 descriptorIndex]
        tables = (st['gfxTable'] if name == 'List_SetGraphicsRootDescriptorTable'
                  else st['compTable'])
        tables[u32(blob, 8)] = (u64(blob, 12), u32(blob, 20))
    elif name == 'List_OMSetRenderTargets':
        # the payload carries the RTV and DSV *descriptors* rather than handles, so this is the one
        # binding that takes a walk (`parse_targets`). An unreadable payload clears the targets
        # instead of leaving the previous ones in place: a stale target is a draw reported as writing
        # something it does not have, which is worse than reporting none.
        targets = parse_targets(blob)
        st['rtv'], st['dsv'] = targets if targets is not None else ([], 0)
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

def draw_state_lines(state: Optional[DrawState], compute: bool, resources: Dict[int, ResourceInfo],
                     heaps: Dict[int, Dict[int, DescriptorInfo]], sigs: Dict[int, RootSignature],
                     binds: Dict[str, Dict[Tuple[str, int, int], str]]) -> List[str]:
    """The bindings in effect for one draw or dispatch, as the lines under its row.

    Returned rather than printed, and that is the whole reason it is a function: `draws` prints them
    indented, and `draws --format csv` folds them into one cell of a row, so the two forms of the same
    draw cannot disagree about what was bound.

    `compute` selects the namespace: a dispatch uses the compute root parameters, a draw the
    graphics ones. Vertex streams, the index buffer and the render targets are graphics-only state.
    Every resource id that the capture named gets its name appended (`_name_suffix`); a
    descriptor-table binding gets the descriptor written into the slot it names
    (`_descriptor_label`), or the heap's name when the slot was never written. `rpN` is annotated
    with what the root signature says it holds (`_root_param_label`), which is what keeps an index
    from being read as something it is not.

    The root descriptors are listed per kind (`CBV`, `SRV`, `UAV`) because the kind is the access:
    a UAV is reachable for reading *and* writing, which a bare resource id would not say.
    """
    lines: List[str] = []
    if state is None:
        return lines
    sig_id = state['compSig'] if compute else state['gfxSig']
    sig = sigs.get(sig_id) if sig_id is not None else None

    def label(rp: int) -> str:
        return _root_param_label(sig, binds, rp)

    for tag, roots in (('CBV', state['compCbv'] if compute else state['gfxCbv']),
                       ('SRV', state['compSrv'] if compute else state['gfxSrv']),
                       ('UAV', state['compUav'] if compute else state['gfxUav'])):
        if roots:
            lines.append('%s: %s' % (tag, '  '.join(
                '%s=res%d+0x%x%s' % (label(rp), res, off, rdc_resources._name_suffix(resources, res))
                for rp, (res, off) in sorted(roots.items()))))
    tables = state['compTable'] if compute else state['gfxTable']
    if tables:
        lines.append('Table: ' + '  '.join(
            '%s=heap%d[%d]%s' % (label(rp), heap, idx, _descriptor_label(heaps, resources, heap, idx)
                                 or rdc_resources._name_suffix(resources, heap))
            for rp, (heap, idx) in sorted(tables.items())))
    if compute:
        return lines
    if state['rtv']:
        lines.append('RTV: ' + '  '.join('res%d%s' % (res, rdc_resources._name_suffix(resources, res))
                                         for res in state['rtv']))
    if state['dsv']:
        lines.append('DSV: res%d%s' % (state['dsv'],
                                       rdc_resources._name_suffix(resources, state['dsv'])))
    if state['vbs']:
        lines.append('VB : ' + '  '.join(
            'res%d+0x%x(sz%d,st%d)%s' % (view + (rdc_resources._name_suffix(resources, view[0]),))
            for _, view in sorted(state['vbs'].items())))
    if state['ib']:
        ib_res, ib_off = state['ib']
        lines.append('IB : res%d+0x%x%s' % (ib_res, ib_off,
                                            rdc_resources._name_suffix(resources, ib_res)))
    return lines

__all__ = [
    'BARRIER_ACCESS',
    'BARRIER_LAYOUTS',
    'DESCRIPTOR_TYPES',
    'RESOURCE_STATES',
    'TEXTURE_BARRIER_DISCARD',
    '_apply_state_chunk',
    '_draw_state',
    'access_text',
    'clear_target',
    'copy_pair',
    'decode_chunk',
    'discard_target',
    'draw_state_lines',
    'parse_barrier_groups',
    'parse_barriers',
    'parse_targets',
    'state_text',
]
