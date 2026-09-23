"""Synthetic fixtures for the rdc_analysis.py test-suite.

Everything here is built in memory from the layouts documented in the tool itself and in the
RenderDoc source, so the tests need no .rdc capture, no GPU, no renderdoc.pyd and no
`renderdoc-src` checkout:

  * container      -> renderdoc/serialise/rdcfile.cpp  (FileHeader | thumbnail | metadata | sections)
  * chunk framing  -> renderdoc/serialise/serialiser.cpp (Serialiser<Reading>::BeginChunk)
  * chunk payloads -> REFERENCE section 3.4 (the table the decoders were written against)

The fixture builders deliberately do **not** import the module under test except for
`parse_chunk_enum`/`load_chunk_names`, which are used to build the fake `renderdoc-src` tree, so a
bug in a decoder cannot silently reshape the data the tests feed it.
"""
from __future__ import annotations

import os
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple

import rdc_analysis as R

# --------------------------------------------------------------------------- packers
FLAG_CALLSTACK = 0x00010000
FLAG_THREADID = 0x00020000
FLAG_DURATION = 0x00040000
FLAG_TIMESTAMP = 0x00080000
FLAG_64BITSIZE = 0x00100000
ALIGN = 64
SECTION_FRAMECAPTURE = 1  # SectionType::FrameCapture
FLAG_LZ4 = 0x2


def u16b(v: int) -> bytes:
    return struct.pack('<H', v)


def u32b(v: int) -> bytes:
    return struct.pack('<I', v)


def u64b(v: int) -> bytes:
    return struct.pack('<Q', v)


# --------------------------------------------------------------------------- SDChunk stream
def pad_to(data: bytes, align: int = ALIGN, pad_byte: int = 0xCC) -> bytes:
    """Pad to a multiple of `align` the way the serialiser does (padding is *not* zeroed)."""
    rem = len(data) % align
    return data if rem == 0 else data + bytes([pad_byte]) * (align - rem)


def chunk(cid: int, payload: bytes = b'', callstack: Optional[Sequence[int]] = None,
          threadid: Optional[int] = None, duration: Optional[int] = None,
          timestamp: Optional[int] = None, extra_flags: int = 0, size64: bool = False,
          align: bool = True, pad_byte: int = 0xCC) -> bytes:
    """One SDChunk. Flags are derived from the fields exactly as the writer does."""
    c = cid & 0xFFFF
    meta = b''
    if callstack:
        c |= FLAG_CALLSTACK
        meta += u32b(len(callstack)) + b''.join(u64b(f) for f in callstack)
    if threadid:
        c |= FLAG_THREADID
        meta += u64b(threadid)
    if duration is not None:
        c |= FLAG_DURATION
        meta += u64b(duration)
    if timestamp is not None:
        c |= FLAG_TIMESTAMP
        meta += u64b(timestamp)
    c |= extra_flags & 0xFFFF0000
    if size64:
        c |= FLAG_64BITSIZE
        size = u64b(len(payload))
    else:
        size = u32b(len(payload))
    out = u32b(c) + meta + size + payload
    return pad_to(out, pad_byte=pad_byte) if align else out


def stream(*chunks: bytes, terminator: bool = False, tail: bytes = b'') -> bytes:
    out = b''.join(chunks)
    if terminator:
        out += u32b(0)
    return out + tail


# --------------------------------------------------------------------------- container
def section(name: str, data: bytes, sec_type: int = SECTION_FRAMECAPTURE, flags: int = 0,
            version: int = 1, comp_len: Optional[int] = None, uncomp_len: Optional[int] = None,
            name_len: Optional[int] = None, name_bytes: Optional[bytes] = None) -> bytes:
    """BinarySectionHeader(40) + name (NUL terminated) + data."""
    name_b = name.encode('ascii') + b'\x00' if name_bytes is None else name_bytes
    nlen = len(name_b) if name_len is None else name_len
    hdr = (u32b(0) + u32b(sec_type) + u64b(len(data) if comp_len is None else comp_len)
           + u64b(len(data) if uncomp_len is None else uncomp_len) + u64b(version) + u32b(flags)
           + u32b(nlen))
    assert len(hdr) == 40
    return hdr + name_b + data


def rdc(sections: Sequence[bytes], magic: bytes = b'RDOC', version: int = 0x10E,
        prog: str = '1.46', thumb: bytes = b'\xff\xd8THUMB\xff\xd9', driver_id: int = 4,
        driver_name: str = 'D3D12', time_base: int = 123456, time_freq: float = 1000000.0,
        thumb_w: int = 64, thumb_h: int = 64, thumb_len: Optional[int] = None,
        header_length: Optional[int] = None, header_pad: int = 0,
        end: bytes = b'\x01\x00\x00\x00') -> bytes:
    """FileHeader(32) | thumbnail | CaptureMetaData | CaptureTimeBase | sections | end marker."""
    name_b = driver_name.encode('ascii') + b'\x00'
    meta = b'\x00' * 8 + u32b(driver_id) + bytes([len(name_b)]) + name_b
    tbase = u64b(time_base) + struct.pack('<d', time_freq)
    computed = 40 + len(thumb) + len(meta) + len(tbase) + header_pad
    hl = computed if header_length is None else header_length
    head = (magic + u32b(0) + u32b(version) + u32b(hl)
            + prog.encode('ascii').ljust(16, b'\x00')
            + u16b(thumb_w) + u16b(thumb_h)
            + u32b(len(thumb) if thumb_len is None else thumb_len) + thumb + meta + tbase)
    return head + b'\x00' * header_pad + b''.join(sections) + end


# --------------------------------------------------------------------------- LZ4
def lz4_literal_block(data: bytes) -> bytes:
    """One raw LZ4 block holding `data` as a single literal-only sequence.

    A literal-only sequence is only legal as the *last* sequence of a block, so each block produced
    here holds exactly one - matches would have to reproduce the input exactly, which is not worth
    implementing for a fixture.
    """
    n = len(data)
    if n < 15:
        head = bytes([n << 4])
    else:
        ext, rest = bytearray(), n - 15
        while rest >= 255:
            ext.append(255)
            rest -= 255
        ext.append(rest)
        head = bytes([0xF0]) + bytes(ext)
    return head + data


def lz4_match_block(literals: bytes, offset: int, match_len: int, tail: bytes) -> bytes:
    """One raw LZ4 block: a literal run, a match, and the literal run a block must end with.

    The shape is dictated by what an LZ4 *decoder* accepts, measured against the vendored one: the
    stored match nibble cannot be 0 (`match_len` at least 5) and a block must end with at least 5
    literals (`LASTLITERALS`). A block that gets either wrong is refused rather than decoded
    differently, which is why hand-writing one is a trap worth this function. Both runs are capped at
    14 bytes so no length extension is needed, and `offset` may reach back into the *previous* block --
    which is the case worth building: the pages of a section are one continuous stream.
    """
    if not 5 <= match_len <= 18:
        raise ValueError('match_len must be 5..18 (a stored nibble of 0 is refused, 15 means extend)')
    if len(literals) > 14 or not 5 <= len(tail) <= 14:
        raise ValueError('literals and tail must be 0..14 and 5..14 bytes (no length extensions here)')
    if not 1 <= offset <= 0xFFFF:
        raise ValueError('offset must be 1..65535')
    out = bytearray([(len(literals) << 4) | (match_len - 4)])
    out += literals
    out += bytes([offset & 0xFF, offset >> 8])
    out.append(len(tail) << 4)
    out += tail
    return bytes(out)

def lz4_container(data: bytes, block_count: int = 1) -> bytes:
    """A `flags & 0x2` section body: [u32 compressedBlockLength][raw LZ4 block] repeated."""
    if not data:
        return b''
    per = len(data) if block_count <= 1 else (len(data) + block_count - 1) // block_count
    out = bytearray()
    for i in range(0, len(data), per):
        blk = lz4_literal_block(data[i:i + per])
        out += u32b(len(blk)) + blk
    return bytes(out)


# --------------------------------------------------------------------------- captures
def capture(chunks: Sequence[bytes], lz4: bool = False, block_count: int = 1,
            name: str = 'FrameCapture', **kw: Any) -> bytes:
    """A whole .rdc file with one frame-capture section holding `chunks`.

    `uncompLen` is the *decompressed* size, as RenderDoc writes it -- the compressed body is
    `compLen`. (Without the explicit `uncomp_len` the section builder would record the compressed
    length for both, which is not what a real capture does and is what `cache_store` validates.)
    """
    data = b''.join(chunks)
    if lz4:
        sec = section(name, lz4_container(data, block_count), flags=FLAG_LZ4, uncomp_len=len(data))
    else:
        sec = section(name, data)
    return rdc([sec], **kw)


def write_bytes(path: str, data: bytes) -> str:
    with open(path, 'wb') as f:
        f.write(data)
    return path


def write_capture(path: str, chunks: Sequence[bytes], **kw: Any) -> str:
    return write_bytes(path, capture(chunks, **kw))


# --------------------------------------------------------------------------- D3D12 payloads
# Layouts follow REFERENCE section 3.4 ("Payload decoding").
#: One vertex-buffer view as the payload builder wants it: (resourceId, offset, size, stride).
VertexView = Tuple[int, int, int, int]


def pl_pso(cmdlist: int, pso: int) -> bytes:
    return u64b(cmdlist) + u64b(pso)


def pl_draw_indexed(cmdlist: int, index_count: int, instance_count: int, start_index: int,
                    base_vertex: int, start_instance: int) -> bytes:
    return (u64b(cmdlist) + u32b(index_count) + u32b(instance_count) + u32b(start_index)
            + u32b(base_vertex) + u32b(start_instance))


def pl_draw_instanced(cmdlist: int, vertex_count: int, instance_count: int, start_vertex: int,
                      start_instance: int) -> bytes:
    return (u64b(cmdlist) + u32b(vertex_count) + u32b(instance_count) + u32b(start_vertex)
            + u32b(start_instance))


def pl_dispatch(cmdlist: int, x: int, y: int, z: int) -> bytes:
    return u64b(cmdlist) + u32b(x) + u32b(y) + u32b(z)


def pl_root_view(cmdlist: int, root_param: int, resid: int, offset: int) -> bytes:
    """CBV/SRV/UAV: cmdList | RootParameterIndex | D3D12BufferLocation(resId, offset)."""
    return u64b(cmdlist) + u32b(root_param) + u64b(resid) + u64b(offset)


def pl_root_table(cmdlist: int, root_param: int, heap: int, index: int) -> bytes:
    """cmdList | rootParam | PortableHandle(heapId u64, descriptorIndex u32) = 24 bytes.

    A `D3D12_GPU_DESCRIPTOR_HANDLE` serialises as a `PortableHandle` (d3d12_manager.h), not as a
    raw pointer.
    """
    return u64b(cmdlist) + u32b(root_param) + u64b(heap) + u32b(index)


def pl_root_signature(cmdlist: int, rootsig: int) -> bytes:
    return u64b(cmdlist) + u64b(rootsig)


def pl_descriptor_write(resource: int, heap: int, index: int, length: int = 68,
                        view_format: int = 0) -> bytes:
    """A `Device_Create*View` payload: the resource id at +16, the view's format at +24, the handle last.

    The tool reads the resource, the format (for an SRV/UAV/RTV, whose serialised description opens
    with `DXGI_FORMAT Format;`) and the destination handle; the descriptor *kind* comes from the chunk
    name. `length` matches what the captures carry for an SRV (68); a UAV payload is 80 in the
    captures, which is why the length is a parameter. `view_format` is a `DXGI_FORMAT` *id* -- 29 is
    `R8G8B8A8_UNORM_SRGB` in the bundled table, 28 the linear form -- and the rest of the description
    stays zeros.
    """
    head = b'\x00' * 16 + u64b(resource) + u32b(view_format)
    return head + b'\x00' * (length - 40) + u64b(heap) + u32b(index)


def pl_copy_descriptors(entries: Sequence[Tuple[int, int, int, int]],
                        heap_type: int = 0) -> bytes:
    """`Device_CopyDescriptors*`: u64 count, then per entry `(dstHeap, dstIndex, srcHeap, srcIndex)`.

    Each entry is `u32 heapType` followed by the destination and source `PortableHandle`s (28 bytes).
    """
    out = u64b(len(entries))
    for dst_heap, dst_index, src_heap, src_index in entries:
        out += (u32b(heap_type) + u64b(dst_heap) + u32b(dst_index) + u64b(src_heap)
                + u32b(src_index))
    return out


def pl_descriptor_heap(heap: int, heap_type: int = 0, num_descriptors: int = 1000000) -> bytes:
    """`Device_CreateDescriptorHeap`: the desc, the IID, then the heap id at `length - 16` (56 B)."""
    return (u32b(heap_type) + u32b(num_descriptors) + b'\x00' * 24 + b'\x00' * 8 + u64b(heap)
            + u64b(0))


def pl_set_name(resid: int, name: str) -> bytes:
    """`SetName`: [u64 resourceId][u32 length][utf-8 name]."""
    raw = name.encode('utf-8')
    return u64b(resid) + u32b(len(raw)) + raw


def pl_resource_desc(dimension: int = 1, width: int = 1024, height: int = 1, depth: int = 1,
                     mips: int = 1, fmt: int = 0, alignment: int = 0) -> bytes:
    """A `D3D12_RESOURCE_DESC` (48 bytes).

    Dimension is a `D3D10_RESOURCE_DIMENSION` (1 = buffer, 2/3/4 = texture1d/2d/3d); for a buffer
    `width` is the size in bytes, for a texture `depth` doubles as the array size.
    """
    return (u32b(dimension) + u64b(alignment) + u64b(width) + u32b(height) + u16b(depth)
            + u16b(mips) + u32b(fmt) + u32b(1) + u32b(0) + u32b(0) + u32b(0))


def _creation_tail(resid: int, gpu_address: int = 0) -> bytes:
    """The end of a resource-creation payload: state(4) | clear-value flag(1) | IID(16) | id | VA.

    Real payloads carry 8 further bytes of IID/padding before the id, which nothing reads; the
    fixture keeps them so the lengths match the captures exactly (117 committed, 109 placed, 93
    reserved) -- the parser reads the descriptor and the id at `len - 16` and nothing else.
    """
    return u32b(0) + b'\x00' + b'\x00' * 16 + b'\x00' * 8 + u64b(resid) + u64b(gpu_address)


def pl_committed_resource(resid: int, desc: bytes, gpu_address: int = 0) -> bytes:
    """`Device_CreateCommittedResource`: heap props(20) | heap flags(4) | desc | tail (117 bytes)."""
    return b'\x02' + b'\x00' * 19 + u32b(0) + desc + _creation_tail(resid, gpu_address)


def pl_placed_resource(resid: int, desc: bytes, heap: int = 1, heap_offset: int = 0,
                       gpu_address: int = 0) -> bytes:
    """`Device_CreatePlacedResource`: heap(8) | heap offset(8) | desc | tail (109 bytes)."""
    return u64b(heap) + u64b(heap_offset) + desc + _creation_tail(resid, gpu_address)


def pl_committed_resource3(resid: int, desc: bytes, gpu_address: int = 0) -> bytes:
    """`Device_CreateCommittedResource3`: heap props | flags | `D3D12_RESOURCE_DESC1` | tail (149 B).

    The descriptor is 12 bytes longer than in the base form and the castable-format list follows it.
    The tool reads the first 48 bytes of the descriptor -- which both structs share -- and the id at
    `length - 16`, so those 32 extra bytes only have to be *there*, which is why they are filler.
    """
    return b'\x02' + b'\x00' * 19 + u32b(0) + desc + b'\x00' * 32 + _creation_tail(resid, gpu_address)


def pl_create_as(asid: int, buffer: int, offset: int = 0, as_type: int = 0,
                 byte_size: int = 4194304) -> bytes:
    """`CreateAS`: u64 buffer, u64 offset, u32 type (0 = top level, 1 = bottom level), u64 byteSize,
    u64 asId -- an acceleration structure is a sub-range of a buffer, not a resource."""
    return u64b(buffer) + u64b(offset) + u32b(as_type) + u64b(byte_size) + u64b(asid)


def pl_reserved_resource(resid: int, desc: bytes, gpu_address: int = 0) -> bytes:
    """`Device_CreateReservedResource`: desc | tail (93 bytes)."""
    return desc + _creation_tail(resid, gpu_address)


def pl_reset(cmdlist: int, initial_pso: int = 0) -> bytes:
    """`List_Reset`: 64 bytes, with the command-list id at +40 and the initial PSO at +48.

    The first 40 bytes are the list's creation parameters (IID, node mask, type, baked list id),
    none of which the tool needs. The id at +40 is the one the other `List_*` chunks carry at +0 --
    measured on both captures in this repo (170/170 setter chunks matched in the PC capture), and
    pinned by `verify` through `EXPECTED_LENGTHS`.
    """
    return b'\x00' * 40 + u64b(cmdlist) + u64b(initial_pso) + u64b(0)


def pl_vertex_buffers(cmdlist: int, start_slot: int,
                      views: Sequence[VertexView]) -> bytes:
    """cmdList | startSlot | numViews | arrayCount(u64) | per view: resId, offset, size, stride."""
    out = u64b(cmdlist) + u32b(start_slot) + u32b(len(views)) + u64b(len(views))
    for resid, offset, size, stride in views:
        out += u64b(resid) + u64b(offset) + u32b(size) + u32b(stride)
    return out


def pl_index_buffer(cmdlist: int, resid: int, offset: int, size: int, fmt: int,
                    present: bool = True) -> bytes:
    """cmdList | present(u8) | resId | offset | size | format.

    The view is serialised through `SERIALISE_ELEMENT_OPT`, so a "present" bool comes first and the
    payload is 33 bytes (9 when the view is null).
    """
    out = u64b(cmdlist) + bytes([1 if present else 0])
    if present:
        out += u64b(resid) + u64b(offset) + u32b(size) + u32b(fmt)
    return out


def pl_create_pso(pso_id: int, tail: bytes = b'\xAB\xCD' * 16) -> bytes:
    """The minimal form: the id then whatever bytes. `decode_chunk`'s test pins its preview's tail with
    this, so it stays deliberately unfaithful to the real layout -- a payload that says what it is fed and
    nothing else. `pl_create_pso_stages` below is the one that models the engine's framing.
    """
    return u64b(pso_id) + tail


def pl_create_heap(heap_id: int, size: int, heap_type: int = 1, flags: int = 0x1000) -> bytes:
    """`Device_CreateHeap`: the `D3D12_HEAP_DESC` (40 B) | IID | heap id -- 72 bytes in all.

    The IID is `Data1(4) | Data2(2) | Data3(2) | Data4[8]`, and the array serialises with its own
    `u64` count in front of it, which is why the payload is 72 bytes rather than the 64 the struct
    sizes suggest. Measured on all 31 `Device_CreateHeap` payloads of the two captures: every one is
    72 bytes with the size at +0 and the heap id at `length - 8`.
    """
    desc = (u64b(size) + u32b(heap_type) + u32b(0) + u32b(0) + u32b(1) + u32b(1) + u64b(0)
            + u32b(flags))
    return (desc + u32b(0x6b3b2502) + u16b(0x6e51) + u16b(0x45b3) + u64b(8) + u64b(0x90EE9884265E8DF3)
            + u64b(heap_id))


def pl_resources_barrier(cmdlist: int, entries: Sequence[Tuple[Any, ...]]) -> bytes:
    """`List_ResourceBarrier`: cmdList | count | arrayCount | entries.

    An entry is `('transition', resource, subresource, before, after)`, `('aliasing', from, to)` or
    `('uav', resource)` -- the three arms of `D3D12_RESOURCE_BARRIER`, 28/24/16 bytes each.
    """
    out = u64b(cmdlist) + u32b(len(entries)) + u64b(len(entries))
    for entry in entries:
        if entry[0] == 'transition':
            out += (u32b(0) + u32b(0) + u64b(entry[1]) + u32b(entry[2]) + u32b(entry[3])
                    + u32b(entry[4]))
        elif entry[0] == 'aliasing':
            out += u32b(1) + u32b(0) + u64b(entry[1]) + u64b(entry[2])
        else:
            out += u32b(2) + u32b(0) + u64b(entry[1])
    return out


def pl_barrier_groups(cmdlist: int, groups: Sequence[Tuple[str, Sequence[Tuple[Any, ...]]]]) -> bytes:
    """`List_Barrier`: cmdList | groupCount | arrayCount | groups.

    A group is `('texture', [(resource, access, layout, flags), ...])`,
    `('buffer', [(resource, access), ...])` or `('global', [(access,), ...])`; the elements are
    60/40/16 bytes, and the group header carries the count twice (u32, then u64).
    """
    out = u64b(cmdlist) + u32b(len(groups)) + u64b(len(groups))
    for kind, elements in groups:
        out += (u32b({'global': 0, 'texture': 1, 'buffer': 2}[kind]) + u32b(len(elements))
                + u64b(len(elements)))
        for entry in elements:
            if kind == 'texture':
                resource, access, layout, flags = entry
                out += (u32b(0) + u32b(0) + u32b(0) + u32b(access) + u32b(0) + u32b(layout)
                        + u64b(resource) + b'\x00' * 24 + u32b(flags))
            elif kind == 'buffer':
                resource, access = entry
                out += (u32b(0) + u32b(0) + u32b(0) + u32b(access) + u64b(resource) + u64b(0)
                        + u64b(0))
            else:
                out += u32b(0) + u32b(0) + u32b(0) + u32b(entry[0])
    return out


def pl_rtv_descriptor(resource: int, heap: int = 1, index: int = 0, dimension: int = 4,
                      fmt: int = 28) -> bytes:
    """One serialised RTV `D3D12Descriptor`, as `List_OMSetRenderTargets` writes them.

    `u32 type (0x1003) | u64 heap | u32 index | u64 resource | u32 format | u32 dimension | the arm
    the dimension selects`: TEXTURE2D (4) is an 8-byte arm (a mip slice and a plane slice), BUFFER
    (1) is 12, TEXTURE2DMS (6) is empty.
    """
    arm = {0: 0, 1: 12, 2: 4, 3: 12, 4: 8, 5: 16, 6: 0, 7: 8, 8: 12}[dimension]
    return (u32b(0x1003) + u64b(heap) + u32b(index) + u64b(resource) + u32b(fmt) + u32b(dimension)
            + b'\x00' * arm)


def pl_dsv_descriptor(resource: int, heap: int = 1, index: int = 0, dimension: int = 3,
                      fmt: int = 20) -> bytes:
    """One serialised DSV `D3D12Descriptor`: a DSV description carries flags as well as the format."""
    arm = {0: 0, 1: 4, 2: 12, 3: 4, 4: 12, 5: 0, 6: 8}[dimension]
    return (u32b(0x1004) + u64b(heap) + u32b(index) + u64b(resource) + u32b(fmt) + u32b(0)
            + u32b(dimension) + b'\x00' * arm)


def pl_omset(cmdlist: int, rtvs: Sequence[int], dsv: int = 0) -> bytes:
    """`List_OMSetRenderTargets`: cmdList | count(u32) | arrayCount(u64) | RTVs | present | [DSV]."""
    out = u64b(cmdlist) + u32b(len(rtvs)) + u64b(len(rtvs))
    for resource in rtvs:
        out += pl_rtv_descriptor(resource)
    return out + (bytes([1]) + pl_dsv_descriptor(dsv) if dsv else bytes([0]))


def pl_clear(cmdlist: int, resource: int, kind: str = 'rtv') -> bytes:
    """A `List_Clear*View` payload with the part the tool reads: the descriptor and its resource.

    An RTV/DSV clear serialises its descriptor first (id at +24); a UAV clear puts a `PortableHandle`
    in front of it (id at +36). What follows the descriptor -- the clear value, the rect count, the
    rect array -- is not decoded, so the fixture carries a short tail of zeroes.
    """
    if kind == 'uav':
        return (u64b(cmdlist) + u64b(1) + u32b(0) + u32b(0x1002) + u64b(2) + u32b(0)
                + u64b(resource) + u32b(0) + u32b(0) + b'\x00' * 64)
    desc = pl_dsv_descriptor(resource) if kind == 'dsv' else pl_rtv_descriptor(resource)
    return u64b(cmdlist) + desc + b'\x00' * 40


def pl_discard(cmdlist: int, resource: int) -> bytes:
    """`List_DiscardResource`: cmdList | resource | OPT(region) -- the region's shape is not read."""
    return u64b(cmdlist) + u64b(resource) + b'\x00' * 13


def pl_copy_buffer(cmdlist: int, dst: int, dst_offset: int, src: int, src_offset: int,
                   num_bytes: int) -> bytes:
    """`List_CopyBufferRegion` -- 48 bytes, which is what `EXPECTED_LENGTHS` checks it against."""
    return (u64b(cmdlist) + u64b(dst) + u64b(dst_offset) + u64b(src) + u64b(src_offset)
            + u64b(num_bytes))


def pl_copy_texture(cmdlist: int, dst: int, src: int, dst_sub: int = 0, src_sub: int = 0) -> bytes:
    """`List_CopyTextureRegion`: cmdList | dst location | X, Y, Z | src location | box.

    Both locations take the SUBRESOURCE_INDEX arm (type **0**, one `UINT`, so 16 bytes) and the box
    is present (a 1-byte flag then 24 bytes of coordinates). All five payloads in the captures here
    are 77 bytes with this shape.
    """
    loc = lambda res, sub: u64b(res) + u32b(0) + u32b(sub)
    return (u64b(cmdlist) + loc(dst, dst_sub) + u32b(0) * 3 + loc(src, src_sub) + bytes([1])
            + u32b(0) * 3 + u32b(1) * 3)


def pl_resolve(cmdlist: int, dst: int, src: int, dst_sub: int = 0, src_sub: int = 0,
               fmt: int = 0) -> bytes:
    """`List_ResolveSubresource`: cmdList | dst | dstSubresource | src | srcSubresource | format.

    36 bytes, which is what `EXPECTED_LENGTHS` checks it against. `fmt` is a `DXGI_FORMAT` *id* -- the
    payload carries the number, not a name.
    """
    return u64b(cmdlist) + u64b(dst) + u32b(dst_sub) + u64b(src) + u32b(src_sub) + u32b(fmt)


def pl_resolve_region(cmdlist: int, dst: int, src: int, dst_sub: int = 0, src_sub: int = 0,
                      fmt: int = 0, rect: bool = False, mode: int = 0) -> bytes:
    """`List_ResolveSubresourceRegion`: the same pair, a different order and an optional rect.

    After the destination subresource come the destination offset (X, Y), then the source resource and
    its subresource, then the source rect as a present byte plus four coordinates, then the format and
    the resolve mode -- so the payload is 49 bytes without a rect and 65 with one.
    """
    body = u64b(cmdlist) + u64b(dst) + u32b(dst_sub) + u32b(0) + u32b(0) + u64b(src) + u32b(src_sub)
    return body + (bytes([1]) + u32b(0) * 4 if rect else bytes([0])) + u32b(fmt) + u32b(mode)


# --------------------------------------------------------------------------- root signatures
#: `D3D12_ROOT_PARAMETER_TYPE` / `D3D12_DESCRIPTOR_RANGE_TYPE` by the words the tool uses.
PARAM_KIND_CODES = {'table': 0, '32bit': 1, 'cbv': 2, 'srv': 3, 'uav': 4}
RANGE_KIND_CODES = {'srv': 0, 'uav': 1, 'cbv': 2, 'sampler': 3}

#: One descriptor range: `(kind, baseRegister, count, space, offsetInTable)`.
RootRangeSpec = Tuple[str, int, int, int, int]
#: One root parameter: `(kind, visibility, register, space, count, ranges)`.
RootParamSpec = Tuple[str, int, int, int, int, Sequence[RootRangeSpec]]


def root_signature(params: Sequence[RootParamSpec], version: int = 2, flags: int = 0,
                   samplers: int = 0) -> bytes:
    """The `RTS0` part's data: a serialised D3D12 root signature (`DecodeRootSig` layout).

    Header(24) | param array (12 bytes each) | each parameter's out-of-line data. Every offset is
    from the start of this buffer, and a 1.0 signature's descriptor ranges are 20 bytes instead of
    24 -- the difference the tool has to get right to walk them.
    """
    array_len = 24 + 12 * len(params)
    bodies: List[Tuple[int, bytes]] = []
    data = b''
    for kind, _vis, reg, space, count, ranges in params:
        if kind == 'table':
            ranges_off = array_len + len(data) + 8
            body = u32b(len(ranges)) + u32b(ranges_off)
            for rkind, base, rcount, rspace, roffset in ranges:
                body += (u32b(RANGE_KIND_CODES[rkind]) + u32b(rcount) + u32b(base) + u32b(rspace)
                         + (u32b(0) if version >= 2 else b'') + u32b(roffset))
        elif kind == '32bit':
            body = u32b(reg) + u32b(space) + u32b(count)
        else:
            body = u32b(reg) + u32b(space) + (u32b(0) if version >= 2 else b'')
        bodies.append((array_len + len(data), body))
        data += body
    head = u32b(version) + u32b(len(params)) + u32b(24) + u32b(samplers) + u32b(0) + u32b(flags)
    array = b''.join(u32b(PARAM_KIND_CODES[param[0]]) + u32b(param[1]) + u32b(off)
                     for param, (off, _body) in zip(params, bodies))
    return head + array + data


def pl_create_root_sig(resid: int, sig: bytes, node_mask: int = 0, gap: int = 16) -> bytes:
    """`Device_CreateRootSignature`: the blob is a DXBC container with one `RTS0` part.

    The fields around the container are the serialiser's own framing, so the tool finds it by its
    `DXBC` magic and checks it against the length at +4 -- hence `gap`, which lets a test move the
    container without breaking anything. The id is the last 8 bytes.
    """
    container = dxbc([('RTS0', sig)])
    return (u32b(node_mask) + u64b(len(container)) + b'\x00' * gap + container
            + u64b(len(container)) + b'\x00' * 16 + u64b(resid))


#: The pipeline-creation forms `pl_create_pso` can build: `(how many inline ids, whether they are an array)`.
#: `Device_CreateComputePipeline` writes a single `ResourceId`, the two graphics forms a C array
#: (`d3d12_device_wrap.cpp` / `d3d12_device_wrap2.cpp`) -- the framing a parser gets wrong quietly.
PSO_FORMS: Dict[str, Tuple[int, bool]] = {
    'Device_CreatePipelineState': (8, True),
    'Device_CreateGraphicsPipeline': (5, True),
    'Device_CreateComputePipeline': (1, False),
}

#: The order each form writes its stage bytecodes in (`d3d12_serialise.cpp`), and the order of its own
#: `InlineShaderIDs` array -- which is a *different* order in both graphics forms.
PSO_STAGE_ORDER: Dict[str, Tuple[str, ...]] = {
    'Device_CreatePipelineState': ('VS', 'PS', 'DS', 'HS', 'GS', 'AS', 'MS', 'CS'),
    'Device_CreateGraphicsPipeline': ('VS', 'PS', 'DS', 'HS', 'GS'),
    'Device_CreateComputePipeline': ('CS',),
}
PSO_INLINE_ORDER: Dict[str, Tuple[str, ...]] = {
    'Device_CreatePipelineState': ('VS', 'HS', 'DS', 'GS', 'PS', 'CS', 'AS', 'MS'),
    'Device_CreateGraphicsPipeline': ('VS', 'HS', 'DS', 'GS', 'PS'),
    'Device_CreateComputePipeline': ('CS',),
}


def pl_create_pso_stages(resid: int, shaders: Sequence[Tuple[str, bytes]],
                         form: str = 'Device_CreatePipelineState', root_sig: int = 1,
                         state: int = 16, count_word: Optional[int] = None,
                         inline_ids: Optional[Sequence[int]] = None,
                         root_blob: bytes = b'') -> bytes:
    """A pipeline-creation payload in the layout `rdc_psos` reads -- the staged form of `pl_create_pso`.

    `u64 pRootSignature | u64 (an empty root-signature blob) | each stage's bytecode as
    `u64 len | bytes | u64 len` | `state` bytes of everything that follows them | riid(16) | the id (8) |
    the inline shader ids`, with the ids' own framing and both orders taken from `PSO_FORMS` -- so a
    fixture is a payload the parser has to *pair* rather than a list it can copy, and a stage's bytecodes
    and its inline id sit in different positions on purpose.

    `count_word` overrides the array's element count and `inline_ids` the ids themselves, for a test that
    wants a tail this is not (a capture older than the inline ids, or the wrong number of them), and
    `root_blob` fills the root-signature field with a container -- a `RTS0` blob is a DXBC container that
    must not be mistaken for a stage.
    """
    order, inline = PSO_STAGE_ORDER[form], PSO_INLINE_ORDER[form]
    given = dict(shaders)
    body = b''
    for stage in order:
        if stage in given:
            blob = given[stage]
            body += u64b(len(blob)) + blob + u64b(len(blob))
    ids = [1000 + order.index(stage) for stage in inline] if inline_ids is None else list(inline_ids)
    for i, stage in enumerate(inline):
        if stage not in given:
            ids[i] = 0
    count, is_array = PSO_FORMS[form]
    assert count == len(inline)
    header = (u64b(root_sig) + (u64b(len(root_blob)) + root_blob + u64b(len(root_blob))
                                if root_blob else u64b(0)))
    return (header + body + b'\x00' * state + b'\x00' * 16 + u64b(resid)
            + (u64b(count if count_word is None else count_word) if is_array else b'')
            + b''.join(u64b(v) for v in ids))


def rdef(binds: Sequence[Tuple[str, str, int, int]], target_version: int = 0x501,
         stage: int = 0x4353) -> bytes:
    """An `RDEF` part: `(name, kind, register, space)` per binding (`dxbc_container.cpp` layout).

    Header(24: cbuffers, resources, targetVersion, targetShaderStage, flags, creatorOffset) then one
    40-byte entry per binding plus its name string. `bindPoint` carries the register, which is what
    makes a root parameter's `(kind, register, space)` matchable.
    """
    kinds = {'cbv': 0, 'srv': 2, 'uav': 4, 'sampler': 3}
    stride = 40 if target_version >= 0x501 else 32
    header_len = 32                                  # cbuffers, resources, version, stage, flags...
    strings, entries = b'', b''
    for name, kind, reg, space in binds:
        name_off = header_len + stride * len(binds) + len(strings)
        strings += name.encode('utf-8') + b'\x00'
        entries += (u32b(name_off) + u32b(kinds[kind]) + u32b(0) * 3 + u32b(reg) + u32b(1)
                    + u32b(0) + (u32b(space) + u32b(0) if stride == 40 else b''))
    head = (u32b(0) + u32b(0) + u32b(len(binds)) + u32b(header_len) + u16b(target_version)
            + u16b(stage) + u32b(0) + u32b(0) + b'\x00' * 4)
    return head + entries + strings


# --------------------------------------------------------------------------- DXBC / DXIL
#: One `(fourcc, data)` pair for the `dxbc` builder.
DxbcPartSpec = Tuple[str, bytes]
#: One `(name, semanticIndex, register)` entry for the `signature` builder.
SignatureEntrySpec = Tuple[str, int, int]


def dxbc(parts: Sequence[DxbcPartSpec], version: int = 0x40, hash_: Optional[bytes] = None,
         size: Optional[int] = None, part_count: Optional[int] = None,
         offsets: Optional[Sequence[int]] = None) -> bytes:
    """'DXBC' | hash(16) | version | size | partCount | partOffsets[] then the parts."""
    n = len(parts) if part_count is None else part_count
    head_len = 32 + 4 * len(parts)
    blobs, offs = b'', []
    for fourcc, data in parts:
        offs.append(head_len + len(blobs))
        blobs += fourcc.encode('ascii').ljust(4)[:4] + u32b(len(data)) + data
    if offsets is None:
        offsets = offs
    total = head_len + len(blobs) if size is None else size
    hdr = (b'DXBC' + (hash_ if hash_ is not None else b'\x11' * 16) + u32b(version) + u32b(total)
           + u32b(n) + b''.join(u32b(o) for o in offsets))
    return hdr + blobs


def signature(entries: Sequence[SignatureEntrySpec], count_override: Optional[int] = None,
              declared_strtab: Optional[int] = None, elem_stride: int = 24,
              with_strtab: bool = True) -> bytes:
    """ISG1/OSG1: u32 count, u32 strtab offset, count x 24B elements, then the string table."""
    count = len(entries) if count_override is None else count_override
    elems, names = b'', b''
    for name, sem, reg in entries:
        elems += u32b(len(names)) + u32b(sem) + u32b(reg) + b'\x00' * (elem_stride - 12)
        names += name.encode('ascii') + b'\x00'
    strtab = 8 + count * elem_stride
    declared = strtab if declared_strtab is None else declared_strtab
    return u32b(count) + u32b(declared) + elems + (names if with_strtab else b'')


def isg1_inputs() -> bytes:
    return signature([('POSITION', 0, 0), ('TEXCOORD0', 0, 1), ('TEXCOORD6', 0, 2)])


def osg1_targets() -> bytes:
    return signature([('SV_Target', 0, 0), ('SV_Target', 1, 1)])


def osg1_position() -> bytes:
    return signature([('SV_Position', 0, 0), ('TEXCOORD0', 0, 1)])


# --------------------------------------------------------------------------- fake renderdoc-src
FAKE_CORE_H = '''\
#pragma once

// not an enum at all - must be ignored by parse_chunk_enum
struct SystemChunk
{
  int Fake;
};

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

FAKE_D3D12_COMMON_H = '''\
#pragma once

enum class D3D12Chunk : uint32_t
{
  SetName = (uint32_t)SystemChunk::FirstDriverChunk,
  PushMarker,
  SetMarker,
  PopMarker,
  SetShaderDebugPath,
  Device_CreateCommandList,        // auto-incremented
  Device_CreatePipelineState,
  Queue_BeginEvent,
  Queue_SetMarker,
  Queue_ExecuteCommandLists,

  // The two older pipeline forms (`d3d12_device_wrap.cpp`), which `pl_create_pso` can build. Their ids
  // here are this fixture's own: the tree's numbering is written over the bundled table's (chunkmap.py
  // `load_chunk_names`), and the bundled table's 1009 for `Device_CreateGraphicsPipeline` is taken by
  // `Queue_ExecuteCommandLists` above -- which is exactly how a name goes missing.
  Device_CreateGraphicsPipeline = 1090,
  Device_CreateComputePipeline,

  List_SetPipelineState = 1200,
  List_SetGraphicsRootSignature,
  List_SetGraphicsRootDescriptorTable,
  List_SetGraphicsRootConstantBufferView,
  List_SetGraphicsRootShaderResourceView,
  List_SetGraphicsRootUnorderedAccessView,
  List_SetGraphicsRoot32BitConstant,
  List_SetGraphicsRoot32BitConstants,
  List_SetDescriptorHeaps,
  List_IASetVertexBuffers,
  List_IASetIndexBuffer,
  List_DrawInstanced,
  List_DrawIndexedInstanced,
  List_Dispatch,
  List_ExecuteIndirect,
  List_Reset,
  List_SetComputeRootSignature,
  List_SetComputeRootDescriptorTable,
  List_SetComputeRootConstantBufferView,
  List_SetComputeRootShaderResourceView,
  List_SetComputeRootUnorderedAccessView,
  List_OMSetRenderTargets,
  List_ResourceBarrier,
  List_Barrier,
  List_ClearRenderTargetView,
  List_ClearDepthStencilView,
  List_ClearUnorderedAccessViewUint,
  List_ClearUnorderedAccessViewFloat,
  List_DiscardResource,
  List_CopyBufferRegion,
  List_CopyTextureRegion,
  List_ResolveQueryData,
  List_BuildRaytracingAccelerationStructure,
  Device_CreateHeap,
  Device_CreateCommittedResource,
  Device_CreatePlacedResource,
  Device_CreateReservedResource,
  Device_CreateCommittedResource3,
  Device_CreatePlacedResource2,
  CreateAS,
  Device_CreateDescriptorHeap,
  Device_CreateRootSignature,
  Device_CreateConstantBufferView,
  Device_CreateShaderResourceView,
  Device_CreateUnorderedAccessView,
  Device_CreateRenderTargetView,
  Device_CreateDepthStencilView,
  Device_CreateSampler,
  Device_CopyDescriptors,
  Device_CopyDescriptorsSimple,
};
'''

FAKE_D3D11_COMMON_H = '''\
#pragma once

enum class D3D11Chunk : uint32_t
{
  SetName = (uint32_t)SystemChunk::FirstDriverChunk,
  PushMarker,
  DrawIndexed,
};
'''


FAKE_DDS_READWRITE_CPP = '''\
// from dxgiformat.h
enum DXGI_FORMAT
{
  DXGI_FORMAT_UNKNOWN = 0,
  DXGI_FORMAT_R32G32B32A32_FLOAT = 2,
  DXGI_FORMAT_R16G16B16A16_FLOAT = 10,
  DXGI_FORMAT_R8G8B8A8_TYPELESS = 27,
  DXGI_FORMAT_R8G8B8A8_UNORM = 28,
  DXGI_FORMAT_R8G8B8A8_UNORM_SRGB = 29,
  DXGI_FORMAT_BC4_UNORM = 90,
};
'''


def make_fake_src(root: str) -> Tuple[str, Dict[int, str]]:
    """Write a minimal but faithful `renderdoc-src` tree and return (root, names)."""
    core = os.path.join(root, 'renderdoc', 'core')
    common = os.path.join(root, 'renderdoc', 'common')
    d12 = os.path.join(root, 'renderdoc', 'driver', 'd3d12')
    d11 = os.path.join(root, 'renderdoc', 'driver', 'd3d11')
    for d in (core, common, d12, d11):
        os.makedirs(d, exist_ok=True)
    write_bytes(os.path.join(core, 'core.h'), FAKE_CORE_H.encode())
    write_bytes(os.path.join(common, 'dds_readwrite.cpp'), FAKE_DDS_READWRITE_CPP.encode())
    write_bytes(os.path.join(d12, 'd3d12_common.h'), FAKE_D3D12_COMMON_H.encode())
    write_bytes(os.path.join(d11, 'd3d11_common.h'), FAKE_D3D11_COMMON_H.encode())
    return root, R.load_chunk_names(root, 'D3D12')


def invert(names: Dict[int, str]) -> Dict[str, int]:
    """{id: name} -> {name: id}."""
    return {v: k for k, v in names.items()}
