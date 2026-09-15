"""Synthetic fixtures for the rdc_analysis.py test-suite.

Everything here is built in memory from the layouts documented in the tool itself and in the
RenderDoc source, so the tests need no .rdc capture, no GPU, no renderdoc.pyd and no
`renderdoc-src` checkout:

  * container      -> renderdoc/serialise/rdcfile.cpp  (FileHeader | thumbnail | metadata | sections)
  * chunk framing  -> renderdoc/serialise/serialiser.cpp (Serialiser<Reading>::BeginChunk)
  * chunk payloads -> README section 3.4 (the table the decoders were written against)

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


def f32b(*vals: float) -> bytes:
    return struct.pack('<%df' % len(vals), *vals)


def fbits(*vals: float) -> List[int]:
    """float32 values as their raw u32 bit patterns (what root constants carry)."""
    return [struct.unpack('<I', struct.pack('<f', v))[0] for v in vals]


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
# Layouts follow README section 3.4 ("Payload decoding").
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


def pl_descriptor_write(resource: int, heap: int, index: int, length: int = 68) -> bytes:
    """A `Device_Create*View` payload: the resource id at +16, the destination handle last.

    The tool reads exactly those two things -- the descriptor *kind* comes from the chunk name -- so
    the bytes in between are zeros. `length` matches what the captures carry for an SRV (68); a UAV
    payload is 80 in the captures, which is why the length is a parameter.
    """
    return b'\x00' * 16 + u64b(resource) + b'\x00' * (length - 36) + u64b(heap) + u32b(index)


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


def pl_32bit_constants(cmdlist: int, root_param: int, values: Sequence[int],
                       dest_offset: int = 0, array_count: Optional[int] = None) -> bytes:
    """cmdList | rootParam | numValues | arrayCount(u64) | values[n] | destOffset.

    `SERIALISE_ELEMENT_ARRAY` writes the element count before the values, so the payload is
    `28 + 4n` bytes. `array_count` overrides the inline count (for mismatch tests).
    """
    count = len(values) if array_count is None else array_count
    out = u64b(cmdlist) + u32b(root_param) + u32b(len(values)) + u64b(count)
    out += b''.join(u32b(v & 0xFFFFFFFF) for v in values)
    return out + u32b(dest_offset)


def pl_32bit_constant(cmdlist: int, root_param: int, value: int, dest_offset: int = 0) -> bytes:
    return u64b(cmdlist) + u32b(root_param) + u32b(value & 0xFFFFFFFF) + u32b(dest_offset)


def pl_create_pso(pso_id: int, tail: bytes = b'\xAB\xCD' * 16) -> bytes:
    return u64b(pso_id) + tail


SINGLEPROBE_SIG = f32b(0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0)


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
  Device_CreateCommittedResource,
  Device_CreatePlacedResource,
  Device_CreateReservedResource,
  Device_CreateCommittedResource3,
  Device_CreatePlacedResource2,
  CreateAS,
  Device_CreateDescriptorHeap,
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
  DXGI_FORMAT_R8G8B8A8_UNORM = 28,
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
