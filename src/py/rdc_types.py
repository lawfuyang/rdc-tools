"""The shapes of the things this tool reads and prints, and the constants they are framed with. TypedDicts because every one of these starts as JSON from a capture or from the engine, and naming the keys is what keeps a typo out of a decode."""

from __future__ import annotations

import mmap
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
    _data: Buffer

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

    `rtv`/`dsv` are the render targets `OMSetRenderTargets` bound -- the write side of the state, and
    the only place a target binding is recorded in the stream (`List_OMSetRenderTargets` carries the
    RTV and DSV *descriptors*, not handles). `gfxSrv`/`gfxUav`/`compSrv`/`compUav` are the root
    descriptors of those kinds, which `List_Set{Graphics,Compute}Root{ShaderResource,UnorderedAccess}View`
    binds and which the stream carries in the same (resource, byteOffset) shape a root CBV uses.
    """
    pso: Optional[int]
    gfxSig: Optional[int]
    compSig: Optional[int]
    gfxCbv: Dict[int, Tuple[int, int]]
    compCbv: Dict[int, Tuple[int, int]]
    gfxSrv: Dict[int, Tuple[int, int]]
    compSrv: Dict[int, Tuple[int, int]]
    gfxUav: Dict[int, Tuple[int, int]]
    compUav: Dict[int, Tuple[int, int]]
    gfxTable: Dict[int, Tuple[int, int]]
    compTable: Dict[int, Tuple[int, int]]
    vbs: Dict[int, Tuple[int, int, int, int]]
    ib: Optional[Tuple[int, int]]
    rtv: List[int]
    dsv: int

class BarrierInfo(TypedDict):
    """One entry of a `List_ResourceBarrier` payload (`D3D12_RESOURCE_BARRIER`).

    `kind` is `transition` / `aliasing` / `uav`, and the union is walked arm by arm because the
    entries are not the same length: a transition serialises as 28 bytes (resource, subresource,
    state before, state after), an aliasing barrier as 24 (the two resources) and a UAV barrier as
    16. `resource2` is the second resource of an aliasing barrier -- the one the memory is handed
    *to* -- and is 0 for the other kinds. `before`/`after` are `D3D12_RESOURCE_STATES` words, and
    `subresource` is 0xffffffff for "all of them".
    """
    kind: str
    resource: int
    resource2: int
    before: int
    after: int
    subresource: int

class GroupBarrier(TypedDict):
    """One entry of a `List_Barrier` payload (`D3D12_TEXTURE_BARRIER` / `_BUFFER_BARRIER` / `_GLOBAL_BARRIER`).

    The 1.7-era barrier names an access rather than a state, which is why `access` is a
    `D3D12_BARRIER_ACCESS` bitmask (the thing the ledger classifies) and `layout` is only meaningful
    for a texture barrier. `sync` is the `D3D12_BARRIER_SYNC` after the barrier and `flags` the
    texture barrier flags (`DISCARD` says the previous contents are dropped). A global barrier has
    no resource at all, so its `resource` is 0.
    """
    kind: str
    resource: int
    sync: int
    access: int
    layout: int
    flags: int

class UseInfo(TypedDict):
    """One event's use of one resource, as an offline walk of the stream sees it.

    `access` is `read` / `write` / `read+write` / `discard` -- a UAV binding and a transition to
    `UNORDERED_ACCESS` are both read *and* write, which is the same reading the report gives a
    bundle's `CS_RWResource` row. `how` is what made the use (`cbv`, `srv`, `uav`, `rtv`, `dsv`,
    `vb`, `ib`, `clear`, `copy-dst`, `copy-src`, `discard`, `barrier`), `call` the chunk it happened
    in (the label a graph node prints) and `detail` the payload's own words for it (`rp3`,
    `copy 144 B`, `RenderTarget -> PixelShaderResource`).
    """
    eid: int
    access: str
    how: str
    call: str
    detail: str

class ResourceUse(TypedDict):
    """One resource's capture-relative life: where it came from, and every use the stream shows.

    `created` is the chunk index of the creation payload and 0 when the capture does not create the
    resource at all -- a frame capture records what happened *during* the frame, and UE allocates
    its heaps and static textures at startup, so most referenced resources are older than the file.
    `placement` is `committed` / `placed` / `reserved` for one the capture does create and `external`
    for one it does not; `heap`/`offset` are the heap a placed resource lives in (`Device_CreateHeap`
    is where a heap's *size* comes from). `uses` is in stream order.
    """
    created: int
    placement: str
    heap: int
    offset: int
    uses: List[UseInfo]

class UseLedger(TypedDict):
    """Everything one walk of the stream says about who touches what (see `rdc_uses.walk_uses`).

    `aliases` is every aliasing barrier as `(eid, from, to)`, `heaps` the resource heaps the capture
    creates by size, `seen` the chunk-name histogram of the whole walk (the commands use it to say
    which resource-referencing chunks are *not* decoded as uses), and `failed` counts the payloads
    that did not parse cleanly -- a barrier whose walk does not land exactly at the end of its
    payload is never guessed at. `unresolved` counts the descriptor-table bindings whose heap slot
    the capture never wrote, which resolve to nothing rather than to a guess.
    """
    resources: Dict[int, ResourceUse]
    aliases: List[Tuple[int, int, int]]
    heaps: Dict[int, int]
    seen: Dict[str, int]
    events: int
    failed: Dict[str, int]
    unresolved: int

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
#: Read-only bytes the tool walks: `bytes`/`bytearray` built in memory, or an `mmap` of a file it is only
#: reading (the container, the cached stream). All three index, slice, compare, `.find` and unpack the
#: same way, and all three are accepted by `re` -- which is what the decoders use. Mapping a 1.5 GB stream
#: instead of reading it is worth ~0.3 s and 1.5 GB of memory per command, and a frame walk over a map is
#: 0.038 s against 0.489 s for the walk over a read-in copy (REFERENCE 4.14). A *slice* of one of these is
#: always `bytes` (or `bytearray`), never a map, which is why the payload readers below can still rely on
#: `bytes`-only methods.
Buffer = Union[bytes, bytearray, 'mmap.mmap']

#: What `struct.unpack_from`, `re` and `.find` accept: the buffers above, plus a `memoryview` of one. A
#: stream is never a view (a slice of one must be `bytes`, and `mmap` has no `decode`), but the primitives
#: here are handed views by tests and by anything that has already taken a slice in its own way.
BufferLike = Union[bytes, bytearray, memoryview, 'mmap.mmap']

#: Cached `rb'[\x20-\x7e]{minlen,}'` patterns, keyed by the effective minimum length. `STR_RE` is the
#: entry for the historical default of 6.
_RUN_PATTERNS: Dict[int, re.Pattern[bytes]] = {6: STR_RE}

#: The same cache for the text patterns used on UTF-16LE-decoded payloads.
_WIDE_PATTERNS: Dict[int, re.Pattern[str]] = {}

__all__ = [
    'BarrierInfo',
    'Buffer',
    'BufferLike',
    'CacheEntry',
    'CaptureInfo',
    'CaptureMetaData',
    'ChunkInfo',
    'DescriptorInfo',
    'DrawState',
    'DxbcContainer',
    'DxbcPart',
    'GroupBarrier',
    'ResourceInfo',
    'ResourceUse',
    'RootParam',
    'RootRange',
    'RootSignature',
    'STR_RE',
    'SectionInfo',
    'ShaderBind',
    'UseInfo',
    'UseLedger',
    'ZSTD_MAGIC',
    '_RUN_PATTERNS',
    '_WIDE_PATTERNS',
]
