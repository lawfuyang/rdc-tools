"""Chunk ids to names, and the classes of chunk this tool cares about: the tree's enums where they are, the bundled table where they are not.

The names come from the RenderDoc source tree at runtime (`ROADMAP.md`'s environment convention), which is
what makes them the *capture's* vocabulary rather than this tool's guess. A tree that is absent, or from an
older release, or half-extracted is no longer a reason to print `1207` instead of `List_DrawInstanced`: the
checked-in `rdc_chunknames` table -- the same enums, generated from a released RenderDoc -- names what the
tree does not, and the warning says which version those names are from (`chunk-names` regenerates it).
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import rdc_chunknames        # the bundled table: the fallback the tree is preferred over
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

#: The marker chunks that open a scope and the ones that close it. `SetMarker`/`Queue_SetMarker` are not
#: here: they *set* the current marker's name without opening one, so a tree built from them would leave
#: every later marker nested inside a scope that never closes. They live in `MARKER_CHUNKS` (a reader
#: listing what it saw) and not in the tree. The two families are one stack because that is how the
#: marker-imbalance rule has always read them, and a frame that opens a queue marker around its command
#: lists reads as one pass over all of them -- which is what it is.
PUSH_MARKER_CHUNKS = ('PushMarker', 'Queue_BeginEvent')
POP_MARKER_CHUNKS = ('PopMarker', 'Queue_EndEvent')
_SRC_WARNED = False
DRAW_CHUNKS = ('List_DrawIndexedInstanced', 'List_DrawInstanced', 'List_Dispatch',
               'List_ExecuteIndirect')

#: The draw chunks that are always compute. The rest of `DRAW_CHUNKS` are graphics, and an
#: `ExecuteIndirect` can be either, so `draws` reports both namespaces for it.
COMPUTE_CHUNKS = ('List_Dispatch',)

#: Command-list chunks that change the state `cmd_draws` reports (see `_apply_state_chunk`).
STATE_SETTERS = ('List_SetPipelineState', 'List_SetGraphicsRootSignature',
                 'List_SetGraphicsRootConstantBufferView', 'List_SetGraphicsRootShaderResourceView',
                 'List_SetGraphicsRootUnorderedAccessView', 'List_SetGraphicsRootDescriptorTable',
                 'List_SetComputeRootSignature', 'List_SetComputeRootConstantBufferView',
                 'List_SetComputeRootShaderResourceView', 'List_SetComputeRootUnorderedAccessView',
                 'List_SetComputeRootDescriptorTable', 'List_IASetVertexBuffers',
                 'List_IASetIndexBuffer', 'List_OMSetRenderTargets')

#: Every `List_*` chunk whose payload a state tracker reads: the setters above plus `List_Reset`, which is
#: handled before the setter check because its payload carries the command list id at +40 rather than +0.
#: A reader that skips the payload for anything outside this set is right; gating on `STATE_SETTERS` alone
#: cleared the state on every `List_Reset` (caught by the tests).
STATE_CHUNKS = STATE_SETTERS + ('List_Reset',)

#: The two barrier chunks: `List_ResourceBarrier` (the D3D12.0 `D3D12_RESOURCE_BARRIER`, whose union has
#: three arms) and `List_Barrier` (the ID3D12GraphicsCommandList7 form, which names an *access* instead
#: of a state and adds a discard flag). Both payloads are arrays and neither is in `EXPECTED_LENGTHS`;
#: the decoders walk them by type and refuse an entry that does not land exactly at the end.
BARRIER_CHUNKS = ('List_ResourceBarrier', 'List_Barrier')

#: Payloads that name a render target in their own bytes rather than through a descriptor heap:
#: `List_OMSetRenderTargets` binds them, a clear writes one, a discard drops one.
TARGET_CHUNKS = ('List_OMSetRenderTargets',)
CLEAR_CHUNKS = ('List_ClearRenderTargetView', 'List_ClearDepthStencilView',
                'List_ClearUnorderedAccessViewUint', 'List_ClearUnorderedAccessViewFloat')
DISCARD_CHUNKS = ('List_DiscardResource',)

#: Copies: the destination is written and the source read, which is the one place the offline stream
#: shows a buffer being produced without being bound to a pipeline.
COPY_CHUNKS = ('List_CopyBufferRegion', 'List_CopyTextureRegion')

#: The resource heaps the capture creates. `Device_CreateHeap1` is the same payload shape (the
#: descriptor first, the heap id last), so one parse covers both; only the base form has been measured
#: against real payloads, which is why only it is in `EXPECTED_LENGTHS`.
HEAP_CHUNKS = ('Device_CreateHeap', 'Device_CreateHeap1')

#: `List_*` chunks that reference resources which the offline decoders do **not** attribute as uses.
#: They are not decoded because what they touch is a sub-range, an argument buffer or an
#: acceleration-structure build, and a use that is only half-decoded would be read as a fact -- so the
#: ledger counts them instead and `deps`/`memory` print the ones their capture actually contains. This
#: is what keeps a `write-never-read` finding an observation about the *decoded* stream rather than a
#: claim about the frame.
UNATTRIBUTED_CHUNKS = ('List_ResolveQueryData', 'List_BuildRaytracingAccelerationStructure',
                       'List_CopyRaytracingAccelerationStructure', 'List_ExecuteIndirect',
                       'List_SetDescriptorHeaps', 'List_ResolveSubresource',
                       'List_ResolveSubresourceRegion', 'List_CopyResource', 'List_CopyTiles',
                       'List_WriteBufferImmediate', 'List_EmitRaytracingAccelerationStructurePostbuildInfo',
                       'List_CopyRaytracingAccelerationStructureRegion', 'List_SetPredication',
                       'List_ClearStateObject', 'List_BeginRenderPass')

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
    'List_SetComputeRootShaderResourceView': (28,),
    'List_SetComputeRootUnorderedAccessView': (28,),
    'List_IASetIndexBuffer': (9, 33),      # 9 = null view, 33 = present flag + view
    'List_CopyBufferRegion': (48,),        # cmdList, dst, dstOffset, src, srcOffset, numBytes
    'Device_CreateHeap': (72,),            # desc(40) | IID(24, its 8-byte array count included) | id
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
#: one capture here has 5205 small type-0 structures rebuilt every frame (a TLAS per light) and 3 large
#: type-1 ones (the static BLASes), which is what those two are.
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

#: The three enums the tool names things with, and the key each one is known by here: the chunk ids every
#: capture's stream shares (`SystemChunk`), the ones a driver adds on top of them (`D3D12Chunk`), and
#: RenderDoc's own copy of `DXGI_FORMAT`, which is what `resources` prints a texture's format with.
ENUM_KINDS: Tuple[str, ...] = ('system', 'driver', 'formats')


def enum_path(src_root: str, kind: str, driver: str = 'D3D12') -> str:
    """The file in a RenderDoc source tree that declares one of those enums."""
    if kind == 'system':
        return os.path.join(src_root, 'renderdoc', 'core', 'core.h')
    if kind == 'formats':
        return os.path.join(src_root, 'renderdoc', 'common', 'dds_readwrite.cpp')
    lowered = driver.lower()
    return os.path.join(src_root, 'renderdoc', 'driver', lowered, lowered + '_common.h')


def enum_name(kind: str, driver: str = 'D3D12') -> str:
    """The C++ enum's own name, which is what `parse_chunk_enum` has to match."""
    if kind == 'system':
        return 'SystemChunk'
    if kind == 'formats':
        return 'DXGI_FORMAT'
    return driver + 'Chunk'


def parse_enum(src_root: str, kind: str, driver: str = 'D3D12') -> Dict[int, str]:
    """One enum out of a source tree, or `{}` when that tree does not have the file it lives in.

    A missing file is not an error at this level: every caller has something to fall back on
    (`load_chunk_names`, `load_format_names`), and a half-extracted tree is a state to report rather than
    to raise on. `rdc_renderdoc_src.missing_parts` is what says whether a tree is complete.
    """
    path = enum_path(src_root, kind, driver)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding='utf-8', errors='replace') as handle:
        return parse_chunk_enum(handle.read(), enum_name(kind, driver))


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

def _warn_bundled_names(src_root: str, missing: List[str]) -> None:
    """The one line a run prints when some or all of the names are the bundled table's rather than the tree's.

    It says which RenderDoc version those names are from -- a chunk named out of a 1.46 table while the
    capture is 1.47 is a fact the reader has to have, not a detail -- what was missing, so the fix is
    obvious, and where the fix is documented.
    """
    if os.path.isdir(src_root):
        why = '%s is incomplete (%s)' % (src_root, ', '.join(missing))
    else:
        why = 'no RenderDoc source tree at %s' % src_root
    version = ('RenderDoc %s' % rdc_chunknames.VERSION) if rdc_chunknames.VERSION \
        else 'an unversioned bundled table'
    sys.stderr.write(
        'warning: using bundled names for %s (%s).\n'
        '         Fetch the tree with `python src\\py\\rdc_analysis.py bootstrap` to name chunks with the\n'
        '         enums of the version that recorded the capture: a chunk neither of them has prints as a\n'
        '         number (see README section 1.1).\n' % (version, why))


def load_chunk_names(src_root: str = RENDERDOC_SRC, driver: str = 'D3D12') -> Dict[int, str]:
    """Build the chunk-id -> name map: the tree's enums where they are, the bundled table where they are not.

    The tree is asked for first (`rdc_renderdoc_src.ensure`), which fetches the latest tagged RenderDoc source
    when it is absent and the network allows it -- this function is the only place the tool needs an enum, so
    it is the only place the fetch is hooked, and every command that prints chunk names goes through it.

    The order of the two sources is the whole design: the **table is the floor and the tree is written over
    it**. A tree that is there names everything it knows -- it is the preferred source, and reading it is what
    makes the names the capture's own version's vocabulary -- while a tree that is absent, or from an older
    release, or half-extracted no longer costs the names of the ids it does not mention. Before the table
    existed, the second case printed numeric ids.

    Warns once on stderr while the tree is incomplete, naming the version the bundled names are from; the rest
    of the tool is unaffected either way, because a name is only ever printed (README section 1.1).
    """
    global _SRC_WARNED
    src_root = rdc_renderdoc_src.ensure(src_root)
    names: Dict[int, str] = dict(rdc_chunknames.SYSTEM_CHUNKS)
    names.update(rdc_chunknames.DRIVER_CHUNKS.get(driver, {}))
    names.update(parse_enum(src_root, 'system'))
    names.update(parse_enum(src_root, 'driver', driver))
    wanted = [enum_path(src_root, 'system'), enum_path(src_root, 'driver', driver)]
    missing = [os.path.relpath(path, src_root).replace(os.sep, '/')
               for path in wanted if not os.path.isfile(path)]
    if missing and not _SRC_WARNED:
        _SRC_WARNED = True
        _warn_bundled_names(src_root, missing)
    return names


#: The checked-in table's file name: next to this module, and the only place it is imported from.
TABLE_NAME = 'rdc_chunknames.py'


def table_path() -> str:
    """Where the bundled table lives."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), TABLE_NAME)


def table_text(src_root: str, driver: str = 'D3D12') -> str:
    """The bundled table's text, generated from one RenderDoc source tree.

    Deterministic on purpose -- ids sorted, one entry per line, no timestamps and no machine paths -- because
    the file is checked in, and `chunk-names --check` compares it byte for byte with what a tree generates.
    The docstring it writes names the version it is from and the command that rewrites it, so the file itself
    says how to keep it true.
    """
    version = rdc_renderdoc_src.version(src_root)
    # The docstring is written **raw** (`r"""`): it names a command with Windows separators in it
    # (`src\py\...`), and in a normal docstring that backslash would have to be doubled in the text to stay
    # one character -- a generator detail leaking into the file a reader sees, and a lint warning when it
    # leaks the other way.
    header = [
        'r"""The enum names the tool falls back to: %s, generated by `chunk-names --write`.' % (
            'RenderDoc %s' % version if version else 'a RenderDoc source tree that did not say its version'),
        '',
        'The names come from a RenderDoc source tree at runtime, so that they are the vocabulary of the version',
        'that recorded the capture (README section 1.1). This file is what the tool names things with when that',
        'tree is absent, or older than the capture, or half-extracted: the same three enums, generated once',
        'from a released RenderDoc and checked in, so a machine with no network and no tree still prints',
        '`List_DrawIndexedInstanced` rather than `1207`.',
        '',
        'Generated from `renderdoc/core/core.h`, `renderdoc/driver/%s/%s_common.h` and' % (
            driver.lower(), driver.lower()),
        '`renderdoc/common/dds_readwrite.cpp` of that tree. The tree always wins where it is present -- this is',
        'the floor, not the source -- and `python src\\py\\rdc_analysis.py chunk-names --check` reports when it',
        'has drifted from one. **Edit nothing here by hand**: run that command with `--write`.',
        '"""',
        'from __future__ import annotations',
        '',
        'from typing import Dict',
        '',
        'VERSION = %r' % version,
    ]
    body: List[str] = ['', 'SYSTEM_CHUNKS: Dict[int, str] = {']
    body += ['    %d: %r,' % (cid, name) for cid, name in sorted(parse_enum(src_root, 'system').items())]
    body += ['}', '', 'DRIVER_CHUNKS: Dict[str, Dict[int, str]] = {', "    '%s': {" % driver]
    body += ['        %d: %r,' % (cid, name)
             for cid, name in sorted(parse_enum(src_root, 'driver', driver).items())]
    body += ['    },', '}', '',
             "#: RenderDoc's copy of `DXGI_FORMAT`, as written there: `resources` strips the prefix.",
             'FORMAT_NAMES: Dict[int, str] = {']
    body += ['    %d: %r,' % (cid, name) for cid, name in sorted(parse_enum(src_root, 'formats').items())]
    body += ['}']
    return '\n'.join(header + body) + '\n'

def _table_counts(table_text_bytes: str) -> str:
    """`N system + N driver + N format name(s)`, counted off a table file rather than imported from it."""
    counts = {kind: len(re.findall(r'^\s+\d+: ', block, re.M)) for kind, block in
              ((kind, part) for kind, part in zip(ENUM_KINDS, _table_blocks(table_text_bytes)))}
    return '%d system + %d driver + %d format name(s)' % (counts['system'], counts['driver'],
                                                          counts['formats'])


def _table_blocks(text: str) -> List[str]:
    """Split a table file into its three dict bodies, in `ENUM_KINDS` order.

    Read back out of the *text* rather than imported, so a table being compared (`--out <file>`) does not
    have to be the module this process already has.
    """
    blocks = re.split(r'^(?:SYSTEM_CHUNKS|DRIVER_CHUNKS|FORMAT_NAMES)\b.*$', text, flags=re.M)[1:]
    return blocks + [''] * (len(ENUM_KINDS) - len(blocks))


def cmd_chunknames(argv: Sequence[str] = ()) -> int:
    """`chunk-names [--check|--write] [--out <file>] [--src <tree>]`: the bundled table against a source tree.

    The table is what the tool names chunks with where a tree is not (`load_chunk_names`), so it has to keep
    up with the tree this project reads from: `--check` (the default) says whether the checked-in file is what
    that tree would generate, `--write` regenerates it. Exit codes are `build --check`'s: **0** the table is
    current, **1** it differs or is missing, **2** there is no tree to compare it with -- a tree that is not on
    this machine is the state of a working tree rather than a failure, which is the same reason `goldens
    --check` exits 2 and why this cannot be a gate in CI.

    `--out <file>` and `--src <tree>` are for a caller that is not this repository: a test, or a script
    keeping a table for another RenderDoc release beside the tool.
    """
    write = '--write' in argv
    out = table_path()
    src_root = RENDERDOC_SRC
    index = 0
    while index < len(argv):
        option = argv[index]
        if option in ('--write', '--check'):
            pass
        elif option in ('--out', '--src') and index + 1 < len(argv):
            index += 1
            if option == '--out':
                out = argv[index]
            else:
                src_root = argv[index]
        else:
            print('usage: rdc_analysis.py chunk-names [--check|--write] [--out <file>] [--src <tree>]')
            return 2
        index += 1

    wanted = table_text(src_root)
    current = ''
    if os.path.isfile(out):
        with open(out, encoding='utf-8', newline='') as handle:
            current = handle.read()
    print('table     : %s (RenderDoc %s: %s)'
          % (out, rdc_chunknames.VERSION or 'version unknown', _table_counts(current)))
    # A tree is "there" when at least one of the three enum files is: a partial tree can be compared against
    # (what it does not have is what the table is *for*), and only a machine with no tree at all has nothing
    # to say -- which is exit 2 rather than a failure, like every other gate whose input is working-tree state.
    if not any(os.path.isfile(enum_path(src_root, kind)) for kind in ENUM_KINDS):
        print('tree      : %s (not here)' % src_root)
        print('verdict   : no tree to compare with -- nothing was read and nothing was written (exit 2)')
        return 2
    print('tree      : %s (RenderDoc %s: %s)'
          % (src_root, rdc_renderdoc_src.version(src_root) or 'version unknown', _table_counts(wanted)))
    if write:
        if not rdc_renderdoc_src.is_populated(src_root):
            # Writing from a half-extracted tree would *shrink* the table to what that tree happens to have,
            # which is the opposite of the point: the table exists to cover what a tree does not.
            print('error    : %s is not a complete tree (%s), so nothing was written'
                  % (src_root, ', '.join(rdc_renderdoc_src.missing_parts(src_root))))
            return 1
        with open(out, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(wanted)
        print('written   : %s (%d line(s)) -- review the diff before keeping it' % (out, wanted.count('\n')))
        return 0
    # Line endings normalised, like a golden's: `core.autocrlf` rewrites a checkout and would otherwise make
    # every line of a correct table look changed.
    if current.replace('\r\n', '\n') == wanted:
        print('verdict   : current -- the table is what this tree would generate')
        return 0
    print('verdict   : out of date -- %s (write it with `--write`)' % _table_drift(current, wanted))
    return 1


def _table_drift(current: str, wanted: str) -> str:
    """Why a table is not what a tree generates: the counts that moved, and one example name per kind."""
    have, want = _table_blocks(current), _table_blocks(wanted)
    notes: List[str] = []
    for kind, left, right in zip(ENUM_KINDS, have, want):
        left_ids = dict(re.findall(r'^\s+(\d+): (.+),$', left, re.M))
        right_ids = dict(re.findall(r'^\s+(\d+): (.+),$', right, re.M))
        gone = sorted(set(left_ids) - set(right_ids))
        new = sorted(set(right_ids) - set(left_ids))
        renamed = sorted(cid for cid in set(left_ids) & set(right_ids) if left_ids[cid] != right_ids[cid])
        if gone or new or renamed:
            notes.append('%s: %d new, %d gone, %d renamed%s'
                         % (kind, len(new), len(gone), len(renamed),
                            (' (e.g. %s -> %s)' % (left_ids[renamed[0]], right_ids[renamed[0]]))
                            if renamed else ''))
    return '; '.join(notes) if notes else 'the table differs from the tree (rewrite it with `--write`)'


__all__ = [
    'ALIGN_UP_DEFAULT',
    'AS_KINDS',
    'BARRIER_CHUNKS',
    'CHUNK_64BITSIZE',
    'CHUNK_ALIGN',
    'CHUNK_CALLSTACK',
    'CHUNK_DURATION',
    'CHUNK_THREADID',
    'CHUNK_TIMESTAMP',
    'CLEAR_CHUNKS',
    'COMPUTE_CHUNKS',
    'COPY_CHUNKS',
    'DESCRIPTOR_COPY_CHUNKS',
    'DESCRIPTOR_KINDS',
    'DISCARD_CHUNKS',
    'DRAW_CHUNKS',
    'ENUM_KINDS',
    'EXPECTED_LENGTHS',
    'HEAP_CHUNKS',
    'MARKER_CHUNKS',
    'POP_MARKER_CHUNKS',
    'PUSH_MARKER_CHUNKS',
    'RENDERDOC_SRC',
    'RESOURCE_CHUNKS',
    'RESOURCE_KINDS',
    'STATE_CHUNKS',
    'STATE_SETTERS',
    'TABLE_NAME',
    'TARGET_CHUNKS',
    'UNATTRIBUTED_CHUNKS',
    '_DESCRIPTOR_COPY_SIZE',
    '_DESCRIPTOR_WRITE_MIN',
    '_RESOURCE_DESC_SIZE',
    '_RESOURCE_TAIL',
    '_SRC_WARNED',
    '_find_renderdoc_src',
    'align_up',
    'cmd_chunknames',
    'enum_name',
    'enum_path',
    'load_chunk_names',
    'parse_chunk_enum',
    'parse_enum',
    'table_path',
    'table_text',
]
