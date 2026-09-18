"""Chunk ids to names, and the classes of chunk this tool cares about: parsed out of the RenderDoc source tree at runtime (ROADMAP.md's environment convention), with a numeric fallback when that tree is absent."""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import rdc_renderdoc_src     # the tree's location, and fetching it when it is not there

CHUNK_CALLSTACK = 0x00010000
CHUNK_THREADID = 0x00020000
CHUNK_DURATION = 0x00040000
CHUNK_TIMESTAMP = 0x00080000
CHUNK_64BITSIZE = 0x00100000
CHUNK_ALIGN = 64

def _find_renderdoc_src() -> str:
    """Locate the RenderDoc source tree ("real implementation of RenderDoc").

    The tool parses the chunk-name enums out of the RenderDoc source at runtime, so the names it prints match
    the RenderDoc version that produced the capture. **A copy of the RenderDoc source tree must be placed in
    the repository root as `renderdoc-src`** -- see README section 1. Expected layout:

        <root>/src/py/rdc_chunkmap.py         this tool
        <root>/renderdoc-src/                 a copy of the RenderDoc source tree   <- the convention
          renderdoc/core/core.h                      (SystemChunk enum)
          renderdoc/driver/d3d12/d3d12_common.h      (D3D12Chunk enum)

    Search order: $RENDERDOC_SRC, then `renderdoc-src` in the tool's own folder or in any folder above it --
    the tool sits in `src/py` now and sat at the root before, and walking up is right for both -- then the
    historical absolute default. A missing tree is *reported* rather than raised: a capture still analyses,
    with numeric chunk ids and a warning.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates: List[Optional[str]] = [os.environ.get('RENDERDOC_SRC')]
    walk = here
    for _ in range(5):
        candidates.append(os.path.join(walk, 'renderdoc-src'))
        parent = os.path.dirname(walk)
        if parent == walk:
            break
        walk = parent
    candidates.append(r'C:\Workspace WIth Spaces\rdc-tools\renderdoc-src')
    for cand in candidates:
        if cand and os.path.isfile(os.path.join(cand, 'renderdoc', 'core', 'core.h')):
            return cand
    # The documented convention is `renderdoc-src` at the repository root, two levels above this file.
    return os.path.join(os.path.dirname(os.path.dirname(here)), 'renderdoc-src')

RENDERDOC_SRC = _find_renderdoc_src()
MARKER_CHUNKS = ('PushMarker', 'SetMarker', 'Queue_BeginEvent', 'Queue_SetMarker')
_SRC_WARNED = False
DRAW_CHUNKS = ('List_DrawIndexedInstanced', 'List_DrawInstanced', 'List_Dispatch',
               'List_ExecuteIndirect')

#: The draw chunks that are always compute. The rest of `DRAW_CHUNKS` are graphics, and an
#: `ExecuteIndirect` can be either, so `draws` reports both namespaces for it.
COMPUTE_CHUNKS = ('List_Dispatch',)

#: Command-list chunks that change the state `cmd_draws` reports (see `_apply_state_chunk`).
STATE_SETTERS = ('List_SetPipelineState', 'List_SetGraphicsRootSignature',
                 'List_SetGraphicsRootConstantBufferView', 'List_SetGraphicsRootDescriptorTable',
                 'List_SetComputeRootSignature', 'List_SetComputeRootConstantBufferView',
                 'List_SetComputeRootDescriptorTable', 'List_IASetVertexBuffers',
                 'List_IASetIndexBuffer')

#: Every `List_*` chunk whose payload a state tracker reads: the setters above plus `List_Reset`, which is
#: handled before the setter check because its payload carries the command list id at +40 rather than +0.
#: A reader that skips the payload for anything outside this set is right; gating on `STATE_SETTERS` alone
#: cleared the state on every `List_Reset` (caught by the tests).
STATE_CHUNKS = STATE_SETTERS + ('List_Reset',)

#: Payload lengths the decoders expect for the chunks with a fixed layout, used as a checksum by
#: `verify` (REFERENCE 3.4: "chunk length is a checksum for your decoder"). Chunks carrying arrays or
#: variable-length data are deliberately absent.
EXPECTED_LENGTHS: Dict[str, Tuple[int, ...]] = {
    'List_SetPipelineState': (16,),
    'List_Reset': (64,),                   # +40 = cmdList id, +48 = initial PSO
    'List_DrawIndexedInstanced': (28,),
    'List_DrawInstanced': (24,),
    'List_Dispatch': (20,),
    'List_SetGraphicsRootSignature': (16,),
    'List_SetGraphicsRootDescriptorTable': (24,),
    'List_SetGraphicsRootConstantBufferView': (28,),
    'List_SetGraphicsRootShaderResourceView': (28,),
    'List_SetGraphicsRootUnorderedAccessView': (28,),
    'List_SetComputeRootSignature': (16,),
    'List_SetComputeRootDescriptorTable': (24,),
    'List_SetComputeRootConstantBufferView': (28,),
    'List_IASetIndexBuffer': (9, 33),      # 9 = null view, 33 = present flag + view
}

#: `align_up`'s default (kept as a module constant so callers can name it).
ALIGN_UP_DEFAULT = CHUNK_ALIGN

#: Resource-creation chunks and where their `D3D12_RESOURCE_DESC` starts in the payload: committed
#: = heap props (20 bytes) + heap flags (4), placed = heap id (8) + heap offset (8), reserved =
#: straight in (d3d12_device_rescreate_wrap.cpp). Every one of them ends with
#: `IID(16) | resourceId(8) | gpuAddress(8)`, so the id is at `length - 16` and a buffer's base VA
#: at `length - 8`.
#:
#: The `1`/`2`/`3` variants differ only in what *follows* the descriptor (`D3D12_RESOURCE_DESC1`,
#: a protected session, a castable-format list), and that struct starts with the same 48 bytes as
#: `D3D12_RESOURCE_DESC`, so one offset covers them all -- the descriptor fields are read from the
#: first 48 bytes either way. Verified against a capture for `...3` (149-byte payloads, every
#: dimension field valid) and `...2`; the rest follow the same serialiser shape in the source.
RESOURCE_CHUNKS: Dict[str, int] = {
    'Device_CreateCommittedResource': 24,
    'Device_CreateCommittedResource1': 24,
    'Device_CreateCommittedResource2': 24,
    'Device_CreateCommittedResource3': 24,
    'Device_CreatePlacedResource': 16,
    'Device_CreatePlacedResource1': 16,
    'Device_CreatePlacedResource2': 16,
    'Device_CreateReservedResource': 0,
    'Device_CreateReservedResource1': 0,
    'Device_CreateReservedResource2': 0,
}

#: `CreateAS`: an acceleration structure is a *sub-range of a buffer*, not a resource. The payload
#: is `u64 buffer, u64 offset, u32 type, u64 byteSize, u64 asId` (d3d12_device.cpp
#: `Serialise_CreateAS`), and `asId` is the id the frame references. The type is a
#: `D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE`, where **0 is TOP_LEVEL** and 1 BOTTOM_LEVEL --
#: the capture that exercises this has 5205 small type-0 structures rebuilt every frame (RTXDI's
#: per-light TLASes) and 3 large type-1 ones (the static BLASes), which is what those two are.
AS_KINDS: Dict[int, str] = {0: 'tlas', 1: 'blas'}

#: `D3D10_RESOURCE_DIMENSION` (common/dds_readwrite.cpp): what a descriptor's Dimension field means.
RESOURCE_KINDS: Dict[int, str] = {1: 'buffer', 2: 'texture1d', 3: 'texture2d', 4: 'texture3d'}

#: Descriptor-write chunks and the kind of descriptor each one writes (d3d12_device_wrap.cpp).
#: A `Device_Create*View` payload holds the descriptor first and *ends* with the destination
#: `PortableHandle`; the resource id is at +16 in every view payload (`D3D12Descriptor`'s
#: serialiser writes type, heap, index, then the view, whose first field is the resource --
#: d3d12_serialise.cpp). `Device_CreateSampler` writes a sampler: no resource.
DESCRIPTOR_KINDS: Dict[str, str] = {
    'Device_CreateConstantBufferView': 'cbv',
    'Device_CreateShaderResourceView': 'srv',
    'Device_CreateUnorderedAccessView': 'uav',
    'Device_CreateRenderTargetView': 'rtv',
    'Device_CreateDepthStencilView': 'dsv',
    'Device_CreateSampler': 'sampler',
    'Device_CreateSampler2': 'sampler',
}

#: Descriptor-copy chunks: `u64 count` then `count x (u32 heapType, dst PortableHandle, src)`.
DESCRIPTOR_COPY_CHUNKS = ('Device_CopyDescriptors', 'Device_CopyDescriptorsSimple')

#: Bytes of one `DynamicDescriptorCopy`: the heap type, then two PortableHandles.
_DESCRIPTOR_COPY_SIZE = 28

#: Least a descriptor write can be: the resource field (ends at +24) plus the destination handle.
_DESCRIPTOR_WRITE_MIN = 36

#: Bytes of `D3D12_RESOURCE_DESC` to read, and the least that can follow it in a creation payload
#: (the initial state, the optional-clear flag, the IID and the two trailing u64s).
_RESOURCE_DESC_SIZE = 48
_RESOURCE_TAIL = 32

def align_up(x: int, a: int = ALIGN_UP_DEFAULT) -> int:
    """Round `x` up to the next multiple of `a` (chunks are 64-byte aligned)."""
    return (x + a - 1) & ~(a - 1)

def parse_chunk_enum(text: str, enum_name: str) -> Dict[int, str]:
    """Parse a C++ enum into an {id: name} map.

    Handles both `enum class <name> : uint32_t { ... }` (the chunk enums) and a plain
    `enum <name> { ... }` (RenderDoc's copy of `DXGI_FORMAT`). Values come from explicit
    initialisers, from `FirstDriverChunk` (= 1000) and otherwise auto-increment like C++ would.
    """
    m = re.search(r'enum(?:\s+class)?\s+%s\s*(?::\s*uint32_t\s*)?\{(.*?)\n\};' % enum_name,
                  text, re.S)
    if not m:
        return {}
    out: Dict[int, str] = {}
    val = 0
    for line in m.group(1).splitlines():
        line = line.split('//')[0].strip()
        mm = re.match(r'(\w+)\s*(?:=\s*([^,]+))?,', line)
        if not mm:
            continue
        name, expr = mm.group(1), (mm.group(2) or '').strip()
        if expr:
            if 'FirstDriverChunk' in expr:
                val = 1000
            elif re.match(r'^\d+$', expr):
                val = int(expr)
            else:
                val += 1
        out[val] = name
        val += 1
    return out

def load_chunk_names(src_root: str = RENDERDOC_SRC, driver: str = 'D3D12') -> Dict[int, str]:
    """Build the chunk-id -> name map from the RenderDoc source enums.

    The tree is asked for first (`rdc_renderdoc_src.ensure`), which fetches the latest tagged RenderDoc source
    when it is absent and the network allows it -- this function is the only place the tool needs an enum, so
    it is the only place the fetch is hooked, and every command that prints chunk names goes through it.

    Warns once on stderr when there is still no tree (no network, `RDC_NO_BOOTSTRAP`, or a `$RENDERDOC_SRC`
    that does not hold one); the tool then falls back to numeric ids and everything else keeps working (see
    README 1.1).
    """
    global _SRC_WARNED
    names: Dict[int, str] = {}
    src_root = rdc_renderdoc_src.ensure(src_root)
    core = os.path.join(src_root, 'renderdoc', 'core', 'core.h')
    if not os.path.isfile(core) and not _SRC_WARNED:
        _SRC_WARNED = True
        sys.stderr.write(
            'warning: RenderDoc source not found at %s\n'
            '         (the convention is a `renderdoc-src` folder at the repository root). It is fetched\n'
            '         automatically when the network allows it and this one was not, so chunk names fall\n'
            '         back to numeric ids and everything else still works.\n'
            '         Fetch it with `python src\\py\\rdc_analysis.py bootstrap`, point $RENDERDOC_SRC at a\n'
            '         tree, or set %s to skip this (see README section 1.1).\n'
            % (src_root, rdc_renderdoc_src.NO_BOOTSTRAP_ENV))
    if os.path.isfile(core):
        with open(core, encoding='utf-8', errors='replace') as fh:
            names.update(parse_chunk_enum(fh.read(), 'SystemChunk'))
    d = driver.lower()
    drv = os.path.join(src_root, 'renderdoc', 'driver', d, d + '_common.h')
    if os.path.isfile(drv):
        with open(drv, encoding='utf-8', errors='replace') as fh:
            names.update(parse_chunk_enum(fh.read(), driver + 'Chunk'))
    return names

__all__ = [
    'ALIGN_UP_DEFAULT',
    'AS_KINDS',
    'CHUNK_64BITSIZE',
    'CHUNK_ALIGN',
    'CHUNK_CALLSTACK',
    'CHUNK_DURATION',
    'CHUNK_THREADID',
    'CHUNK_TIMESTAMP',
    'COMPUTE_CHUNKS',
    'DESCRIPTOR_COPY_CHUNKS',
    'DESCRIPTOR_KINDS',
    'DRAW_CHUNKS',
    'EXPECTED_LENGTHS',
    'MARKER_CHUNKS',
    'RENDERDOC_SRC',
    'RESOURCE_CHUNKS',
    'RESOURCE_KINDS',
    'STATE_CHUNKS',
    'STATE_SETTERS',
    '_DESCRIPTOR_COPY_SIZE',
    '_DESCRIPTOR_WRITE_MIN',
    '_RESOURCE_DESC_SIZE',
    '_RESOURCE_TAIL',
    '_SRC_WARNED',
    '_find_renderdoc_src',
    'align_up',
    'load_chunk_names',
    'parse_chunk_enum',
]
