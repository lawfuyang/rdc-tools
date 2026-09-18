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
  block** (no frame header). `lz4_block()` is a ~40-line decoder written in-file so the tool has no
  dependency; `decompress_lz4()` loops until `uncompLen` bytes are produced.

  The blocks are **pages of one continuous LZ4 stream, not independent frames**: RenderDoc compresses with
  `LZ4_compress_fast_continue` and decompresses with `LZ4_decompress_safe_continue` over a shared stream
  context (`serialise/lz4io.cpp`), so a match may point up to 64 KB back into the previous page.
  `decompress_lz4()` therefore carries a single output buffer across the blocks, and the blocks **cannot** be
  decoded in parallel — see §8 and `decompress_lz4`'s docstring for the measured evidence.
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
when it is not there (README §1.1, `rdc_renderdoc_src`) — so names stay correct for the RenderDoc version that
produced the capture.

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
| `Device_Create{ConstantBuffer,ShaderResource,UnorderedAccess,RenderTarget,DepthStencil}View` | the descriptor first — the **resource id is at +16** — and the destination `PortableHandle` last (`u64 heapId` at `length - 12`, `u32 index` at `length - 4`). 68 bytes for an SRV, 80 for a UAV in the captures |
| `Device_CopyDescriptors` / `…Simple` | `u64 count`, then `count x (u32 heapType, dst PortableHandle, src PortableHandle)` — 28 bytes per entry (36 bytes for a single copy) |
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
| `resources` | `<rdc> [limit=200] [nameFilter]` | the resource table: id, kind, byte size or dimensions + DXGI format, and the name the application gave it (§4.9) |
| `descriptors` | `<rdc> [limit=200] [heapFilter]` | the written slots of every descriptor heap: heap, slot, kind (cbv/srv/uav/rtv/dsv/sampler) and the resource it points at (§4.10) |
| `cache` | `[list\|dir\|clear]` | inspect or clear the decompressed-stream cache (§4.8); needs no capture file |
| `bootstrap` | `[tag]` | fetch the RenderDoc source tree the chunk names come from into `renderdoc-src` (README §1.1). Every command does this on demand; this runs it up front, pins a tag, and is the one path where a failed download is an error rather than the numeric-id fallback. Needs no capture file |
| `build` | `[--check]` | is `bin/replay_dump.exe` older than the sources it is built from (`src/cpp/*.cpp\|h` and `CMakeLists.txt`)? Without `--check` a stale or missing binary is built with `cmake --build build --config Release`, with the compiler's own output going straight to the console and the verdict printed again afterwards. Exit codes: 0 current (or the build succeeded), 1 out of date (with `--check`) or the build failed, **2 nothing to compare** — no binary or no sources, which is a fresh clone and not a mistake. Needs no capture file. The driver makes the same comparison itself and says so in its log (§9) |

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
| `dump-shaders` | `<rdc> <outdir>` | writes `shader_NN_<hash>.dxil` per container plus `shaders.txt` (hash, size, parts) — feed the `.dxil` to `dxc`/`dxil-spirv`/RenderDoc, or to the D3D12 harness (`ROADMAP.md` §8.5) |

### 4.5 Chunk level

| Command | Arguments | Output |
|---|---|---|
| `summary` | `<rdc>` | chunk count, draw/dispatch count, marker count, chunk-type histogram (top 40), and all markers in order with their chunk index. **A chunk index is not an event id in general** — the replay driver's `probe` showed the engine numbers only what a command list recorded, and on one capture the two were tens of thousands apart (§9) |
| `markers` | `<rdc>` | every marker chunk: index, kind, up to 3 strings (handles ASCII and UTF-16LE) |
| `chunks` | `<rdc> [limit=200] [nameFilter]` | chunk index, offset, **name**, payload length, and a preview of the strings inside — the way to find a chunk by name |
| `chunk` | `<rdc> <index>` | full inspector: id/name/flags/length, payload offset **and header size**, decoded fields via `decode_chunk`, 160-byte hex dump, and the payload's strings |
| `draws` | `<rdc> [maxDraws=80]` | per-draw table (see below) |
| `rootsig` | `<rdc> [maxSigs=40]` | every root signature the capture creates: version, cost in root-argument DWORDs, static samplers, flags, and each parameter with its type, register, space and descriptor ranges |
| `dump-chunk` | `<rdc> <index> <outfile>` | writes the chunk payload to a file |

#### `draws` — the per-draw table

Maintains the state of each **command list** while walking the stream: marker stack (`PushMarker`/`PopMarker`),
pipeline state (`List_SetPipelineState`), root signature and root bindings (graphics and compute, CBVs and
descriptor tables), vertex streams (`List_IASetVertexBuffers`) and the index buffer. On every draw/dispatch it
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
`renderdoc-src/` folder — two from Unreal Engine (PC and Android) and `HobbyRenderer FlyingWorld.rdc` from a
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
| `python src\py\rdc_analysis.py cache` | list the entries: stream size, method, build time, source capture |
| `python src\py\rdc_analysis.py cache dir` | print the cache directory |
| `python src\py\rdc_analysis.py cache clear` | delete every entry (prints files removed and MB freed) |

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
* DXGI format names are parsed out of RenderDoc's own copy of the enum (`common/dds_readwrite.cpp`); without
  the source tree they print as numbers, like chunk names (README §1.1).
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
plus the **report's own** schema, which is `REPORT_SCHEMA` in `rdc_schemas.py` rather than a file, because the
driver does not write `report.json`: the offline tool does. A folder that carries a `report.schema.json` of its
own wins, and `validate <bundleDir> <schemaDir>` checks `report.json` along with everything else, which is what
keeps the report's JSON twin honest. The so the
contract is a file a consumer can read rather than something reverse-engineered from a writer.

```powershell
python src\py\rdc_analysis.py validate bundle schema        # every document in a bundle
python src\py\rdc_analysis.py validate t.json schema textures   # a document saved from a command's stdout
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
| red flags | what the detectors found, each with the evidence that proves it and how certain it is — grouped by the severity table printed with it (the tool's declared judgement, per detector, so a line of it can be disagreed with), and every finding marked `unproven`, because none of them has been checked against a capture whose bug list is known (ROADMAP §6, the capture corpus) |
| recommendations | what to look at first, ranked by the same severity: one row per detector that fired, per oddity rule that matched and per gap the report could not close — each with the driver command that shows its evidence |
| what this report cannot tell you | the report states its own gaps, and every one of them is a roadmap item |
| appendix | the `replay_dump state` / `shaders` / `usage` commands that reproduce a pass, a claim and a notable row |

**Notability is a stated rule, not a score.** Every notable list prints the inputs it ranks by, in order, with
the way each is measured — and it prints the ones it could *not* use just as plainly: a draw's vertex count is
the first input of the notable-pass ranking and no bundle carries it, because the replay API exposes no action list, and
counter cost is only in a bundle written with `--with-counters`. A texture is ranked in *pixels*, not bytes,
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
`Android Renderer.rdc` the table claims **mobile base pass** (`MainVertexShader`/`MainPixelShaderMRT` with
`MobileBasePass` bound) plus the **indirect lighting cache** and **mobile reflection capture** blocks: at eid 262
nothing is bound to the cache (every member reads its default, and the row says so), and at eid 313
`IndirectLightingCacheMaxUV = 1, 1, 1` with `DirectionalLightShadowing = 1` — the cache is filled from the
frame's own state, not left at zero. On `PC Renderer.rdc` it claims **base pass** (`MainVS`/`MainPS` with
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
nothing on the third for a checked reason: `depth-logic` fires once on `PC Renderer.rdc` (depth testing on
with no depth target bound, eids 888–891), `stencil-without-writer` once on the Hobby capture (eids 841–851
testing a *read-only* depth-stencil target nothing wrote), and the two heuristics are silent there because
that frame's 30 GBuffer-named target bindings are all bound with blending *off* and it binds no 8-bit linear
target at all — an HDR frame, not a gap in the rules. That is the difference between a heuristic and a guess:
the finding names what it keys off, so its silence is checkable too. The 601 MB Hobby capture is dumped as a
600-event window (`--max-events`), which the report's provenance states. From
the capture's chunk stream: marker imbalance,
unattributed draws, and calls that can only draw nothing (0 vertices/indices/instances/groups). A detector
that could not look — no usage lists with `--no-usage`, no chunk-name map without the RenderDoc source tree,
a capture that has moved — is reported as *skipped* with the reason, because "clean" and "not checked" are
different answers, and a bundle with no findings says so without implying the frame is fine.

What it does **not** do yet: MSAA's *which subresource did the resolve copy* half (the
`ResolveSubresource` payload is not in a bundle) and the sRGB/linear half of the format rule (a later
sampling view's sRGB flag is not either), both stated as unclaimed rather than guessed at; counters folded into
the pass sections; and the two ranking inputs a bundle cannot carry (a draw's vertex count, §4.11 above). Passes
are *state-derived*, not named — a run of events that agree on call kind and render targets, or on pipeline and
shaders for a dispatch — and the report says so in its own words rather than describing a pass as something it
has not established.

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
`report: bundle read`, `report: detectors`, `string scan (serial)` and `string scan (parallel)`; each is the
`rdc_profile.timed(...)` decorator on the layer that does the work, so a command that calls a layer twice
adds two calls to one slot rather than inventing a slot per command.

**Read the numbers as ratios.** The same serial scan of the same stream measured 14.5 s and 25.2 s an hour
apart on this machine (64 cores, other processes running); what stayed stable is the parallel path being
6-13x faster *within one session*. Measured that way, on `HobbyRenderer FlyingWorld.rdc` (601 MB container,
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

**A worker is a fresh interpreter**, so whatever starts a pool must be importable without side effects:
`rdc_analysis.py` guards its `main()` and is fine; a script or test module that decompresses at module level
does it again once per worker (that is what one "stuck" measurement turned out to be). The suite never reaches
for a pool by accident -- its fixtures are far under `MIN_BYTES`, and the tests that exercise the pool pass
`procs=` explicitly.

**Caching is the other half, and it is already there.** A cold command pays 12-35 s of pure-Python LZ4 for
the 1.47 GB stream; a warm one pays 0.4-0.9 s to read it from disk (§4.8). Nothing *derived* is cached: a
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

`shader_bind_names` is deliberately **not** optimised, and is at least visible now: it scans the stream for
`DXBC` containers so a root parameter can be given the name its reflection offers, which costs 1.2-1.7 s on the
hobby capture and returns **no names at all** (that capture's DXIL has reflection stripped). The scan is one
`find` pass at the primitive's own rate -- 1.2 GB/s over the map, which is *not* slower than over `bytes` -- the
containers are really there (85 of them), and a shader can sit inside any payload, so gating the scan on a chunk
name would be a guess rather than a check. It has a `$RDC_PROFILE` slot now; the reason `draws` looked
mysterious for an hour is that its largest cost had no name in the table.

### 4.14 Reading the file: mapped, not copied

Every command used to pay two full reads before doing anything: the container (601 MB for the hobby capture,
in `parse_container`) and the stream (1.47 GB, in `load_stream`). Both are now `mmap`s, because nothing needs
the bytes all at once -- the header and section table are a few KB, one section body is decompressed on a
cache miss, and a command that only walks the frame touches one 4 KB page per chunk header. Measured on
`HobbyRenderer FlyingWorld.rdc`, in one session:

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
second. Nothing derived is cached for the same reason §4.13 gives: a table or index cache is a new class of
artefact on disk to avoid work that is already sub-second.

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
not on this list"). The ones that stay offline work items are tracked with an acceptance gate in
`ROADMAP.md` §11. A bullet here is a known limitation, not a permanent design decision.

* **Chunk names need the RenderDoc source tree.** The tool fetches it into `<root>/rdc-tools/renderdoc-src/`
  on first use (README §1.1) and says so; when the fetch cannot happen — no network, `$RDC_NO_BOOTSTRAP`, a
  `$RENDERDOC_SRC` that holds no tree — names degrade to numeric IDs. The same happens when the tree's version
  is older than the one that produced the capture, which the tool says when it names a chunk. The framing
  itself is version-stable, so decoding still works; only the labels are missing.
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
  which matched the engine's event ids on the two Unreal captures and does not on the hobby-renderer one
  (`replay_dump probe`, §9). Use the driver's ids when talking to the driver.

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

| Command | Gives |
|---|---|
| `info <rdc>` | RenderDoc version, driver, API properties (`pixelHistory` among them: the flag `pixelhistory` is gated on), resource/texture/buffer/chunk counts |
| `draws <rdc> [max] [filter]` | the engine's action list (`GetRootActions`) in frame order: markers and calls, each with the engine's **event id**, its depth in the marker nest, and the marker path it sits inside. The filter matches a call's name *or* a marker path, so a marker is a handle for the events under it |
| `state <rdc> <eid>` | bound shaders per stage, render targets, depth target, root signature and every root parameter with its register, space and what is bound |
| `shaders <rdc> <eid> [--disasm]` | the reflection: constant blocks with **names** and bind points, resource bindings, input/output signatures, and the disassembly on request |
| `cb <rdc> <eid> <stage> <slot>` | the **named values** of one constant buffer, structs and arrays expanded |
| `textures <rdc> [filter] [--save <dir>]` | the texture list; `--save` decodes each one to PNG through `SaveTexture` |
| `mesh <rdc> <eid> [instance] [max]` | post-VS geometry: what the vertex shader actually emitted |
| `image <rdc> <eid> <out.bmp>` | the texture display at that event, written as a BMP (no PNG encoder needed) |
| `pixelhistory <rdc> <eid\|last> <resId\|name> <x> <y>` | every event up to `<eid>` that tried to write that pixel: the test that rejected each attempt and the value before, from and after it (below) |
| `counters <rdc> [--per-pass [--passes <file>] [--top N]]` | GPU counters per event. `--per-pass` folds one counter over each pass (`FetchCounters` answers per event and takes no range): the passes come from the frame's markers — consecutive calls sharing a marker path are one — or from `--passes`, one `<first eid> <last eid> [<name>]` line per pass. The counter that is the cost is the engine's choice (`EventGPUDuration` when this replay produced one), named in the document with its unit; a replay that produces no results says so rather than printing a table of zeros, because GPU counters are a driver feature |
| `crosscheck <rdc> [eid] [--since N] [--until N] [--max-events N] [--max N]` | what the reflections say a shader wants against what the state says it was given: the vs output signature against the ps input signature, each stage's bindings against the root signature's declared ranges, and the render targets' formats against the ps output signature. Every finding names an event and quotes both sides. `linksChecked`, `bindingsChecked`, `bindingsUnmapped`, `targetsChecked` and `noRootParameters` say how much was actually compared — a capture whose shaders were stripped has no reflection, and then an empty findings list means *nothing was checked*, not that the frame is clean |
| `debug <rdc>` | debug messages |
| `usage <rdc> <resId or name>` | every event that touches a resource |
| `probe <rdc> [maxEid]` | which event ids the engine actually has — see below |
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

**The bundle — `dump` and `bundle-verify`.** `dump` is one replay session turned into files, so the offline
half (and a reader) can work without a device. It writes:

| File | Content |
|---|---|
| `manifest.json` | bundle version, driver and RenderDoc version, the capture's absolute path, byte count and SHA-256, the flags used, the scan result, every written file with its size and hash, and a `notInThisBundle` list saying what it cannot contain and why |
| `capture.json` | the capture header: API, driver, machine, feature flags (`shaderDebugging`, `pixelHistory`), counts, file size |
| `events.json` | one record per id with bound state: eid, pipeline object and `psoKind` (graphics/compute), the shader id per stage, the render targets with format and dimensions, the depth target, the root-parameter count, and a state hash. `psoKind` is the *call kind* from the capture's action tree — a dispatch or not — and not a reading of the bound shaders: on `PC Renderer.rdc` every event has a compute shader bound, so the shaders would call all 2132 of them compute, draws included |
| `states/<eid>.state.json` + `.shaders.json` | the full pipeline state and the reflection, written *through* the `state` and `shaders` commands, so a file is exactly what the command prints |
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
stops when a run of ids has nothing bound, and it is bounded by the file's chunk count, because ids past the
frame's last event *clamp* to it rather than coming back empty (measured: `probe 4500` reports 4405 ids with
state on a capture whose structured file has 723 chunks). All of it is written into the bundle's own
`notInThisBundle` list, so a reader does not conclude that the frame had no copies. Deriving the engine's ids
from the file is the open item in `ROADMAP.md` §3.

**Three things a replay host must do**, and the reason this file has a long comment about them: put
`REPLAY_PROGRAM_MARKER()` at file scope, call `RENDERDOC_InitialiseReplay()` before opening anything,
and `RENDERDOC_ShutdownReplay()` on the way out. Without the first two, the engine runs with
uninitialised global state and dies inside `OpenCapture` with an access violation — no message, no
log, nothing. The build script also has to make an import library from the DLL's exports (the
installer ships none) and put a copy of `renderdoc.dll` beside the exe.

**Event ids are the engine's, not the file's.** `draws` takes its ids from the engine's own action list
(`ActionDescription::eventId`), and so do `probe`, the bundle and every command that takes an `<eid>`; the
*offline* tool's `chunks`/`summary` print their own **chunk indices**, which are a different numbering. On the
two Unreal captures the two happened to agree, and on the hobby-renderer capture they do not: `probe` shows
the engine's first event with pipeline state at 842 while the structured file's first draw is at chunk 316,
because RenderDoc numbers only what a *command list* recorded (resource and PSO creation, `SetName` and
descriptor writes are in the file but are not events). `probe <rdc> <maxEid>` lists the ids that do have state,
so an id can be checked rather than assumed; a wrong id silently returns an *empty* state rather than failing.

**Marker paths come from the same list.** Every event carries the markers it sits inside (`Scene > BasePass`),
written by `state`/`shaders`/`cb` as a `marker` field and by `dump` into `events.json`, because a marker path
survives a re-capture where an event id does not and it is what lets an offline rule name a pass in the
engine's vocabulary. A bundle written before 2026-09-17 has no `marker` member at all: a reader asks for it
with a default rather than by index.

**The DLL is loaded, not linked** (`$RDC_RENDERDOC_DLL` overrides the path). RenderDoc's own
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
of the frame plus a replay device — is ~2 s on the Android capture and ~6 s on the 1.4 GB hobby one,
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
value before, from and after it. That is the answer no other command gives. Measured on `PC Renderer.rdc`, pixel
(960,540) of `SceneColor` is four rows and a story: the `GBufferClear` at eid 599, the lit cube of `BasePass` at
eid 692 — whose `ps` value `0.9535,0.2053,0.3410` is the colour that landed — the reflection pass at 881, and
`SkyAtmosphere` at eid 901 **rejected, `depth test failed`**: the sky is behind the cube. On the scoped form
(`pixelhistory 1035 res61330 500 300`) the single row shows the format's precision too: `ps`
`0.727051,0.328613,...` written into an `R11G11B10_FLOAT` target lands as `post` `0.726562,0.328125,...`.

The scope is the *event*: the engine's history covers every write up to the one the replay is positioned at
(`ReplayController::PixelHistory` filters the usage list by it), so `last` (the frame's own last event, from the
same action list `draws` prints) is the whole frame and a pass's last eid is the answer at the end of that pass.
`--at-marker <path>` supplies the scope instead of a positional id — `pixelhistory --at-marker BasePassParallel
res60857 960 540` on `PC Renderer.rdc` returns one row, the `GBufferClear` at eid 599, because the BasePass
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
so it belongs in a batch with the other questions: measured on the 1.4 GB hobby capture, the first call in a
process costs ~4 s (it builds the instrumented pipelines) and each further one ~1 s, while two calls on
`PC Renderer.rdc` cost ~2 s together.

**The one verdict in it that is not a closed case.** On D3D12 the `sample masked` test is an instrumented
re-draw of the event, and RenderDoc's own source carries `TODO: figure out if we always need to check this` over
the flag that enables it. Measured on the hobby capture's 1-sample targets: every base-pass fragment comes back
flagged, and one of them carries a *changed* `postMod` value in the same row. So the rows always print the
values next to the verdict, and the document's `note` member (a key/value line in the header, in both formats)
says which two to compare rather than letting the flag read as a closed case; a capture where nothing is flagged
— `PC Renderer.rdc` — has an empty `note`, and the same pixel asked of the two captures is what shows which case
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
display readback is **24-bit for some targets** (the hobby capture's 256x256 targets come back as 196,608 bytes,
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
something to read, `--encodings` prints what the target builds (measured on the hobby capture: **dxbc, dxil,
hlsl**), and every failure says which one it was -- an unbuildable encoding, a compiler message, or no shader of
that stage bound at that event.

**What is proven about `patch`, and what is not.** Proven: it builds HLSL/DXBC/DXIL for the target, dumps real
disassembly (112 KB for one of the hobby capture's pixel shaders), compiles a hand-written replacement, reports
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
  they run in. The driver warns when a batch does it.
* **Progress goes to stderr and to one log file per run**, `<exe name>_<date>_<time>.log.txt` beside the
  executable — never a shared file, so a run that hung stays readable after the next one starts, and two
  runs at once cannot write into each other's log (a second run in the same second takes `-2`). It
  records the working directory, each phase with a timestamp, every batch command with its own time,
  and why the run stopped. `--log <file>` names one exact file instead, truncated, since it is still
  that run's log. Long loops — the sweep, the per-event pass, `resources.json`, texture decoding — also
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

**What a bundle dump costs, measured.** On the 1.4 GB hobby capture, 900 events collected out of 1740 ids
scanned: **47 ms per `SetFrameEvent`**, and it is the same 47 ms whether the id changes or not — the call
re-derives the state, which is what costs. Everything else is small change: `GetD3D12PipelineState` returns
a cached pointer (0.0 ms over 1440 calls), an event row is 0.4 ms, and `resources.json` for 11,082
resources took 10.6 s *before* the id-text lookup replaced two linear scans (`IdText` per comparison, 11k ×
5.6k) and 0.2 s after. `RDC_PROFILE=1` on that run:

| call site | total | calls | each |
|---|---|---|---|
| `SetFrameEvent` | 68.3 s | 1440 | 47.4 ms |
| state document (`CmdState`) | 1.5 s | 26 | 58.2 ms |
| shaders document (`CmdShaders`) | 1.5 s | 26 | 58.3 ms |
| cbuffer documents | 6.2 s | 87 | 71.3 ms |
| event row (key, hash, targets, JSON) | 0.1 s | 300 | 0.4 ms |
| everything else, including 11,082 usage lists | ~0 | | |

The state documents each cost ~58 ms because **each one re-positions the replay itself** — 47 ms of that is
another refresh the caller had already paid for. That redundancy is *load-bearing*, and this is the note
that matters for anyone tempted by it:

> **Why the bundle reads the state twice per event.** The sweep refreshes each id to ask "is anything bound
> here?", and the pass after it refreshes the same ids again to build the rows. Building the rows from what
> the sweep had *already* read produces, for the hobby capture's first event, `"shaders": "cs=11388 "`,
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
Measured on the hobby capture, `--max-events 300`: **94.7 s cold, 36.9 s warm**, both bundles byte-identical
to the cold one (143 files, no differing sha256).
