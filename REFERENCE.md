# Reference — internals, the command reference, examples, pitfalls, the driver

The detail behind `README.md`, kept in one place so the README can stay an introduction. **Sections keep
their original numbers** (3–9) because the code, `ROADMAP.md` and the tests cite them (`REFERENCE §4.11`,
`REFERENCE §9`); sections 1, 2 and the agent playbook stayed in the README, and are referred to from here
by name.

## 3. How it works

### 3.1 Container (`.rdc` file)

From `renderdoc/serialise/rdcfile.cpp`:

```
FileHeader(32)                      magic 'RDOC' | version | headerLength | progVersion[16]
BinaryThumbnail(8 + jpg)            width u16 | height u16 | length u32 | jpeg
CaptureMetaData(13 + name)          driverID u32 | nameLen u8 | name | ...
CaptureTimeBase(16)                 timeBase u64 | timeFreq double
then N x { BinarySectionHeader(40) | name | data }
```

`BinarySectionHeader`: type u32 at +4, `compLen` u64 at +8, `uncompLen` u64 at +16, version u64 at +24,
flags u32 at +32, `nameLen` u32 at +36, then the name (NUL-terminated, `nameLen` bytes). `parse_container()`
walks this into `info['sections']`, stopping at the first byte that is not 0 (the end-of-sections marker).

### 3.2 Decompression

`get_stream()` handles the three cases seen in the wild:

* **Zstd** — auto-detected by the `28 b5 2f fd` magic (at offset 0 or 4); needs the `zstandard` module.
* **LZ4** (`flags & 0x2`) — a sequence of `u32 compressedBlockLength` prefixes, each followed by one **raw LZ4
  block** (no frame header), decoded by `decompress_lz4()` through the vendored library
  (`LZ4_decompress_safe_usingDict`, called with `ctypes`): one destination of exactly `uncompLen` bytes, one
  call per page, and the 64 KB before the write position as the dictionary. Measured on this repo's captures:
  **0.54 s** for `desktop-2`'s 1.47 GB and **0.14 s** for `desktop-1`'s 631 MB — the decode itself is
  0.219 s / 0.078 s (6.4 and 7.7 GB/s) and the rest is the container's own body copy and the zeroed
  destination.

  The library is `bin/rdc_lz4.dll`, written by `cmake --build build` from the vendored decoder in
  `src/cpp/third_party/lz4` (its own target, §9); `$RDC_LZ4_DLL` names another explicitly, and a system
  `lz4.dll` / `liblz4.so.1` / `liblz4.dylib` is accepted, so a Linux box with `liblz4` needs no build of ours.
  **There is no second decoder** (deliberately): with none of those present `decompress_lz4` raises
  `FrameError` naming the command that fixes it, because a stream decoded by something laxer than this library
  is a stream every command downstream would go on to read as fact. A page the library refuses, and a body
  that does not come out at exactly `uncompLen` bytes, raise for that same reason. A cache hit (§4.8) pays
  nothing at all, which is what most commands are.

  The speed comes from the shape rather than the library: the dictionary is the destination's own tail, which
  is the contiguous case LZ4 has a fast path for, so nothing is allocated or copied per page. A decoder API
  that hands back a fresh `bytes` object per page (the `lz4` PyPI package, which measured 562 MB/s for exactly
  that reason) is what this replaced, and no `pip` package is involved.

  The blocks are **pages of one continuous LZ4 stream, not independent frames**: RenderDoc compresses with
  `LZ4_compress_fast_continue` and decompresses with `LZ4_decompress_safe_continue` over a shared stream
  context (`serialise/lz4io.cpp`), so a match may point up to 64 KB back into the previous page. That is why
  one destination is carried across the blocks and why the blocks **cannot** be decoded in parallel: a pooled
  attempt produced 625,911,281 bytes for `desktop-1` where the section declares 630,790,592. See §8 and
  `decompress_lz4`'s docstring for the measured evidence.
* **raw** — copied as-is.

Only **section 0** (the frame capture) is decompressed; other sections are listed by `sections` but not parsed.

### 3.3 Structured-data (SDChunk) stream

From `renderdoc/serialise/serialiser.cpp` (`Serialiser<Reading>::BeginChunk`):

```
u32 c                    chunkID = c & 0xffff, flags = c & 0xffff0000
if c & 0x10000   u32 numFrames; u64 frames[numFrames]     (callstack)
if c & 0x20000   u64 threadID
if c & 0x40000   u64 durationMicro
if c & 0x80000   i64 timestampMicro
if c & 0x100000  u64 length      else  u32 length
payload[length]
align to 64              Serialiser::ChunkAlignment
```

`iter_chunks()` implements exactly this and yields `{off, id, flags, length, data}` where **`data` is the
payload offset** — already past the metadata. Chunk IDs come from `SystemChunk` (`renderdoc/core/core.h`,
`FirstDriverChunk = 1000`) and `D3D12Chunk` (`renderdoc/driver/d3d12/d3d12_common.h`); `parse_chunk_enum()`
parses those C++ enums at runtime, **out of the `renderdoc-src` tree in the root folder** — fetched on demand
when it is not there (README §1.1, `rdc_renderdoc_src`) — so the names are the vocabulary of the RenderDoc
version that produced the capture. A tree that is absent, older than the capture, or half-extracted does not
leave the ids unnamed: the same enums of a released RenderDoc are checked in as `src/py/rdc_chunknames.py`, the
tree's are written over them, and the warning says which version the fallback names are from (`chunk-names`,
§4.1).

> **The single most important detail:** in real captures the flags are usually `0xf0000` (callstack + thread +
> duration + timestamp all present), so **the payload starts 36 bytes into the chunk, not 8**. Never hand-compute
> the payload offset — use `chunk_payload(stream, ch)` or `chunk <index>`, which prints the header size.

### 3.4 Payload decoding

`decode_chunk(name, blob)` decodes the command payloads that matter for graphics work. All D3D12 payloads start
with the command list's `ResourceId` (a `u64`), and a `D3D12BufferLocation` is serialised as a
**(resourceId, byteOffset) pair — not a raw GPU virtual address**:

| Chunk | Payload |
|---|---|
| `List_SetPipelineState` | `u64 cmdList, u64 pso` |
| `List_DrawIndexedInstanced` | `u64 cmdList, u32 indexCount, u32 instanceCount, u32 startIndex, i32 baseVertex, u32 startInstance` |
| `List_DrawInstanced` | `u64 cmdList, u32 vertexCount, u32 instanceCount, u32 startVertex, u32 startInstance` |
| `List_Dispatch` | `u64 cmdList, u32 x, u32 y, u32 z` |
| `List_IASetVertexBuffers` | `u64 cmdList, u32 startSlot, u32 numViews, u64 count` then per view 24 B: `u64 resId, u64 offset, u32 size, u32 stride` |
| `List_IASetIndexBuffer` | `u64 cmdList, u8 present, u64 resId, u64 offset, u32 size, u32 format` (33 bytes; `present == 0` means a null view and only the first 9 bytes are written) |
| `List_SetGraphicsRootConstantBufferView` | `u64 cmdList, u32 rootParam, u64 resId, u64 offset` |
| `List_SetGraphicsRoot{ShaderResource,UnorderedAccess}View` | `u64 cmdList, u32 rootParam, u64 resId, u64 offset` |
| `List_SetGraphicsRoot32BitConstants` | `u64 cmdList, u32 rootParam, u32 numValues, u64 arrayCount, u32 values[n], u32 destOffset` (so `length == 28 + 4n`) |
| `List_SetGraphicsRootDescriptorTable` | `u64 cmdList, u32 rootParam, u64 heapId, u32 descriptorIndex` — a `D3D12_GPU_DESCRIPTOR_HANDLE` is a `PortableHandle`, not a pointer |
| `List_SetComputeRoot{Signature,ConstantBufferView,DescriptorTable}` | the same layouts as the `Graphics` ones above; only the root-parameter namespace differs (checked against both captures by `verify`) |
| `List_Reset` | 64 bytes: the list's creation parameters (IID, node mask, type, baked id), then the **command-list id at +40** and the **initial PSO at +48** — the id at +40 is the one every other `List_*` chunk carries at +0 |
| `Device_Create{Committed,Placed,Reserved}Resource` | the `D3D12_RESOURCE_DESC` follows the leading args (committed: heap props 20 B + heap flags 4 B; placed: heap id 8 B + heap offset 8 B; reserved: none), and every one of them ends `IID(16), u64 resourceId, u64 gpuAddress` — so the **id is at `length - 16`** and a buffer's base VA at `length - 8`. 117/145, 109/118/137 and 93 bytes in the captures tested |
| `Device_Create…Resource1/2/3` | the same, with a `D3D12_RESOURCE_DESC1` (whose **first 48 bytes are the same struct**) and a castable-format list after it — 149 bytes for `…CommittedResource3` in a capture, and one offset covers them all |
| `CreateAS` | `u64 buffer, u64 offset, u32 type, u64 byteSize, u64 asId` (36 bytes) — an acceleration structure is a **sub-range of a buffer**, and the type is `TOP_LEVEL = 0` / `BOTTOM_LEVEL = 1` |
| `Device_CreateRootSignature` | the serialiser's framing around a one-part DXBC container holding `RTS0` (found by its magic, cross-checked against the length at `+4`); the **id is the last 8 bytes** — see §8 for the signature's own layout |
| `SetName` | `u64 objectId, u32 length, utf-8 name` — RenderDoc names every object, not only resources, which is what makes heaps and queues identifiable |
| `Device_CreateDescriptorHeap` | `D3D12_DESCRIPTOR_HEAP_DESC` (type, count) + IID + the heap id at `length - 16` + the original GPU base — 56 bytes |
| `Device_Create{ConstantBuffer,ShaderResource,UnorderedAccess,RenderTarget,DepthStencil}View` | the descriptor first — the **resource id is at +16**, and for an SRV/UAV/RTV (whose serialised description opens with `DXGI_FORMAT Format;`) the **view's format is at +24**. A DSV carries `Flags` as well and a CBV has no format at all, so both are left as 0 rather than read at an offset belonging to another field. The destination `PortableHandle` is last (`u64 heapId` at `length - 12`, `u32 index` at `length - 4`). 68 bytes for an SRV, 80 for a UAV in the captures. The view's format is what `srgb-view-mismatch` compares a resource's declared format against (§4.11) |
| `Device_CopyDescriptors` / `…Simple` | `u64 count`, then `count x (u32 heapType, dst PortableHandle, src PortableHandle)` — 28 bytes per entry (36 bytes for a single copy) |
| `List_ResourceBarrier` | `u64 cmdList, u32 NumBarriers, u64 arrayCount`, then one entry per barrier — `u32 Type, u32 Flags` and the arm the type selects: a transition is 28 B (`u64 resId, u32 Subresource, u32 StateBefore, u32 StateAfter`), an aliasing barrier 24 (`u64 before, u64 after`) and a UAV barrier 16 (`u64 resId`). The serialiser writes the *active arm member by member*, so the entries are not one length and a decoder has to walk them by type |
| `List_Barrier` | `u64 cmdList, u32 NumBarrierGroups, u64 arrayCount`, then per group `u32 Type (0/1/2 = global/texture/buffer), u32 NumBarriers, u64 count` and its elements: global 16 B (`SyncBefore, SyncAfter, AccessBefore, AccessAfter`), texture 60 B (the same six access words, `u64 resId`, a 24-byte subresource range, flags) and buffer 40 B (the access words, `u64 resId`, offset, size) |
| `List_OMSetRenderTargets` | `u64 cmdList, u32 NumRenderTargetDescriptors, u64 arrayCount`, the RTV `D3D12Descriptor`s, a `u8` "DSV present" flag and the DSV descriptor. A descriptor is `u32 type, u64 heap, u32 index, u64 resId, u32 format, u32 dimension` and then the arm the dimension selects (up to 16 B for an RTV, 12 for a DSV) — the call serialises the descriptors, not handles, so this is a walk |
| `List_Clear*View` | the target's descriptor (an RTV/DSV clear) or a 12-byte `PortableHandle` and then the descriptor (a UAV clear), then the clear value, `NumRects` and the rects. The **resource is at +24** (RTV/DSV) or **+36** (UAV) — a descriptor's own resource field, verified against the resource table on all 100 clears and discards of the two captures |
| `List_DiscardResource` | `u64 cmdList, u64 resId, OPT(D3D12_DISCARD_REGION)` — 17 bytes with no region, 37 with one |
| `List_CopyBufferRegion` | `u64 cmdList, u64 dst, u64 dstOffset, u64 src, u64 srcOffset, u64 numBytes` (48 B, in `EXPECTED_LENGTHS`) |
| `List_CopyTextureRegion` | `u64 cmdList`, a `D3D12_TEXTURE_COPY_LOCATION` on each side with `DstX/Y/Z` between them, then `OPT(D3D12_BOX)`. A location is `u64 resId, u32 Type, the arm the type selects` — and the types are the other way round from the obvious guess: **0 = a subresource index** (4 B, so the location is 16) and **1 = a placed footprint** (28 B, so 40). All five payloads in the captures here are 77 bytes and take the type-0 arm |
| `List_ResolveSubresource` | `u64 cmdList, u64 dst, u32 dstSubresource, u64 src, u32 srcSubresource, u32 format` — **36 bytes**, in `EXPECTED_LENGTHS`, with the format last. The `Region` form (`List_ResolveSubresourceRegion`, the ID3D12GraphicsCommandList7 call) inserts the destination X/Y after the subresource, then the source subresource, then the optional source rect as a present byte + 16 bytes, then the format and the resolve mode: **49 bytes without a rect, 65 with one**, and an unreadable length is refused rather than read as a resolve of the wrong subresource. Decoded since 2026-09-22 as a read→write pair naming *which* subresource of each side (§4.15) — before that the two chunks were counted as unattributed, so a resource only ever resolved into read as unused |
| `Device_CreateHeap` | the `D3D12_HEAP_DESC` (40 B: size, 20 B of properties, alignment, flags), the IID (24 B — its `Data4[8]` array carries its own `u64` count, which is why the payload is 72 and not 64) and the heap id at `length - 8` |
| `InitialContents` | `u64 resourceId` + resource description, then the data — only the id and the first header bytes are decoded (`chunk <N>`); reading the contents is the replay driver's job |
| `Device_CreatePipelineState` | created PSO id first, then the desc with inlined shader bytecode (DXBC containers embedded) |

Chunk **length is a checksum for your decoder**: `IASetVertexBuffers` with 7 views must be `24 + 24*7 = 192`.
`verify` checks the fixed-length payloads in this table against every chunk in a capture, which is how the
descriptor-table length (24, not the 20 the struct alone suggests) was caught.

### 3.5 DXBC / DXIL containers

`parse_dxil_containers()` finds every `DXBC` magic and validates the container header
(`magic | hash[16] | version u32 | size u32 | partCount u32 | partOffsets[partCount] u32`), then yields the
parts (`fourcc, offset, length`). Part meanings:

| Part | Content |
|---|---|
| `RDEF` | resource bindings (cbuffers, textures, samplers, bind points) — **absent from every capture tested here**, see §8 |
| `RDAT` | reflection blob — cbuffer variable names, source file names, type info (the tool only reads its strings as ordinary stream text; what a shader *reads* is the replay driver's job, §8) |
| `ISG1` / `OSG1` | input / output signatures: `u32 count`, then `count` × 24-byte elements (`nameOffset u32, semanticIndex u32, register u32, ...`), with the string table after the array. The tool reads them only far enough to label a container `PS`/`VS` (`SV_Target` vs `SV_Position`) |
| `ILDN` / `ILDB` | shader bytecode |
| `RTS0` | **root signature** — decoded by `_parse_root_signature()` (§3.4, §8); `rootsig` prints it and `draws` annotates every `rpN` with it (§4.10) |

---

## 4. Command reference

### 4.1 Container level (fast, no decompression)

| Command | Arguments | Output |
|---|---|---|
| `sections` | `<rdc>` | file size, rdc version, progVersion, thumbnail, driver name/id; every section (type, flags, version, compressed/uncompressed size, name); then the decompressed size of section 0 vs expected, and the method used |
| `verify` | `<rdc>` | walks the chunk stream and checks what would make a parse untrustworthy: frames claiming bytes the stream does not hold, and payload lengths that disagree with the layout the decoder expects (see §3.4). Also reports the alignment padding totals — non-zero padding is legal (stale buffer bytes) so it is a note, not a failure. Exit code 0/1, so it can gate a script |
| `blocks` | `<rdc>` | per section: name, flags, first 16 bytes hex — enough to identify compression (`28b52ffd` = Zstd) |
| `resources` | `<rdc> [limit=200] [nameFilter] [--format table|csv|markdown]` | the resource table: id, kind, byte size or dimensions + DXGI format, and the name the application gave it (§4.9) |
| `descriptors` | `<rdc> [limit=200] [heapFilter] [--format table|csv|markdown]` | the written slots of every descriptor heap: heap, slot, kind (cbv/srv/uav/rtv/dsv/sampler) and the resource it points at (§4.10) |
| `cache` | `[list\|dir\|clear]` | inspect or clear the decompressed-stream cache (§4.8); needs no capture file |
| `diff` | `<a.rdc> <b.rdc> [--all] [--format table\|csv\|markdown]` | the two streams' own calls, compared: marker path, arguments, the state chunks that changed before each call, and every binding — by what each slot is, not by its index (§4.18) |
| `rootsig-check` | `<rdc> [bundleDir] [--format table\|csv\|markdown]` | what the root signatures declare against what the stream binds and the heaps hold, and (with a bundle) against the engine's own rows. Exit 0/1 (§4.19) |
| `vram` | `<rdc> [maxPasses=8] [--drop <nameFilter>] [--format table\|csv\|markdown]` | the frame's memory by role, the pass with the largest peak live inside it, and the what-if arithmetic (§4.20) |
| `sweep` | `<dir> [--out <root>] [--commands <file>] [--overwrite] [--min-bytes N] [--limit N] [--exe]` | one bundle per capture in a folder, plus an index of what they are — the corpus's GPU half, and the command that makes the replay driver's library worth having (§4.21) |
| `bootstrap` | `[tag]` | fetch the RenderDoc source tree the chunk names come from into `renderdoc-src` (README §1.1). Every command does this on demand; this runs it up front, pins a tag, and is the one path where a failed download is an error rather than the fallback to the bundled table. Needs no capture file |
| `chunk-names` | `[--check\|--write] [--out <file>] [--src <tree>]` | the bundled enum table (`src/py/rdc_chunknames.py`) against a source tree: `--write` regenerates it from that tree's `SystemChunk`, `D3D12Chunk` and `DXGI_FORMAT` enums, `--check` reports the drift (exit 0 current, 1 differs or is missing, **2 no tree to compare with**). `--write` refuses an incomplete tree, because a table written from one would shrink to whatever that tree happens to have. Needs no capture file |
| `build` | `[--check]` | is `bin/replay_dump.exe` older than the sources it is built from (`src/cpp/*.cpp\|h` and `CMakeLists.txt`)? Without `--check` a stale or missing binary is built with `cmake --build build --config Release`, with the compiler's own output going straight to the console and the verdict printed again afterwards. Exit codes: 0 current (or the build succeeded), 1 out of date (with `--check`) or the build failed, **2 nothing to compare for an artefact** — no binary, no library or no sources, which is a fresh clone and not a mistake. Needs no capture file. The driver makes the same comparison for its own binary and says so in its log (§9) — that warning does not cover the library, which it never loads. **All three artefacts `cmake --build` writes are compared, each in its own block**, because they can disagree and the difference matters: a stale exe answers with the previous revision's behaviour, while `bin/rdc_lz4.dll` *is* the offline tool's decoder (§3.2), so a stale one decodes every capture with code its source no longer says. The exe and `bin/rdc_replay.dll` share their sources and so are compared against the same tree — deliberately, because a library from one revision answering beside an exe from another is the drift that makes a library worse than a subprocess — while the decoder is independent of both: a new `lz4.c` makes neither of them stale, and a new `replay_dump.cpp` does not make the decoder stale |

### 4.2 Stream text mining

`minlen` is the exact minimum length of a printable-ASCII run (`string_runs()` builds the pattern per
request), so short names are found rather than silently dropped. The commands that print marker or shader
names use their own floor — `markers` / `summary` / `draws` ask for 3+ characters, signature parts for 2–4 —
so a two-character name is still not shown by those.

| Command | Arguments | Output |
|---|---|---|
| `strings` | `<rdc> [minlen=6] [maxlines=200]` | unique ASCII strings ≥ `minlen`, ranked by occurrence count then first offset, with offsets |
| `names` | `<rdc> [minlen=10]` | strings matching UE/RenderDoc keywords (`Shader`, `BasePass`, `Lightmap`, `Volumetric`, `IndirectLighting`, `HISM`, `Instanced`, `StaticMesh`, `Mobile`, `CachedPoint`, `Policy`, `Permutation`, `SceneColor`, `Primitive`, `View`, `FShader`, `VertexFactory`…), in stream order, max 400 |
| `grep` | `<rdc> <pattern> [context=200]` | every byte-occurrence of the ASCII pattern, printed with ±context bytes rendered as text (cap 30 hits) |
| `dump` | `<rdc> <start> <length> [minlen=4]` | every ASCII string inside the byte window (decimal offsets only — `hex` takes `0x…`), max 500 |
| `count` | `<rdc> <pat1> [pat2 …]` | count and first offset for each pattern |
| `hex` | `<rdc> <start> <length>` | hex + ASCII dump of a window (accepts `0x…`) |

### 4.3 Numeric / pattern search — removed

`float` and `pattern` used to search the raw stream for uniform *values* (the tool's own docs called the
pattern command "built for locating uniform buffers by a known prefix"). That is a guess at frame data the
replay engine hands over directly and exactly — `GetCBufferVariableContents` returns the named members and
their values — so both commands were removed rather than kept as a worse answer (§9). The
structural search commands (`grep`, `count`, `names`, `strings`, `hex`, `dump`) stay: they inspect the *file*,
which replay does not expose.

### 4.4 Shader level

| Command | Arguments | Output |
|---|---|---|
| `dxbc` | `<rdc>` | one row per DXBC/DXIL container: index, offset, size, stage (`PS`/`VS`/`root-sig`/`?`, from `SV_Target` vs `SV_Position`), hash and the parts it carries. This is an inventory — what a shader *reads* is the reflection's job (§8) |
| `dump-shaders` | `<rdc> <outdir>` | writes `shader_NN_<hash>.dxil` per container plus `shaders.txt` (hash, size, parts) — feed the `.dxil` to `dxc`/`dxil-spirv`/RenderDoc, or to a harness of your own (README recipe K) |

### 4.5 Chunk level

| Command | Arguments | Output |
|---|---|---|
| `summary` | `<rdc> [--format table|csv|markdown]` | chunk count, draw/dispatch count, marker count, chunk-type histogram (top 40), and all markers in order with their chunk index. **A chunk index is not an event id in general** — the replay driver's `probe` showed the engine numbers only what a command list recorded, and on one capture the two were tens of thousands apart (§9) |
| `markers` | `<rdc>` | every marker chunk: index, kind, up to 3 strings (handles ASCII and UTF-16LE) |
| `chunks` | `<rdc> [limit=200] [nameFilter]` | chunk index, offset, **name**, payload length, and a preview of the strings inside — the way to find a chunk by name |
| `chunk` | `<rdc> <index>` | full inspector: id/name/flags/length, payload offset **and header size**, decoded fields via `decode_chunk`, 160-byte hex dump, and the payload's strings |
| `draws` | `<rdc> [maxDraws=80] [--format table|csv|markdown]` | per-draw table (see below) |
| `deps` | `<rdc> [maxResources=40] [table\|dot\|mermaid]` | who writes what and who reads it, from the capture's own stream: writes/reads per resource with the first and last event, `read-before-write` and `write-never-read` flagged and their evidence printed, and the same graph in DOT or Mermaid (§4.15) |
| `memory` | `<rdc> [maxRows=20]` | what the frame's memory adds up to: placement and kind with byte totals, capture-relative lifetimes, the aliasing barriers it hands memory over with, heaps ranked by what sharing could save, and every figure's caveat (§4.15) |
| `rootsig` | `<rdc> [maxSigs=40] [--format table|csv|markdown]` | every root signature the capture creates: version, cost in root-argument DWORDs, static samplers, flags, and each parameter with its type, register, space and descriptor ranges |
| `psos` | `<rdc> [maxRows=40] [--hash <hash>] [--format table\|csv\|markdown]` | the pipeline state objects the frame creates and the shaders each one holds, by hash: the id a command list binds, kind, how many `SetPipelineState` calls name it, whether its debug data is embedded or needs a PDB, and each stage's hash. `--hash` answers one hash -- any of a container's three identities, by prefix -- from the cached index, **exit 1** when it is not in the capture (§4.22) |
| `dump-chunk` | `<rdc> <index> <outfile>` | writes the chunk payload to a file |

**`--format table|csv|markdown` on the commands that print rows** (`draws`, `resources`, `descriptors`,
`rootsig`, `summary`, `formats`, `psos`, `vram`, `rootsig-check`, `diff`) prints the same rows as a CSV or a
Markdown table instead of the terminal's own layout, for
pasting into an issue or opening in a spreadsheet. `table` is the default and is the command's own printing,
byte for byte. In the other two **stdout is the table alone** and the lines the terminal form prints around
it (`resources: 542 ids (516 with a descriptor, 528 named)`, `total resources: 542 (shown 40)`) go to
*stderr*, because a prose line in the middle of a CSV is not a row. A format changes the *shape* of the
answer, never its selection: `resources` still honours its limit, `summary` still lists the top 40 chunk
types and the first 120 markers, and `draws` folds each draw's indented state lines into one cell separated
by `; `. Values are quoted as RFC 4180 asks (a comma, a quote or a newline), and a `|` in a Markdown cell is
escaped. The rows themselves live in `rdc_table.py`; the commands build them once and print either form from
that one list, which is what keeps the terminal and the CSV from disagreeing.

#### `draws` — the per-draw table

Maintains the state of each **command list** while walking the stream: marker stack (`PushMarker`/`PopMarker`),
pipeline state (`List_SetPipelineState`), root signature and root bindings (graphics and compute: CBVs, root
SRV/UAV descriptors and descriptor tables), vertex streams (`List_IASetVertexBuffers`), the index buffer and the
render targets `List_OMSetRenderTargets` binds. On every draw/dispatch it
prints the state that is *in effect* — everything still bound, not only what changed since the previous draw:

```
#1098   WorldGridMaterial Sphere 60964    x=314 y=0 z=1 ExecuteIndirect
        CBV: rp2(cbv b0 s0)=res1907+0x184e00[Resource Allocator Under]
        Table: rp0(table t0 n64 s0)=heap298[279360][GlobalResourceHeap]
        CBV: rp1(vs cbv b0 s0)=res1907+0x93300[Resource Allocator Under]  rp2(vs cbv b1 s0)=res1907+0x19c000[Resource Allocator Under]
        Table: rp0(vs table t0 n64 s0)=heap298[279373][GlobalResourceHeap]
        VB : res315+0x3f7400(sz6708,st12)[Resource Allocator Under]  res60868+0x0(sz65536,st0)[InstanceCulling.Instance]
        IB : res315+0x3f2b00[Resource Allocator Under]
```

`rp<n>(...)` carries **what the root signature says that parameter is** (§3.4, `rootsig`): `cbv b0 s0` is a root
descriptor at register b0 space 0, `table t0 n64 s0` is a descriptor table whose range starts at t0 with 64
descriptors, `32bit b0 s0 n4` is four root constants, and a leading `vs`/`ps` is the parameter's visibility. A
parameter whose `RDEF` reflection survived carries its name too (`rp2(cbv b1 s2) [SceneCB]`); none of the
captures here do (§8). This matters most where an index alone is ambiguous: the `ExecuteIndirect` above reports
*both* namespaces, and `rp2` is a compute CBV at b0 in one and a vertex CBV at b1 in the other.

`rp<n>=res<id>+0x<offset>` is a root-parameter CBV binding; `rp<n>=heap<id>[index]` is a root-parameter
descriptor table — the heap and the slot it points at, plus what the capture wrote into that slot
(`-> srv res2233[SkyViewLut]`, §4.10) or the heap's name when the slot was never written in this frame (§8);
`res<id>+0x<off>(sz,st)` is a vertex stream (resource, byte offset, size, stride). A `res0+0x0(sz0,st0)` entry
is a **NULL vertex buffer** — a useful signature in itself.

The *write* side of the state is printed the same way: `RTV:`/`DSV:` are the targets the list last bound
(`List_OMSetRenderTargets`, whose payload carries the descriptors themselves — §3.4), and `SRV:`/`UAV:` are root
descriptors of those kinds, which the stream carries in the same `(resource, byteOffset)` shape a root CBV uses.
Two draws of `desktop-1`'s deferred base pass:

```
        RTV: res60857[SceneColor]  res60858[GBufferA]  res60859[GBufferB]  res60860[GBufferC]  res60861[GBufferD]  res60862[GBufferE]
        DSV: res60843[SceneDepthZ]
```

Those lines are bindings, not uses — a draw that binds a UAV and writes nothing still says so there. `deps`
(§4.15) is what turns them into writes and reads, and says what it cannot see.

Every resource the capture named carries that name in `[...]`, truncated to 24 characters, and descriptor
tables carry their heap's name — `GlobalSamplerHeap` says the table holds samplers, which is the difference
between "a resource I cannot see" and "a sampler table":

```
#270    GridInject:NotLinkedList 2300     x=8 y=5 z=2 Dispatch
        CBV: rp2(cbv b0 s0)=res1907+0x174b00[Resource Allocator Under]
        Table: rp0(table t0 n64 s0)=heap298[279425] -> srv res384[Resource Allocator Under]
```

UE sub-allocates its buffers inside page buffers, so a binding into a `Resource Allocator Underlying Buffer`
or a `Fast Allocator Page` is a *sub-allocation*: the page has a name, the logical buffer inside it does not
(§8). Use `resources` (§4.9) to turn any id into a kind/size/name.

D3D12 bindings belong to the command list, so this is the real state and not a heuristic: they survive
`SetPipelineState` and every draw, and only change when something rebinds them. Two events clear the root
bindings — `Reset()` (a fresh list, which may start with an initial PSO) and a root signature that actually
*differs* from the current one ("if a root signature is changed, all previous root arguments become stale";
setting the same one again keeps them). State is tracked per command list, and a null index buffer clears the
binding. Dispatches report the compute root bindings, draws the graphics ones plus IA state; the two namespaces
are separate. At most 16 vertex views per `IASetVertexBuffers` chunk are tracked.

### 4.6 Tests

`tests/` holds a self-contained unittest suite covering every parser, decoder, command and the CLI dispatch.
It needs **no capture file, no GPU, no `renderdoc.pyd` and no `renderdoc-src` checkout**: the
fixtures build synthetic `.rdc` containers, SDChunk streams, D3D12 payloads and DXBC containers in memory
(`tests/rdc_fixtures.py`), and the chunk-name map is stubbed with a fake enum tree.

| Command | Effect |
|---|---|
| `python src\py\rdc_analysis.py selftest` | run the whole suite (`test` is an alias) |
| `python src\py\rdc_analysis.py selftest -v` | per-test output |
| `python src\py\rdc_analysis.py selftest -k Draws` | only tests whose id contains `Draws` |
| `python tests/test_rdc_analysis.py` | the container, the compression and the cache (95) |
| `python tests/test_rdc_chunks.py` | the chunk stream, the payloads and the shader containers (112) |
| `python tests/test_rdc_resources.py` | the resource table, descriptor heaps and the enum parsing (93) |
| `python tests/test_rdc_commands.py` | commands and CLI dispatch (151) |
| `python tests/test_rdc_report.py` | the report, its notables, its recommendations, its detectors and the report schema (70) |
| `python tests/test_rdc_engine_schema.py` | the engine table: recognition, concepts, markers, values, no match (21) |
| `python tests/test_rdc_validate.py` | the schema validator (17) |
| `python -m unittest discover -s tests -t tests` | the same suite through unittest |

The files are split by area, and `tests/rdc_testcase.py` holds what they share: the capture builders'
case classes, the stdout capture helpers, and the scratch-directory handling. It is a support module, not a
test file — `discover` only collects `test_*.py`.

Exit code is 0 when everything passes, 1 on failure, 2 for a bad option.

The real-capture integration tests are skipped unless a capture is pointed at them
(`$env:RDC_TEST_CAPTURE = 'C:\path\capture.rdc'`). Copies of the three captures used here live in the ignored
`renderdoc-src/` folder — two from Unreal Engine (PC and Android) and `desktop-2` from a
custom D3D12 renderer, which is the one that exercises ray tracing, `...CommittedResource3` and `CreateAS`.
They take about a minute, because each command re-decompresses the whole stream unless the cache (§4.8) has
it. A third class parses the real `renderdoc-src` enums and is skipped when the tree is absent. Tests that pin
behaviour which looks wrong are marked `CHARACTERIZATION` in the source, so a deliberate fix does not read as
a regression.

### 4.7 Type checking

The tool and its tests are kept clean under **Pylance/Pyright "Standard"** mode: `pyrightconfig.json` pins
`typeCheckingMode` (and `pythonVersion` 3.8), `typings/zstandard.pyi` stubs the optional dependency, and the
whole codebase is annotated — `TypedDict`s describe the dicts the parsers return, and no `Any` is left in
`rdc_analysis.py`. To check it:

```powershell
npx --yes pyright@latest        # expect: 0 errors, 0 warnings
```

The coding rules that keep it that way are in `AGENTS.md`.

### 4.8 Caching

Decompressing a frame-capture section costs seconds (§8) and every command needs the same stream, so the
decompressed bytes are cached on disk, keyed by the capture's identity: absolute path, size, mtime, section
index and cache format version. The path is **case-normalised** (`os.path.normcase`), so `C:\x.rdc` and
`c:\x.rdc` are one entry — on Windows a shell prompt and `Resolve-Path` disagree about the drive letter, and
without that a capture was cached twice. The first command on a capture decompresses and writes the cache;
every later one reads it back, and the method label says so:

```
framecapture stream: 630790592 bytes  (expected 630790592)  [lz4(602 blocks)]           <- first run
framecapture stream: 630790592 bytes  (expected 630790592)  [lz4(602 blocks, cached)]   <- after that
```

Measured on the 61 MB PC capture in this repo: `sections` 3.56 s → 0.27 s, `verify` 4.0 s → 0.41 s.

| Environment variable | Effect |
|---|---|
| `RDC_CACHE_DIR` | where the cache lives (default `%LOCALAPPDATA%\rdc-tools\cache`, or `$XDG_CACHE_HOME`/`~/.cache` elsewhere) |
| `RDC_NO_CACHE` | set to anything non-empty to disable the cache: no reads, no writes |

| Command | Effect |
|---|---|
| `python src\py\rdc_analysis.py cache` | list the entries: stream size, method, build time, source capture — and, on its own line, any *derived* files (an answer about a stream rather than a stream: `derived_names`) |
| `python src\py\rdc_analysis.py cache dir` | print the cache directory |
| `python src\py\rdc_analysis.py cache clear` | delete every entry **and every derived file** (prints files removed and MB freed) |

The cache is pure optimisation and cannot change what a command prints apart from that label. An entry is used
only when it was built from exactly this file (same absolute path, size and mtime), for this section, and holds
a stream at least as long as the section's `uncompLen` — anything else is ignored and rebuilt, and the file is
deleted. Writes go to a temporary name and are renamed into place, so an interrupted run cannot leave a
half-written stream behind, and a rebuild prunes the entries for the same capture/section that were built from
an older version of it. Raw sections are never cached (there is nothing to decompress); a cache directory that
cannot be written only warns once on stderr, and nothing else changes.

The unit tests point `RDC_CACHE_DIR` at a scratch directory, so they never touch the real cache (§4.6).

### 4.9 Resources

`resources` builds an id → description table from the resource-creation chunks and the `SetName` chunks, and
`draws` uses it to annotate every binding (§4.5):

```powershell
python src\py\rdc_analysis.py resources 'capture.rdc' 20      # first 20 rows
python src\py\rdc_analysis.py resources 'capture.rdc' 0 lut   # every row whose name contains "lut"
```

```
resources: 241 ids (211 with a descriptor, 234 named)
res271      buffer    2097168 B                              m_MeshletBuf
res2233     texture2d 256x64x1 mips=1 fmt=R8G8B8A8_TYPELESS  SkyAtmosphere.TransmittanceLut
res298      unknown   -                                      GlobalResourceHeap
total resources: 241 (shown 2)
```

* `kind` and the size come from the `D3D12_RESOURCE_DESC` in `Device_CreateCommittedResource` /
  `CreatePlacedResource` / `CreateReservedResource` (offsets in §3.4): a buffer reports its byte size, a
  texture its dimensions, array size, mip count and DXGI format. `CreateAS` adds ray-tracing acceleration
  structures as `blas` / `tlas` with their byte size — they are sub-ranges of a buffer and the frame
  references them by their own id.
* The names come from `SetName`, which RenderDoc emits for every D3D12 object — heaps, queues, fences and PSOs
  included. Those have no descriptor, so they show `-` and `kind=unknown`; they are in the table because
  `draws` prints their ids (`heap298[279377]`).
* DXGI format names are parsed out of RenderDoc's own copy of the enum (`common/dds_readwrite.cpp`), and fall
  back to the bundled table's copy of it on a machine with no tree, exactly like chunk names (README §1.1).
* `limit` counts rows (`0` = no limit, as in `chunks`) and `nameFilter` is a case-insensitive substring of the
  name, so `0 lut` means "every row with `lut` in the name".

### 4.10 Descriptors

`descriptors` lists what the capture *wrote* into each descriptor heap, which is what makes a table binding
readable:

```powershell
python src\py\rdc_analysis.py descriptors 'capture.rdc'        # every written slot
python src\py\rdc_analysis.py descriptors 'capture.rdc' 0 298  # one heap, by id or by name
```

```
descriptor heaps: 2, 16 written slots
heap298 GlobalResourceHeap
  [279336   ] uav      res60823[VirtualTextureFeedbackGP]
  [279422   ] srv      res384[Resource Allocator Under]
heap300 FD3D12OfflineDescriptorManager
  [533      ] uav      res2266[NumCulledLightsGrid]
total slots: 16 (shown 16)
```

`draws` uses the same table: a root descriptor table prints the descriptor in the slot it names
(`rp0(table t0 n64 s0)=heap298[279425] -> srv res384[Resource Allocator Under]`) and falls back to the heap's
name when that
slot was never written during the capture (§8). The layouts are in §3.4; what matters here is:

* a `Device_Create*View` payload holds the descriptor (the **resource id at +16**) and *ends* with the
  destination `PortableHandle` (`u64 heapId` at `length - 12`, `u32 index` at `length - 4`). The *kind* comes
  from the chunk name, so a sampler records no resource;
* `Device_CopyDescriptors` / `…Simple` are `u64 count` followed by
  `count x (u32 heapType, dst PortableHandle, src PortableHandle)` — 28 bytes each. Following them is not
  optional: UE writes a descriptor into one heap and copies it into the heap the frame binds from;
* writes and copies are applied **in stream order**, which is the order D3D12 applies them in: a slot written
  twice holds the second write, and a copy sees its source as it was at that point;
* only written slots are recorded, so "no entry" means "the capture does not say", never "empty".

---

### 4.12 Schema validation

`validate <file|bundleDir> <schemaDir|one.schema.json> [kind]` checks documents against the schemas the
driver publishes — `schema/` in this repo, written by `replay_dump schema --out schema` and checked in —
plus the schemas of the two documents the *offline tool* writes, which are data in `rdc_schemas.py` rather
than files, because that folder is what the driver publishes: `REPORT_SCHEMA` for `report.json` and
`AB_SCHEMA` for `replaydiff.json`. A folder that carries a `report.schema.json` or a
`replaydiff.schema.json` of its own wins, and `validate <bundleDir> <schemaDir>` checks `report.json` along
with everything else, which is what keeps the report's JSON twin honest. The contract is therefore a file a
consumer can read rather than something reverse-engineered from a writer — and for the A/B it closed the same
gap: `replaydiff.json` used to carry a `schemaVersion` and no shape at all, so a reader could tell which
version of the document it held and nothing else, and `validate` could not answer "is this one this tool
wrote?".

```powershell
python src\py\rdc_analysis.py validate bundle schema        # every document in a bundle
python src\py\rdc_analysis.py validate t.json schema textures   # a document saved from a command's stdout
python src\py\rdc_analysis.py validate ab\diff\replaydiff.json schema replaydiff   # the A/B's own kind
```

Every `--json` document carries `schemaVersion` (1 today), so a consumer can refuse a shape it does not
know. The validator implements the subset the schemas are written in — `type`, `required`, `properties`,
`items`, `enum`, `const`, `additionalProperties` — and reports any *other* keyword as a failure instead of
ignoring it: a silently-skipped keyword is how "validated" stops meaning anything. `additionalProperties` is
`false` throughout, so an unlisted member is an error, which is what catches a writer and its schema drifting
apart. A schema change is a one-command regeneration plus a review of the diff:

```powershell
.\bin\replay_dump.exe schema --out schema
.\bin\replay_dump.exe schema --check schema      # fails when the folder and the driver disagree
```

That check is what makes the regeneration a rule rather than a habit: it compares the folder against the
driver's own table — missing files, files whose text differs, and files left behind for a document kind that
no longer exists — and exits non-zero on any difference (line endings are folded away first, so a checkout
that rewrites them is not reported as a difference). Every branch of it is covered by the driver's `selftest`,
which is why the checked-in copy cannot quietly go stale after a document changes.

### 4.11 The frame report

`report <rdc> <bundleDir> [outDir]` turns a **bundle** — what `replay_dump dump` writes (§9) — into
`report.md` and `report.json`, written into the bundle directory unless `outDir` says otherwise.

| part of the report | what it is |
|---|---|
| provenance | the capture path as recorded, its SHA-256, the RenderDoc and driver versions, the bundle's flags, which files were read, the capture's own properties (API, local replay, vendor, shader debugging, counters), and how many detectors ran of how many |
| frame at a glance | counts, resources by kind and bytes, the render targets and formats seen, debug messages by severity |
| pipeline map | the passes in order — eid range, call kind, target, structure — plus a Mermaid graph of pass → target |
| the engine's vocabulary | which engine the capture's own names identify, every concept those names claim (with the name behind it), and the values the table tags for them — see below |
| pass by pass | per pass: why it *starts* there (the boundary reason), work in events, targets, structure, the shaders it uses, their constant blocks, and the resources first used in it |
| notable passes | the ranking's top five, plus every pass an oddity rule matches (depth-only, one call, unmarked, dead, the only pass touching a resource) — with the **rule printed above the rows**: each input, how it is measured, and which ones *this* bundle could not answer |
| notable resources | the same for resources: ranked by size and by how many passes read them, plus never-read, targets, formats whose bytes mislead, and textures the table names no format for |
| red flags | what the detectors found, each with the evidence that proves it and how certain it is — grouped by the severity table printed with it (the tool's declared judgement, per detector, so a line of it can be disagreed with). A finding is marked **`proven`** with the *cause* the corpus knows for it, or **`unproven`** where no cause has been established (§4.17): the corpus's `known` list is matched by the capture's SHA-256, and `rdc_report.apply_known` is the only thing that opens that gate (§4.17) |
| recommendations | what to look at first, ranked by the same severity: one row per detector that fired, per oddity rule that matched and per gap the report could not close — each with the driver command that shows its evidence |
| what this report cannot tell you | the report states its own gaps, and every one of them is a roadmap item |
| appendix | the `replay_dump state` / `shaders` / `usage` commands that reproduce a pass, a claim and a notable row |

**Notability is a stated rule, not a score.** Every notable list prints the inputs it ranks by, in order, with
the way each is measured — and it prints the ones it could *not* use just as plainly: the *work* a pass's calls
asked for is the first input of the notable-pass ranking, and it is in a bundle written from 2026-09-22 on (the
driver takes it from the engine's action list, §9's `volume`); a bundle older than that carries none, and the
table then says so rather than ranking every pass by a zero. Counter cost is only in a bundle written with
`--with-counters`. A texture is ranked in *pixels*, not bytes,
because a bundle records its dimensions and format but not its byte count; where one figure has to compare a
texture with a buffer, it is counted at 4 bytes per pixel and the `~` on that estimate says so. Two claims the
lists deliberately do not make: "read by 0 passes" is only printed for a resource the engine *tracked*, and a
resource with no usage rows at all, or with only the documented `eid 0, Unused` marker, is reported as what it
is — untracked, which is not the same as unread. UAV rows (`CS_RWResource`) are counted separately, because
they say a resource was reachable for reading *and* writing and not which happened.

It is **deterministic** — byte-stable for a fixed bundle (sorted tables, no timestamps, no paths in the prose),
so two runs diff cleanly and an analysis change shows up as a reviewable diff. It reads only the bundle's own
files: no capture, no GPU, no device, no `renderdoc-src`. `tests/test_rdc_report.py` tests it from fixture
bundles written by hand, which is what keeps the analysis honest without a capture to hand.

### The engine's vocabulary (`engine-schemas/*.json`)

A pass is described by its state everywhere else in the report. To say what it is *for* — `mobile base pass`,
`indirect lighting cache`, `shadow depth pass` — the report needs the meaning of the names, and those are the
engine's, so the mapping lives in a table a reader can check, extend or disagree with:
`engine-schemas/*.json` at the repository root (this is not the driver's `schema/`, §4.12 — that is the
contract for the driver's own documents). `$RDC_ENGINE_SCHEMAS` or a folder named `engine-schemas` in any folder
above `src/py/` overrides the location.

Three rules, and the section in the report is written to show all three:

* **Name-based, nothing else.** A concept is claimed because the capture itself contains a name the table
  lists: a constant-block name, a shader entry point, a resource name, a marker name, or the pass *structure*
  string the report itself computed. Nothing is inferred from values, order or timing.
* **Every claim carries its evidence.** Each row names the pass or the frame it was claimed for and the exact
  string that matched, so a reader can open `states/<eid>.shaders.json` and see it.
* **A marker matches by its own name, not by the whole path.** The driver records the engine's marker path per
  event (`Scene > BasePass`), and a table lists `BasePass`: every element of a path counts, because the paths
  carry dynamic text (`CullLights 22x14x8 NumLights 0`) that no table could enumerate. A bundle written before
  2026-09-17 carries no marker at all, and then a marker-based concept is reported as *not claimed for want of
  evidence* rather than as absent.
* **No match, no claim.** An engine whose names the table does not list gets no interpretation at all, and the
  report says so instead of guessing. A table with fewer than two name matches is not an identification.
  A concept is claimed only when *every* kind it asks about matches: `{"constantBlocks": ["MobileBasePass"],
  "shaderEntries": ["MainVertexShader", "MainPixelShaderMRT"]}` needs both, because a block that is merely
  *bound* in a pass is a leftover from an earlier call as often as it is a fact about that pass.

The values under a concept are the members the table **tags** (`"members": ["IndirectLightingSHCoefficients",
…]`, matched by name or by the `Name[0]` / `Name_Field` spelling of one), read from the bundle's cbuffer
documents, each with the eid its document was written at and the report pass that event falls in — which is what
makes the claim checkable: `replay_dump cb <capture> <eid> <stage> <slot>` prints the same numbers.

The project's original question is the acceptance case, and it is two lines of the same section. On
`mobile-1` the table claims **mobile base pass** (`MainVertexShader`/`MainPixelShaderMRT` with
`MobileBasePass` bound) plus the **indirect lighting cache** and **mobile reflection capture** blocks: at eid 262
nothing is bound to the cache (every member reads its default, and the row says so), and at eid 313
`IndirectLightingCacheMaxUV = 1, 1, 1` with `DirectionalLightShadowing = 1` — the cache is filled from the
frame's own state, not left at zero. On `desktop-1` it claims **base pass** (`MainVS`/`MainPS` with
`Scene`, `Material`) plus **reflection capture (SM5)** and **forward lighting**, with
`ForwardLightData.NumReflectionCaptures = 0`, and the marker concepts name the passes outright
(`BasePass (engine marker)` for pass 24, `depth prepass` for pass 9). That is the difference between the two
frames in the engine's own words — a mobile forward base pass reading a cached indirect lighting volume against
an SM5 base pass with no reflection captures — and every number is one `replay_dump cb` away.

The **detectors** that run today are the ones the evidence can prove. From the bundle: the engine's own debug
messages, constant blocks whose every value is zero (including the "no descriptor is bound for this block"
case), constant blocks whose register a root parameter is not set for at all, and — from the descriptor tables
the driver resolves through the engine — registers whose slot holds nothing or whose declared range and heap
type disagree (the flagship row: *nothing bound where the reflection expects something*, in all three of its
halves, and *binding kind mismatch*), pixel inputs the vertex shader does not emit (*VS out ≠ PS in*, ignoring
the `SV_` system values and interpolation suffixes — and, from the engine's component counts in the reflection
rows, an input that reads *wider* than the vertex shader writes), and textures or buffers no call in the frame
uses. From
the **usage chain** — the engine's own record of which events used each resource and how — four more: a read
with nothing in the frame writing it first, a write nothing afterwards reads, a render target first used with
nothing clearing or writing it, and a compute pass whose UAV bindings nothing afterwards reads. Each is a
*question*, not a verdict, and the finding says why: a static
asset, a CPU readback and a present are indistinguishable from a bug in a usage list. From
the **pipeline state** the driver records per state change — viewport and scissor, depth, stencil, blend —
five more, each stated per eid *range* because that is what one state document covers (and never at a
dispatch, which inherits whatever the last draw left bound): depth writes with the test off (or a test with
nothing bound), an enabled viewport or scissor with no extent, stencil testing a target nothing earlier wrote,
and two `[heuristic]`s — blending on while writing a target whose *name* says GBuffer or base pass, and a
`float` pixel-shader output into an 8-bit-or-narrower linear target. `mismatched-msaa` needs none of that
state: `samples` is in the resource table and a resolve is a usage row, so a multisampled colour target
nothing ever resolved is decidable as it stands.

Cross-referenced against the three captures to hand, the state rules say something on two of them and
nothing on the third for a checked reason: `depth-logic` fires once on `desktop-1` (depth testing on
with no depth target bound, eids 888–891), `stencil-without-writer` once on `desktop-2` (eids 841–851
testing a *read-only* depth-stencil target nothing wrote), and the two heuristics are silent there because
that frame's 30 GBuffer-named target bindings are all bound with blending *off* and it binds no 8-bit linear
target at all — an HDR frame, not a gap in the rules. That is the difference between a heuristic and a guess:
the finding names what it keys off, so its silence is checkable too. The 601 MB `desktop-2` is dumped as a
600-event window (`--max-events`), which the report's provenance states. From
the capture's chunk stream: marker imbalance,
unattributed draws, calls that can only draw nothing (0 vertices/indices/instances/groups), and a texture
declared with a linear format read through its sRGB form — the resource table's `format` against the view
formats the descriptor writes declare, which is the half of the format rule a bundle cannot answer. A detector
that could not look — no usage lists with `--no-usage`, no chunk-name map at all (no source tree *and* an
empty bundled table), a capture that has moved — is reported as *skipped* with the reason, because "clean"
and "not checked" are
different answers, and a bundle with no findings says so without implying the frame is fine.

What it does **not** do yet: counters folded into the pass sections; and the UAV side of a dispatch (§2's item,
so a compute pass still says "targets and depth not applicable"). Two gaps it used to carry are closed, one on
each side of the line: the *work* a pass's calls asked for is in a bundle written from 2026-09-22 on (the
driver's `volume`, §9, taken from the engine's action list) and is the notable-pass ranking's first input, and
the sRGB/linear half of the format rule is its own finding (`srgb-view-mismatch`, §4.11) read from the file —
the resource table's declared format against the formats the views over it declare. MSAA's *which subresource*
half is readable from the file too (`deps` prints every resolve as a read→write pair naming both subresources,
§4.15); what stays unclaimed is whether a given resolve was the *right* one. Passes are *state-derived*, not
named — a run of events that agree on call kind and render targets, or on pipeline and shaders for a dispatch —
and the report says so in its own words rather than describing a pass as something it has not established.

### 4.13 Speed: what a long command costs, and the scan that splits

Every command above is dominated by one of three things, and `$RDC_PROFILE=1` prints which: the container
parse, the stream (a cache hit, or decompression), and the one pass over the bytes the command actually asks
for. It ends the run with a table on **stderr** -- stdout is the contract and nothing here touches it:

    profile: where the time went (this run has $RDC_PROFILE set)
    profile:   string scan (parallel)     3.67s in      1 call(s)
    profile:   stream: cache read         0.84s in      1 call(s)
    profile:   container parse            0.33s in      1 call(s)

`$RDC_PROGRESS=1` adds the live lines without the table (`$RDC_PROFILE` implies both): a line every ten
seconds with the rate and what is left.

    progress: string scan: 16 slice(s): 1.4 GB
    progress: string scan: 3/16 slice(s) (18%), 0.7 s each, ~9 s left
    progress: string scan: 1.4 GB done in 5.4s (260.4 MB/s)

The slots are `container parse`, `stream: cache read`, `stream: decompress`, `stream: cache write`,
`resource table`, `descriptor heaps`, `root signatures`, `chunk payload decode`, `verify: walk`,
`shader bind names`, `report: bundle read`, `report: detectors`, `string scan (serial)`, `string scan
(parallel)` and `byte find (parallel)`; each is the `rdc_profile.timed(...)` decorator on the layer that does
the work, so a command that calls a layer twice adds two calls to one slot rather than inventing a slot per
command.

**Read the numbers as ratios.** The same serial scan of the same stream measured 14.5 s and 25.2 s an hour
apart on this machine (64 cores, other processes running); what stayed stable is the parallel path being
6-13x faster *within one session*. Measured that way, on `desktop-2` (601 MB container,
1.47 GB stream, 29,212 chunks):

| command | before | after | what it was |
|---|---|---|---|
| `chunks <rdc> 0` (all 29,212, a preview each) | >300 s (killed) | 12-19 s | collecting every run at `minlen=4` (8.4 M of them), de-duplicating with `not in`, then capping at 6 |
| `names` (minlen 10, 427,823 unique) | 17.7 s | 3.9-6.5 s | one `re` pass over 1.47 GB |
| `strings` (minlen 6, 1.16 M unique) | 17.6 s | 7.9-10.2 s | the same pass, with counting |
| `report` (either bundle) | 2.9 s / 1.7 s | 2.5 s | unchanged: bundle read 0.36 s + detectors 1.4 s |
| the rest (`sections`, `summary`, `chunks N`, `resources`, `descriptors`, `draws`, `markers`, `verify`, `rootsig`, `dxbc`, `validate`) | -- | 0.8-2.6 s | container + stream + one table |

**Why threads and async cannot help.** The scan is one `re` call over a 1.47 GB buffer: it holds the GIL for
the whole call, so threads take turns and buy nothing, and there is no I/O to overlap for async. Processes are
the only lever, and `rdc_scan` pulls it:

* the stream is cut into slices at boundaries **no match can cross** -- a cut is only allowed where the byte
  is non-printable, because a run cannot contain such a byte, so the concatenated per-slice results are
  exactly the whole-stream ones. The search for a boundary is bounded; when it finds none the range is simply
  not cut, which costs parallelism and never correctness;
* each slice is scanned in its own process and the results are merged in offset order -- counts add, the
  lowest first offset wins, which is what dict insertion order gave before;
* the children **map the stream cache's file** and are never sent the bytes, so a 1.47 GB scan costs no
  copies and no IPC beyond the per-slice dictionaries;
* every way that could go wrong falls back to the serial loop: no cache file (`$RDC_NO_CACHE`, a section that
  was never stored, a failed write), a file whose first or last 64 bytes are not this stream, a pool that will
  not start, or a stream under `MIN_BYTES` (64 MB -- below that the spawn costs more than it saves). `procs=`
  on `scan_runs` forces the count, and `procs=1` always means the serial path;
* `MAX_SLICES` is 32, and never more than the cores there are. The sweep that picked it, in one session:
  serial 31.1 s, 8 slices 7.3 s, 16 slices 5.3 s, 32 slices 4.0 s at `minlen=6`; 26.6 / 4.9 / 2.7 / 2.1 s at
  `minlen=10`.

**A byte pattern is the same shape with different arithmetic** (`rdc_scan.find_all`). The DXBC container
search that `draws`, `rootsig`, `dxbc` and `dump-shaders` each pay for is a `find` over the whole stream, not
a string run, so `split_ranges`' cut rule does not apply -- a byte pattern may start anywhere -- and the
slices are even cuts with a `len(needle) - 1` byte overlap, each hit reported by the slice that holds its
*first* byte. Both bounds are load-bearing and both are off by `len(needle) - 1`: the search starts early so
a needle straddling a cut is findable, and it runs *past* the slice end because `find` only reports a match
that fits inside its bounds -- a needle in the last three bytes of a slice ends outside it, and that slice is
the one that owns it. Measured in one session on the 1.47 GB capture, with byte-identical output: `draws`
1.32 s -> 0.88 s, `dxbc` 1.12 s -> 0.70 s, `rootsig` 1.14 s -> 0.74 s. `FIND_MIN_BYTES` is a **gigabyte**,
sixteen times `MIN_BYTES`, because this scan is 1 s where the string scan is 14: the pool costs 0.48 s to
start here, so on the 631 MB PC capture the same change measured 0.53 s -> 0.62 s, i.e. *slower* -- and there
the serial loop runs, which is why `find_all` carries its own threshold instead of borrowing `MIN_BYTES`.

**A worker is a fresh interpreter**, so whatever starts a pool must be importable without side effects:
`rdc_analysis.py` guards its `main()` and is fine; a script or test module that decompresses at module level
does it again once per worker (that is what one "stuck" measurement turned out to be). The suite never reaches
for a pool by accident -- its fixtures are far under `MIN_BYTES`, and the tests that exercise the pool pass
`procs=` explicitly.

**Caching is the other half, and it is already there.** A cold command pays the 1.47 GB stream's decode —
**0.5 s** through the library the build writes (§3.2) — and a warm one pays 0.4-0.9 s to read it from disk
(§4.8). Nothing *derived* is cached: a
string index would be tens of MB per capture to save a scan that is now seconds. The one derived thing worth
computing differently was per-chunk: `chunk_strings` stops as soon as it holds `limit` strings and skips the
UTF-16 pass entirely when the ASCII half already filled the cap -- the answer the old order produced (ASCII
first, then wide, then the cap), so `chunks <rdc> 0` went from over five minutes to under twenty seconds with
identical output, and none of the saving was in the decode.

**A second look** (2026-09-18) took the same profile apart again and found four things, all measured:

| what | before | after |
|---|---|---|
| `cmd_draws` slicing every payload up front | 1.082 s (copying 1.47 GB it never reads) | 0.004 s -- the slice is gated on `DRAW_CHUNKS`/`STATE_CHUNKS` |
| ranking 1.16 M strings with a `(-count, offset)` tuple key | 1.574 s | 0.716 s -- `key=counts.__getitem__`; ties keep dict insertion order, which *is* first-offset order |
| `unittest` (+ `http.client`) imported by every command | ~120 ms + ~90 ms | 0 -- both are imported where they are used (`selftest`, an actual download) |
| `sections`, `summary`, `markers`, `resources`, `descriptors`, `verify` | 1.1-1.8 s | 0.55-0.68 s |

`shader_bind_names` scans the stream for `DXBC` containers so a root parameter can be given the name its
reflection offers: 1.2-1.7 s on `desktop-2`, and **no names at all** on it (that capture's DXIL has
reflection stripped). Gating that scan on a chunk *name* would be a guess rather than a check -- a shader can
sit inside any payload, and the containers really are there (85 of them) -- so what it gets is the same sliced
`find` as everything else (`find_all`, above): the same one full pass, split across processes when the stream
is big enough. It has a `$RDC_PROFILE` slot (`shader bind names`) either way, because the reason `draws`
looked mysterious for an hour is that its largest cost had no name in the table.

**A third look** (2026-09-18, later the same day) measured what is *left*, and the largest numbers are not in
the scan at all:

| what | measured | why it stays |
|---|---|---|
| interpreter + `site`, before any work | 0.37 s of **every** command, and of every pool worker | `python -c pass` costs 0.37 s here, 0.14 s of it a `sitecustomize` an IDE installs; nothing inside the tool can change that |
| the stream's pages, in a fresh process | 0.78 s warm, 1.54 s once the file cache has dropped them | a full-stream scan faults them itself. `madvise(MADV_WILLNEED)` does not exist on Windows (checked: `mmap` has no `madvise`) and `PrefetchVirtualMemory` needs a *writable* buffer, which a read-only map cannot give `ctypes`; reading the stream into memory instead was already measured 0.30 s slower plus 1.5 GB (§4.14) |
| starting a pool | 0.39 s for 8 workers, 0.44 s for 16, 0.48 s for 32 | every worker is a fresh interpreter again -- which is why `MIN_BYTES` and `FIND_MIN_BYTES` are thresholds rather than "always use 32" |
| a derived container/name index | would remove this scan outright (~0.4 s) | declined again: a second on-disk artefact with its own invalidation, for less than the stream cache already saves |
| `count <rdc> <pattern>...` | ~0.7 s per pattern on the 1.47 GB stream (was ~1.0 s serial) | wired to `find_all` like the DXBC search: one split pass per pattern |
| re-running the string scan's own merge | the parent merges 586,944 keys in 0.257 s of a 1.74 s scan | not worth moving into the workers (added complexity for 15%) |

For the driver half of the same question, see §9: `dump` is one `SetFrameEvent` per event plus a sweep, both
bounded by the frame's last event id, and the sweep cache is what makes a re-run affordable (measured on a
full `desktop-1` frame: 68 s cold, 45 s with the sweep answered from the cache).

**A fourth look** (2026-09-22) went after what the three above left, and found that the *editing* half of the
tool had already been optimised while the *scanning* half had one lever nobody had pulled: the worker count
was 32 for both scans, and for a byte find that is four times too many. Measured on `desktop-2` (1.47 GB),
best of three in one session, `find_all(b'DXBC')`:

| workers | 1 | 4 | 8 | 16 | 32 |
|---|---|---|---|---|---|
| seconds | 0.678 | 0.702 | **0.554** | 0.613 | 0.741 |

32 is the **worst** of the five. A `find` is memory-bandwidth bound where the `re` pass is CPU-bound, so past
a handful of workers the only thing still growing is fresh interpreters; the string scan keeps its 32 (16
slices 2.50 s against 32 slices 2.21 s in the same session). `FIND_MAX_SLICES` is now 8 for `find_all`, and
`FIND_MIN_BYTES` still keeps the pool off `desktop-1` altogether (serial 0.255 s against 8 slices 0.383 s
there: a pool that saves 25% on a gigabyte costs 50% on 631 MB). A second session's A/B, same capture, old
against new: **0.637 s → 0.547 s** for one find, and **2.217 s → 0.746 s** for three.

The three patterns are `count`'s case, and they were three *pools* -- `cmd_count` called `find_all` once per
pattern, so the spawn, the mapping and the slice arithmetic were paid again for each. `find_all_many` scans
for a list of needles over one pool and one mapping per slice, one `find` loop each: the offsets it returns
are `find_all`'s, needle for needle and in the same order (an empty pattern still answers nothing, because a
`find` has no opinion about it). `count <rdc> a b c` on `desktop-2`: **2.247 s → 0.758 s**.

**The whole-stream DXBC search is now cached** (`<stream>.bindnames.json`, `rdc_resources.shader_bind_names`).
It is the one scan whose answer is a property of the *stream* rather than of the command -- where the
containers are, and what their `RDEF` parts name -- and it was being paid by `draws`, `rootsig`, `dxbc` and
both sides of `diff`: 0.60 s of `desktop-1`'s 1.15 s `draws` and 0.72 s of `desktop-2`'s 1.33 s, with
**zero names at the end of it on every capture in the corpus** (their DXIL has no `RDEF` left), which is
exactly why the empty answer is cached too. The sidecar is named after the stream cache entry
(`rdc_cache.sidecar_path`) and stamped with the stream's length and a digest of its first and last 64 KB, so
another stream's file is refused rather than half-believed; a version, a corrupted document, a missing file
and `$RDC_NO_CACHE` all fall back to the scan, which is why `$RDC_NO_CACHE` remains the transcripts' setting:
a golden cannot depend on what this machine has already scanned. Measured: `shader bind names` **0.60 s →
0.01 s** on `desktop-1` (`draws` 1.15 s → 0.35 s) and **0.72 s → 0.01 s** on `desktop-2` (`draws` 1.33 s →
0.62 s); the two files this machine wrote are 0.2 KB together. This reopens the artefact §4.13 declined
twice -- and the difference is what is being stored: not a container/name index over the whole stream, but
one small answer about a stream that already has a cache file beside it, with the same identity and the same
`cache clear`.

**A detector was answering a yes/no question with 579,154 `float()` calls.**
`detect_zero_constant_blocks` asked "is every value in this block zero?" by parsing every number out of
every row: 64,246 `findall` calls and a conversion per token, **0.234 s of `desktop-1`'s 0.277 s of detector
time** (0.098 of 0.109 on the mobile bundle). A decimal literal is zero exactly when its digits are: the rule
is now one `match` per row against `0+(\.0+)?([eE][-+]?\d+)?` with everything that is not a digit skipped,
and the token *count* that the finding's text carries is computed only for a document that is all zeros.
Measured: **0.234 s → 0.129 s** (`desktop-1`, same 6 flags) and **0.098 s → 0.048 s** (mobile, same 7). The
equivalence is pinned by a test that walks the awkward shapes against the old rule written out --
`-0.0`, `.0`, `1e-320`, `0x1F`, `nan`, a row with no number in it -- and it earned its keep on the first run:
`0.000e-9` is zero and carries a `9`, which the first version of the rule (and a naive "no digit but `0`")
called non-zero.

**The startup floor is the environment, not the tool.** `import rdc_analysis` costs ~135 ms here, of which
~90-125 ms is `site` plus the `sitecustomize` an IDE installs -- a bare `python -c pass` is 88-167 ms on this
machine, and the tool's own tree is 50-80 ms of that, most of which (`json`, `hashlib`, `tempfile`,
`subprocess`, `bz2`/`lzma`/`zlib`) *the environment loaded first*: with `-S`, the tool adds only `zlib`,
`json` and `hashlib`. What the tool was loading for nothing is now deferred to where it is used:
`multiprocessing` (**18.1 ms** of every command process, and most commands never start a pool -- `_pool` is
the one place one is made, and the tests' seam for "a pool that cannot start"), `tarfile`+`shutil` (4.8 ms,
the fetch), `subprocess` (the build, the harness). A *pool worker* still pays `multiprocessing`, because a
worker is what the import is for. There is nothing else here worth having: `-S` would take the optional
`zstandard` with it, and the interpreter's own share is not the tool's to spend.

**`goldens --check --capture <name>` no longer pays the pair's A/B.** The pair is a check about two captures,
and `check_pair` ran it whichever capture the filter named: 17.8 s of a 32.8 s single-capture run (measured
with `cProfile`: 35.3 s of `_thread.lock.acquire`, i.e. waiting for the A/B, against 3.6 s of that capture's
own transcripts and 3.9 s of its driver session). With `--capture` given, the pair is reported as **not
compared** -- "`--capture mobile-1` limits this run to one capture" -- and the whole-corpus run is unchanged.
The same session's numbers for the run itself: 33 s → 22.5 s for `--capture mobile-1`.


### 4.14 Reading the file: mapped, not copied

Every command used to pay two full reads before doing anything: the container (601 MB for `desktop-2`,
in `parse_container`) and the stream (1.47 GB, in `load_stream`). Both are now `mmap`s, because nothing needs
the bytes all at once -- the header and section table are a few KB, one section body is decompressed on a
cache miss, and a command that only walks the frame touches one 4 KB page per chunk header. Measured on
`desktop-2`, in one session:

| phase | before | after |
|---|---|---|
| `container parse` | 0.12-0.18 s | 0.00 s |
| `stream: cache read` | 0.30 s | 0.00 s |
| a frame walk (`iter_chunks` over 29,212 chunks) | 0.489 s (0.459 read + 0.030 walk) | 0.038 s |
| a full scan (one `re` pass over every byte) | 14.50 s | 14.59 s |

So the commands that walk lost 0.5-1.1 s each -- `chunks`, `summary`, `markers`, `resources`, `descriptors`,
`verify` and `sections` now run in 0.6-0.8 s where they were 1.5-2.6 s -- the scan commands lost the same read
and pay ~0.6% more for the regex over a map, and no command allocates 2 GB to look at a 601 MB capture any
more (the same stream also stops being copied out of the cache file, and `decompress_lz4` no longer copies the
1.47 GB it produced into `bytes`).

Three consequences worth knowing:

* **A stream may be an `mmap`, a `bytearray` or `bytes`** -- the type is `rdc_types.Buffer`, and everything
  that reads a capture takes one. Slicing it gives `bytes`, which is what the decoders do; a caller that needs
  plain bytes (to compare with `==`, or to keep after the call) writes `bytes(stream)`.
* **The cache format is version 2**: the header is padded to `CACHE_ALIGN` (Windows' 64 KB allocation
  granularity) so the stream begins where `mmap(offset=...)` accepts. Version 1 entries are ignored rather
  than misread, so the first run after this change decompresses once more; `cache clear` reclaims the old
  files, which `cache list` counts as unusable until then.
* **A mapped file is locked against writing on Windows** while a command runs: a capture cannot be replaced
  mid-run, and `cache clear` in another process fails on the file in use (it says so and moves on).
  `$RDC_NO_CACHE`, an unmappable file or a zero-length one falls back to reading.

What is left, and where the line is: the per-chunk loops inside the commands (`draws` is 2.8 s because it
walks all 29,212 chunks in Python and slices every payload; `summary` and `markers` do the same work for
0.8 s), the `re` pass `strings` and `names` are built on (14.5 s, split across processes -- §4.13), and the
report's detectors (1.4 s of a 2.5 s `report`). The walk itself is 0.038 s, so splitting those loop bodies
across processes would mean handing each worker the chunk index for its slice -- a real change, for about a
second. The line on derived artefacts has moved four times now, each one the same shape: an answer that is a property
of the *stream*, asked for by more than one command, cached beside the stream the cache already holds
(`rdc_cache.sidecar_path`) and stamped with the stream's length and `rdc_cache.stream_digest`. The
whole-stream DXBC search was the first (four commands and both halves of `diff`, 0.3-0.5 s each time); `psos`'
index (§4.22) the second; and the survey that produced this paragraph added two more, both of them the tool's
*slowest* commands, where the win is what a reader notices:

| sidecar | what it saves | measured |
|---|---|---|
| `.bindnames.json` | the DXBC search behind `draws`/`rootsig`/`diff` | 0.30 s of a 0.51 s `draws` (REFERENCE §4.13) |
| `.psos.json` | the same search for `dxbc`/`dump-shaders`/`psos` | `dxbc` 0.91 s → 0.23 s |
| `.names.json` | the `re` pass `names` filters | `names` 1.71 s → 0.14 s |
| `.strings.json` | the same pass's ranked head, for `strings` | `strings` 2.71 s → 0.14 s |

Each is keyed on the question it answers as well as the stream -- `names` and `strings` store their `minlen`,
and `strings` stores the *ranked head* rather than the 1.2 M unique strings, because a sidecar of that size
would cost more to read than the scan costs to run. Two things are still declined, and both were measured
rather than assumed. A *general* container or name index over a capture, for the reason this paragraph has
always given: one that anything could be looked up in, with its own invalidation and no single question behind
it, is a different kind of artefact. And a **C string kernel**: `re.finditer` is 93% of one slice's serial scan
(2.421 s of 2.603 s over a 200 MB slice), but that scan is already split across processes and what a kernel
cannot remove is the per-run decode and the merge of 1.2 M unique strings -- with the ranking cached at 0.14 s,
a kernel would have to beat that, and the measurement says it would not.

### 4.15 The frame's uses and its memory: `deps` and `memory`

`rdc_detect_usage` (§4.11) asks the same two questions of a replay driver's **bundle** — the engine's own
per-resource history — and these two commands are the file-side twin: one walk of the chunk stream, no device, no
GPU, and the same two names for the same two findings, so a result from either side can be read beside the other.
`tests/test_rdc_uses.py` tests both from fixtures (§4.6); the walk itself lives in `rdc_uses.py`.

**What counts as a use** (`rdc_uses.walk_uses`) is a stated list, not a guess:

| the use | how it is classified |
|---|---|
| a draw's or dispatch's bindings | a root or table CBV/SRV is a read, a UAV — root or through a table — is a read *and* a write, a vertex/index buffer is a read, a bound target (`RTV`/`DSV`) a write. A table slot resolves through the heap the frame wrote (§4.10); a slot it never wrote resolves to nothing and is *counted* |
| a clear, a discard | `List_Clear*View` writes; `List_DiscardResource` is its own kind of use (`discard`) |
| a copy | `List_CopyBufferRegion`/`List_CopyTextureRegion`: destination a write, source a read — the one place the stream shows a buffer produced without being bound to a pipeline |
| a barrier | classified by the state (or 1.7-era access word) the resource *enters*: `RenderTarget`, `UnorderedAccess`, `DepthWrite`, `CopyDest`, `ResolveDest` write, the shader-resource and copy-source states read, `UnorderedAccess` does both, `Common` neither. A `List_Barrier` texture barrier with the discard flag is additionally a discard |

Creating a resource, naming one, or writing a descriptor into a heap (`Device_Create*View`) is **not** a use.
Binding is not using either: a draw that binds a UAV and writes nothing reads as a write here, which is what the
words around every finding say.

**Measured** on the two captures (the stream is cached for every run after the first, §4.8; the ledger phase is
0.08 s of `deps`' 0.27 s / 0.45 s):

| | `desktop-1` (631 MB stream, 52 draws) | `desktop-2` (1.47 GB, 50 draws) |
|---|---|---|
| resources with a use the stream shows | 88 | 117 |
| `read-before-write` | 27 | 26 |
| `write-never-read` | 52 | 62 |
| table bindings that resolve to nothing | 118 | 9 |

Those counts are large for a reason worth stating plainly: **most of UE's descriptor tables are filled at
startup**, so a read through an unwritten slot is invisible — the PC capture resolves 9 slots of its 118 table
bindings (§8). The commands print that count *with* the finding, and `desktop-2`'s nine is why its
`write-never-read` list is the more meaningful of the two. `deps` also prints which resource-referencing chunks
it does **not** attribute (`rdc_chunkmap.UNATTRIBUTED_CHUNKS`: `List_ResolveQueryData`, `List_ExecuteIndirect`,
`List_BuildRaytracingAccelerationStructure`, `List_SetDescriptorHeaps`, …), because "nothing read it" and
"nothing this can see read it" are different sentences — and a payload that does not parse cleanly (a barrier
whose walk does not land at its end) is refused and counted rather than half-read.

`deps <rdc> [maxResources] [table|dot|mermaid]` — `maxResources` caps the table rows and the graph's resources
(0 = all). The graph is bipartite and literal: an event that wrote is a `w<eid>` node, one that read is `r<eid>`,
a resource is a box, and each edge is labelled with the binding kind. A resource nothing wrote has no producer to
connect from and is left out (the table still counts it); a UAV is drawn as both because it is both. Event ids
are **chunk indices** — the numbering `draws` prints, not the engine's (§8).

`memory <rdc> [maxRows]` — placement, size and lifetime from the same walk: `Device_CreateHeap` gives a heap its
size, a placed resource its heap and offset, and every figure containing a texture is marked `~` (pixels × 4: a
capture records no texture byte count). Its aliasing section reads the frame's own **aliasing barriers** first —
two resources handed the same memory, in order — and then ranks heaps by `total − peak`, what perfect packing
could save *if* every pair were legal (D3D12's aliasing requirements are not in the file, so it is an upper
bound). Two things it prints rather than hides: a lifetime here is **capture-relative** — D3D12 writes no release
to the stream, so it ends at the frame's last use, and a resource older than the capture has no creation event at
all — and a heap's recorded size need not agree with the totals beside it, because a texture's real size is not
in the file.

### 4.16 The A/B pair: `passdiff` and `replaydiff`

Two captures, one answer — the mobile-vs-PC question the project was started for. There are two commands
because there are two halves, and the halves are the tools' usual line (§2): `passdiff` reads the two
*`.rdc` files* (the marker trees inside their frame streams, no device, no bundle), while `replaydiff`
compares what the *engine said* about each capture, which it reads out of the two bundles.

```powershell
python src\py\rdc_analysis.py passdiff 'android.rdc' 'pc.rdc'                 # offline, no bundle
.\bin\replay_dump.exe dump 'android.rdc' ab\mobile --with-images
.\bin\replay_dump.exe dump 'pc.rdc'      ab\pc     --with-images
python src\py\rdc_analysis.py replaydiff ab\mobile ab\pc --out ab\diff --with-images
```

**Why `replaydiff` takes bundles and not two `.rdc` paths.** Because the engine replays one capture at a
time: two replay devices on one GPU is what makes a run look stuck (§9), so a command that opened both
would be a command that hangs. Everything it needs is already a file after `dump`, and reading files is
the half that is testable without a GPU, a capture or a driver (§4.6). The driver half stays what it
was — one `dump` per capture, the engine's answers — and this half is the analysis.

**`passdiff <a.rdc> <b.rdc> [--all]`** — the marker trees side by side. A *pass* here is a marker with at
least one draw or dispatch inside it (the rule the driver's `sheet` groups a frame by, so a path this
prints can be pasted into `--at-marker`), and a parent marker counts its children's calls, so a heading is
a pass too. The tree is built from `PushMarker`/`Queue_BeginEvent` through to `PopMarker`/`Queue_EndEvent`;
`SetMarker` sets the current name without opening a scope and is not walked, and a marker still open where
the stream ends is still recorded (the frame was cut — `verify` and the report's marker-balance detector
are what report the imbalance itself). A name is read with `MARKER_MINLEN = 3` characters, because a
marker payload's own frame decodes as one- and two-character printable runs *before* the name does
(measured: `chunk_strings(..., 1, 1)` answers `B` where the name is `MobileSceneRender`, and at two
characters it still answers runs that are not names); the shortest real names on these captures are
`Sky` and `Clear`.

The two lists are aligned by **full path** first and the **innermost name** second — a path carries dynamic
text no table could list, and one real pair has `CullLights 32x20x8 NumLights 0` against
`CullLights 22x14x8 NumLights 0`, the same pass with a different light grid. Both pair by *occurrence*, so
the second `Scene > Shadow` pairs with the second. What is left is `only in A` / `only in B`, except that a
pair in the same slot, under the **same parent marker**, with the **same call count**, is offered as
`renamed?` with that evidence in the row: the file cannot prove a rename, and a suggestion made on
position alone suggested nineteen renames between a mobile and a PC frame that share almost no passes.
Chunk indices are the file's numbering, not the engine's event ids (§9).

**`replaydiff <bundleA> <bundleB> [--out <dir>] [--with-images] [--image-detail N] [--threshold N]`** —
`replaydiff.md` and `replaydiff.json` (deterministic: sorted, no timestamps, every path relative to the
bundle or the output directory it belongs to, so two runs are diffable), plus a summary on stdout. It
compares, all of it from the documents the bundle carries:

| | |
|---|---|
| the frames' shape | events, passes, resources, API, and whether the two bundles describe **the same capture** (one `captureSha256` means a tools A/B, not a capture A/B) |
| the pass lists | aligned **by marker path**, then by **innermost name**, then by the **resource names both passes write**, then by **call order** (same call kind only) — each fallback writes itself into the row's note, so a match can be disagreed with instead of guessed at |
| effective state | at each aligned pass's first state event: the `rootParameters`, `shaders` and `renderTargets` rows with resource ids annotated by name (`res2207[SceneColour]`), plus the depth target |
| named values | every constant block of that event, paired by `(stage, slot)`, member by member — a block only one side binds is reported *with* its members, because "not bound over here" is often the answer |
| shaders | the `hash` the driver stamps each stage with (§9): *the same shader* or *a different one* as a fact, with the reflection rows that moved; a bundle from a driver before the hash says `no-hash-in-this-bundle` rather than calling equal reflection the same shader |
| the renders | with `--with-images`: each aligned pass's readback per slot, taken at the **last** event inside the pass that wrote one (a pass's first state event often has no target bound) |

A pass's marker is the path of the first event inside it that sits in one: the ids between two command
lists carry state but no markers (§9, the replay-history ghosts), and a pass that begins on one of those
would otherwise have no name to align on.

**The images are compared in two steps**, because only one of them is cheap: every pair is compared by the
file's **bytes** first (one encoder wrote both, so equal bytes are equal pixels and it costs nothing), and
a pair that differs is *decoded* and compared pixel by pixel while `--image-detail` lasts (`IMAGE_DETAIL`
= 8 by default; `rdc_image` is pure Python and measured at ~0.15 s per megabyte — a 9 MB readback is
1.0-1.4 s, dominated by Paeth rows at ~230 ns/byte, where an all-`Up` image runs at 66 ns/byte). The rest
are named **as differing** with the reason they were not compared: an image nobody looked at must never
read as an image that did not change. A compared pair reports the pixels that changed, the mean and worst
delta, the 64-bit difference hash's distance and a heat map (`<out>/heat/<pass>_<slot>.png`); two pictures
of different sizes are reported as two sizes, because a difference across sizes is a different question.
`--threshold` is a percentage: pairs below it are counted in the summary instead of listed row by row.

**Measured on the pair this exists for** (`mobile-1` against
`desktop-1`, bundles written with `--with-images`): `passdiff` reports 34 passes in the
mobile frame against 71 in the PC one, 20 paths in both, and the shifts that matter —
`SceneColorRendering > MobileBasePass` present on both, `desktop-1`'s ray-tracing, HZB, SSR, TAA and eye
adaptation passes present on one side only. `replaydiff` aligns 10 passes, reports 55 constant blocks
whose values moved and 16 shaders whose hash differs, names the adds/removes, and compares the one
`SceneColorRendering` pair both bundles hold a readback for: **49.077% of its 2,517,012 pixels differ**,
mean delta 245.904, worst 255. The other passes have no readback *pair* (the two frames' targets are bound
at different events), which the document states per row rather than leaving the reader to infer.

### 4.17 The corpus and its goldens: `goldens`

The regression net for the **analyser**, where the payload tests are the net for the parsers. One command
over one checked-in corpus:

```powershell
python src\py\rdc_analysis.py goldens                    # --check is the default
python src\py\rdc_analysis.py goldens --capture desktop-1 --verbose
python src\py\rdc_analysis.py goldens --write            # refresh the transcripts, then read the diff
```

| file | what it is |
|---|---|
| `goldens/captures.json` | the index: each capture's **key**, size and SHA-256, what it is and what is *known* about it, the commands whose transcript is pinned, the bundle to check it against, and the `pair` whose `replaydiff` numbers must reproduce. It names no files: a capture here is `desktop-1`, `desktop-2` or `mobile-1` (platform, then size ascending) and its identity is the digest |
| `goldens/captures.local.json` | **not in git**: `capturePaths` says where *this machine's* copies of those keys live. A key with no entry is *not compared*; a key the corpus does not have is refused (that is a typo, not a missing capture); a file that is absent -- a fresh clone, CI -- is simply no paths, which is the same answer as no captures |
| `goldens/<key>.expect.json` | the **labels**: which detectors must fire over the `.rdc` (and how many findings each), what the report over that capture's bundle must count, the findings *histogram*, and what `replaydiff` must say about the bundle against itself |
| `goldens/<key>/<command>.txt` | a **transcript**: the command line, the exit code, stdout and stderr, written by `--write` and compared byte for byte by `--check`, with the capture written `<capture>` and never a path |
| `goldens/<key>/driver.txt` | the **driver's own text**, in the same shape: one `batch` session per capture over that capture's `driverCommands`, its stdout and any finding on stderr, so the half that used to be compared by hand (rebuild, run, diff in the terminal) is a file that `--check` diffs |

**A transcript is a run of the CLI, not a call to a function**: each command is a subprocess of
`rdc_analysis.py` with the capture path inserted after the command name (`<capture>` in the corpus stands
for it where a command takes it twice, as `passdiff` does), so what is pinned is the command's output
contract, usage line and exit code. Three environment variables are fixed for those children:
`$RDC_PROFILE`/`$RDC_PROGRESS` are removed (a phase table would be a golden of this machine's speed),
`$RDC_NO_CACHE` is set, because the stream cache is invisible *except* in one place — the `, cached` marker
in a method label that `dxbc` and `verify` print — and a transcript that depends on what this machine has
already decoded fails on a fresh checkout, and `$PYTHONIOENCODING` is pinned to `utf-8`, because the parent
decodes the child's stdout as UTF-8: a shell that exports the variable (or a console on another code page)
would otherwise decide whether the `§` in the tool's own text arrives as itself or as a replacement
character. That was found the direct way rather than reasoned about — a `--write` run under
`$PYTHONIOENCODING=utf-8` showed two checked-in transcripts carrying the replacement character, which is a
golden that passes or fails depending on who runs it. Line endings are normalised on the way in and out, so
a `core.autocrlf` checkout compares equal.

**The driver's text is pinned the same way, with two differences that are the environment's and not the
answer's.** One `batch` session per capture (`replay_dump batch <capture> <file>`, written under
`build/goldens/<key>/driver.batch.txt`) runs the capture's `driverCommands` — `info`, `draws 12`,
`textures`, `debug` for the two captures here — and the golden records the command line, the exit code and
what came back. The engine's own identity is normalised: the header's `renderdoc` field names the *installed*
RenderDoc rather than the capture's, so it is written `<engine>`, where the version guard is what judges that
number (§9). And the driver's stderr enters the golden as **findings only**: its per-step log lines carry
seconds, an absolute working directory and a timestamped log-file name, which are this machine's run rather
than the answer, while `error:`/`note:`/`warning:` lines — written outside the log — are kept. A run that
fails for the *machine's* reasons (no `renderdoc.dll`, no replay system, no device, or a capture from a newer
RenderDoc than the engine, which the guard refuses) is reported as **not compared**, exactly as a missing
capture is, so `goldens --check` still answers on a machine that cannot replay; any other failure is compared
and fails, and a failure with no output is **never** written as an expectation, because a golden of a crash
would be a golden that says a crash is correct. `desktop-2` pins no driver commands for the same reason it
pins no transcript.

**The path is redacted.** Everything a run records — the command line, both streams, and the `capture`
member of an A/B document — says `<capture>` where the path was, in each spelling it arrives in (the path as
given, its other slash, and the JSON-escaped form a document carries). Two reasons, and the second is why
this is a rule rather than a nicety: the corpus is committed and read by everyone, while a path is this
machine's state and a file's name may name the frame it came from; and a golden that carried the path would
fail the moment the same capture moved, where the digest is what identifies it. The file's *stem* (`My
Frame` for `My Frame.rdc`) is deliberately not one of the forms: it cannot be told from the tool's or the
frame's own words, so rewriting it could silently alter an answer. That no checked-in golden carries a path
or a file name is a test over the corpus rather than a hope.

**A capture's own strings are a different question from its path, and redaction cannot touch them.** A
transcript *is* the frame's words — its marker names, its resource names, its shader entry points — because
reproducing what the tool prints is the whole point of one. So a capture whose author has not said its words
may be published is in the corpus **by identity only**: `desktop-2` has no transcript, no label, no
document and no driver text, its `commands` and `driverCommands` lists are empty by design, and its `known`
lines are numbers and API facts rather than quotations. What is published about it is its size and digest, which still verify the file where it is
present. The check that nothing of it is left anywhere is a scan rather than a reading: regenerate that
capture's transcripts into a throwaway corpus under `build/`, subtract the words the other captures use, keep
the identifier-like remainder, and grep the tree for those. That remainder was 111 identifiers, and the only
hits are the public ones the tool cannot avoid — `List_Barrier`, `Device_CreateCommittedResource3`,
`R8G8B8A8_UNORM`, and the two Unreal names the other captures print (`Niagara::GPUProfiler_BeginFrame`,
`FSlateFontTextureRHIResource`).

**The captures are not in the repository**, and neither are the bundles or the paths they sit at: a capture
lives in the gitignored `renderdoc-src/` and its path in the local `captures.local.json`, a bundle is engine
output for one machine's GPU, and all of them are one command away. What
is checked in is what must hold *about* them, so a capture or a bundle that is not on this machine is reported
as **not compared**, and the exit code says which of the three answers a run is: **0** everything compared
matched, **1** a transcript, a label or a document differs, **2** nothing could be compared at all. That is
`build --check`'s rule for the same reason — whether a capture, a bundle or a binary has been produced is
the state of a working tree, not a property of the tool — and it is why none of this is in `tests/`: the
suite is hermetic and takes eleven seconds, this reads hundreds of megabytes.

**The bundle half** answers three questions about the engine's side of the tool, all from files: are the
driver's documents still documents (`validate` against the checked-in `schema/`, which now also refuses a
repeated JSON key — the one check a schema cannot make, because a plain parse keeps the last value and says
nothing), does the report still count what it counted (events, passes, resources, findings, and the
findings *per detector*, so a detector that stops firing while another fires more is a mismatch rather than
a smaller total), and does the analyser agree with itself — `replaydiff` of a bundle against the **same**
bundle must find no difference at all, which is what makes "the same capture through two builds of the
tools" a command rather than a reading (§4.16). The driver's own device-free check (`schema --check schema`)
runs in the same pass when `bin/replay_dump.exe` is built; everything else the driver does needs a GPU and
stays a manual gate (§9).

**What is here, checked on 2026-09-21**: three captures, 22 transcripts, 58 labels (49 of them about a
capture, nine about the pair) and two driver texts. `goldens --check` compares them in about a minute on
this machine — the offline half is ~36 s (every child runs without the stream cache (§4.8) and pays its own
decode, and the pair's A/B reads both bundles) and the driver half is three replay sessions, one per capture
that pins driver commands. On a machine with no GPU and no capture none of that is paid: both halves report
themselves as **not compared** — nothing runs, and nothing is claimed either — which is what keeps a run on a
capture-less machine fast and honest. One of the
three is held **by identity only** — `desktop-2` is a frame from a renderer that is not Unreal, and what a
frame says about itself (its marker names, its resource names, its pass names) is its own, so that capture
has no transcript, label, document or driver text here and its `commands` and `driverCommands` lists are
empty **by design**. Its size and
digest are still checked, so the file is verified when it is on this machine; nothing it prints is compared,
and the corpus's `known` lines for it are numbers and API facts rather than quotations. The two bundles' self-A/Bs are `47`
and `12` passes with every difference at 0, and the pair — the mobile frame against the desktop one, both
dumped `--with-images` — answers `10` passes in both, `2` only in A, `37` only in B, `55` constant blocks
whose values moved, `16` shader hashes differing and `2` image pairs compared. **The captures themselves are not verified**: the SHA-256 in the corpus is, so a re-capture under
the same name is caught, but nothing here can tell you that a frame recorded on a phone is what it says it
is.

**`known`: notes, and the causes that make a finding a verdict.** A capture's `known` list holds two kinds of
entry. A **note** (a string) is prose about the frame for a reader — "its markers balance", "1186 ids carry
bound state" — and nothing reads it. A **cause** is an object, and the report *uses* it:

| member | what it is |
|---|---|
| `detector` | the detector whose finding this explains |
| `what` | a substring the finding's own `what` must contain |
| `evidence` | a substring one of the finding's evidence lines must contain; **empty matches any**, which is how one cause covers all fourteen findings of a rule that has one explanation |
| `cause` | the sentence the report prints: what following the finding to the frame (or to the engine's answer) concluded |
| `verdict` | `confirmed` (the finding is real and this is why) or `not a defect` (the observation is real, its cause is not a frame bug) |

A match turns the finding's **`unproven`** flag off and prints the cause beside it, and this is the *only*
path by which that gate opens: the gate is the cause being written down, not a detector being
trusted. `load_corpus` checks the shape of every entry — a cause whose `detector` is a typo would simply never
apply, and a corpus that says a bug is known while the report keeps calling it unproven is worse than a corpus
with no causes at all — and `cmd_report` prints any entry that matched nothing as **stale**, because a cause
written for a rule that has since stopped firing is the corpus lying about the frame.

The corpus is matched to a capture by the **SHA-256** it already carries, never by name: a cause was
established on one file, and a re-capture under the same name must not inherit it. A machine with no corpus,
or a capture the corpus does not have, leaves every finding unproven — the answer that claims least. Today:
`desktop-1` has one cause (its 48 MB `Nanite.VisibleClustersSWHW` allocation, `confirmed` from two paths that
agree nothing touches it) and `mobile-1` has one that covers all fourteen of its `unbound-root-parameter`
findings (`not a defect`: the reflection and the state's root parameters come from different namespaces at
those events, and no dispatch or draw runs with a block unbound).

---

### 4.18 The file's own diff: `diff`

`diff <a.rdc> <b.rdc> [--all] [--format table|csv|markdown]` compares two captures by what their **streams**
recorded — the third thing beside `passdiff` (the marker trees) and `replaydiff` (two bundles of engine
answers), and the one neither can do. Per call it compares four things:

| what | how it is compared |
|---|---|
| the marker path and the call | paths pair by full text first and then by their **innermost name** — the same two rules `passdiff` uses, deliberately reused (`rdc_passdiff._pair_by`) so the two commands cannot align one pair of frames differently — and inside a paired path, calls pair by name in occurrence order |
| the call's own arguments | `idx=… inst=…` / `x=… y=… z=…`, the words `draws` prints |
| the state chunks that changed before it | the setter chunks seen on that call's *command list* since its previous call, in order (`SetPipelineState + OMSetRenderTargets`) |
| every binding in force | one key per slot, and a description per value |

**The keys are what a slot *is*, not what it is numbered.** A binding is keyed `cbv b1 s0`,
`table t0 n5 s0`, `32bit b0 s0 n4` — `rdc_resources._root_param_what`, the words `draws` prints inside
`rpN(...)` — plus the stage when a parameter is not visible everywhere, so a slot that moved from `rp10` to
`rp5` between two builds of a scene still compares as the *same* binding, while a slot whose kind, register or
space changed does not. The fixed slots (`RTV`, `DSV`, `VB0`, `IB`, `PSO`, `rootsig`) are keyed by name.

**The values are descriptions, not ids**: a named resource by the name the application gave it, an unnamed one
by kind and shape (`texture2d 1024x1024x1`, `buffer 4096 B`), a table slot by what the capture wrote into it.
Two consequences are stated in the output rather than hidden: two unnamed resources of the same shape compare
*equal*, and the byte offset of a binding is **not** compared — for a resource sub-allocated out of a UE page
(§4.9) it is where that recording put the sub-allocation, and comparing it would make every page binding in a
pair differ for a reason nobody could act on.

Output: the counts, then sections for the changed rows, the calls only one side ran, and the paths that paired
by their innermost name. `--all` lifts the per-section cap; `--format csv|markdown` prints one row per
difference (`status,path,call,field,a,b`) in the same order, with the prose on stderr. Exit **1** when no
chunk can be named at all (no source tree, README §1.1); otherwise 0 — a difference is not an error, it is the
answer. Measured on the pair: 24 calls in the mobile frame against 52 in the desktop one, 3 identical, 6
changed, 15 and 43 one-sided, and 10 paths paired by their innermost name.

### 4.19 The signature against the stream: `rootsig-check`

`rootsig-check <rdc> [bundleDir] [--format table|csv|markdown]` reads the two records of one fact — the root
signature the capture *creates* and the bindings the frame *makes* — and reports where they disagree, with an
exit code (**1** on a `certain` finding, 0 otherwise) so it can gate a script like `verify`.

| check | certainty | what it says |
|---|---|---|
| `undeclared-parameter` | certain | the stream sets `rpN` on a signature that has fewer parameters than that |
| `range-kind-mismatch` | certain | a slot a range covers holds another kind of descriptor than the range declares |
| `partial-heap` | question | the frame writes descriptors into a heap and binds slots in it that it never wrote |
| `unknown-resource` | question | a binding names a resource id the capture never creates |
| `never-set-parameter` | question | a signature a call binds declares a parameter no call in the frame sets |
| `no-signature` / `unknown-signature` | question | a binding set on a list whose signature this stream does not have, or one it never creates |

Two things make it exact rather than approximate. **A table binding is resolved against the heap as it stood
when the table was bound**: the heaps are rebuilt chunk by chunk (`rdc_resources.apply_descriptor_chunk`, the
same write/copy decoding `parse_descriptor_heaps` uses, so the two cannot drift) instead of read from the
frame's final state — UE re-uses descriptor memory, so one slot can be an SRV early and a UAV later, and a
final-state answer reports every such re-use as a mismatch (measured: 87 findings became 1). And a range
offset of `0xffffffff` is `D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND`, which means "after the previous range", not
four billion: the ranges are walked in declaration order and each append takes the running end.

**A slot the frame never wrote is not a finding**: UE fills its descriptor heaps at startup and the frame only
references them, so that is the normal case (measured: 3,520 of `desktop-1`'s 4,960 bound slots). It is
reported as coverage — which heaps the frame binds and never writes, and how many slots it wrote — and the
question is kept for the sharper case of a heap the frame *does* write without writing the slot it binds.

**With a bundle** (`replay_dump dump`, §9) the same facts are compared against the engine's own rows: the
signature ids it reports against the ids the file decodes, every parameter row's class, register, space and
visibility against the decoded signature, and every `heapH+0xO` it resolves against the bindings the file
recorded (the offset is in descriptors on both sides). Measured on `desktop-1`: 71 state documents, 21
signature ids, 88 parameter rows and 29 table bindings compared, **0 disagreements** — two readers of one
capture agreeing exactly. What is *not* compared is what the engine resolved inside a table (`cat(N) type(N)`):
its rows are per event and the file's writes cannot be aligned to an event (no event-to-chunk mapping exists,
§8), so a difference between the two sets would not be a disagreement; that comparison is the driver's own
`crosscheck`.

### 4.20 The budget and the what-if: `vram`

`vram <rdc> [maxPasses] [--drop <nameFilter>] [--format table|csv|markdown]` is `memory`'s ledger asked a
different question — not where the bytes are, but **what they are for and what would change**. It reads the
same walk as `deps`/`memory` (§4.15) and adds no extraction of its own.

**By role**: the resources the frame *references* (an unused allocation is `memory`'s business; a budget that
counted it would charge the frame for it) grouped by what they are for — a resource with an `rtv`/`dsv` use is
a **render target** even if it is a texture, then textures, buffers, acceleration structures. Textures are
counted at 4 bytes a pixel and every figure containing one is marked `~`, the report's own convention. Heaps
are reported beside the roles, and the output says plainly that placed resources are counted in both: a budget
wants what the frame uses, `memory` is where the placement is unpacked.

**The widest pass** is the pass with the largest **peak live bytes inside it** — the resources whose use
windows overlap the pass, swept the way `memory` sweeps a heap (`_peak_live`), with the windows clipped to the
pass so a resource that outlives it is not charged to it twice. Pass ranges come from the file's own markers
(`rdc_passdiff.marker_passes`), in chunk indices. Measured: `desktop-1`'s `FRDGBuilder::Execute` touches 83
resources (~201 MB) and peaks at ~146 MB; mobile's peaks at ~59 MB.

**What-if** is the arithmetic the budget exists for, and nothing more: `now`, `at half resolution` (render
targets and textures scale by *area* — a quarter of their bytes; buffers and acceleration structures do not)
and one row per `--drop <nameFilter>` (a subtraction over the resources whose name matches). Whether either
change is *legal* — a target's format, a pass's dependencies, D3D12's aliasing requirements — is not in the
arithmetic, and the last section of the output says so. Measured on `desktop-1`: 21 render targets ~103.8 MB,
45 buffers 62.2 MB, 21 textures ~45.6 MB (~211.6 MB total), ~99.5 MB at half resolution, and `--drop GBuffer`
saves ~48.0 MB over 5 resources.

### 4.21 A folder of captures: `sweep <dir>`

`sweep <dir> [--out <root>] [--commands <file>] [--overwrite] [--min-bytes N] [--limit N] [--exe]`

The corpus (§4.17) names captures, hashes them and pins their transcripts; this is the half of that which
needs a GPU to rebuild in bulk. For every `.rdc` under `<dir>` (recursively, in sorted order) it writes one
*bundle* — `dump`, REFERENCE §9 — under `<out>/<key>/`, runs the extra lines in `--commands` in the same
session and writes each one's document beside the bundle, and then writes `<out>/sweep.json`: one row per
capture with the key its bundle sits under, the capture's size and SHA-256, the engine version, and what the
bundle holds.

    sweep   : captures (3 capture(s)) -> bundles, the library
    Android Renderer         swept    53.8 s  256 file(s), 3.1 MB
    desktop                  present  a1b2c3d4e5f6
    index   : bundles\sweep.json (1 swept, 1 present, 0 failed, 34.2 MB of captures)

The destination with no argument is `bundles`, relative to the working directory, and it is in `.gitignore`
with the driver's `bundle/` — a bundle is local state that records the capture's absolute path (§9), which is
why the sweep's own default must not be committable and why a test pins that it is not
(`tests/test_rdc_sweep.py`).

The line it sends for a bundle is **quoted** (`dump "<dir>"`), and that is load-bearing rather than tidy: a
key can hold a space, the command language splits on whitespace, and `dump` takes the *last* positional as its
destination. Unquoted, `dump out/Android Renderer` sends `Renderer` as a second argument and the bundle lands
in a directory named after the capture, resolved against the process's working directory — which happened
once, as 257 files in `Renderer/` at the repository root, before the quoting and the `.gitignore` entry. The
sweep noticed (no manifest at the path it asked for, so the capture was reported `failed`), and that is the
half a reader sees: the stray directory is the half nothing watched.

Three decisions carry it:

* **One process, one session per capture.** A folder of captures would otherwise be a program per capture,
  each paying the engine's standup (~4 s on a small capture, ~11 s on a 1.4 GB one) — this is what the
  driver's library is for (§9), and `sweep` is its first caller. The replay system is built once and the
  captures come and go on it (`api.h`), which is the shape RenderDoc's own application uses; `--exe` falls
  back to a `replay_dump.exe dump` per capture, for a machine that has the exe and not the library.
* **The index is built from the bundles' manifests, not from the run.** A manifest is written last (§9), so
  its presence is what "this bundle is complete" means: a capture whose bundle is there is reported
  `present` and is not replayed unless `--overwrite`, and the numbers a corpus entry wants come from the
  bundle that was actually written. A sweep is therefore resumable, and a second run over a folder that
  gained one capture sweeps one capture. Measured on a folder of one: the first run 54.6 s (a 36 MB mobile
  capture, 256 files, 3.1 MB of bundle), the second **0.7 s**.
* **A key is where the capture sits, and nothing more**: its path under `<dir>`, extension dropped, with the
  separators replaced (`sub/a.rdc` is `sub-a`). The index therefore names local files, which is local state
  — the corpus keeps a key and a hash in git and the paths out of it (§4.17) — and what a capture is
  *called* in a corpus stays the corpus's own word.

A capture that cannot be replayed is a `failed` row carrying the engine's own sentence in `note`, and the
run exits 1 with the failures named at the end: one unreadable file in a folder of thirty should not cost the
other twenty-nine, and it should not be silent either. `sweep.json` is validated against the tool's own
schema before it is written (§4.12) — a document this tool cannot validate is a document a consumer cannot
either, and `validate <out>/sweep.json <schemaDir>` needs no kind, because the file's name says which one
it is.

### 4.22 The pipelines and their shaders: `psos`

Where `dxbc` (§4.5) is a container inventory, `psos` is the *join*: which pipeline state objects the frame
creates, which shader each stage of each one holds, and how to get from a hash you were handed — a line in a
log, a `.pdb` name, another tool's output — back to the shader and to the draws that use it.

**The edges are already in the file.** A capture's pipeline-creation chunks carry the whole description,
each stage's DXBC/DXIL container verbatim included, so this is a payload read and not a decompile: 14
payloads holding 606 KB over `Android Renderer.rdc`'s 374 MB stream, 43 holding 2.4 MB over
HobbyRenderer's 1.47 GB, parsed by `parse_dxil_containers` — the same parser `dxbc` uses — in 1–3 ms. No
session and no device (`trace`'s own floor is a replay session, measured 1.8–3.7 s, of which the debug
attempt itself is noise), nothing decompiled, nothing written to disk, and no shader files extracted: the
containers are read in place.

**The id is the PSO's own resource id**, read from the payload's tail, so the table joins to `draws`' `pso=`
column and to any `List_SetPipelineState` payload. Three chunk forms create a pipeline object and all three
are read (`rdc_psos.PSO_FORMS`):

| chunk (`d3d12_device_wrap2.cpp` / `_wrap.cpp`) | inline ids | the id's place in the tail | bytecode order | the ids' own order |
|---|---|---|---|---|
| `Device_CreatePipelineState` | 8, a C array | `n - 80` | VS,PS,DS,HS,GS,AS,MS,CS | VS,HS,DS,GS,PS,CS,AS,MS |
| `Device_CreateGraphicsPipeline` | 5, a C array | `n - 56` | VS,PS,DS,HS,GS | VS,HS,DS,GS,PS |
| `Device_CreateComputePipeline` | 1, a single `ResourceId` | `n - 16` | CS | CS |

Both orders are the *engine's own* (`d3d12_serialise.cpp` for the bytecodes, the two `Serialise_Create*`
functions for the ids) and they differ in every graphics form, which is why neither is derived from the
other. The array framing is the part that has to be right: a C array is written with its **element count
first**, so eight ids are 8 + 64 bytes and the id sits at `n - 80` — reading `n - 72` answers with the count
word, which is how seven pipelines of one capture came to answer to the id `8`, and how a listing can look
complete while every row is wrong. That count word is also the tail's own check (it must be the form's id
count), and a payload that fails it is *counted and said* — an older capture whose serialiser predates the
inline ids — rather than silently missing, because a table that omits a PSO looks like a frame that has
none.

**A stage is a pairing, not a copy.** The containers appear in the payload in the bytecode order above, and
which of those stages exist is known from the tail's non-zero ids; the two are zipped, and only when the
counts agree — otherwise the stages print as `?` rather than as a guess. Three independent checks stand
behind the labels: every parsed id is one the capture's command lists actually bind (14/14 on the UE
capture, 43/43 on HobbyRenderer), no id appears twice, and every container whose own `OSG1` signature speaks
agrees with its label (16 agreements, no disagreement on the UE capture — including the pixel shader whose
PDB the engine went looking for, which is `pso 3042`'s and `pso 3048`'s `ps`). A mesh shader writes
`SV_Position` exactly as a vertex shader does, so HobbyRenderer's two mesh pipelines are the one case the
signature cannot confirm; the inline ids are what settle them.

**Three identities, and none of them is another's value.** `dxbc` prints a container's **header hash**
(bytes 4..20); a shader's **`HASH` part** (20 bytes on every container measured here, its digest the last
16) is what `dxc -Fd` names a `.pdb` after — measured: `trace`'s refusal on the UE capture asks for
`ec6e6433f96a985d5086cd232bf42fd8.pdb`, and that value is one pixel shader's `HASH` part, not any header
hash in the file. The **third** is what the driver side prints: a bundle's per-stage `hash` is
`sha256(rawBytes)` (`commands_state.cpp`), and measured on `desktop-1`'s event 1003 all three of its
stages' hashes landed on a container in the capture, each with `size` equal to the bundle's `bytes` — so
the file side and the driver side join by a hash neither of them computes the same way, and asking `psos`
about a hash copied out of a bundle is answered rather than refused. `--hash` takes any of the three, by
prefix (eight characters is already specific; every match is printed, and a container serialised more than
once is one answer listing its offsets), and answers with the container, all three identities, its debug
data, and every pipeline and stage that binds it.

**`ILDB` is the column that decides whether `trace` can run at all.** A DXIL shader is stepped *through*
the debug bitcode in its own container, so 46 of HobbyRenderer's 53 containers need nothing else while all
22 of the UE capture's need their PDB found by name (`--pdb <dir>`, §9.4). `psos` says which of the two a
frame's shaders are before any session is paid for — the difference between a capture that can be debugged
offline and one that needs a folder of symbol files.

**The index is cached beside the stream cache** (`.psos.json`, `rdc_cache.sidecar_path`), keyed on the
stream's length and `rdc_cache.stream_digest` — the identity the bind names' sidecar uses too (§4.13). The
version covers *how the index is built* and not only what it stores: it moved twice while this was written,
once when reading the older chunk forms turned 7 PSOs into 43 and once when the ids' framing was corrected,
and both times the sidecar already on disk held an answer the new code would gladly have served. A digest
cannot catch that — the stream did not change, the answer did — so the number is the only thing that can
say "this file is not what this build computes". With it, a hash lookup is a dictionary hit: 0.24 s from a
cold shell (Python plus the cache read) against 0.65 s for the same command doing the whole-stream scan, and
no scan on a 1.5 GB capture at all.

---

## 5. Worked examples

**Find the two sphere groups in a mobile base pass and see how they differ**

```powershell
& $py src\py\rdc_analysis.py markers 'mobile.rdc' | Select-String -Pattern 'BasePass' -Context 0,12
& $py src\py\rdc_analysis.py draws   'mobile.rdc' 40
```

**Read a constant buffer that a draw binds**

Not possible offline any more: `draws` tells you *which* buffer is bound and at which register
(`rp7(vs cbv b2 s0)=res342+0x8d200`) but not what is inside it. Reading the contents needs the replay driver
(`GetCBufferVariableContents`, §9); the old
`initial` command guessed the data offset by scoring a known signature against candidate header sizes, which
did not survive contact with the captures it was pointed at, so it was removed rather than fixed.

**Find out what a shader reads, or which permutation ran**

Not offline. The tool used to guess at this — `pattern` hunted for a uniform's known value, `dxbc` scanned
containers for GI-ish strings, `sig` decoded the vertex signatures — and all three were removed because the
shader reflection answers it exactly: names, bind points, values, signatures, disassembly (§9).
Offline you can still see *which* shaders the capture embeds and where (`dxbc`) and extract them
(`dump-shaders`), and `draws` says what each one is bound to.

**Dump shaders for external disassembly**

```powershell
& $py src\py\rdc_analysis.py dxbc          'mobile.rdc'          # where they are
& $py src\py\rdc_analysis.py dump-shaders  'mobile.rdc' '.\out\mobile'
```

---

## 6. Verified payload facts worth remembering

These were derived from the RenderDoc source and then confirmed against real captures; they are the assumptions
the decoders rely on.

* Chunk payload = `chunk['payload_offset']`, which is **36 bytes** past the chunk start when flags are `0xf0000`.
* Chunks are **64-byte aligned**; the padding after a payload is not guaranteed to be zeroed, so it must not be
  read as data. `verify` reports the padding totals and lists non-zero runs — both captures tested have all-zero
  padding, so this is a hazard rather than an observed problem.
* `ResourceId` is a `u64`; `0` means null.
* D3D12 bindings are **command-list state**, and the stream is read the same way D3D12 defines it:
  `SetPipelineState` and every draw leave them alone, `Reset()` clears them (and can set an initial PSO), and a
  root signature that *differs* from the current one makes all previous root arguments stale while re-setting
  the same signature keeps them. Graphics and compute root parameters are separate namespaces. `draws` reports
  the state in effect at each call on the strength of this (§4.5).
* `D3D12BufferLocation` (CBV/SRV/UAV and vertex/index views) serialises as **(resourceId, byteOffset)** — this is
  the VA→resource mapping, obtained for free.
* `D3D12_GPU_DESCRIPTOR_HANDLE` serialises as a **`PortableHandle` (`u64 heapId, u32 descriptorIndex`)**, not as
  a pointer (`d3d12_manager.h`), which is why `List_SetGraphicsRootDescriptorTable` payloads are 24 bytes.
  Verified on both captures, and it is what `verify` caught: the decoder assumed 20.
* The serialised D3D12 root signature (`RTS0` part) is a 24-byte header
  (`u32 version | u32 numRootParameters | u32 paramDataOffset | u32 numStaticSamplers | u32
  staticSamplerOffset | u32 flags`) followed by a **12-byte** parameter array
  (`u32 type | u32 visibility | u32 dataOffset`) whose data is *out of line*, addressed by `dataOffset` from the
  start of the part: `D3D12_ROOT_CONSTANTS` (register, space, count), `D3D12_ROOT_DESCRIPTOR1` (register,
  space, flags — no flags in 1.0) or a table (`u32 numRanges | u32 rangesOffset`) of
  `D3D12_DESCRIPTOR_RANGE1`s (24 bytes: type, count, base, space, flags, tableOffset — **20 for a 1.0
  signature**, which has no flags word, so the version decides the stride). Version 1 = 1.0, 2 = 1.1, 3 = 1.2.
  `parse_root_signatures()` decodes it, `rootsig` prints it, and `draws` annotates every `rpN` with it.
* `Device_CreateRootSignature`'s payload is the serialiser's own framing around a one-part DXBC container
  holding that `RTS0`. The tool locates the container by its `DXBC` magic rather than at a fixed offset and
  cross-checks it against the length field at `+4` (they agree in all three captures), and the signature's id
  is the **last 8 bytes** of the payload — not `length - 16` like a resource creation, because nothing follows
  it.
* **The root signature has no parameter names, and neither do these captures.** Names could only come from
  shader reflection, and every capture tested is DXIL with it stripped — the shader parts are
  `SFI0 ISG1 OSG1 PSV0 STAT HASH DXIL` with **no `RDEF` and no `RDAT`**. `parse_rdef()` reads an `RDEF` when a
  capture has one (the layout is in `dxbc_container.cpp`; `bindPoint` is the register, which is what makes it
  matchable), but offline there is nothing to name a parameter with, so the tool prints what it *knows* —
  type, register, space — instead of guessing. The replay driver (§9) is the way to get real names.
* `Device_CreatePipelineState` embeds the DXBC/DXIL containers of the shaders it references, which is why
  `parse_dxil_containers()` finds shaders at offsets *inside* those chunks.
* Resource **names come from `SetName`**, not from the creation call, and the creation call is what carries the
  descriptor (a buffer's `Width` is its byte size; a texture's dimensions, format and mips describe it). UE
  sub-allocates buffers inside page buffers (`Resource Allocator Underlying Buffer`, `Fast Allocator Page`), so
  `res<id>+0x<offset>` can point *into* a page: the page is what the capture names, and the logical buffer
  inside it is not in the D3D12 stream at all.
* A `PortableHandle` is `u64 heapId, u32 index` — 12 bytes, no padding (`d3d12_manager.h`). Both a
  descriptor-table binding and a descriptor write name their slot that way, which is why the two can be matched
  at all (§4.10). Descriptor heaps are created with up to a million slots and their **contents are not
  snapshotted** into a frame capture: the stream only carries the writes and copies of the captured frame.
* Every array in a payload is preceded by a `u64` element count (`SERIALISE_ELEMENT_ARRAY`, `serialiser.h`).
  That is why `List_IASetVertexBuffers` views start at `+24`, and why
  `List_SetGraphicsRoot32BitConstants` is `28 + 4n` with the values at `+24` — the count is part of the
  payload, not padding.
* A *nullable* pointer (`SERIALISE_ELEMENT_OPT` → `SerialiseNullable`) writes a 1-byte `present` flag before
  the value. `List_IASetIndexBuffer` is the one chunk here that uses it, so its view is 33 bytes with the
  resource id at `+9`, not `+8` — verified on both captures: `size` comes out as `indexCount * 2` and the
  resource id matches the vertex-buffer pool the same draw uses.
* `chunk <N>` and `draws` now read the root CBV/SRV/UAV payload the same way (the 28-byte
  `(resourceId, byteOffset)` pair from `D3D12BufferLocation`), so the two commands print identical
  `res<id>+0x<offset>` strings for the same chunk.

---

## 7. How to implement extra features

### 7.1 The five-step recipe

1. **Find the chunk.** `chunks <rdc> 4000 List_` or `summary` gives names and indices.
2. **Find its serialisation.** In the RenderDoc source:
   ```powershell
   Select-String -Path "$src\renderdoc\driver\d3d12\d3d12_command_list_wrap.cpp" -Pattern 'Serialise_SetPipelineState' -Context 0,25
   ```
   The order of `SERIALISE_ELEMENT(...)` / `SERIALISE_ELEMENT_ARRAY(...)` **is** the byte layout: `u64` for
   `ResourceId`, `u32` for enums/UINT, and `SERIALISE_ELEMENT_TYPED` types such as `D3D12BufferLocation` are
   the (resourceId, offset) pair. Array serialisation writes a `u64` count before the elements.
3. **Write `cmd_<name>(path, ...)`** using the helpers (`load_stream`, `iter_chunks`, `chunk_payload`,
   `decode_chunk`, `u16/u32/u64`, `chunk_strings`).
4. **Dispatch it** in `main()` (one `elif cmd == '<name>':`) and add a line to the module docstring.
5. **Validate with the chunk length** (`length == fixed + n*stride`) and against a second capture.

### 7.2 Worked example — a `psos` command that lists pipelines and their shaders

```python
def cmd_psos(path):
    """List Device_CreatePipelineState chunks with the DXIL containers they embed."""
    info, stream, how = load_stream(path)
    names = load_chunk_names()
    for idx, ch in enumerate(iter_chunks(stream), 1):
        if names.get(ch['id'], '') != 'Device_CreatePipelineState':
            continue
        blob = chunk_payload(stream, ch)
        # the created PSO's ResourceId is the first u64 of the payload
        print('#%-6d pso=%-6d chunkLen=%-8d' % (idx, u64(blob, 0), ch['length']))
        for off, size, h, parts in parse_dxil_containers(blob):
            print('        shader @+0x%-8x size=%-8d parts=%s hash=%s'
                  % (off, size, ','.join(p[0] for p in parts), h[:16]))
```

Add to `main()`:

```python
    elif cmd == 'psos':
        cmd_psos(path)
```

Then: `python src\py\rdc_analysis.py psos capture.rdc`.

### 7.3 Useful internal helpers

| Helper | Purpose |
|---|---|
| `load_stream(path)` | `(info, decompressed_stream, method)` |
| `iter_chunks(stream)` | yields `{off, id, flags, length, data}` for every chunk |
| `chunk_payload(stream, ch)` | the payload bytes (correct offset, always) |
| `chunk_strings(stream, ch, minlen, limit)` | strings inside a payload (ASCII **and** UTF-16LE) |
| `decode_chunk(name, blob)` | decoded fields for the known chunk types |
| `walk_uses(stream, names, resources, heaps)` | the whole use ledger: `{resources, aliases, heaps, seen, events, failed, unresolved}` (§4.15) |
| `parse_barriers(blob)` / `parse_barrier_groups(blob)` | the two barrier payloads: every entry, or None when the walk does not land exactly at the end (§3.4) |
| `parse_targets(blob)` | `(render targets, depth target)` of `List_OMSetRenderTargets` |
| `clear_target(blob)` / `discard_target(blob)` | the resource a clear or a discard names, 0 when the payload is not shaped as one |
| `copy_pair(name, blob)` | a copy's `(destination, source)` |
| `parse_dxil_containers(stream)` | `(offset, size, hash, parts)` for every DXBC container |
| `parse_signature(blob)` | decoded `ISG1`/`OSG1` `(name, semanticIndex, register)` list |
| `part_strings(blob, off, ln)` | strings inside one container part |
| `load_chunk_names()` | chunk-id → name map, parsed from the RenderDoc source enums |
| `u16/u32/u64`, `align_up` | little-endian readers and 64-byte alignment |

### 7.4 Interactive use

```python
import rdc_analysis as R
info, stream, how = R.load_stream(r'C:\path\capture.rdc')
names = R.load_chunk_names()
chunks = list(R.iter_chunks(stream))          # keep this; decompression is the slow part
draws = [c for c in chunks if names.get(c['id'], '') in R.DRAW_CHUNKS]
```

---

## 8. Pitfalls and known limitations

Most of these bullets are **replay's job, not offline work** — decoded textures, disassembly, the non-frame
sections, uniform names — and `ROADMAP.md` keeps them out of the offline plan on purpose ("what is deliberately
not on this list"). The one that did stay offline work — chunk names needing the source tree — is closed: the
enums are bundled (`chunk-names`, §4.1), so a machine without a tree still prints names. A further bullet here
would be tracked the way `ROADMAP.md` tracks work: with an acceptance gate naming what closes it. A bullet
here is a known limitation, not a permanent design decision.

* **Only section 0 is decompressed.** Additional sections are listed but not parsed. Replay reads them for you
  (§9), so this is not planned as offline work.
* **No name resolution for root parameters.** The serialised root signature carries no names, and neither do
  these captures' shaders: every one is DXIL with the reflection stripped (`RDEF` and `RDAT` are both absent),
  so there is nothing offline to map `rpN` to a uniform name with. What `draws` does instead is say what each
  parameter *is* — `rp2(cbv b1 s0)`, `rp0(table t0 n64 s0, u0 n16 s0)`, `rp3(32bit b0 s0 n4)` — so an index can
  no longer be mistaken for something it is not (§4.10, §8). A name appears when a capture does carry an
  `RDEF`. Real names need the replay driver (§9).
* **A descriptor-table binding resolves only as far as the capture goes.** The stream holds the descriptor
  *writes and copies of the captured frame*, not the contents of the heap, and UE fills its million-slot global
  heap at startup: on the Android capture every table binding therefore still shows the heap name, while the PC
  capture resolves 9 of them. `descriptors` (§4.10) reports what is known and a slot the capture never wrote is
  reported as the heap — never guessed at. Descriptors *after* the first in a table are the root signature's
  business: `rootsig` (§4.1) prints the ranges, which say how many descriptors each table covers.
* **A binding into an allocator page names the page, not the buffer inside it.** UE sub-allocates, so
  `rp1(vs cbv b0 s0)=res1907+0x93300[Resource Allocator Under]` is as specific as the D3D12 stream gets: the
  logical buffer's
  name lives in UE's own bookkeeping (§3.5). Descriptor-table bindings are the other half of this problem and
  have the same shape: what the capture wrote is resolved (§4.10), what it did not is only the heap.
* **No texture decoding** *offline*. Only raw bytes can be dumped; `replay_dump textures --save` (§9) writes
  every one of them as a PNG through the engine's own decoder.
* **No shader disassembly** *offline*. `dump-shaders` extracts containers; `replay_dump shaders <eid> --disasm`
  (§9) prints the disassembly the engine generates.
* **Chunk indices are not event ids.** `summary`/`markers`/`draws` number chunks the way the file stores them,
  which matched the engine's event ids on the two Unreal captures and does not on `desktop-2`
  (`replay_dump probe`, §9). Use the driver's ids when talking to the driver.
* **A lifetime read out of the file is capture-relative.** D3D12 writes no release to the stream — no chunk
  records a resource being destroyed — so the frame's *last use* is where a lifetime ends, and a resource created
  before the capture (UE allocates its heaps and static textures at startup) has no creation event at all.
  `memory` (§4.15) reports both rather than inventing an end, and counts a resource the frame never touches as
  live for the whole frame. The same file gives a texture no byte count: every figure containing one is an
  estimate at 4 bytes a pixel, marked `~`.

## 9. The replay driver (`replay_dump`)

Everything the offline tool leaves to RenderDoc — uniform *names*, values, decoded textures,
disassembly, post-VS geometry, the rendered image — is what `src/cpp/replay_dump.cpp` asks the engine for. It
is a second tool, built against the installed `renderdoc.dll`, and it exists because the offline
tool's job is what replay is bad at (the container, the chunk stream, sub-second queries) while this
one's job is what reading the file cannot answer at all.

```powershell
cmake -S . -B build -A x64 && cmake --build build --config Release   # MSVC + the installed DLL -> .\bin\
.\bin\replay_dump.exe shaders 'capture.rdc' 270      # reflection: cbuffers, bindings, signatures
.\bin\replay_dump.exe cb      'capture.rdc' 270 ps 3 # the named values of one cbuffer
.\bin\replay_dump.exe state   'capture.rdc' 270      # bound shaders, outputs, root parameters
.\bin\replay_dump.exe textures 'capture.rdc' --save .\out   # every texture, decoded to PNG
.\bin\replay_dump.exe shaders 'capture.rdc' 270 --disasm    # ... with the disassembly
```

The same build writes a second thing into `bin/`: `rdc_lz4.dll`, the LZ4 decoder the offline tool loads with
`ctypes` (§3.2), compiled from the vendored source in `src/cpp/third_party/lz4` (the decoder RenderDoc itself
vendors, v1.9.2, BSD-2-Clause — its own target, so it never touches this exe and is held to its own warning
level rather than this project's `/W4 /WX`). It is what the offline tool decodes an LZ4 section with, so a
tree that has not built it cannot read a capture's stream: `blocks` and `info` never needed it and still
answer, `sections` prints its table and then says so (it reports the decompressed size, which is a cache miss
away from a decode), and every command that walks the stream reports the same one-line `error:` and exits 1
rather than a traceback. Being *behind its source* stopped being cosmetic at the same time, so
`build [--check]` compares it too (§4.1): a `bin/rdc_lz4.dll` older than `src/cpp/third_party/lz4` means every
capture is decoded by the previous decoder, and nothing else in the tool would notice — the exe is current,
the answers look like answers, and only the source says what the library should have been built from.

| Command | Gives |
|---|---|
| `info <rdc>` | RenderDoc version, driver, API properties (`pixelHistory` among them: the flag `pixelhistory` is gated on), resource/texture/buffer/chunk counts |
| `draws <rdc> [max] [filter]` | the engine's action list (`GetRootActions`) in frame order: markers and calls, each with the engine's **event id**, its depth in the marker nest, and the marker path it sits inside. The filter matches a call's name *or* a marker path, so a marker is a handle for the events under it |
| — | the `hash` line under a stage in `shaders` is a deliberate text-output change (2026-09-21): `bytes` is a shader's *size*, and two shaders can share one, so an A/B of two captures (`replaydiff`, §4.16) needs the digest to say "the same shader" as a fact |
| `state <rdc> <eid>` | bound shaders per stage, render targets, depth target, root signature and every root parameter with its register, space and what is bound |
| `shaders <rdc> <eid> [--disasm]` | the reflection: constant blocks with **names** and bind points, resource bindings, input/output signatures, the SHA-256 of the shader's own bytes per stage, and the disassembly on request |
| `cb <rdc> <eid> <stage> <slot>` | the **named values** of one constant buffer, structs and arrays expanded |
| `watch <rdc> <name> [--since A] [--until B] [--max-events N] [--stage <stage>] [--all]` | one reflection member's value at every event of a range, as one row per *change* — see below |
| `textures <rdc> [filter] [--save <dir>] [--mip N] [--slice N] [--sample N] [--raw] [--cast <type>] [--range min,max]` | the texture list; `--save` decodes **one subresource** at a time to PNG through `SaveTexture` (`--mip`/`--slice`/`--sample`; a cubemap face is a slice), into a folder the tool creates (parents included, like `dump`/`sheet`/`patch` destinations) and **fails** on if it cannot — it used to warn once per texture and exit 0, which left the files a caller asked for missing behind a run that said it succeeded. `--raw` writes the engine's undecoded bytes as `<name>.bin` with the format and size named, which is the answer for a format the display path will not take; `--cast` reads the bits as another type (a typeless texture needs one) and `--range min,max` is the black/white point mapping that turns a float/HDR texture into a file a person can look at |
| `formats <rdc>` | the format coverage audit: every format in the frame's texture list, how many resources use it and how many bytes it costs, what it is made of (components × width, type, sRGB, block-compressed, element size) and whether the engine can make a picture of it — `yes`, `with a cast` (typeless: `--cast` says how to read the bits) or `no` (a special layout the display path does not take), with the reason. `needCast` and `noLayout` count the textures in the two "cannot just look at it" classes, so a summary can say *12 textures use something this engine cannot show* rather than silently skipping them. The other half of the RT-format question — what the shader *wrote* into those targets — is `crosscheck`'s |
| `cubemap <rdc> <resId\|name> [outDir=cross]` | an environment map as six pictures plus the engine's own cruciform: `<outDir>/px.png` .. `nz.png` in D3D's face order (+X, −X, +Y, −Y, +Z, −Z) and `<outDir>/cross.png`, which is `TextureSliceMapping::cubeCruciform` — the engine laying out the unfolded cross itself, because reassembling one from six bitmaps by hand is where the rotations go wrong. `--mip`/`--cast`/`--range` apply; a resource that is not a cube is refused with what it is (and `textures --save --slice N` is the way to one subresource of it) |
| `mesh <rdc> <eid> [instance] [max] [--stage vsin\|vsout\|gsout\|taskout\|meshout] [--obj <file>]` | one instance's geometry **at one stage**: `vsout` (the default) is what the vertex shader emitted, `vsin` the stream the draw read, and `gsout`/`taskout`/`meshout` what a geometry, task/amplification or mesh shader produced — with the vertices, the index count, the **primitive count** (absent, with `primitivesNote` in its place, when the topology does not fix one from the counts: a strip with adjacency, a meshlet list), and the **position bounds** (the first three components, vertices that are not all finite dropped whole, with `boundsNote` when none was usable). `--obj <file>` exports a Wavefront OBJ for an external viewer — one `v` line per vertex and faces only where the vertex order *is* the primitive's, which the file's own header says |
| `image <rdc> <eid> <out.bmp> [--overlay <name>] [--mip N] [--slice N] [--sample N] [--cast <type>] [--hdr M] [--gamma]` | the texture display at that event, written as a BMP (no PNG encoder needed). `--overlay` draws the engine's own `DebugOverlay` **into** the picture — `wireframe` is the topology the frame actually rasterised, `quad-draw`/`quad-pass` and `triangle-size-draw`/`triangle-size-pass` are the cost hunches, and a typo is refused with the list of fifteen — and `--hdr <multiplier>`/`--gamma` are the display path's tonemapping for float/HDR content. The subresource and cast options are the same ones `textures --save` takes; the document says which overlay and which subresource the file is |
| `pixelhistory <rdc> <eid\|last> <resId\|name> <x> <y>` | every event up to `<eid>` that tried to write that pixel: the test that rejected each attempt and the value before, from and after it (below) |
| `trace <rdc> <eid> --pixel <x,y>` · `--vertex <v[,inst[,idx[,view]]]>` · `--thread <gx,gy,gz,tx,ty,tz>` · `--mesh-thread <gx,gy,gz,tx,ty,tz>` `[--sample N] [--primitive N] [--view N] [--max-steps N] [--all]` | **one shader invocation, stepped** (below): the engine's own debugger runs the shader the selector names — the stage follows from it, pixel=ps, vertex=vs, thread=cs, mesh-thread=ms — and the document holds the values it started with, one row per step (program counter, the `ShaderEvents` that fired, the source line, the callstack, every variable that changed as `before -> after`) and the variable list it ended with. Stepping a **DXIL** shader goes through the debug data DXC emitted, so a capture without it answers with the file the engine went looking for; a **DXBC** shader is stepped from its own bytecode. `sourceDebugInfo` says which of the two, and a refusal distinguishes "no debug data" from "this invocation could not be run" rather than guessing |
| `counters <rdc> [--per-pass [--passes <file>] [--top N]]` | GPU counters per event. `--per-pass` folds one counter over each pass (`FetchCounters` answers per event and takes no range): the passes come from the frame's markers — consecutive calls sharing a marker path are one — or from `--passes`, one `<first eid> <last eid> [<name>]` line per pass. The counter that is the cost is the engine's choice (`EventGPUDuration` when this replay produced one), named in the document with its unit; a replay that produces no results says so rather than printing a table of zeros, because GPU counters are a driver feature |
| `crosscheck <rdc> [eid] [--since N] [--until N] [--max-events N] [--max N]` | what the reflections say a shader wants against what the state says it was given: the vs output signature against the ps input signature, each stage's bindings against the root signature's declared ranges, and the render targets' formats against the ps output signature. Every finding names an event and quotes both sides. `linksChecked`, `bindingsChecked`, `bindingsUnmapped`, `targetsChecked` and `noRootParameters` say how much was actually compared — a capture whose shaders were stripped has no reflection, and then an empty findings list means *nothing was checked*, not that the frame is clean |
| `debug <rdc> [--group] [--fail-on high\|medium\|low\|info]` | the engine's own messages (validation layers, driver complaints). One row per message; `--group` folds each *distinct* message — the engine's own `messageID` plus severity, category and source — into one row with its count and its first/last eid, which is what makes ten thousand messages a table. `--fail-on` is the pass/fail line: the run exits **1** when anything at or above that severity was reported, and `high` is the *most* severe, so `--fail-on medium` means High or Medium. Nothing else in the driver fails on a *finding* rather than on a failure, and the exit code is the point — "did the engine complain about this frame" becomes a line in a script instead of a paragraph someone has to judge |
| `usage <rdc> <resId or name>` | every event that touches a resource |
| `probe <rdc> [maxEid\|last]` | which event ids the engine actually has: the whole frame by default, the first `maxEid` ids when one is given, and the answer is cached — see below |
| `dump <rdc> [outDir=bundle]` | the whole frame to disk as a *bundle* for the offline tool — see below |
| `bundle-verify <dir>` | re-hash a bundle's files against its manifest (no device, no DLL) |
| `schema [<name>] [--out <dir>]` | the JSON Schema for each `--json` document (no capture, no DLL) |
| `selftest` | the driver checking itself: JSON writer, schema table, help text, the DLL it loads |

`--json` works on every command.

**The contract — `schema`, `selftest` and `schemaVersion`.** Every `--json` document carries
`schemaVersion` (1 today), so a reader can refuse a shape it does not understand instead of guessing. `schema`
prints the JSON Schema for each document kind, and `schema --out schema` writes the checked-in `schema/`
folder that `python src\py\rdc_analysis.py validate <bundle> schema` reads (§4.12), and `schema --check <dir>`
fails when that folder and the driver disagree — which is how a committed copy is kept from going stale.
`selftest` runs where a
capture cannot: the JSON writer's escaping, separators and balance, the schema table, the help text, and the
`renderdoc.dll` it would load — skipping the DLL checks, rather than failing, when RenderDoc is not installed.

**Paths are `std::filesystem`'s.** Every filesystem operation in the driver — directories, existence, sizes,
write times, iteration, absolute resolution, joins — goes through `std::filesystem`, with the `error_code`
overloads throughout: an exception must not leave a helper, and a `filesystem_error` thrown out of a
directory walk would take the process down for a file that went away between two calls. What that replaced:
`MakeDir` is now one `create_directories` call (the drive/UNC/trailing-separator walk it did by hand was
`root_name`/`root_directory` spelled out), `DirIsEmpty` is one `directory_iterator`, `FileBytes` is
`file_size`, `FileWriteTime`/`NewestSourceTime` return `file_time_type` (so `>` compares two of them and the
difference becomes seconds at the one place that prints one) and `AbsolutePath` is
`absolute(...).lexically_normal()`. Three things stay Win32 on purpose, each saying so where it is:
`GetModuleFileName` (a *module*'s path, not a file's), `CaptureStdout`'s `_dup2` (the documents have to stay
byte-identical to the command line's), and `_wfopen` inside `FileOpen` — the one place a path becomes a
`FILE *`, because a printf-style writer has no `std::filesystem` equivalent.

The pass paid for itself in three places where the hand-written version was *wrong* rather than merely
long: `BundleRelative` cut `root.size()` bytes off the front of a path, which is not a relative path at all
unless the root is a literal prefix of it (`out` and `outer\a.txt` gave `er/a.txt`); `bundle-verify` rewrote
`/` to `\` by hand, which is what Windows already does; and `MakeDir` re-derived a path's root component by
component. Measured: the two bundles dumped before and after the change are **byte-identical**, file for
file (`--until 1` on the Android capture, including `capture.json`'s `absPath`, the manifest's
`captureAbsolute` and its `/`-separated file list, and a capture spelled
`renderdoc-src/../renderdoc-src/./Android Renderer.rdc`); the corpus's byte-for-byte comparison of the
driver's text passes unchanged, and the driver's 154 self-checks pass with two of them renamed
(`newest-source-of-an-empty-dir-is-zero` → `-is-none` and `file-write-time-of-a-missing-file-is-zero` →
`-is-none`, because 0 was the old sentinel and `std::optional` is the new one).

One limit is deliberately unchanged: a path still arrives as narrow bytes in the machine's ANSI codepage
(that is what `argv` and the ABI hand over), so a capture whose name is not representable there still cannot
be opened by the exe. Fixing that needs the wide command line (`GetCommandLineW`, or `wmain`) plus an
explicit encoding convention — a different change from this one, and `FileOpen` is where it would land.

**The trace — one invocation, stepped (`trace`).** Every other command reads what the frame *did*: what was
bound, what a block held, which pixels a draw wrote. `trace` runs one invocation of one shader and reports what
happened **inside** it — `DebugPixel`/`DebugVertex`/`DebugThread`/`DebugMeshThread` hand back a
`ShaderDebugTrace`, `ContinueDebug` steps it (an implementation-defined number of steps per call, an empty list
meaning the invocation is finished) and `FreeTrace` releases it, and the document is the three lists that holds:
`inputs`, one row per `steps` entry, and `outputs`. The invocation is *named* rather than discovered, because
there is no sensible default — a command that picked "the pixel at 0,0" would be answering a question nobody
asked — so exactly one selector is required, and the stage is not a separate option: a `--stage ps` beside
`--thread` could only disagree with the call about to be made. `stage` and `stageAsked` are both in the document
so such a disagreement would be *visible*. `--sample`/`--primitive`/`--view` are `DebugPixelInputs`, whose three
fields default to `~0U` ("no preference": any fragment writing that co-ordinate, any sample, the first view).
`--max-steps` (default 200) stops between steps rather than between calls — one call can return thousands, and a
cap the engine could not see would be no cap at all — and `truncated` says when it stopped, so `outputs` is not
misread as what the invocation produced when it was cut short.

Three facts about that debugger decide how the command behaves, and the first two were measured rather than
assumed:

* **A trace is one stage.** A pixel's whole history is two traces (the vertex shader that made its inputs, then
  the pixel shader), and this runs the one whose invocation was named. The reflection and the debug-info verdict
  are read for the stage the selector implies, through `BoundReflection` — the shader the engine *bound*, not the
  entry point it would disassemble by default, because the debug info hangs off that one.
* **What the engine needs to step a shader depends on what the shader is.** A DXBC (SM5) shader is interpreted
  from its own bytecode; a **DXIL** shader — DXC, and every shader in this project's UE captures — is stepped
  *through* the debug data DXC emitted. Measured: `trace 289 --pixel 640,360` on the Android capture answers
  `sourceDebugInfo is 0` and the engine's own loading log says `Did not find debug data for
  '<hash>.pdb'`; on the HobbyRenderer capture the same answer is `Found debug data in the shader`, and there
  `--vertex 0` on eid 1715 traces **31 steps** with source lines (`imgui.hlsl:21` → `:26`) and 29 output
  variables, while `--pixel` on three of its draws found no fragment at any co-ordinate tried.
* **A trace that could not be run comes back empty, and says nothing about why.** The engine answers a NULL
  trace or one whose `debugger` is NULL (the second is what it actually returns — RenderDoc's own Qt viewers all
  test `trace->debugger == NULL`), and the reason lives in its log and in `ShaderDebugInfo`. So the refusal is
  built from what the reflection holds and **branches on `sourceDebugInfo`**: no debug data → the engine's
  whole loading log, which is a *search* (every line of it names a path it tried: that list is the answer to
  "where does this PDB have to be?"), and what to do about it — embed the debug info in the shader with
  `-Zi -Qembed_debug`, or point the engine at the folder that holds it with `--pdb <dir>`; debug data *present* →
  the *invocation* is what failed, which for a pixel is usually a co-ordinate no fragment wrote or none that
  passed the depth test there, and `--vertex`/`--thread` on the same event is the cheapest way to tell the two
  apart because an invocation with no fragment to find cannot fail for want of one. The engine's own
  `debuggable`/`debugStatus` are checked *before* a trace is asked for, so a shader the engine knows it cannot
  run is refused in its own words.

`outputs` is accumulated the way RenderDoc's own UI accumulates a debug state
(`ShaderViewer::AddCurrentState`), which is the one rule here that is wrong *quietly* if it is wrong: a change's
name is its `before` name when it has one and its `after` name otherwise — a variable that came into scope has
no `before` — an empty `after` name is a variable that **stopped existing**, which removes it from the list
rather than leaving it at its last value, and every other change is a new value. The source line per step comes
from `instInfo`, which is **not** indexed by instruction (the API says so: it holds the unique mappings in
instruction order and the entry at or below the instruction applies), so it is a lower-bound search — a linear
scan there would turn a 10,000-step trace into minutes of nothing. All four of those rules are pinned by
device-free checks in `selftest` (26 of them, `trace-*`), because a trace needs a capture with debug data *and*
a device, and the rules that go wrong quietly are exactly the ones a machine with neither should still be able
to falsify. The document's own contract is `schema/trace.schema.json` (28 files after this command), and a real
trace document from the HobbyRenderer capture validates against it — `stepCount` rather than a second `steps`,
because two members with one key is the defect `bundle-verify` shipped once.

**The bundle — `dump` and `bundle-verify`.** `dump` is one replay session turned into files, so the offline
half (and a reader) can work without a device. **A bundle is local state and is never committed**: it belongs
to one `.rdc` on this machine, and the first two files below carry the capture's **absolute path** — which is
the point (a bundle says which file it came from) and also why it must not be shared. The destination with no
argument is `bundle`, relative to the *working directory*, and it is in `.gitignore` beside the sweep's
`bundles/` (§4.21); the driver will not write into a directory that is not empty unless `--overwrite` is
passed, so the accident to guard against is a *new* directory, not an existing one. Finally, the destination
and **every folder above it that is not there yet are created** — `dump cap.rdc out/frames/cap1` needs no
`mkdir` first, and the same is true of the other two commands whose destination is a folder a caller names,
`sheet <rdc> [outDir=sheet]` and `patch <rdc> <eid> <stage> [outDir=patch]` (the table above). `MakeDir`
walks the path one separator at a time for it, skipping the part that is not the caller's (`C:\`, a UNC
share) and treating a component that already exists as the normal case; a *file* where a component must be is
still the failure those commands report as `cannot create <what>`. It writes:

| File | Content |
|---|---|
| `manifest.json` | bundle version, driver and RenderDoc version, the capture's absolute path, byte count and SHA-256, the flags used, the scan result, every written file with its size and hash, and a `notInThisBundle` list saying what it cannot contain and why |
| `capture.json` | the capture header: API, driver, machine, feature flags (`shaderDebugging`, `pixelHistory`), counts, file size |
| `events.json` | one record per id with bound state: eid, pipeline object and `psoKind` (graphics/compute), the shader id per stage, the render targets with format and dimensions, the depth target, the root-parameter count, a state hash, and — for a call — a `volume`. `psoKind` is the *call kind* from the capture's action tree — a dispatch or not — and not a reading of the bound shaders: on `desktop-1` every event has a compute shader bound, so the shaders would call all 2132 of them compute, draws included. `volume` is what the *call* asked for, taken from the same action list: a draw carries `vertices` (its index count, or its vertex count when the draw is not indexed), `instances` and `triangles` (`0` when the topology does not fix one — a patch list, a meshlet list), a dispatch carries `groups`, `threadsPerGroup` (the call's own override, else the bound shader's `[numthreads]`) and `threads`. It is absent for an event that is not a call, so a reader can tell "asked for nothing" from "not a call"; a bundle written before 2026-09-22 carries none at all, and the report says which of its rankings could not read it |
| `states/<eid>.state.json` + `.shaders.json` | the full pipeline state and the reflection, written *through* the `state` and `shaders` commands, so a file is exactly what the command prints — including each stage's `hash` (the SHA-256 of its bytes), which is what `replaydiff` compares two bundles' shaders by (§4.16) |
| `cbuffers/<eid>_<stage>_<slot>.json` | the named values of every constant block of every bound stage, at the state events |
| `resources.json` | every resource: id, name, kind, format/dimensions or byte size, and its usage list with the first and last event that touches it || `messages.json` | debug messages as objects: eid, numeric severity, severity text, text |
| `counters.json` | with `--with-counters`: eid, counter, value |
| `rt/<eid>_<slot>.png` | with `--with-images`: the bound render targets at the state events, through the engine's encoder |
| `textures/<resId>.png` | with `--textures`: every texture decoded, full size — the engine decodes but does not resize |

The state document's `rootParameters` array is rows, not objects, and three shapes matter to the offline
rules: `rpN reg=R space=S vis=<stages> <target>` is the parameter as set (`vis=` names the stages it is
visible to — measured, a base-pass draw's vertex and pixel shaders both declare `t0`..`t4`, each served by its
own table — and is absent in bundles written by older drivers, which read as visible to every stage);
`rpN <letter><reg> s<space> cat(N) type(N) <res…|none>` is one *resolved* table slot, printed since the driver
started asking the engine (`GetDescriptors`) what each set table holds: `letter` is the range's register space,
`cat(N)` the range's declared category, `type(N)` the heap slot's own descriptor type, and `none` an empty
slot. The offline rules compare `cat` against `type` (through `CategoryForDescriptorType`) and match the
reflection by the letter — never one letter against another letter's register, because `b0` and `t0` are
separate register spaces: that mistake produced ~60 false positives before a real capture caught it.

Flags: `--since <eid>` · `--until <eid>` · `--max-events N` (bounds the *sweep* as well as the writing) ·
`--events 270,452` (force state files for those ids) · `--with-images` · `--with-counters` · `--textures` ·
`--no-usage` (skip the usage lists, the slow part) · `--overwrite`. `bundle-verify <dir>` re-hashes everything
the manifest lists and exits 1 on a missing, resized or changed file; it needs no device, no DLL and no
capture, so a bundle from another machine can be checked before it is trusted. Files are written as they are
produced and the manifest is written last, so a crash leaves a bundle *without* a manifest — visibly
incomplete rather than silently partial.

**What a bundle cannot contain, and why.** Three items are one limitation: the replay API exposes no *action
list*, so the driver cannot ask the engine what a given id *was*. A bundle therefore has no call kind
(draw/copy/clear/marker), no per-event triangle or thread counts, and no marker or pass names — an id is only
an id with bound state. Those ids come from `probe`'s rule (a root signature or a bound shader), the sweep
stops when a run of ids has nothing bound, and it is bounded by the **frame's last event id**, which the
action list gives exactly: every driver ends a capture's action list with an "End of Capture" action, so the
largest event id in the tree is the last event there is (`LastEventId`), and ids past it *clamp* to it rather
than coming back empty (measured: `probe 4500` reports 4405 ids with state on a capture whose structured file
has 723 chunks — the clamp is why a bound is needed at all). Before that bound existed the sweep ran to the
file's chunk count and collected the whole clamped tail as if it were events — on `desktop-1`, 946 of
its 2,132 collected ids were one state repeated past the last event; on `desktop-2` the tail past event
1736 would have been 94% of a default dump. The chunk count is the fallback bound when no action list comes
back, and `--until` can narrow the range but no longer extend it past the frame's end. All of the gaps are
written into the bundle's own `notInThisBundle` list, so a reader does not conclude that the frame had no
copies. Nothing offline derives one numbering from the other: the mismatch between event ids and chunk indices is
one of the chunk-level findings that has no home in this tool's scope (`ROADMAP.md`), and no item anywhere in that file
proposes to close it here.

**The sweep is cached** (`sweep-<key>.txt` in the cache directory, keyed by the capture and the dump options
that change the answer -- including the scan bound, which is why every cache written before the bound existed
is refused; `$RDC_NO_CACHE` turns it off), because it is the most expensive thing the driver does and a re-run
asks the same question again. Measured on a full `desktop-1` frame (1,186 ids with state, 1,305
scanned): **68 s** with the sweep and **45 s** with it answered from the cache -- the sweep is 21 s of that,
the per-event pass is the rest, and both halves are the engine's own cost (REFERENCE §9's "the engine is a
black box behind a call"). Bounding the sweep at the frame's last event took the same dump from 105 s to 68 s
on the same afternoon's machine: the 946 clamped-tail ids it stopped collecting were 946 `SetFrameEvent`
calls in the sweep and as many again in the writing pass, and every state document, cbuffer and image the
bundle contains was byte-identical across the change.

**Why the sweep is not parallel.** It looks embarrassingly parallel -- one `SetFrameEvent` per id, each
independent -- and a parallel version was built and measured before being removed: four worker
processes, each opening its own replay and sweeping a slice of the range, their answers merged under
the sweep's own stop rules. Mechanically it worked and it was fast: **68.4 s against 209.4 s** for the
same dump on the same afternoon (3.1x), the sweep phase alone 20.6 s against 71.8 s. It was removed
because its answer is not the serial sweep's answer, and cannot be made so: the engine's pipeline
state at an id is a function of *how replay reached that id*, so a worker that starts cold at its
slice's head answers a different question than a walk from the frame's start. Measured on the PC
capture: the serial sweep collects 1,186 ids, the parallel one 1,169, and the 17 it loses are exactly
ids 979..995 -- the head of the last slice, a region between command lists where the serial walk
still reports the previous list's bindings while a fresh replay reports none (the `state` command's
own backwards double-jump agrees with the worker at 979, and reports the next bindings only from
996). Two consequences worth keeping: those 17 serial rows are **replay-history ghosts** -- bindings
left in the reported state by earlier replay passes, real rows in every bundle to date, and a caveat
on any per-event reading of a state that sits between command lists -- and the two things that would
make a parallel sweep honest are both out of reach: reproducing the serial history per worker costs
the whole prefix (the last worker would pay the entire range), and splitting at command-list
boundaries needs the event-id-to-chunk mapping the offline tool does not have — and no item in `ROADMAP.md` proposes to build one.
The attempt also left
two Windows findings behind: a spawned child must be given a stdin it can use (an inherited slot it
cannot takes its whole stdio down -- three "successful" workers once left three empty logs), and
simultaneous replay-device creations can leave one hung at zero CPU with no error, so any such design
needs a deadline and a serial fallback rather than an unbounded wait.

**The same driver is a library** (`bin/rdc_replay.dll`, ABI in `src/cpp/api.h`, Python caller
`src/py/rdc_replay.py`). `replay_dump.exe` is one command per process, which is right for a command line and
wrong for a tool that asks several questions: the standup is the engine opening the capture — measured on the
1.55 GB UE capture, `info` 7.07 s and `state` 7.30 s, while six commands through one session were 7.14 s in
total, so a command run alone is ~7 s of engine around ~6 ms of work. The library builds from the *same
sources* — one command language, one dispatcher, so a library line and a batch line mean the same thing — and
what it adds is a session held open across calls. The same idea has four spellings now: `batch <rdc> <file>`
for a file, `--repl`/`--stdin` for a terminal or a pipe, `multi <rdc> <line>...` for a command line, and this
library for a caller with its own handle. `multi` was added when the measurement above was taken: writing a
temporary batch file to ask three questions of one event was the common case, and every spelling pays the
session exactly once (`CommandSequence` in `replay_dump.cpp` is the loop they share, so a line's meaning
cannot drift between them):

| ABI | what it does |
|---|---|
| `RdcReplayAbi()` | the ABI's own version (`rdc_replay/1`); a caller refuses one it does not know rather than guessing |
| `RdcReplayOpen(capture, log, err, errLen)` | a session; `capture` NULL or empty is a session with **no capture**, which answers `schema` and needs no device |
| `RdcReplayCommand(session, line, out, err, errLen)` | one line in the batch syntax; returns the command's own exit code and the document it printed |
| `RdcReplayClose(session)` · `RdcReplayFree(block)` | the session's teardown; and `free`, for the buffer the previous call returned |

`RdcReplayOpen` reads exactly like the CLI's own path — the version guard first (a capture from a newer
engine is refused by name, before a device exists), then `OpenFile`, then `OpenCapture` — and it returns the
same sentences in `err` that `main` prints, because `GuardCaptureVersion` writes the message into a string
the library can return rather than only to stderr. A command's *document* comes back in `out`; its progress
and its own failure messages still go to the process's stderr, which is what a caller sitting in the same
process sees anyway. One detail is not obvious and is worth the two lines it costs: `CaptureStdout` `_dup2`s
a file opened `"wb"` over stdout, and `_dup2` gives the target the *source's* mode — so without
`_setmode(_O_TEXT)` the library's document would carry LF where the command line carries CRLF. Measured
before that fix: the same `info` was 435 bytes on the command line and 418 here, one byte per line. The
bundle's own documents are written the same way on purpose and stay LF (a bundle's `capture.json`: 0 CRLF).

**What a session is, exactly: a batch file.** The library's promise is not "a fresh engine per command" —
it cannot be, because pipeline state at an id depends on how replay reached it (that is the paragraph on the
parallel sweep). A session that runs eight commands gives the same text as a `batch` file holding the same
eight lines, byte for byte: measured over `info`, `draws 5`, `state 270`, `statediff 270 300`, `debug`,
`shaders 270`, `state 270 --json` and `crosscheck 270 --max 4` on `desktop-1`, including the rows that *move*
when a state read follows a jump forward. One command per process is the other regime, and the CLI still
offers it; a caller who needs it should not use a session for two questions.

**The replay system is the process's, not the session's**, and that is the engine's rule rather than a design
preference. The first version of the library shut the system down with each session, and the first session
was clean while **every one after it faulted inside the teardown** — which is "initialise once per process"
being enforced by a crash rather than by a message. So the DLL owns the system (and the log: one per
process, named by the first session), a session owns a capture and its controller, and the teardown happens
when the DLL unloads. RenderDoc's own application opens captures the same way. The first caller to lean on it
is `sweep` (§4.21): one process, one session per capture, a folder at a time. The device-free half of that
ABI is what the test suite checks — a session with no capture answering `schema`, a capture that is not there
failing before a device would exist, and a capture from a future engine being refused with the guard's own
sentence — while the text it produces is pinned by comparison against the executable and against `batch`.

**A stale library is caught twice over, and it needs both.** `build --check` compares `bin/rdc_replay.dll`
against `src/cpp` as its own artefact (§4.1), independently of the exe — measured: a library aged an hour
behind a current exe is reported as the *only* stale one, exit 1 — and `build` rebuilds it (measured: a
deleted DLL comes back in 4.5 s). What the gate cannot do is speak to a run in progress, and the driver's own
warning cannot either: `WarnIfDriverIsStale` asks about the process it is running in, which under a library
session is the *host* — `python.exe` — so it finds no `src\cpp` beside it and stays quiet exactly when the
answers are the previous build's. `rdc_replay.stale_note()` closes that: when the DLL loads, the same
comparison `build --check` makes (`rdc_driver.target_staleness`, not a second implementation of it) is asked
about the file just loaded, and a stale one gets the driver's own two lines on stderr — *"this replay library
is older than its sources: `src\cpp\x.cpp` was written N s later, so every answer from this session is the
previous build's"* — with the build command under it. Nothing is printed when it cannot tell: a DLL copied out
of the tree, a missing one (that is the "no library at" error, not a staleness warning) or a current one.

**The probe is cached too, and bounded by the frame** (`probe-<key>.txt`: the same directory, header and
one-row-per-line format as the sweep's, and the same two environment variables). `probe` sweeps the same ids
for the same reason -- one `SetFrameEvent` each, 12 ms on `desktop-1` and 47 on `desktop-2` -- and it had no
cache at all: **39 s per run** on the PC capture, every run. Its old default also swept 695 ids *past* the
frame's end (the frame's last event is 1,305, the default was 2,000), which is harmless there and was the
whole answer on `desktop-2`: a five-figure frame, ids 1..2000 with nothing bound, and a minute of sweeping to
say so. Three changes, each measured on the PC capture: the range is the frame's own last event unless a cap
says otherwise (`ProbeUntil`; a cap past it is the frame, `last`/`all` spells the default, and a number is the
old capped behaviour), the answer is cached (a repeat is the session's standup -- **7.7 s against 110.9 s** in
one loaded session, same command line, the two outputs identical line for line), and a run that is not the
session's first command says so on stderr: `AnyEventReplayed()` is the rule this file has always documented
("probe must be the first thing the process asks") made checkable, and only a first run's answer is written,
because a cached answer is somebody's first run. Two limits are deliberate. A request *wider* than the cached
prefix sweeps from id 1 again rather than extending it -- arriving at 501 by a cold jump instead of through
500 is the paragraph above, and it is why the key is the capture rather than the range: the file's
`# scanned: N` is a claim that ids 1..N were walked in order, which answers `probe N` and every smaller
request. And the key holds the capture's path, size and write time, so a capture edited in place cannot read
an answer about the file it replaced.

**Three things a replay host must do**, and the reason this file has a long comment about them: put
`REPLAY_PROGRAM_MARKER()` at file scope, call `RENDERDOC_InitialiseReplay()` before opening anything,
and `RENDERDOC_ShutdownReplay()` on the way out. Without the first two, the engine runs with
uninitialised global state and dies inside `OpenCapture` with an access violation — no message, no
log, nothing. The build script also has to make an import library from the DLL's exports (the
installer ships none) and put a copy of `renderdoc.dll` beside the exe.

**Event ids are the engine's, not the file's.** `draws` takes its ids from the engine's own action list
(`ActionDescription::eventId`), and so do `probe`, the bundle and every command that takes an `<eid>`; the
*offline* tool's `chunks`/`summary` print their own **chunk indices**, which are a different numbering. On the
two Unreal captures the two happened to agree, and on `desktop-2` they do not: `probe` shows
the engine's first event with pipeline state at 842 while the structured file's first draw is at chunk 316,
because RenderDoc numbers only what a *command list* recorded (resource and PSO creation, `SetName` and
descriptor writes are in the file but are not events). `probe <rdc> <maxEid>` lists the ids that do have state,
so an id can be checked rather than assumed; a wrong id silently returns an *empty* state rather than failing.

**Marker paths come from the same list.** Every event carries the markers it sits inside (`Scene > BasePass`),
written by `state`/`shaders`/`cb` as a `marker` field and by `dump` into `events.json`, because a marker path
survives a re-capture where an event id does not and it is what lets an offline rule name a pass in the
engine's vocabulary. A bundle written before 2026-09-17 has no `marker` member at all: a reader asks for it
with a default rather than by index.

**The engine is version-checked before it is used, and `--dll` is how another one is named.** `--dll <path>`
wins over `$RDC_RENDERDOC_DLL` (both exist so that "does this capture replay the same under 1.46 and under
1.47?" is the same command twice rather than an environment variable re-set between runs, which cannot be
done per run at all). The replay API exposes no accessor for the version that *recorded* a capture, so the
driver reads the container's fixed 32-byte header itself — `RDOC | version | headerLength | progVersion`, the
same bytes `rdc_stream.parse_container` reads — and compares its `MAJOR.MINOR` against
`RENDERDOC_GetVersionString()` from the loaded DLL, **before** `InitialiseReplay` and before any device
exists. An older engine than the capture is refused, with both versions named and exit 1: an older engine
answers from another version's decoding, and that looks exactly like an answer. Newer is allowed and says so
in a note; equal is silent; a version that does not parse (a fork, a development build) is said and passed on,
because a guard that guesses about a file it cannot read is worse than no guard.

| engine | capture | what happens |
|---|---|---|
| 1.46 | the corpus's own three (`1.46 e4bd23`, logfile version 258) | replayed, no note — the tested direction |
| 1.46 | a copy whose header says `1.0 abcdef` | replayed, one note: the engine is newer than the capture |
| 1.46 | a copy whose header says `1.99 abcdef` | **refused**, exit 1, both versions named, no device created |
| any | a version that does not parse (`unknown`) | a note, then the run continues: not a verdict |

The guard judges the **program** version, not the logfile format version: the engine checks that one itself
(`FileIncompatibleVersion`), and the program version is the number a reader is actually holding. The measured
rows above are `tests`-free and reproducible by hand — copy a capture, rewrite sixteen bytes at offset 16,
and run `info`.

**`--pdb <dir>` is where the engine looks for shader debug info** (repeatable; `$RDC_PDB` is the same list
with `;` between directories, and it is the only way in for a caller that does not write the command line —
the library takes no options, and `sweep`/`batch` run lines with no room for a global flag). It exists
because of what the file *is*: a DXIL shader is stepped through its debug data, that data is looked up by the
**name the shader carries** (a hash — `Did not find debug data for 'ec6e6433f96a985d5086cd232bf42fd8.pdb'`),
and the capture does not record where the shader was built. So a reader cannot guess the folder, and the
alternative was editing RenderDoc's own `renderdoc.conf` before a command that is otherwise one line.

It is RenderDoc's setting, not a private mechanism: the driver resolves `RENDERDOC_SetConfigSetting` from the
loaded DLL and writes `DXBC_Debug_SearchDirPaths` — the engine's list of directories to walk **recursively**,
comparing file names against the name the shader asks for — as an array of strings, which is the shape
`ConfigVarRegistration<rdcarray<rdcstr>>` reads (`value`'s children, each one's `data.str`). Four details are
deliberate: it is set **after** `RENDERDOC_InitialiseReplay` (which is what loads the config) and **before**
the capture is opened, so nothing overwrites it; the list is **replaced** rather than appended to, because a
per-process setting that starts empty makes the command line the whole answer rather than a contribution to
whatever the last run left behind; each directory is made **absolute** here, so a relative path means what the
caller's working directory says rather than whatever directory the *engine* happens to be in; and a directory
that is not there is **warned about** instead of being searched in silence, because the engine's answer to a
path it cannot read is identical to its answer to an empty one. Nothing is written back to the user's
`renderdoc.conf`: a replay app writes that file only from `ProcessConfig`, which runs inside
`InitialiseReplay`, so a value set afterwards lives and dies with this process.

Measured on the Android capture, whose pixel shader wants a PDB that is not in the capture: with
`--pdb build\perf\pdbs` the engine's own loading log reads `Recursive Search Path(s):` and then the absolute
form of that directory, and with a file of the wanted *name* placed there it goes on to say `Ignoring debug
info file '<dir>/ec6e6433f96a985d5086cd232bf42fd8.pdb' hash 0x080080080080 does not match shader hash
0x0833646EEC085D986AF90823CD865008D82FF42BFile found in recursive directory search` — the file was found, by
name, in the folder the flag named, and rejected for the one reason a placeholder deserves. With a PDB whose
hash matches, that is where the debug data comes from, and it is what turns `sourceDebugInfo is 0` into a
trace. The refusal quotes the whole loading log for exactly this reason: it is the search, line by line, and
the fix is to put the file in one of the paths it lists.

**The DLL is loaded, not linked** (`$RDC_RENDERDOC_DLL` overrides the path; `--dll` overrides both). RenderDoc's own
stringisers for `ResultCode`, `ResourceUsage`, `MessageSeverity` and `GPUCounter` are not exported, so
the driver does **not** supply the missing template specialisations: RenderDoc's definitions exist in
its `stringise.cpp` and are unreachable from this translation unit, which is
`[ifndr:temp.expl.spec.unreachable.declaration]`, and two definitions that do not match are
`[basic.def.odr]`. Instead the local helpers print the same numeric text as before, and
`ResultDetails` is read through its public `internal_msg`/`code` members rather than `Message()`.
`$RDC_REPLAY_DEBUG=1` traces each step on stderr, which is how the `OpenCapture` crash above was
found.

**`--json` is valid JSON, and that is checked:** `replay_dump <cmd> <rdc> --json | python -m json.tool`
(one object per run; arrays of strings, plus one object per row where a row has fields). It used to be
unparseable — the `capture` path was emitted with a raw backslash, `draws`/`textures` rows and the
shader stage blocks carried trailing commas, and `cb` put object members inside an array — so a
consumer that wants to check a change should validate rather than eyeball it. The writer's rule is
that a separator goes *in front of* every item after the first, never after a last one; the one thing
it cannot work out on its own is whether a field is the object's last, which is what the `last`
argument at those call sites is for.

**The build is strict on purpose** (`CMakeLists.txt`): `/W4 /permissive- /Zc:__cplusplus
/Zc:preprocessor /utf-8`, with `/external:W0 /external:anglebrackets` so RenderDoc's own headers stay
quiet, `/WX` in Release (a warning is a finding: this tool exists to report what it cannot prove), and
`/MP` so the modules compile in parallel — with one target the build tool has nothing to overlap, which
is why the split into `src/cpp/*.cpp` costs no wall-clock. The build tree stays in `build/` (gitignored)
and the executable and its DLLs go to `bin/` (gitignored), so the path every command in this document
uses does not move when the build system or its flags change. Two targets are for style, not building:
`clang-format` rewrites `src/cpp` to RenderDoc's own `.clang-format` and `clang-format-check` fails on a
diff. The SAL annotation on the `Fmt` helper makes the compiler check every format string against its
arguments — a varargs mismatch is undefined behaviour, and it is also how the tool would print nonsense.
`renderdoccmd.exe` is deployed into `bin/` along with the DLL: the engine spawns `<its own
directory>\renderdoccmd.exe crashhandle` for its crash handler, and without it every run logs `Failed to
create crashhandle server: 2`, waits 400 ms for a server that never arrives, and continues with no handler.

**Opening a capture is the expensive part, so batch it.** Standing the replay engine up — its own copy
of the frame plus a replay device — is ~2 s on `mobile-1` and ~6 s on `desktop-2` (1.4 GB),
while individual commands cost 0.0–1.5 s. A batch file pays the open once:

```powershell
# each line is a command, in the same syntax minus the executable and the capture
"probe 120`ninfo`nstate 270`nshaders 270 --json" | Set-Content .\run.txt -Encoding ASCII
.\bin\replay_dump.exe batch 'capture.rdc' .\run.txt
```

Measured, three runs covering 26 commands: **19.8 s total** (18 commands 4.9 s, the two probes 5.8 s,
6 on the 1.4 GB capture 9.1 s) where one process per command cost the open 26 times. Each line's output
is preceded by a `#=== <line>` marker, in both formats, so a stream can be split back into one document
per command.

Three things to know about running it:

* **One replay at a time.** The engine creates a device per process; two on one GPU at once is what
  makes a run look stuck, and a replay that is force-killed can leave the driver in a state where the
  next device creation blocks for minutes. If a run hangs, kill it and run it again — the log says
  which phase it reached, and its last line is decisive: `failed: ...` means the run stopped there and
  says why, `done: exit N` means it finished, and neither means it died or was killed mid-run.
  (`replay_dump` writes that last line itself precisely because a log that just stopped used to be
  unreadable — a run that failed to open a missing capture looks exactly like one that hung there.)
**One uniform across the frame (`watch`).** `cb` answers "what is in this block at this event"; the question
it answers badly is "is this uniform right at the draw that matters and wrong at the next one", because that
is a `cb` per event and forty documents to read. `watch <name>` reads every bound constant block at every
event of a range and prints **one row per change**:

    eid 284     vs  b0  View.TranslatedWorldToClip  = 0, 0, 0, 0 | 0, 0, 0, 0 | 0, 0, 0, 0 | 0, 0, 0, 0
    eid 298     vs  b0  View.TranslatedWorldToClip  = -0.2726, 0.639458, 0, 0.874883 | 0.962127, 0.64…
    eid 338     vs  b0  View.TranslatedWorldToClip  = 0, 0, 0, 0 | 0, 0, 0, 0 | 0, 0, 0, 0 | 0, 0, 0, 0
    eid 341     vs  b0  View.TranslatedWorldToClip  = -0.2726, 0.639458, 0, 0.874883 | 0.962127, 0.64…

That is `desktop-1` verbatim (`watch View.TranslatedWorldToClip --max-events 500`, 31 rows, 255 readings),
and it is the whole value of the command in four lines: the same uniform is a matrix at 298 and 341 and zeros
at 284 and 338 — the engine's own lazy zeroing, invisible to a `cb` at the wrong event. The name rules are
pinned by the device-free selftest: the whole dotted path, or the last component when the request has no dot
in it (`intensity` finds `Light.intensity`), both case-insensitively, and **nothing else** — a substring rule
would watch `intensityScale` alongside `intensity` without saying so. A matched *struct* reports its members,
because a struct's own value text is `-`. Note that `cb` prints nesting as indentation while `watch` names it
as a path, so the two spell the same member differently on purpose.

It is slow by nature — a constant-buffer read per event, measured at 15 ms per id over `desktop-1` — so it
takes a range and reports what it actually read: `scanned`, `withState`, `blocksRead`, `valuesMatched`,
`changes` and `found`. `found` false is the case worth knowing: the name matched nothing in any block of any
event, which is a fact about the *question* (or about a frame whose blocks were never read) and not the same
as "it never changed"; the log then says which commands list a block's names. It reads what it reads through
the same code as `cb` (`BlockRead`, commands_state.cpp), so a `watch` row and a `cb` document cannot disagree
about which buffer a block is.

**Finding your way around a frame (the four navigation commands).** `find <substring> [max]` searches what the
engine already publishes -- every call's name and marker path from the action list, every resource's name from
the resource table -- and says which field matched, because `View` hitting a marker and `View` hitting a
resource are different answers. An **event id argument may be a marker path** in every command that takes one
(`state "Base Pass Render (Phase 1) - Opaque"`), and `--at-marker <path>` supplies it as an option instead; both
resolve through the same rule (full path, then a component of it, then a substring) and both write the
resolution to the log, so an answer taken from a path can be checked. `statediff <eidA> <eidB>` prints one
changed field per line, with `state`'s own field names, and reads *both* sides by stepping onto the id from past
it -- a forward read hands back only what the replay has accumulated, so two events read forward would appear to
differ in ways that are about the replay's travel rather than about the events (the trap the bundle's second
state read exists for, §9 above). `buffer <resId|name> [offset] [len] [--as u32|f32|hex|ascii]` reads a buffer
through `GetBufferData` at the current event (use `--at-marker` to choose it) -- the one thing the offline tool
cannot show, because a buffer's *size* and *name* are in the file and its *contents* are not.

`--repl` and `--stdin` keep the capture open and read commands from the terminal (or a pipe) one per line, which
is what makes a session cheap: standing the engine up costs 4-11 s and each command in it costs only itself
(measured: three commands in one 14 s session at 1.6 s, 0.2 s and 0.0 s). A failing line is logged and the
session continues -- `Fail` returns a code rather than exiting, which is also why `batch` can run a failing line
and carry on. One thing to know when driving it: a script piped in by PowerShell arrives with a UTF-8 BOM, and
the first token is then `\xEF\xBB\xBFstate`; the driver strips it (and `--stdin < script.txt` avoids the
question entirely, which is the tested path).

**Why one pixel is that colour (`pixelhistory`).** `pixelhistory <rdc> <eid|last> <resId|name> <x> <y>
[--mip/--slice/--sample N] [--cast <type>] [--max N]` asks the engine for one pixel's history: every event up
to `<eid>` that tried to write it, the test that rejected each attempt (`depth test failed`, `stencil test
failed`, `scissor clipped`, `view clipped`, `shader discarded`, `backface culled`, `sample masked`, ...), and the
value before, from and after it. That is the answer no other command gives. Measured on `desktop-1`, pixel
(960,540) of `SceneColor` is four rows and a story: the `GBufferClear` at eid 599, the lit cube of `BasePass` at
eid 692 — whose `ps` value `0.9535,0.2053,0.3410` is the colour that landed — the reflection pass at 881, and
`SkyAtmosphere` at eid 901 **rejected, `depth test failed`**: the sky is behind the cube. On the scoped form
(`pixelhistory 1035 res61330 500 300`) the single row shows the format's precision too: `ps`
`0.727051,0.328613,...` written into an `R11G11B10_FLOAT` target lands as `post` `0.726562,0.328125,...`.

The scope is the *event*: the engine's history covers every write up to the one the replay is positioned at
(`ReplayController::PixelHistory` filters the usage list by it), so `last` (the frame's own last event, from the
same action list `draws` prints) is the whole frame and a pass's last eid is the answer at the end of that pass.
`--at-marker <path>` supplies the scope instead of a positional id — `pixelhistory --at-marker BasePassParallel
res60857 960 540` on `desktop-1` returns one row, the `GBufferClear` at eid 599, because the BasePass
write at 692 is *after* the scope the marker resolves to, which is the semantics rather than a bug: a marker
path resolves to its first call, so a pass's *last* eid is what asks about the end of it.
Two things it refuses to fake, both because an empty answer reads as a fact: the capture's driver must support
pixel history at all (`APIProperties.pixelHistory`, which `info` prints), and the pixel must be inside the
texture — the engine answers an out-of-range pixel with an *empty* list, which is exactly what "nothing wrote
it" looks like. An empty answer that is real is reported with its evidence (`usagesUpTo`: how many events up to
the scope touch the texture at all, "not one event up to it touches the texture" when that is zero); an empty
answer the engine's own source explains — a texture whose format it does not know
(`D3D12Replay::PixelHistory` returns before doing anything) — is an error rather than an answer.
`--mip/--slice/--sample` pick the subresource and are checked against the engine's own description of the
texture (the slice count is the texture viewer's rule: `depth >> mip` for a 3D texture, one per array element
otherwise, which for a cubemap is one per face); `--cast` overrides how the four components are read, the
texture's own format deciding otherwise and a typeless format printing raw 32-bit words. Values print as
`pre`/`ps`/`post` with `-` where the engine marked a value invalid: an invalid value's union holds
`0xdeadbeef`, and printing that is a plausible-looking colour that never existed. It is not a file read — the
D3D12 implementation re-runs the frame's draws with instrumented shaders to catch this pixel's fragments —
so it belongs in a batch with the other questions: measured on `desktop-2` (1.4 GB), the first call in a
process costs ~4 s (it builds the instrumented pipelines) and each further one ~1 s, while two calls on
`desktop-1` cost ~2 s together.

**The one verdict in it that is not a closed case.** On D3D12 the `sample masked` test is an instrumented
re-draw of the event, and RenderDoc's own source carries `TODO: figure out if we always need to check this` over
the flag that enables it. Measured on `desktop-2`'s 1-sample targets: every base-pass fragment comes back
flagged, and one of them carries a *changed* `postMod` value in the same row. So the rows always print the
values next to the verdict, and the document's `note` member (a key/value line in the header, in both formats)
says which two to compare rather than letting the flag read as a closed case; a capture where nothing is flagged
— `desktop-1` — has an empty `note`, and the same pixel asked of the two captures is what shows which case
a capture is.

**The frame's pictures, and the one experiment (`sheet`, `imgdiff`, `patch`).** `sheet <rdc> [outDir]` renders
one image per pass -- taken at the pass's last call that has a bound target, not at its last call, because a
barrier or a clear at the end of a pass has no state at all (measured: the `Clear` pass's last call has no
render target, no depth target and no root signature) -- writes them as BMPs, lays thumbnails out in a montage
and writes an index that names every tile with its marker eid, image eid and hash. `--list` prints what the
sheet *would* contain without rendering anything. `--every N`/`--max N` thin it, `--tile N` sets the thumbnail
size (the display readback is square, so tiles are square and nothing is cropped).

`imgdiff <a.bmp> <b.bmp>` reports both halves of "did the picture change": how many pixels differ and by how
much (exact), and a 64-bit difference hash of each and the distance between them (perceptual) -- because a
re-render on another driver differs in a handful of pixels and in none of the hash's bits, while a shader that
stopped writing colour differs in both. It reads back the BMPs this tool writes and the uncompressed 24- and
32-bit ones a viewer writes; anything else is refused with what it is rather than half-read. `--out` writes a
heat map. Like `bundle-verify`, it needs no device -- it runs inside a session, but nothing about the answer
depends on one.

One consequence worth knowing, because it is a bundle-visible change and was measured rather than assumed: the
display readback is **24-bit for some targets** (`desktop-2`'s 256x256 targets come back as 196,608 bytes,
three bytes per pixel), and such an image is now expanded to RGBA and written as a BMP -- where the old code
handed the wrong byte count to `WriteBMP`, failed, and fell back to the engine's own PNG encoder. So a bundle's
`rt/` set can now hold BMPs where it used to hold PNGs, with the same pixels: more images, not different ones.
The bundle's own manifest check (a dump hashed against the previous build) is what would catch an unintended
difference here, and the writer's bytes are unchanged -- `WriteBMP` itself was moved, not edited.

`patch <rdc> <eid> <stage> [outDir] [--from <file>] [--enc hlsl|dxbc|dxil|glsl|spirv] [--entry <name>]
[--flag name=value] [--dump <file>] [--compare] [--encodings]` is the "what if" command: it compiles a shader
for *this replay device* with `BuildTargetShader`, substitutes it for the capture's own with `ReplaceResource`,
clears the replay cache, re-runs the frame, and with `--compare` writes `before.bmp`, `after.bmp` and
`diff.bmp` plus the same numbers `imgdiff` prints. `--dump` writes the shader's disassembly so there is
something to read, `--encodings` prints what the target builds (measured on `desktop-2`: **dxbc, dxil,
hlsl**), and every failure says which one it was -- an unbuildable encoding, a compiler message, or no shader of
that stage bound at that event.

**What is proven about `patch`, and what is not.** Proven: it builds HLSL/DXBC/DXIL for the target, dumps real
disassembly (112 KB for one of `desktop-2`'s pixel shaders), compiles a hand-written replacement, reports
the compiler's own message when one fails, installs the replacement, re-runs the frame, and writes the three
images with an exact difference. **Not proven: that the replacement reaches the draw.** A pixel shader that
`discard`s every pixel -- a change no bookkeeping can fake -- rendered byte-identically to the original at the
event measured, with and without `ClearReplayCache`, so on this capture the substitution is either not reaching
the draw or that event's draw does not write the colour the display shows (its pass's colour may come from
earlier draws while the last one still has the target bound). The next step is to point it at an event that is
demonstrably the colour source -- `sheet`'s index names one image eid per pass, and `draws` says what kind of
call it is -- and only then treat a `patch` render as evidence. Reporting this is the point: the tool says
"nothing changed" rather than implying the patch worked.

* **`probe` runs alone.** It forces non-events on purpose, and a forced non-event keeps the last real
  event's state, so mixing it with other commands makes *one* of the two answers wrong whichever order
  they run in. The driver warns when a batch does it, and a probe that is not the first command of its
  session says so on stderr as well — the same fact, reported by the run that knows it rather than by one
  that predicted it. That warning is also what decides whether the answer is *cached*: only a first run's
  is, because a cached answer is somebody's first run (see "the probe is cached too", above).
* **Progress goes to stderr and to one log file per run**, `<exe name>_<date>_<time>.log.txt` beside the
  executable — never a shared file, so a run that hung stays readable after the next one starts, and two
  runs at once cannot write into each other's log (a second run in the same second takes `-2`). It
  records the working directory, each phase with a timestamp, every batch command with its own time,
  and why the run stopped. `--log <file>` names one exact file instead, truncated, since it is still
  that run's log. A message reaches stderr and the log through one formatting path each (`Log`, and `Fail`
  for a failure, which prints the same text to both), and both format into a `std::string` rather than a
  fixed buffer: a message can carry the engine's own words, and a 512-byte `vsnprintf` — what both used
  until 2026-09-22 — cut the shader-trace refusal mid-path, dropping every line that named a directory the
  engine had searched. Nothing else in the driver formats a message into a fixed buffer except the two
  places where the *caller* owns the buffer (`api.cpp`'s ABI error slot, whose length is a parameter).
  Long loops — the sweep, the per-event pass, `resources.json`, texture decoding — also
  print a **line every ten seconds** with the rate and what is left (`900/1740 (52%), 47 ms each, ~40 s
  left`), because the count-based version (`every 2000 ids`) stayed silent for the whole 164 s of a
  `--max-events 900` sweep: it stopped at id 1740, before the first line was ever due. A loop that
  finishes in under five seconds prints only its summary.
* **The log says when the binary is older than the sources it was built from.** `WarnIfDriverIsStale`
  compares the newest of `src/cpp/*.cpp|h` and `CMakeLists.txt` against the executable's own write time and
  prints `warning: this replay_dump.exe is older than its sources: src\cpp\x.cpp was written N s later…`
  with the build command under it. A replay host answers from the code it was compiled with, so a stale exe
  is indistinguishable from a current one from the outside — the answer looks exactly like an answer — and
  that is the one kind of wrong answer this tool exists not to produce: it was already used for a whole
  verification pass in this repository's own history. Silent when there is nothing to compare (the exe was
  copied out of the tree, or `src/cpp` is not there), never an error: running an old build on purpose is how
  a bundle from the previous revision gets reproduced. The other half is
  `python src\py\rdc_analysis.py build [--check]` (§4.1), because a running image cannot be overwritten on
  Windows — the link that would replace `bin\replay_dump.exe` fails with `LNK1104` while that same exe is
  what is running.
* **`$RDC_PROFILE=1` prints where the time went**, one line per measured call site at the end of the run:
  `SetFrameEvent`, the state copy, the state/shaders/cbuffer documents, the event rows, the action tree,
  `resources.json`, the usage lists. It is compiled in and off by default (two clock reads per call site,
  no arithmetic), and it is how the measurements below were taken — the engine is a black box behind a
  call, so timing the calls is the only way to answer "why is this taking minutes".

**What a bundle dump costs, measured.** On `desktop-2` (1.4 GB), a full default dump (896 events
collected out of 1,736 ids scanned, both bounded by the frame's last event): **47 ms per `SetFrameEvent`**,
and it is the same 47 ms whether the id changes or not — the call re-derives the state, which is what costs.
Everything else is small change: `GetD3D12PipelineState` returns a cached pointer (0.0 ms over 2,632 calls),
an event row is 0.6 ms, and `resources.json` for 11,082 resources took 10.6 s *before* the id-text lookup
replaced two linear scans (`IdText` per comparison, 11k × 5.6k) and 0.2 s after. The same dump before the
sweep was bounded would have walked ~30,000 ids — the clamped tail past event 1736 — for an hour-scale run;
bounded, it is 160 s. `RDC_PROFILE=1` on that run:

| call site | total | calls | each |
|---|---|---|---|
| `SetFrameEvent` | 124.0 s | 2632 | 47.1 ms |
| state document (`CmdState`) | 5.7 s | 98 | 57.7 ms |
| shaders document (`CmdShaders`) | 5.6 s | 98 | 56.9 ms |
| cbuffer documents | 17.1 s | 245 | 70.0 ms |
| event row (key, hash, targets, JSON) | 0.5 s | 896 | 0.6 ms |
| everything else, including 11,082 usage lists | ~0 | | |

The state documents each cost ~58 ms because **each one re-positions the replay itself** — 47 ms of that is
another refresh the caller had already paid for. That redundancy is *load-bearing*, and this is the note
that matters for anyone tempted by it:

> **Why the bundle reads the state twice per event.** The sweep refreshes each id to ask "is anything bound
> here?", and the pass after it refreshes the same ids again to build the rows. Building the rows from what
> the sweep had *already* read produces, for `desktop-2`'s first event, `"shaders": "cs=11388 "`,
> `"targets": []`, `"depth": "0"` — where the second read gets `"ps=11402 cs=11388 ms=11368 "`, a 1920x1080
> target and a depth buffer. The state a `SetFrameEvent` hands back depends on the *direction* the replay
> travelled to reach that event: the second pass's first move is backwards from where the sweep stopped,
> which makes the engine replay the frame from its start and produce a complete state, while a forward step
> onto an event hands back what the sweep's chunk-by-chunk accumulation had reached. Dropping the second read
> (and the document writers' own re-positioning) cut the run from 89 s to 71 s and changed three files of the
> bundle; both were reverted after hashing the bundles against a known-good reference. **The second read is
> not a redundant refresh: it is what makes the state complete.**

**The host's own timing is part of that too** (measured 2026-09-18). A change that only makes the *host
thread* faster between engine calls also changes what the engine answers: buffering the bundle's document
writes -- `setvbuf(stdout, NULL, _IOFBF, 1 << 20)` inside `CaptureStdout` -- took a 300-event dump from 36.5 s
to 30.1 s and moved `states/841.state.json` (3148 bytes against 2717) and `events.json`. It reproduced both
ways: five buffered dumps agreed with each other, two unbuffered ones agreed with the pre-change bundle byte
for byte. The rule for a future driver change is stronger than "hash the bundle against a fresh run": **hash it
against a bundle from the *previous* build**. What the host does between engine calls is not free time, it is
part of the input; the same is true of the offline side, which is why `$RDC_PROFILE` measures rather than
assumes (4.13).

What *is* allowed, and now taken: a document written **after the last engine call** may be buffered, because
nothing downstream of it can be answered differently. `SetDocumentBuffering(true)` is called once, after the
events loop, and `resources.json`'s 3.8 s of write syscalls becomes 0.1 s; the manifest of a 300-event dump
stays byte-identical to the unbuffered reference. Everything before that point -- the per-event state, shader
and cbuffer documents -- stays unbuffered exactly as before.

**The sweep is cached** (`$RDC_CACHE_DIR` moves the cache, `$RDC_NO_CACHE` disables it,
`%LOCALAPPDATA%\rdc-tools\cache` by default — the offline tool's own directory, so both halves have one cache
to inspect). `sweep-<key>.txt` holds a `# key: value` header and one id per line: not JSON, because a JSON
reader is a parser this program has nowhere else and a cache can also just be ignored. Every header field —
capture path, size, modification time, engine version, `--since`/`--until`/`--max-events` — is compared
before an id is trusted, so a stale or truncated file is refused and the sweep runs. On a hit the driver
still makes **one** `SetFrameEvent` to the last scanned id before starting the writing pass, which is what
reproduces the position the sweep would have left and keeps the state complete (without it, the warm bundle
differed from the cold one in five files — the same trap as above, one call cheaper than falling into it).
Measured on `desktop-2`, `--max-events 300`: **94.7 s cold, 36.9 s warm**, both bundles byte-identical
to the cold one (143 files, no differing sha256).
