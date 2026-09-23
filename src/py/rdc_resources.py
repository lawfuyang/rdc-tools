"""The resource side of the file: DXGI format names, the resource table, the descriptor heaps, the root signatures and the shader `RDEF` reflection."""

from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_dxbc import *  # noqa: F401,F403
import rdc_cache       # noqa: F401  (used qualified: a sidecar is the cache's file naming, not ours)
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_chunknames  # noqa: F401  (the bundled format names, when no tree is there)
import rdc_profile

import os

from typing import Any, Dict, List, Optional, Tuple

def load_format_names(src_root: Optional[str] = None) -> Dict[int, str]:
    """DXGI format id -> short name (`R8G8B8A8_UNORM`): the tree's copy of the enum, else the bundled table.

    `DXGI_FORMAT` belongs to the Windows SDK, but `common/dds_readwrite.cpp` carries a copy with explicit
    values, and the bundled table carries that copy too -- so a machine with no source tree prints
    `R8G8B8A8_UNORM` rather than `28`, the same fallback chunk names get (`rdc_chunkmap.load_chunk_names`,
    README §1.1). `src_root` defaults to `rdc_chunkmap.RENDERDOC_SRC` at call time, so tests (and a patched
    `rdc_chunkmap.RENDERDOC_SRC`) can point it somewhere else.
    """
    raw = rdc_chunkmap.parse_enum(rdc_chunkmap.RENDERDOC_SRC if src_root is None else src_root, 'formats')
    if not raw:
        raw = rdc_chunknames.FORMAT_NAMES
    return {fmt_id: name.replace('DXGI_FORMAT_', '', 1) for fmt_id, name in raw.items()}

#: Every class `classify_format` can return, and nothing else -- so a test can sweep the whole name table and
#: assert it never lands somewhere outside this list. sRGB is deliberately *not* one of them: it is a flag on a
#: layout (`R8G8B8A8_UNORM_SRGB` is still UNORM's components), and a class for it would say a SRGB texture has
#: no components.
CLASSES = ('unorm', 'snorm', 'float', 'uint', 'sint', 'depth', 'block', 'packed', 'yuv', 'typeless', 'other')

#: The video layouts: a YUV plane, or a palettised block, with no plain texel size at all. Listed rather than
#: inferred, because the names carry no rule -- `NV12` is two planes and `AI44` is one byte of palette index
#: plus one of alpha -- and guessing at one of them would be a wrong number where "a video layout" is right.
_VIDEO_FORMATS = ('NV12', 'NV11', 'P010', 'P016', 'P208', 'P216', 'P416', '420_OPAQUE', 'AI44', 'IA44',
                  'AYUV', 'Y410', 'Y416', 'YUY2', 'Y210', 'Y216', 'P8', 'A8P8')

#: A block-compressed format's element is a whole block, so its "widths" are per block and its size is the
#: block's: 8 bytes for BC1/BC4 (4x4 texels, 4 bits each) and 16 for the rest, 4x4 or 4x5. Kept as a table
#: rather than derived: deriving it means re-implementing the format table the engine already has.
_BLOCK_BYTES = {'BC1': 8, 'BC4': 8}

def classify_format(name: str) -> Dict[str, Any]:
    """What a DXGI **name** says about itself, for the format coverage audit (`formats`).

    The name is the input, not the enum value: the tool reads names out of the capture's own resource table
    (`load_format_names`), and a classifier that wanted the numeric id could not answer for a format the
    bundled table has never heard of. So everything here is a *reading of the name*, and `other` is the honest
    answer for a name whose shape this does not know -- an unknown format becomes a row in the audit rather
    than an exception, which is the whole point of an audit.

    The reading is deliberately shallow: which type the components are, how many and how wide, whether the
    layout is block-compressed, packed or a video one, and whether the bits are typeless -- the one class that
    changes what a *picture* of it means, because the display path needs a cast. It does not try to be the
    engine's `ResourceFormat`: the driver's `formats` prints that one directly, and this half exists for the
    captures and formats a driver is not around to describe.
    """
    upper = name.upper()
    typeless = upper.endswith('_TYPELESS')
    srgb = upper.endswith('_SRGB') or '_SRGB_' in upper
    body = upper
    for suffix in ('_TYPELESS', '_SRGB'):
        if body.endswith(suffix):
            body = body[: -len(suffix)]

    shape: Dict[str, Any] = {'class': 'other', 'components': 0, 'bits': 0, 'layout': '',
                             'srgb': srgb, 'typeless': typeless, 'blockCompressed': False, 'note': ''}
    if not body or body == 'UNKNOWN':
        shape['note'] = 'the enum has no name for it'
        return shape

    head = body.split('_', 1)[0]
    for block, size in _BLOCK_BYTES.items():
        if head.startswith(block):
            shape.update({'class': 'block', 'blockCompressed': True, 'layout': head,
                          'note': '%d bytes per 4x4 block (%s)' % (size, '4 bits a texel'
                                                                   if block in ('BC1', 'BC4') else '8')})
            return shape
    if head.startswith('BC'):
        shape.update({'class': 'block', 'blockCompressed': True, 'layout': head,
                      'note': '16 bytes per block (4x4, or 4x5 for BC7)'})
        return shape
    if body in _VIDEO_FORMATS:
        shape.update({'class': 'yuv', 'note': 'a palettised or video layout: no plain texel size'})
        return shape

    # The channel widths: the digit runs before the type suffix (`R10G10B10A2_UNORM` -> 10,10,10,2; `R8G8_B8G8`
    # and `G8R8_G8B8` are two texels in one 16-bit word, which is why they are their own class). Split by hand
    # rather than with a regular expression: this module reads the *name* and nothing else, and a three-line
    # scan of digits and non-digits needs no import to explain.
    channels: List[int] = []
    digits = ''
    for char in head + 'X':
        if char.isdigit():
            digits += char
        elif digits:
            channels.append(int(digits))
            digits = ''
    shape['components'] = len(channels)
    shape['bits'] = channels[0] if channels and len(set(channels)) == 1 else 0
    shape['layout'] = ':'.join(str(part) for part in channels)

    if body.startswith('D') and (channels or '_' in body):    # D32_FLOAT, D24_UNORM_S8_UINT, D16_UNORM
        shape.update({'class': 'depth', 'components': 1, 'bits': channels[0] if channels else 0})
        if 'S8' in body:
            shape['note'] = 'depth and stencil in one element'
        return shape
    if head in ('R8G8', 'G8R8') or '_B8G8' in head or '_G8B8' in head:
        shape.update({'class': 'packed', 'components': 2, 'bits': 8,
                      'note': 'two texels per 16-bit element'})
        return shape

    # The type comes from a *suffix*, and a name without one is not guessed at: defaulting to `unorm` would make
    # `SOMETHING_NEW_FROM_A_DRIVER` read as a plain 8-bit format, which is the one mistake this classifier
    # cannot afford -- the audit's whole point is that an unknown name is a row that says so.
    kind = ''
    if '_FLOAT' in body:
        kind = 'float'
    elif '_SNORM' in body:
        kind = 'snorm'
    elif '_UINT' in body:
        kind = 'uint'
    elif '_SINT' in body:
        kind = 'sint'
    elif '_UNORM' in body:
        kind = 'unorm'
    elif '_SHAREDEXP' in body:
        kind = 'float'
        shape['note'] = 'a shared exponent across the components'
    shape['class'] = 'typeless' if typeless else (kind or 'other')
    if typeless:
        shape['note'] = 'no type of its own: a picture needs `--cast` to say how to read the bits'
    elif not kind:
        shape['note'] = 'no type in the name: this table does not know the layout'
    return shape

def _parse_resource(blob: Buffer, desc_off: int) -> Optional[ResourceInfo]:
    """Decode the `D3D12_RESOURCE_DESC` of one creation payload, or None if it does not fit.

    The dimension field is the check that this really is a descriptor (0/unknown means it is not),
    which keeps the decoder honest without pinning a payload length that varies with the optional
    clear value.
    """
    if len(blob) < desc_off + _RESOURCE_DESC_SIZE + _RESOURCE_TAIL:
        return None
    dim = u32(blob, desc_off)
    if dim not in RESOURCE_KINDS:
        return None
    width = u64(blob, desc_off + 12)
    return ResourceInfo(kind=RESOURCE_KINDS[dim], name='', size=width if dim == 1 else 0,
                        width=width, height=u32(blob, desc_off + 20),
                        depth=u16(blob, desc_off + 24), mips=u16(blob, desc_off + 26),
                        format=u32(blob, desc_off + 28), gpuAddress=u64(blob, len(blob) - 8))

def _parse_acceleration_structure(blob: Buffer) -> Optional[Tuple[int, ResourceInfo]]:
    """`(id, info)` of one `CreateAS` payload, or None when it does not fit the layout.

    The buffer and offset it lives at are decoded by `AS_KINDS`' caller but not recorded: the id is
    what the rest of the tool prints, and `size` is the acceleration structure's own byte size.
    """
    if len(blob) < 36:
        return None
    kind = AS_KINDS.get(u32(blob, 16))
    if kind is None:
        return None
    return u64(blob, 28), ResourceInfo(kind=kind, name='', size=u64(blob, 20), width=0, height=0,
                                       depth=0, mips=0, format=0, gpuAddress=0)

@rdc_profile.timed('resource table')
def parse_resource_table(stream: Buffer,
                         names: Optional[Dict[int, str]] = None) -> Dict[int, ResourceInfo]:
    """Build the resource table (id -> description) from the creation chunks and `SetName`.

    `RESOURCE_CHUNKS` says where each creation payload's descriptor starts, and the resource id is
    always at `length - 16`; `CreateAS` adds acceleration structures, whose ids the frame references
    just like resources. `SetName` names any D3D12 object, so ids that are only named -- heaps,
    queues, fences, PSOs -- end up in the table too, with `kind == 'unknown'` and no size; that is
    what `draws` prints as `heap298[279377]`.
    """
    if names is None:
        names = rdc_chunkmap.load_chunk_names()
    table: Dict[int, ResourceInfo] = {}
    named: Dict[int, str] = {}
    for ch in iter_chunks(stream):
        nm = names.get(ch['id'], '')
        if nm == 'SetName':
            blob = chunk_payload(stream, ch)
            nlen = u32(blob, 8) if len(blob) >= 12 else 0
            if nlen <= len(blob) - 12:
                named[u64(blob, 0)] = blob[12:12 + nlen].decode('utf-8', 'replace')
        elif nm in RESOURCE_CHUNKS:
            blob = chunk_payload(stream, ch)
            info = _parse_resource(blob, RESOURCE_CHUNKS[nm])
            if info is not None:
                table[u64(blob, len(blob) - 16)] = info
        elif nm == 'CreateAS':
            parsed = _parse_acceleration_structure(chunk_payload(stream, ch))
            if parsed is not None:
                table[parsed[0]] = parsed[1]
    for rid, name in named.items():
        entry = table.get(rid)
        if entry is None:
            table[rid] = ResourceInfo(kind='unknown', name=name, size=0, width=0, height=0,
                                      depth=0, mips=0, format=0, gpuAddress=0)
        else:
            entry['name'] = name
    return table

def _portable_handle(blob: Buffer, offset: int) -> Optional[Tuple[int, int]]:
    """`(heapId, index)` of the `PortableHandle` at `offset`, or None when it does not fit.

    A `PortableHandle` is `u64 heapId, u32 index` (`d3d12_manager.h`): 12 bytes, no padding, and the
    same pair a descriptor-table binding carries (REFERENCE 3.4).
    """
    if offset < 0 or offset + 12 > len(blob):
        return None
    return u64(blob, offset), u32(blob, offset + 8)

def apply_descriptor_chunk(name: str, blob: Buffer,
                           heaps: Dict[int, Dict[int, DescriptorInfo]]) -> bool:
    """Apply one descriptor write or copy to `heaps` in place; True when `name` is one of them.

    Split out of `parse_descriptor_heaps` because the *order* matters and not every caller wants the
    whole stream: a walk that resolves a binding has to know what a slot holds *at that moment*
    (`rdc_sigcheck`), while `parse_descriptor_heaps` hands back the state at the end of the frame.
    One implementation of the payload layouts, so the two cannot drift.
    """
    kind = DESCRIPTOR_KINDS.get(name)
    if kind is not None:
        if len(blob) < _DESCRIPTOR_WRITE_MIN:
            return True
        dst = _portable_handle(blob, len(blob) - 12)
        if dst is None:
            return True
        resource = 0 if kind == 'sampler' else u64(blob, 16)
        # The view's own `Format` for the kinds whose serialised description opens with it: an SRV, a UAV
        # and an RTV all begin with `DXGI_FORMAT Format;` (`DoSerialise(D3D12_*_VIEW_DESC &)` in
        # d3d12_serialise.cpp, then the view dimension), and the description starts after the 16-byte
        # `D3D12Descriptor` (type, heap, index) and the resource -- so it is at +24. A DSV instead carries
        # `Flags` and a CBV has no format at all, so both stay 0 rather than being read at an offset that
        # belongs to another field. This is the one place a view format is available offline.
        view_format = u32(blob, 24) if kind in ('srv', 'uav', 'rtv') and len(blob) >= 28 else 0
        heaps.setdefault(dst[0], {})[dst[1]] = DescriptorInfo(kind=kind, resource=resource,
                                                              viewFormat=view_format)
        return True
    if name in DESCRIPTOR_COPY_CHUNKS:
        count = u64(blob, 0) if len(blob) >= 8 else 0
        for i in range(count):
            entry = 8 + i * _DESCRIPTOR_COPY_SIZE
            dst = _portable_handle(blob, entry + 4)
            src = _portable_handle(blob, entry + 16)
            if dst is None or src is None:
                break
            info = heaps.get(src[0], {}).get(src[1])
            if info is not None:
                heaps.setdefault(dst[0], {})[dst[1]] = info
        return True
    return False

@rdc_profile.timed('descriptor heaps')
def parse_descriptor_heaps(stream: Buffer, names: Optional[Dict[int, str]] = None
                           ) -> Dict[int, Dict[int, DescriptorInfo]]:
    """Build `heapId -> {index: DescriptorInfo}` from the descriptor writes and copies.

    Writes and copies are applied in stream order, which is the order D3D12 applies them in: a slot
    written twice ends up holding the second write, and a copy reads the slot as it is *at that
    point* in the frame. Only written slots are recorded -- heaps are created with up to a million
    slots and the rest stay undefined, which is also what an unresolved `draws` binding means.
    """
    if names is None:
        names = rdc_chunkmap.load_chunk_names()
    heaps: Dict[int, Dict[int, DescriptorInfo]] = {}
    for ch in iter_chunks(stream):
        nm = names.get(ch['id'], '')
        if nm in DESCRIPTOR_KINDS or nm in DESCRIPTOR_COPY_CHUNKS:
            apply_descriptor_chunk(nm, chunk_payload(stream, ch), heaps)
    return heaps

def _descriptor_label(heaps: Dict[int, Dict[int, DescriptorInfo]],
                      resources: Dict[int, ResourceInfo], heap: int, index: int) -> str:
    """` -> srv res2233[Name]` for a descriptor-table slot, or '' when it was never written."""
    info = heaps.get(heap, {}).get(index)
    if info is None:
        return ''
    if info['kind'] == 'sampler':
        return ' -> sampler'
    return ' -> %s res%d%s' % (info['kind'], info['resource'],
                               _name_suffix(resources, info['resource']))

# ---------------------------------------------------------------------------
# Root signatures.
#
# `Device_CreateRootSignature` carries the serialised D3D12 root signature as a DXBC container with
# one `RTS0` part, and `DecodeRootSig` (driver/d3d12/d3d12_rootsig.cpp) is the layout:
#
#   header : u32 version | u32 numParams | u32 paramDataOffset | u32 numStaticSamplers
#            | u32 staticSamplerOffset | u32 flags                    (24 bytes; every offset is from
#                                                                     the start of the part's data)
#   params : u32 type | u32 visibility | u32 dataOffset              (12 bytes each, likewise from
#                                                                     the part's data start)
#   data   : the parameter's payload -- `D3D12_ROOT_CONSTANTS` (register, space, count) for 32-bit
#            constants, `D3D12_ROOT_DESCRIPTOR1` (register, space, flags) for a root descriptor, or a
#            table (`u32 numRanges | u32 rangesOffset`) whose ranges are `type | count | base |
#            space | flags | tableOffset`, 24 bytes for a 1.1+ signature and 20 for a 1.0 one.
#
# The signature holds no names at all: `parse_rdef` is the only place they can come from, and every
# capture in this repo strips it (see REFERENCE 3.6). What the decode *does* give is what each `rpN` is
# -- a table, a CBV at b1, four root constants -- which is the part that used to be a guess.
# ---------------------------------------------------------------------------
#: `D3D12_ROOT_PARAMETER_TYPE`.
PARAM_KINDS: Dict[int, str] = {0: 'table', 1: '32bit', 2: 'cbv', 3: 'srv', 4: 'uav'}

#: `D3D12_SHADER_VISIBILITY`.
VISIBILITIES: Dict[int, str] = {0: 'all', 1: 'vs', 2: 'hs', 3: 'ds', 4: 'gs', 5: 'ps', 6: 'as',
                                7: 'ms'}

#: `D3D12_DESCRIPTOR_RANGE_TYPE`.
RANGE_KINDS: Dict[int, str] = {0: 'srv', 1: 'uav', 2: 'cbv', 3: 'sampler'}

#: `D3D_ROOT_SIGNATURE_VERSION`.
ROOT_VERSIONS: Dict[int, str] = {1: '1.0', 2: '1.1', 3: '1.2'}

#: `D3D12_ROOT_SIGNATURE_FLAGS` bits worth naming, in bit order.
ROOT_FLAGS: List[Tuple[int, str]] = [(0x1, 'ia-layout'), (0x2, 'deny-vs'), (0x4, 'deny-hs'),
                                     (0x8, 'deny-ds'), (0x10, 'deny-gs'), (0x20, 'deny-ps'),
                                     (0x40, 'stream-out'), (0x80, 'local'), (0x100, 'deny-as'),
                                     (0x200, 'deny-ms'), (0x400, 'cbv-srv-uav-heap-indexed'),
                                     (0x800, 'sampler-heap-indexed')]

#: The register letter per binding kind, so a label reads the way the HLSL does (`t0`, `b1`, `u2`).
REGISTER_LETTERS: Dict[str, str] = {'cbv': 'b', 'srv': 't', 'uav': 'u', 'sampler': 's'}

#: `D3D_SHADER_INPUT_TYPE` (`dxbc_common.h` `ShaderInputBind::InputType`) -> the kind word used
#: everywhere else. A tbuffer is read like an SRV, and every UAV flavour is a UAV.
RDEF_KINDS: Dict[int, str] = {0: 'cbv', 1: 'srv', 2: 'srv', 3: 'sampler', 4: 'uav', 5: 'srv',
                              6: 'uav', 7: 'srv', 8: 'uav', 9: 'uav', 10: 'uav', 11: 'uav', 12: 'srv'}

#: `RDEFHeader.targetShaderStage` -> stage name (dxbc_container.cpp); anything else is unknown.
RDEF_STAGES: Dict[int, str] = {0xffff: 'ps', 0xfffe: 'vs', 0x4353: 'cs', 0x4753: 'gs', 0x4853: 'hs',
                               0x4453: 'ds'}

def _parse_root_signature(data: Buffer) -> Optional[RootSignature]:
    """Decode one `RTS0` part's data, or None when it does not fit the layout.

    Every offset in the blob is relative to `data`, and each one is checked against its length before
    it is followed: a truncated or corrupt signature decodes to None instead of reading garbage.
    """
    if len(data) < 24:
        return None
    version = u32(data, 0)
    num_params = u32(data, 4)
    param_off = u32(data, 8)
    num_samplers = u32(data, 12)
    if ROOT_VERSIONS.get(version) is None or num_params > 64 or param_off + num_params * 12 > len(data):
        return None
    params: List[RootParam] = []
    dwords = 0
    for i in range(num_params):
        base = param_off + i * 12
        kind = PARAM_KINDS.get(u32(data, base))
        visibility = VISIBILITIES.get(u32(data, base + 4), 'all')
        data_off = u32(data, base + 8)
        if kind is None or data_off + 8 > len(data):
            return None
        param = RootParam(kind=kind, visibility=visibility, register=0, space=0, count=0, ranges=[])
        if kind == '32bit':
            # D3D12_ROOT_CONSTANTS: ShaderRegister | RegisterSpace | Num32BitValues
            param['register'] = u32(data, data_off)
            param['space'] = u32(data, data_off + 4)
            param['count'] = u32(data, data_off + 8)
            dwords += param['count']
        elif kind == 'table':
            # a RootSigDescriptorTable: NumRanges | DataOffset, then the D3D12_DESCRIPTOR_RANGEs
            num_ranges, ranges_off = u32(data, data_off), u32(data, data_off + 4)
            stride = 24 if version >= 2 else 20
            if num_ranges > 64 or ranges_off + num_ranges * stride > len(data):
                return None
            for j in range(num_ranges):
                r = ranges_off + j * stride
                rkind = RANGE_KINDS.get(u32(data, r))
                if rkind is None:
                    return None
                param['ranges'].append(RootRange(kind=rkind, base=u32(data, r + 8),
                                                 count=u32(data, r + 4), space=u32(data, r + 12),
                                                 offset=u32(data, r + (20 if version >= 2 else 16))))
            dwords += 1
        else:
            # D3D12_ROOT_DESCRIPTOR1: ShaderRegister | RegisterSpace | Flags (1.0 has no flags)
            param['register'] = u32(data, data_off)
            param['space'] = u32(data, data_off + 4)
            dwords += 2
        params.append(param)
    return RootSignature(id=0, version=ROOT_VERSIONS[version], flags=u32(data, 20), dwords=dwords,
                         params=params, samplers=num_samplers)

@rdc_profile.timed('root signatures')
def parse_root_signatures(stream: Buffer,
                          names: Optional[Dict[int, str]] = None) -> Dict[int, RootSignature]:
    """Every root signature the capture creates, by the resource id that binds it.

    The payload is the serialiser's own framing around the blob, so the container is located by its
    `DXBC` magic rather than at a fixed offset, and cross-checked against the length field at +4 of
    the payload (they agree in every capture seen). The id is the last 8 bytes of the payload --
    not `length - 16` like a resource creation, because there is no GPU address after it.
    """
    if names is None:
        names = rdc_chunkmap.load_chunk_names()
    sigs: Dict[int, RootSignature] = {}
    for ch in iter_chunks(stream):
        if names.get(ch['id'], '') != 'Device_CreateRootSignature':
            continue
        blob = chunk_payload(stream, ch)
        i = blob.find(b'DXBC')
        if i < 0 or len(blob) < i + 32 or len(blob) < 8:
            continue
        size = u32(blob, i + 24)                       # the container's own declared size
        if i + size > len(blob) or u64(blob, 4) != size:
            continue
        part_count = u32(blob, i + 28)
        if part_count != 1:
            continue
        po = i + u32(blob, i + 32)
        if blob[po:po + 4] != b'RTS0':
            continue
        sig = _parse_root_signature(blob[po + 8:po + 8 + u32(blob, po + 4)])
        if sig is None:
            continue
        sig['id'] = u64(blob, len(blob) - 8)
        sigs[sig['id']] = sig
    return sigs

def parse_rdef(data: Buffer) -> List[ShaderBind]:
    """Decode the resource bindings of one `RDEF` part (`dxbc_container.cpp` `RDEFHeader`).

    Header: `cbuffers(u32 count, u32 offset) | resources(u32 count, u32 offset) | u16 targetVersion
    | u16 targetShaderStage | ...`, every offset relative to the part's data start. A resource entry
    is `nameOffset | type | retType | dimension | sampleCount | bindPoint | bindCount | flags | space
    | ID` -- 40 bytes, or 32 before shader model 5.1, which has no space or ID.

    `bindPoint` is read as the *register* (`desc.reg = res->bindPoint` in the source), so it is the
    same number a root signature's descriptors and ranges use, which is what makes the mapping
    possible. **No capture in this repo has an `RDEF` part** (all of them are DXIL with the
    reflection stripped), so this half of the item is source-derived rather than capture-verified --
    REFERENCE 8 says so.
    """
    if len(data) < 20:
        return []
    res_count, res_off = u32(data, 8), u32(data, 12)
    target_version = u16(data, 16)
    stride = 40 if target_version >= 0x501 else 32
    if res_count > 4096 or res_off + res_count * stride > len(data):
        return []
    binds: List[ShaderBind] = []
    for i in range(res_count):
        e = res_off + i * stride
        name_off = u32(data, e)
        kind = RDEF_KINDS.get(u32(data, e + 4))
        if kind is None or name_off >= len(data):
            continue
        end = data.find(b'\x00', name_off)
        binds.append(ShaderBind(name=data[name_off:end if end >= 0 else len(data)]
                                .decode('utf-8', 'replace'), kind=kind, register=u32(data, e + 20),
                                space=u32(data, e + 32) if stride == 40 else 0,
                                count=u32(data, e + 24)))
    return binds

@rdc_profile.timed('shader bind names')
def shader_bind_names(stream: Buffer,
                      source: Optional[CacheEntry] = None) -> Dict[str, Dict[Tuple[str, int, int], str]]:
    """`stage -> (kind, register, space) -> name` for every `RDEF` the capture still has.

    This is the only source of root-parameter *names* in a capture: the root signature
    itself has none, so a name can only come from what a shader says about its bindings. Keyed by
    stage because the same slot may be named differently in different stages, and the lookup in
    `_root_param_label` refuses to pick one when they disagree.

    `source` is the stream-cache entry, passed down to the container search so its whole-stream `find`
    can be split across processes (`rdc_scan.find_all`); without it the search is the serial loop, and
    nothing is cached. With it the answer is written beside the stream cache entry (`_load_bind_names`)
    and reused, because the question is about the *stream* and not about the command: the find is 0.30 s
    of `desktop-1`'s 0.51 s `draws` and 0.52 s of `desktop-2`'s 0.85 s, it is paid again by `rootsig`,
    `dxbc` and both sides of `diff`, and on all three real captures the answer is the same empty dict --
    their DXIL has no `RDEF` left. The empty answer is worth caching for exactly that reason.
    """
    if source is not None:
        cached = _load_bind_names(stream, source)
        if cached is not None:
            return cached
    out: Dict[str, Dict[Tuple[str, int, int], str]] = {}
    for _off, _size, _hash, parts in parse_dxil_containers(stream, source):
        part = next((p for p in parts if p[0] == 'RDEF'), None)
        if part is None:
            continue
        data = stream[part[1]:part[1] + part[2]]
        stage = RDEF_STAGES.get(u16(data, 18), '?') if len(data) >= 20 else '?'
        for bind in parse_rdef(data):
            out.setdefault(stage, {})[(bind['kind'], bind['register'], bind['space'])] = bind['name']
    if source is not None:
        _store_bind_names(stream, source, out)
    return out

#: The sidecar's name: the stream cache file's stem with this suffix (`rdc_cache.sidecar_path`), and it
#: must be one of `rdc_cache.DERIVED_SUFFIXES` -- a test says so -- or `cache clear` would leave it.
BIND_NAMES_SUFFIX = '.bindnames.json'
#: The sidecar's own version: bumped when what is stored (or how it is keyed) changes, so an older file
#: is ignored rather than half-read. The cache-version rule of the stream cache, one level down.
BIND_NAMES_VERSION = 1
#: How much of a stream a sidecar's identity covers, at each end. Hashing 1.5 GB per command would cost
#: more than the scan it saves; 64 KB at each end plus the stream cache's own name (path, size, mtime,
#: section and cache version hashed into it, REFERENCE 4.8) is what makes a stale answer unreadable.
BIND_NAMES_SAMPLE = 64 << 10

def _stream_digest(stream: Buffer) -> str:
    """`sha256` of the stream's first and last `BIND_NAMES_SAMPLE` bytes (the whole stream if shorter)."""
    import hashlib
    if len(stream) <= 2 * BIND_NAMES_SAMPLE:
        return hashlib.sha256(bytes(stream)).hexdigest()
    digest = hashlib.sha256(bytes(stream[:BIND_NAMES_SAMPLE]))
    digest.update(bytes(stream[len(stream) - BIND_NAMES_SAMPLE:]))
    return digest.hexdigest()

def _load_bind_names(stream: Buffer,
                     source: CacheEntry) -> Optional[Dict[str, Dict[Tuple[str, int, int], str]]]:
    """The names from `source`'s sidecar, or None when there is none for *this* stream.

    Every refusal falls back to the scan: no file (`$RDC_NO_CACHE`, a first run, a cache directory that
    cannot be written), a version or a stream digest that is not this one, or a file that is not the
    document this writes. A cache can only ever save work, so a broken one is never an error.
    """
    import json
    try:
        with open(rdc_cache.sidecar_path(source, BIND_NAMES_SUFFIX), encoding='utf-8') as fh:
            document = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get('version') != BIND_NAMES_VERSION:
        return None
    if document.get('streamLen') != len(stream) or document.get('digest') != _stream_digest(stream):
        return None
    rows = document.get('names')
    if not isinstance(rows, list):
        return None
    out: Dict[str, Dict[Tuple[str, int, int], str]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 5:
            return None
        stage, kind, register, space, name = row
        out.setdefault(str(stage), {})[(str(kind), int(register), int(space))] = str(name)
    return out

def _store_bind_names(stream: Buffer, source: CacheEntry,
                      names: Dict[str, Dict[Tuple[str, int, int], str]]) -> None:
    """Write the sidecar: a temporary name renamed into place, and never an error if it fails.

    The rows are `[stage, kind, register, space, name]`, because a JSON object cannot key on the tuple
    the lookup uses and a joined string key would be one more thing to get wrong. Sorted, so two runs of
    two different commands write byte-identical files.
    """
    import json
    path = rdc_cache.sidecar_path(source, BIND_NAMES_SUFFIX)
    rows = [[stage, kind, register, space, name]
            for stage, binds in sorted(names.items())
            for (kind, register, space), name in sorted(binds.items())]
    document = {'version': BIND_NAMES_VERSION, 'streamLen': len(stream),
                'digest': _stream_digest(stream), 'names': rows}
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(document, fh, separators=(',', ':'), sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass

def _bind_name(binds: Dict[str, Dict[Tuple[str, int, int], str]], param: RootParam) -> str:
    """The name `RDEF` reflection gives this root parameter, or '' when there is none to trust.

    A parameter restricted to one stage takes that stage's name; one visible everywhere takes a name
    only when every stage that binds that slot agrees on it, so a disagreement shows as no name
    rather than as a coin flip.
    """
    key = (param['kind'], param['register'], param['space'])
    if param['kind'] in ('table', '32bit'):
        return ''
    if param['visibility'] != 'all':
        return binds.get(param['visibility'], {}).get(key, '')
    names = {m.get(key, '') for m in binds.values()} - {''}
    return names.pop() if len(names) == 1 else ''

def _root_param_what(param: RootParam) -> str:
    """What a parameter *is*, without its index: `cbv b1 s0`, `table t0 n5 s0`, `32bit b0 s0 n4`.

    A table lists its ranges as HLSL would name them (`table t0 n5 s0`; `u0 n3 s0` for the UAV range of
    the same table), where the letter is the register kind and `n` the descriptor count; a root
    descriptor and root constants get their register and space.

    Split out of `_root_param_label` because the index is the one part of a label two captures do not
    share: `rdc_filediff` keys a binding by what it *is*, so a slot that moved from `rp10` to `rp5`
    between two builds of the same scene still compares as the same binding, while a slot whose kind,
    register or space changed does not.
    """
    if param['kind'] == 'table':
        return 'table ' + ', '.join(
            '%s%d n%s s%d' % (REGISTER_LETTERS.get(r['kind'], '?'), r['base'],
                              'unbounded' if r['count'] == 0xffffffff else r['count'], r['space'])
            for r in param['ranges'])
    if param['kind'] == '32bit':
        return '32bit b%d s%d n%d' % (param['register'], param['space'], param['count'])
    return '%s %s%d s%d' % (param['kind'], REGISTER_LETTERS.get(param['kind'], '?'),
                            param['register'], param['space'])

def _root_param_label(sig: Optional[RootSignature],
                      binds: Dict[str, Dict[Tuple[str, int, int], str]], rp: int) -> str:
    """`rp2(cbv b1 s0)` -- the index `draws` prints, plus what the signature says it holds.

    When `RDEF` reflection named the binding the name is appended (`rp2(cbv b1 s0) [SceneCB]`), which
    is the mapping this item is about; when the capture has no reflection -- every DXIL capture seen so
    far -- the type, register and space still say what the parameter is, which the bare index did not.
    """
    if sig is None or not (0 <= rp < len(sig['params'])):
        return 'rp%d' % rp
    param = sig['params'][rp]
    vis = '' if param['visibility'] == 'all' else param['visibility'] + ' '
    name = _bind_name(binds, param)
    return 'rp%d(%s%s)%s' % (rp, vis, _root_param_what(param), ' [%s]' % name if name else '')

def _resource_size(info: ResourceInfo, formats: Dict[int, str]) -> str:
    """The size column of `resources`: bytes for a buffer or an AS, dimensions + format for a texture."""
    if info['kind'] in ('buffer', 'blas', 'tlas'):
        return '%d B' % info['size']
    if info['kind'] == 'unknown':
        return '-'
    return '%dx%dx%d mips=%d fmt=%s' % (info['width'], info['height'], info['depth'], info['mips'],
                                        formats.get(info['format'], str(info['format'])))

def _name_suffix(table: Dict[int, ResourceInfo], rid: int, width: int = 24) -> str:
    """`[Name]` for a resource the capture named, else '' (used by `draws`, truncated to `width`)."""
    entry = table.get(rid)
    if entry is None or not entry['name']:
        return ''
    return '[%s]' % entry['name'][:width]

__all__ = [
    'BIND_NAMES_SAMPLE',
    'BIND_NAMES_SUFFIX',
    'BIND_NAMES_VERSION',
    'PARAM_KINDS',
    'RANGE_KINDS',
    'RDEF_KINDS',
    'RDEF_STAGES',
    'REGISTER_LETTERS',
    'ROOT_FLAGS',
    'ROOT_VERSIONS',
    'VISIBILITIES',
    '_bind_name',
    '_descriptor_label',
    '_name_suffix',
    '_parse_acceleration_structure',
    '_parse_resource',
    '_parse_root_signature',
    '_portable_handle',
    '_resource_size',
    '_root_param_label',
    '_root_param_what',
    'apply_descriptor_chunk',
    'load_format_names',
    'parse_descriptor_heaps',
    'parse_rdef',
    'parse_resource_table',
    'parse_root_signatures',
    'shader_bind_names',
]
