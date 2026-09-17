"""The shapes of the things this tool reads and prints, and the constants they are framed with. TypedDicts because every one of these starts as JSON from a capture or from the engine, and naming the keys is what keeps a typo out of a decode."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple, TypedDict, Union

ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
STR_RE = re.compile(rb'[\x20-\x7e]{6,}')

# ---------------------------------------------------------------------------
# Dict shapes.
#
# These TypedDicts document the plain `dict`s the parsers return. They exist only for the type
# checker: at runtime every value is an ordinary dict, so the shapes seen by the `cmd_*` printers,
# by the tests and by any external script are unchanged.
# ---------------------------------------------------------------------------
class SectionInfo(TypedDict):
    """One BinarySectionHeader plus where its (still compressed) payload lives.

    `dataOffset` is the absolute offset of the section body inside the container; `name` is the
    NUL-trimmed, UTF-8 decoded section name.
    """
    type: int
    compLen: int
    uncompLen: int
    version: int
    flags: int
    nameLen: int
    name: str
    dataOffset: int

class CaptureMetaData(TypedDict):
    """The CaptureMetaData + CaptureTimeBase records that follow the thumbnail."""
    driverID: int
    driverName: str
    timeBase: int
    timeFreq: float

class CaptureInfo(TypedDict):
    """A whole `.rdc` container: file header fields, thumbnail, metadata and the section table."""
    size: int
    version: int
    headerLength: int
    progVersion: str
    thumbnail: Tuple[int, int, int]
    meta: CaptureMetaData
    sections: List[SectionInfo]
    _data: bytes

class ChunkInfo(TypedDict):
    """One framed SDChunk.

    `off` is the chunk start and `payload_offset` is where the payload begins, *after* the per-chunk
    metadata (36 bytes in for the usual flags, see REFERENCE 3.3) -- never assume `off + 8`. The
    payload is `length` bytes at `payload_offset`; `pad_start`/`pad_len` describe the 64-byte
    alignment padding after it (stale buffer bytes, not data -- see `check_stream`).
    """
    off: int
    id: int
    flags: int
    length: int
    payload_offset: int
    pad_start: int
    pad_len: int

class CacheEntry(TypedDict):
    """One cached decompressed stream: where it is and what it was built from.

    `hdrLen` is where the payload starts, `streamLen` how long it is; `srcPath`/`srcSize`/`srcMtime`
    identify the capture it belongs to and are re-checked on every read.
    """
    file: str
    srcPath: str
    hdrLen: int
    section: int
    method: int
    srcSize: int
    srcMtime: int
    streamLen: int
    blocks: int

class ResourceInfo(TypedDict):
    """One D3D12 resource: what it is, how big, and what the capture calls it.

    `kind` is `buffer` / `texture1d` / `texture2d` / `texture3d` / `blas` / `tlas` / `unknown` (the
    last one for ids that are only *named*: heaps, queues, fences, PSOs). `size` is the byte size of
    a buffer or an acceleration structure, and 0 for textures, which are described by
    `width`/`height`/`depth` (depth doubles as array size), `mips` and `format` instead. `name` is
    empty when the capture never named the resource, and `gpuAddress` is the base VA of a buffer.
    """
    kind: str
    name: str
    size: int
    width: int
    height: int
    depth: int
    mips: int
    format: int
    gpuAddress: int

class DescriptorInfo(TypedDict):
    """One written descriptor-heap slot: what kind of view it holds and which resource it names.

    `kind` is `cbv` / `srv` / `uav` / `rtv` / `dsv` / `sampler`. `resource` is 0 for a sampler (a
    sampler points at no resource) and for a slot the capture never wrote -- heaps are created with
    up to a million slots, and only written ones are recorded.
    """
    kind: str
    resource: int

class DrawState(TypedDict):
    """The D3D12 command-list state `draws` reports at each draw.

    Bindings belong to the *command list*, not to the PSO and not to one draw: they survive
    `SetPipelineState` and every draw, and only change when something rebinds them. Two events
    invalidate the root bindings: `Reset()` (a fresh list) and a root signature that actually
    differs from the current one -- "if a root signature is changed on a command list, all previous
    root arguments become stale", which is what RenderDoc's own replay implements
    (`d3d12_command_list_wrap.cpp`). Graphics and compute root parameters are separate namespaces.
    """
    pso: Optional[int]
    gfxSig: Optional[int]
    compSig: Optional[int]
    gfxCbv: Dict[int, Tuple[int, int]]
    compCbv: Dict[int, Tuple[int, int]]
    gfxTable: Dict[int, Tuple[int, int]]
    compTable: Dict[int, Tuple[int, int]]
    vbs: Dict[int, Tuple[int, int, int, int]]
    ib: Optional[Tuple[int, int]]

class RootRange(TypedDict):
    """One descriptor range of a root signature table.

    `count` is `NumDescriptors` (`0xffffffff` is unbounded) and `offset` is
    `OffsetInDescriptorsFromTableStart`: where the range's first descriptor sits in the table the
    shader sees. One table may hold several ranges, which is how a signature mixes SRVs and UAVs in
    a single root parameter.
    """
    kind: str
    base: int
    count: int
    space: int
    offset: int

class RootParam(TypedDict):
    """One root parameter -- what `draws` prints as `rpN`.

    `kind` is `table` / `32bit` / `cbv` / `srv` / `uav` (`D3D12_ROOT_PARAMETER_TYPE`), and
    `visibility` is the stage that may use it (`all` when unrestricted). A `table` binds descriptors
    through `ranges`; everything else binds one thing at `register`/`space`, and for `32bit` that is
    `count` immediate DWORDs.
    """
    kind: str
    visibility: str
    register: int
    space: int
    count: int
    ranges: List[RootRange]

class RootSignature(TypedDict):
    """A decoded `RTS0` root signature: the parameter list that `draws`' `rpN` indexes into.

    `dwords` is the signature's cost in 32-bit root arguments (a table costs 1, root constants cost
    their count, a root descriptor costs 2) -- the range `SetGraphicsRoot32BitConstant` indexes.
    `flags` is the raw `D3D12_ROOT_SIGNATURE_FLAGS` word.
    """
    id: int
    version: str
    flags: int
    dwords: int
    params: List[RootParam]
    samplers: int

class ShaderBind(TypedDict):
    """One `RDEF` resource binding: its name, kind and where the shader expects it."""
    name: str
    kind: str
    register: int
    space: int
    count: int

#: One part of a DXBC/DXIL container: `(fourcc, offset, length)`. `offset` is absolute, past the
#: part's own fourcc/size header; `length` is its data length.
DxbcPart = Tuple[str, int, int]

#: A validated DXBC/DXIL container: `(offset, size, hash_hex, parts)`.
DxbcContainer = Tuple[int, int, str, List[DxbcPart]]

#: Any object `struct.unpack_from` accepts (bytes, bytearray, memoryview).
Buffer = Union[bytes, bytearray, memoryview]

#: Cached `rb'[\x20-\x7e]{minlen,}'` patterns, keyed by the effective minimum length. `STR_RE` is the
#: entry for the historical default of 6.
_RUN_PATTERNS: Dict[int, re.Pattern[bytes]] = {6: STR_RE}

#: The same cache for the text patterns used on UTF-16LE-decoded payloads.
_WIDE_PATTERNS: Dict[int, re.Pattern[str]] = {}

__all__ = [
    'Buffer',
    'CacheEntry',
    'CaptureInfo',
    'CaptureMetaData',
    'ChunkInfo',
    'DescriptorInfo',
    'DrawState',
    'DxbcContainer',
    'DxbcPart',
    'ResourceInfo',
    'RootParam',
    'RootRange',
    'RootSignature',
    'STR_RE',
    'SectionInfo',
    'ShaderBind',
    'ZSTD_MAGIC',
    '_RUN_PATTERNS',
    '_WIDE_PATTERNS',
]
