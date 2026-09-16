# rdc_analysis.py — offline RenderDoc `.rdc` analyser

A single-file, dependency-light analyser for RenderDoc captures. It parses the `.rdc` container, decompresses
the frame-capture stream, walks the structured-data (SDChunk) stream, and decodes the D3D12 command payloads —
**without `renderdoc.pyd`, without the GUI, and without a GPU**.

Written to answer graphics questions that the RenderDoc UI makes tedious: *which pipeline state does this draw
use, which constant buffers are bound, what each root parameter is, what a descriptor table resolves to, and
where a resource id points.* It is used from the command line and from scripts; every command prints plain text.

What it deliberately does **not** do is reconstruct frame data that RenderDoc's own replay engine hands over
directly — uniform values, shader signatures, disassembly, decoded textures. Those are the replay driver's job
(§9); this tool stays on the file's structure and the command stream, where it is fast and needs no
device. §8 lists the sharp edges that follow from that.

> ## Vibe coded — use at your own risk
>
> Every feature and every line of code here is **vibe coded**, written ad-hoc by me, for me, because I am lazy
> and this was the fastest way to get my own answers while debugging RenderDoc captures. It is not a product,
> not a library, not supported, and not reviewed. Commands exist because one specific capture needed them;
> heuristics and hard-coded assumptions (chunk names parsed out of a source tree, descriptor-write and
> state-tracking heuristics, "this layout worked once") are load-bearing throughout. **Use at your own risk** —
> validate anything you plan to rely on against the capture you are actually debugging, and read §8 for the
> known sharp edges.

```
rdc-tools/
  rdc_analysis.py     the tool (single file, ~2400 lines)
  replay_dump.cpp     the replay driver: asks RenderDoc's engine what the file cannot say (§9)
  build_replay.ps1    builds it against the installed renderdoc.dll (output in .\build\)
  README.md           this file — usage, features, internals, how to extend
  ROADMAP.md          unimplemented features and planned work
  tests/              self-contained unittest suite (run: rdc_analysis.py selftest)
  pyrightconfig.json  type-checker config: typeCheckingMode "standard", target Python 3.8
  typings/            stub for the optional zstandard dependency
```

---

## 1. Requirements and setup

| Requirement | Notes |
|---|---|
| Python 3.8+ | tested with `C:\Program Files\Python311\python.exe` |
| `zstandard` (optional) | only for Zstd-compressed sections. Not needed for the captures used so far (they are LZ4, which is implemented in-file). `pip install zstandard` if `sections` reports `zstd`. |
| **RenderDoc source tree in the root folder** | **Required for readable chunk names.** The tool reads the *real implementation of RenderDoc* — the chunk-name enums — from a `renderdoc-src` folder in the root folder: `<root>/rdc-tools/renderdoc-src/`. **You must have a copy of the RenderDoc source tree there** (see §1.1). Without it the tool still runs, but `chunks` / `summary` / `draws` print numeric chunk IDs (`1040`) instead of names (`List_DrawIndexedInstanced`). |
| A `.rdc` capture | any D3D12 capture; Vulkan captures parse at container level but the chunk decoders are D3D12-specific |

Run it as:

```powershell
& 'C:\Program Files\Python311\python.exe' rdc_analysis.py <command> <file.rdc> [args...]
```

Running with no arguments prints the command list (the module docstring).

### 1.1 The RenderDoc source tree — `renderdoc-src` in the root folder

The tool does **not** guess chunk names: it parses the chunk-name enums out of the **real RenderDoc
implementation** at runtime, so the names it prints always match the RenderDoc version that produced the
capture. That means **you must have a copy of the RenderDoc source tree in the root folder, named
`renderdoc-src`**:

```
<root>/rdc-tools/                      <- the root folder of this tool
    rdc_analysis.py                    this tool
    README.md
    ROADMAP.md
    renderdoc-src/                     <- a copy of the RenderDoc source tree goes HERE
        renderdoc/
            core/core.h                        SystemChunk enum      (PushMarker, InitialContents, ...)
            driver/d3d12/d3d12_common.h        D3D12Chunk enum       (List_DrawIndexedInstanced, ...)
        renderdoccmd/
        ...
```

Only two header files are actually read (`renderdoc/core/core.h` and
`renderdoc/driver/d3d12/d3d12_common.h`), but keep the whole tree so that other parts can be consulted while
extending the tool.

**Where to get it.** Clone or download the source of the RenderDoc version used to take the capture — matching
the installed capture tool is what makes the enum values line up:

```powershell
cd 'C:\Workspace WIth Spaces\rdc-tools'
git clone --depth 1 --branch v1.46 https://github.com/baldurk/renderdoc.git renderdoc-src
# or unzip the source archive for the matching release into .\renderdoc-src
```

This project was developed against RenderDoc **1.46** (`C:\Program Files\RenderDoc`, see `sections` output for
the capture's own version).

**How the location is resolved** (`_find_renderdoc_src()` in the tool), in order:

1. the `RENDERDOC_SRC` environment variable, if set;
2. `<folder containing rdc_analysis.py>/renderdoc-src` — **the documented convention**;
3. `<parent of that folder>/renderdoc-src` — a sibling folder, for when the tool is nested;
4. `C:\Workspace WIth Spaces\renderdoc-src` — the historical absolute default.

If none of them contains `renderdoc/core/core.h`, the tool prints a one-line warning to stderr and continues
with **numeric chunk IDs**. Everything else — container parsing, decompression, payload decoding, `draws`,
`dxbc`, `verify` — is unaffected.

To point at a tree somewhere else for a single run:

```powershell
$env:RENDERDOC_SRC = 'D:\src\renderdoc'
& $py rdc_analysis.py chunks 'capture.rdc' 40 List_Draw
Remove-Item Env:\RENDERDOC_SRC
```

**Cost model.** The *first* command on a capture decompresses the frame-capture stream (≈3.2 s for the 374 MB
one, ≈3.6 s for the 631 MB one, plus the stream's size in RAM); every later command is served from the
decompressed-stream cache in ≈0.3 s (§4.8), and commands that only need the container (`blocks`) are instant
either way. For scripted queries inside one Python session, keep `stream` in a variable (see §7).

---

## 2. Quick start — pick the command by what you are asking

| Question | Command |
|---|---|
| What is this file, what sections does it have? | `sections` |
| Why is my capture unreadable / what compression is used? | `blocks` |
| What is the shape of the frame (passes, draws, chunk histogram)? | `summary` |
| Show me the pass/primitive tree | `markers` |
| Which draw is the one I care about? | `markers`, then `chunks <limit> List_Draw` |
| What pipeline state, constant buffers and vertex streams does draw N use? | `draws` |
| What is `res342`, and which buffers/textures exist at all? | `resources`, `resources <limit> <nameFilter>` |
| What does the descriptor heap hold that this table binding points into? | `descriptors`, `descriptors <rdc> <heapId>` |
| What exactly is in chunk N (payload hex + decoded fields)? | `chunk <N>` |
| Is the parse trustworthy? | `verify` |
| Which shaders are in this capture, and where? | `dxbc`, then `dump-shaders` |
| What does a shader read? (uniform names, signatures) | the replay driver — §9, not this tool (§8) |
| Where is this string / name in the stream? | `grep`, `count`, `names`, `strings` |
| Dump all shaders to disk for disassembly | `dump-shaders <outdir>` |
| Dump one chunk to disk | `dump-chunk <N> <outfile>` |

A typical triage session:

```powershell
$py = 'C:\Program Files\Python311\python.exe'
& $py rdc_analysis.py sections 'capture.rdc'
& $py rdc_analysis.py summary  'capture.rdc' | Select-Object -First 60
& $py rdc_analysis.py draws    'capture.rdc' 40
& $py rdc_analysis.py chunk    'capture.rdc' 452
```

---

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
parses those C++ enums at runtime, **out of the `renderdoc-src` copy in the root folder** (§1.1), so names stay
correct for the RenderDoc version that produced the capture.

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
| `python rdc_analysis.py selftest` | run the whole suite (`test` is an alias) |
| `python rdc_analysis.py selftest -v` | per-test output |
| `python rdc_analysis.py selftest -k Draws` | only tests whose id contains `Draws` |
| `python tests/test_rdc_analysis.py` | parsers and decoders only |
| `python tests/test_rdc_commands.py` | commands and CLI dispatch only |
| `python -m unittest discover -s tests -t tests` | the same suite through unittest |

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
| `python rdc_analysis.py cache` | list the entries: stream size, method, build time, source capture |
| `python rdc_analysis.py cache dir` | print the cache directory |
| `python rdc_analysis.py cache clear` | delete every entry (prints files removed and MB freed) |

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
python rdc_analysis.py resources 'capture.rdc' 20      # first 20 rows
python rdc_analysis.py resources 'capture.rdc' 0 lut   # every row whose name contains "lut"
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
  the source tree they print as numbers, like chunk names (§1.1).
* `limit` counts rows (`0` = no limit, as in `chunks`) and `nameFilter` is a case-insensitive substring of the
  name, so `0 lut` means "every row with `lut` in the name".

### 4.10 Descriptors

`descriptors` lists what the capture *wrote* into each descriptor heap, which is what makes a table binding
readable:

```powershell
python rdc_analysis.py descriptors 'capture.rdc'        # every written slot
python rdc_analysis.py descriptors 'capture.rdc' 0 298  # one heap, by id or by name
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

## 5. Worked examples

**Find the two sphere groups in a mobile base pass and see how they differ**

```powershell
& $py rdc_analysis.py markers 'mobile.rdc' | Select-String -Pattern 'BasePass' -Context 0,12
& $py rdc_analysis.py draws   'mobile.rdc' 40
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
& $py rdc_analysis.py dxbc          'mobile.rdc'          # where they are
& $py rdc_analysis.py dump-shaders  'mobile.rdc' '.\out\mobile'
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

Then: `python rdc_analysis.py psos capture.rdc`.

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
not on this list"). The ones that stay offline work items are tracked with an acceptance gate in `ROADMAP.md`
§11. A bullet here is a known limitation, not a permanent design decision.

* **Chunk names need the RenderDoc source tree in the root folder.** The tool expects
  `<root>/rdc-tools/renderdoc-src/` (see §1.1); if it is absent, or if its version is older than the one that
  produced the capture, names degrade to numeric IDs. The framing itself is version-stable, so decoding still
  works — only the labels are missing.
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
disassembly, post-VS geometry, the rendered image — is what `replay_dump.cpp` asks the engine for. It
is a second tool, built against the installed `renderdoc.dll`, and it exists because the offline
tool's job is what replay is bad at (the container, the chunk stream, sub-second queries) while this
one's job is what reading the file cannot answer at all.

```powershell
.\build_replay.ps1                              # MSVC + the installed DLL; output in .\build\
.\build\replay_dump.exe shaders 'capture.rdc' 270      # reflection: cbuffers, bindings, signatures
.\build\replay_dump.exe cb      'capture.rdc' 270 ps 3 # the named values of one cbuffer
.\build\replay_dump.exe state   'capture.rdc' 270      # bound shaders, outputs, root parameters
.\build\replay_dump.exe textures 'capture.rdc' --save .\out   # every texture, decoded to PNG
.\build\replay_dump.exe shaders 'capture.rdc' 270 --disasm    # ... with the disassembly
```

| Command | Gives |
|---|---|
| `info <rdc>` | RenderDoc version, driver, API properties, resource/texture/buffer/chunk counts |
| `draws <rdc> [max] [filter]` | the action tree (markers and calls) out of the structured file |
| `state <rdc> <eid>` | bound shaders per stage, render targets, depth target, root signature and every root parameter with its register, space and what is bound |
| `shaders <rdc> <eid> [--disasm]` | the reflection: constant blocks with **names** and bind points, resource bindings, input/output signatures, and the disassembly on request |
| `cb <rdc> <eid> <stage> <slot>` | the **named values** of one constant buffer, structs and arrays expanded |
| `textures <rdc> [filter] [--save <dir>]` | the texture list; `--save` decodes each one to PNG through `SaveTexture` |
| `mesh <rdc> <eid> [instance] [max]` | post-VS geometry: what the vertex shader actually emitted |
| `image <rdc> <eid> <out.bmp>` | the texture display at that event, written as a BMP (no PNG encoder needed) |
| `counters <rdc>` / `debug <rdc>` | GPU counters / debug messages |
| `usage <rdc> <resId or name>` | every event that touches a resource |
| `probe <rdc> [maxEid]` | which event ids the engine actually has — see below |
| `dump <rdc> [outDir=bundle]` | the whole frame to disk as a *bundle* for the offline tool — see below |
| `bundle-verify <dir>` | re-hash a bundle's files against its manifest (no device, no DLL) |

`--json` works on every command.

**The bundle — `dump` and `bundle-verify`.** `dump` is one replay session turned into files, so the offline
half (and a reader) can work without a device. It writes:

| File | Content |
|---|---|
| `manifest.json` | bundle version, driver and RenderDoc version, the capture's absolute path, byte count and SHA-256, the flags used, the scan result, every written file with its size and hash, and a `notInThisBundle` list saying what it cannot contain and why |
| `capture.json` | the capture header: API, driver, machine, feature flags (`shaderDebugging`, `pixelHistory`), counts, file size |
| `events.json` | one record per id with bound state: eid, pipeline object and `psoKind` (graphics/compute), the shader id per stage, the render targets with format and dimensions, the depth target, the root-parameter count, and a state hash |
| `states/<eid>.state.json` + `.shaders.json` | the full pipeline state and the reflection, written *through* the `state` and `shaders` commands, so a file is exactly what the command prints |
| `cbuffers/<eid>_<stage>_<slot>.json` | the named values of every constant block of every bound stage, at the state events |
| `resources.json` | every resource: id, name, kind, format/dimensions or byte size, and its usage list with the first and last event that touches it |
| `messages.json` | debug messages as objects: eid, numeric severity, severity text, text |
| `counters.json` | with `--with-counters`: eid, counter, value |
| `rt/<eid>_<slot>.png` | with `--with-images`: the bound render targets at the state events, through the engine's encoder |
| `textures/<resId>.png` | with `--textures`: every texture decoded, full size — the engine decodes but does not resize |

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

**Event ids are the engine's, not the file's.** The offline tool prints *chunk indices* and calls
them event ids; on the two Unreal captures that happened to be true, and on the hobby-renderer
capture it is not: `probe` shows the engine's first event with pipeline state at id 842 while the
structured file's first draw is at chunk 316, because RenderDoc numbers only what a *command list*
recorded (resource and PSO creation, `SetName` and descriptor writes are in the file but are not
events). `probe <rdc> <maxEid>` lists the ids that do have state, so an id can be checked rather than
assumed. `state`/`shaders`/`cb`/`mesh`/`image` all take the engine's ids.

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

**The build is strict on purpose** (`build_replay.ps1`): `/W4 /permissive- /Zc:__cplusplus
/Zc:preprocessor /utf-8`, with `/external:W0 /external:anglebrackets` so RenderDoc's own headers stay
quiet. It builds with zero warnings, and the SAL annotation on the `Fmt` helper makes the compiler
check every format string against its arguments — a varargs mismatch is undefined behaviour, and it is
also how the tool would print nonsense. `renderdoccmd.exe` is copied into `build/` along with the DLL:
the engine spawns `<its own directory>\renderdoccmd.exe crashhandle` for its crash handler, and without
it every run logs `Failed to create crashhandle server: 2`, waits 400 ms for a server that never
arrives, and continues with no handler.

**Opening a capture is the expensive part, so batch it.** Standing the replay engine up — its own copy
of the frame plus a replay device — is ~2 s on the Android capture and ~6 s on the 1.4 GB hobby one,
while individual commands cost 0.0–1.5 s. A batch file pays the open once:

```powershell
# each line is a command, in the same syntax minus the executable and the capture
"probe 120`ninfo`nstate 270`nshaders 270 --json" | Set-Content .\run.txt -Encoding ASCII
.\build\replay_dump.exe batch 'capture.rdc' .\run.txt
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
* **`probe` runs alone.** It forces non-events on purpose, and a forced non-event keeps the last real
  event's state, so mixing it with other commands makes *one* of the two answers wrong whichever order
  they run in. The driver warns when a batch does it.
* **Progress goes to stderr and to one log file per run**, `<exe name>_<date>_<time>.log.txt` beside the
  executable — never a shared file, so a run that hung stays readable after the next one starts, and two
  runs at once cannot write into each other's log (a second run in the same second takes `-2`). It
  records the working directory, each phase with a timestamp, every batch command with its own time,
  and why the run stopped. `--log <file>` names one exact file instead, truncated, since it is still
  that run's log.

## 10. Working with both tools — a playbook for an AI agent

The two tools split one job: **the engine extracts, the files get analysed, and only open questions go back to
the engine.** This section is the workflow that follows from that split, written for an agent (or a human) who
has to produce an analysis rather than run a command.

It is written against the roadmap as implemented: anything marked *(roadmap §N)* is a `ROADMAP.md` item and does
not exist yet, everything else runs today.

* **Offline today** — `sections`, `blocks`, `resources`, `descriptors`, `verify`, `summary`, `markers`,
  `chunks`, `chunk`, `draws`, `rootsig`, `strings`, `names`, `grep`, `dump`, `count`, `hex`, `dxbc`,
  `dump-chunk`, `dump-shaders`, `cache`, `selftest` (README §4).
* **Driver today** — `info`, `draws`, `state`, `shaders`, `cb`, `textures`, `mesh`, `image`, `counters`,
  `debug`, `usage`, `probe`, `batch`, and the bundle pair `dump` + `bundle-verify` (README §9).
* **Roadmap** — `report` (ROADMAP §1), `--repl`, `find`/`--at-marker`,
  `statediff`, `buffer`, `watch`, `debug --group`, `schema`, `sweep` (ROADMAP§2), pixel history, shader patching,
  shader debugging, overlays (ROADMAP§3), contact sheets, per-pass counters, `mesh --stage/--obj`, texture
  subresources (ROADMAP§4), `deps`, memory/aliasing report, `--format`, structural `diff` (ROADMAP§5),
  `replaydiff` (ROADMAP§6), the capture corpus and the golden/fixture tests (ROADMAP§7).

### 10.1 The rule, and why it is the rule

* **Opening a capture is the expensive thing.** It creates a device and takes ~2 s on a small capture and ~6 s
  on a 1.4 GB one, and only one replay may run at a time. A question the *files* can answer must never be
  asked of the engine: one `dump` (README §9) or one `batch` file pays the open once for the whole frame.
* **The offline half is the verifiable half.** No device, sub-second once the stream is cached, deterministic,
  and covered by the unittest suite — which is why the roadmap puts the analysis *heuristics* there (the report
  generator is offline code over a bundle, ROADMAP §1) and keeps the driver a data source.
* **Only the engine knows frame *data*** (names, values, decoded pixels, geometry, the rendered image); only
  the file knows *structure* (chunk stream, resource table, descriptor writes, lifetimes). A claim that needs
  both is assembled offline, from both.
* **An answer without evidence is not an answer.** Every claim should carry the event id or resource id it came
  from, plus the command that shows it — the same rule the tools follow themselves (`AGENTS.md`), and the
  reason an agent's answer can be checked rather than believed.

### 10.2 Extract once, to disk, and keep it

Extract to **files**, not to a terminal. A bundle can be re-read, grepped, diffed, hashed and handed to the
offline tool, and it survives the process that produced it; stdout does not, and a crash loses it.

| What to extract | Command | Why an agent wants it |
|---|---|---|
| the action list and markers | `replay_dump draws <rdc> 100000` (or the bundle's `events.json`, README §9) | the frame's structure: what is a pass, what is a draw, which eids are real |
| per-event state | `state <eid>`, or `states/<eid>.json` in the bundle (README §9) | render targets, depth, shaders, root parameters — the "what was bound" half of every claim |
| shader reflection | `shaders <eid>` (add `--disasm` when the shader itself is the question) | the **names** (`MobileBasePass`, `IndirectLightingCache`) and the bind points that turn a root parameter into a meaning |
| named constant values | `cb <eid> <stage> <slot>`, or the bundle's `cbuffers/` (README §9) | what the shader actually read: the numbers behind "the light is too bright" |
| buffer contents | `buffer <resId> [offset] [len]` *(ROADMAP§2)* | what is really in a buffer that reflection cannot describe (index data, structured buffers) |
| textures | `textures --save <dir>` today; subresources and raw/HDR options *(ROADMAP§4)* | decoded pixels to look at, plus the format/dimension facts for the audit |
| render targets | `image <eid> <out.bmp>` today; bundle `rt/` (README §9) and contact sheets *(ROADMAP§4)* | what the pass produced — the fastest way to see "this pass drew nothing" |
| geometry | `mesh <eid>` today; other stages and `--obj` *(ROADMAP§4)* | what the VS/GS emitted, which is where vertex bugs show themselves |
| GPU counters | `counters` today; per-pass fold *(ROADMAP§4)* | where the time went, where the driver supports it |
| debug messages | `debug` | the API's own complaints — the highest-value red flags there are |
| usage chains | `usage <resId>`, or `resources.json` (README §9) | who writes and who reads a resource: the evidence for "dead" and "uninitialised" |
| resource identity | `resources <rdc>` (offline) | names and sizes for every id, so output speaks in names instead of `res342` |
| the file's own view | `sections`, `verify`, `markers`, `draws`, `rootsig`, `descriptors` (offline) | structure, integrity, and the descriptor writes the engine does not report |
| the `.rdc` itself | keep it next to the bundle | the offline commands read it directly; the engine's output is a *cache* of what it said, never the only copy |

### 10.3 The order of operations

1. **Offline first, because it is free.** `verify` (is the file intact?), `summary`/`markers` (what is in it?),
   `resources` (ids → names), `draws` (the command stream's own account). No device, ~0.3 s each once the
   stream is cached (§4.8).
2. **One replay session for everything the engine alone can answer.** `dump` (README §9), or a
   `batch` file holding the specific questions (README §9). Never open the capture twice for the same
   question, and call `probe` first if the eids are not certain.
3. **Analyse offline over the bundle *and* the `.rdc`**: the report generator *(ROADMAP §1)* for the map, then targeted
   offline commands (`resources`, `deps` *(ROADMAP§5)*, `diff` *(ROADMAP§5)*, `rootsig`) for the specific thing.
4. **Targeted engine follow-ups only** for what is still open, using the eids the offline step produced — not a
   second fishing expedition. Files narrow the question, the engine answers it, files again.
5. **Assemble the answer with evidence**, and state plainly what could not be determined (§10.6).

### 10.4 Recipes

**A. "Explain this frame to me."** The five-minute pass, and the one to run before any other recipe.

```powershell
.\build\replay_dump.exe dump 'capture.rdc' bundle --with-images        # README §9: one replay, everything
python rdc_analysis.py report 'capture.rdc' --out out                   # roadmap §1: the executive summary
```

Read it in this order: frame at a glance → pipeline map → red flags → recommendations → the appendix of
reproduction commands. Each later recipe is a follow-up on one line of that report. Offline-only fallback
today: `sections`, `summary`, `markers`, `draws`, `resources`.

**B. "Why is this object missing, black, or the wrong colour?"** The pixel-level route, in order of cost.

```powershell
.\build\replay_dump.exe draws 'capture.rdc' 200 Shadow          # find the pass and the eids (markers first)
.\build\replay_dump.exe state 'capture.rdc' <eid>              # was it even drawn? RTs, shaders, root params
.\build\replay_dump.exe shaders 'capture.rdc' <eid>            # names + bind points: what the shader reads
.\build\replay_dump.exe cb 'capture.rdc' <eid> ps 3            # ... and the values it read
```

If the draw is there and the values look right, the pixel history *(ROADMAP§3)* is the next step and usually the
answer: it lists every event that touched that pixel **and the reason each was rejected** — `depthTestFailed`,
`stencilTestFailed`, `scissorClipped`, `viewClipped`, `shaderDiscarded`, `backfaceCulled`, `sampleMasked` —
with the values before and after. "Nothing drew it" then becomes "the scissor was 0×0 at eid 812". If the pixel
history says the shader itself is responsible, patch it *(ROADMAP§3)* — force the return value, disable the branch —
and re-render the draw to see what changes. That experiment is often faster than reasoning about the
disassembly.

**C. "Why do mobile and PC look different?"** The project's original question, and the reason `diff` and
`replaydiff` exist.

```powershell
python rdc_analysis.py diff mobile.rdc pc.rdc                      # roadmap§5: the file's view, no device
.\build\replay_dump.exe replaydiff mobile.rdc pc.rdc --with-images # roadmap§6: what the engine saw, and the renders
```

Then narrow by name rather than by index: the pass list (aligned by **marker path**, so it survives
re-captures), the named cbuffer values that moved (`watch <name>` *(ROADMAP§2)* turns that into a table over the whole
frame), and the schema table *(ROADMAP §1.7)* to say which *concept* the differing block is (`IndirectLightingCache`,
`Material`, …). Where the two engines' reflections disagree on names entirely, the offline `resources` and
`rootsig` views are the fallback: they compare what the *file* recorded. State the GPU caveat (§10.5) in the
answer: both frames were replayed on *this* machine's GPU.

**D. "Is this texture the problem?"**

```powershell
python rdc_analysis.py resources 'capture.rdc' 0 SkyViewLut       # name → id, size, format
.\build\replay_dump.exe usage 'capture.rdc' <resId>              # every event that touches it
.\build\replay_dump.exe textures 'capture.rdc' Sky --save .\tex  # decode it and look at it
```

Three things to check, in this order: is the *content* right (the decoded PNG), is the *format* right for how
it is sampled (the RT-format audit, *ROADMAP§4*), and was it *written* before it was read (`deps` *(ROADMAP§5)*: the
write→read chain). To prove its contribution rather than argue about it, substitute a flat texture for it and
diff the renders *(ROADMAP§3, §5)* — if the picture does not change, the texture is not the problem.

**E. "What is in this uniform — and is it ever what we expect?"**

```powershell
.\build\replay_dump.exe cb 'capture.rdc' 27931 ps 3     # named values, structs and arrays expanded
.\build\replay_dump.exe watch 'capture.rdc' Light.intensity   # roadmap§2: the value at every event
```

`cb` answers "what is bound here"; `watch` answers "is it ever different" — the difference between a constant
that is wrong and a constant that is never set at all. An all-zero buffer where the reflection says the shader
reads it is a red flag the report generator looks for *(ROADMAP §1.4)*, and `buffer <resId>` *(ROADMAP§2)* shows the raw bytes
when the reflection is not enough (structured buffers, index data, hand-built tables).

**F. "What does the shader actually do?"** Four independent views, cheapest first.

```powershell
.\build\replay_dump.exe shaders 'capture.rdc' <eid> --disasm   # the code, with the reflection next to it
python rdc_analysis.py dxbc 'capture.rdc' verbose              # which containers exist, and their hashes
.\build\replay_dump.exe mesh 'capture.rdc' <eid> 0 20          # what the VS emitted (and, ROADMAP§4, the rest)
```

Cross-check the signatures before reading the maths: VS output vs PS input (same semantic, index and width —
a mismatch is a real bug and a *certain* finding), and each stage's expected bindings vs what the root
signature actually binds. When debug info exists in the capture, the shader debugger *(ROADMAP§3)* steps one
invocation and prints the variables; when it does not (usually), say so — that is a limitation to report, not
a puzzle to keep grinding at.

**G. "Where does the time and the bandwidth go?"**

```powershell
.\build\replay_dump.exe counters 'capture.rdc'                 # what the driver can measure
.\build\replay_dump.exe counters 'capture.rdc' --per-pass      # roadmap§4: folded per pass (fetch is per event)
.\build\replay_dump.exe image 'capture.rdc' <eid> out.bmp      # what each pass produced (contact sheet, ROADMAP§4)
```

The offline half supplies the parts the GPU cannot: `deps` *(ROADMAP§5)* for writes nobody reads and reads nobody
wrote, the memory/aliasing report *(ROADMAP§5)* for "these N MB could be shared", and the VRAM budget for "what if
this were half resolution". Counters are hardware and driver dependent — if they are unavailable, the honest
answer is "not measurable here", not zero.

**H. "This looks uninitialised, or garbage."**

```powershell
python rdc_analysis.py draws 'capture.rdc'                     # offline: clears, copies and the order of writes
.\build\replay_dump.exe usage 'capture.rdc' <resId>            # engine: the same question, from the device's side
.\build\replay_dump.exe buffer 'capture.rdc' <resId> 0 256     # roadmap§2: the actual bytes
```

The class of bug where the answer is a *question*: a resource read in a pass that no earlier pass wrote
(legitimate for persistent resources, so it is reported as read-before-write rather than as a verdict), a
render target loaded instead of cleared, a constant buffer that reads as all zeros, a descriptor that points at
a resource the file shows was never filled in. Say which of those it is, and whether it can be *proved* from an
earlier eid.

**I. "Did my change fix it — or break something else?"**

```powershell
.\build\replay_dump.exe batch 'capture.rdc' run.txt > before.txt   # or `dump`, README §9
# ... rebuild / recapture ...
.\build\replay_dump.exe batch 'capture.rdc' run.txt > after.txt
```

Compare the **text** output byte-for-byte (it is the contract: this is how the driver's own regression pass is
run, README §9), and validate the JSON separately — parse it, and check for duplicate keys, because a plain
parse hides a repeated key and that is exactly how a dropped vertex-shader block went unnoticed once. With
images, compare with a difference threshold *(ROADMAP§4)* rather than by eye: "did the picture change" should be a
number. Keep the two bundles: the golden/fixture tests *(ROADMAP§7)* are the same idea, checked in.

**J. "Triage a capture someone sent me."** A fixed order, because each step can end the investigation.

1. `python rdc_analysis.py verify <rdc>` — is the file itself intact? (framing, padding, payload checks)
2. `replay_dump info <rdc>` — API, driver, GPU, feature flags (`pixelHistory`, `shaderDebugging`), counts.
3. `replay_dump debug <rdc>` — the API's own complaints; validation errors outrank any self-made hypothesis.
4. `replay_dump draws <rdc> 200` — the marker map: which passes exist, and which eids are real.
5. `python rdc_analysis.py resources <rdc>` — names for the ids, and the sizes that tell you what is big.
6. `replay_dump probe <rdc> <maxEid>` if the numbers look wrong: a wrong eid returns an *empty* state rather
   than an error, so "nothing is bound" must be checked before it is believed.
7. Then the report *(ROADMAP §1)* or the specific recipe above.

**K. "Answer a shader question the capture cannot."** Some questions are not in the frame: what the shader does
with *different* inputs. Replay has no `SetBufferData`, and `ReplaceResource` needs an existing replacement, so
this is the one case for the standalone harness (ROADMAP§8.5):

```powershell
python rdc_analysis.py dump-shaders 'capture.rdc' .\shaders   # the DXIL containers
python rdc_analysis.py rootsig 'capture.rdc'                  # the exact binding layout to reproduce
.\build\replay_dump.exe cb 'capture.rdc' <eid> ps 0           # realistic constants to start from
```

Feed those three into the harness with hand-built constants, and compare its result against what replay reports
for the same draw (`cb`, `mesh`, `image`) — the capture is the reference implementation.

**L. "Write the answer."** An agent's report should look like the tools' own output in one respect: evidence or
silence.

* Every claim cites the eid and/or resource id, and the command that reproduces it.
* Distinguish *certain* (the engine returned it, the file recorded it) from *heuristic* (a name matched a
  pattern) from *unknown* (undecoded format, missing reflection, unresolved bindless descriptor, no debug info,
  no counters).
* Prefer the exact answer: `shaders` names a cbuffer, `cb` gives its values — do not infer either from the
  stream, and never invent a name.
* Keep the numbers with their units and their source (`1.6 MB` from `resources`, not from a guess).
* State the environment caveats: replayed on this machine's GPU (not the device that recorded it), counters may
  be absent, and a stale bundle is stale (its manifest has the capture hash).

**M. "Run it headless."** Two different regimes, and they should not be confused:

* **CI, no GPU:** the offline tool only — `verify`, `summary`, `draws`, `resources`, `report` *(ROADMAP §1)*, `selftest`,
  Pyright. The fixture bundles *(ROADMAP§7)* are what make even the report generator testable there.
* **A machine with the GPU and the capture:** the driver, gated — `probe` alone, one replay at a time,
  `debug --fail-on error` *(ROADMAP§2)* as the pass/fail line, `bundle-verify` over the artefacts, and the
  golden/baseline comparison of §10.4 I. Record what CI cannot cover rather than implying coverage.

### 10.5 Pitfalls an agent must not walk into

* **A wrong eid is not an error.** `state`/`shaders` on an id that has no pipeline state return an *empty*
  result. Check with `probe`, or use a marker path *(ROADMAP§2)* instead of a number.
* **A chunk index is not an event id.** The offline tool numbers chunks; the engine numbers what a command list
  recorded. On one capture the first draw was chunk 316 while the first event with state was 842 (README §9).
* **`probe` runs alone** — it forces non-events on purpose and leaves the last real event's state behind, so
  mixing it into a batch of other commands makes one answer wrong whichever order they run in.
* **One replay at a time.** The engine creates a device per process; two at once is what makes a run look
  stuck, and a force-killed replay can leave the driver slow to create the next device.
* **Validate the JSON.** `--json | python -m json.tool`, plus a duplicate-key check; and know that the driver's
  log is per run, its last line decisive (`failed: …` = it stopped and says why, `done: exit N` = it finished,
  neither = killed).
* **Text output is the contract.** It stays byte-identical unless a change is deliberate and recorded; the
  JSON is what a consumer should parse (README §9).
* **Three names, three tools.** `replay_dump dump` (a bundle, README §9) is not `rdc_analysis.py dump` (a raw stream
  range); `replay_dump draws` (events, with engine state) is not `rdc_analysis.py draws` (chunk-level); and the
  report generator is `report` *(ROADMAP §1)*, because `summary` is already the structural histogram.
* **Formats and features are conditional.** Not every texture format can be decoded, shader debugging needs
  debug info that captures usually lack, counters need driver support, and pixel history needs the capture to
  support it. Report the gap; do not synthesise around it.

### 10.6 What "best analysis" means here

The bar is not the number of findings, it is that every finding is **checkable** and **ranked**: what the frame
does (from the report), what looks wrong (each red flag with its evidence and its certainty), what it costs
(counters, budgets, dead work), what to do next (the ranked recommendations with the command that reproduces
each), and what remains unknown. An answer that ends with "here is what I could not determine, and how you
could" is a better answer than one that ends with a confident guess — that is the rule the whole project is
built on.

## 11. See also

* `ROADMAP.md` — features not implemented yet, in priority order: the **executive summary** and the replay bundle
  it reads (§1), the driver's navigation and experiment commands (§3–§5), the offline **dependency graph**
  and memory report (§6), **two-capture A/B** (§7), the **verification corpus** (§8), and the D3D12 harness for
  synthetic inputs (`ROADMAP.md` §8.5).
* RenderDoc source, expected at `<root>/rdc-tools/renderdoc-src/` (§1.1). The files this tool and its docs rely
  on: `serialise/serialiser.cpp` (chunk framing), `serialise/rdcfile.cpp` (container), `core/core.h` and
  `driver/d3d12/d3d12_common.h` (chunk-name enums), `driver/d3d12/d3d12_command_list_wrap.cpp` (payload
  layouts), `driver/d3d12/d3d12_serialise.cpp` + `d3d12_manager.h` (`D3D12BufferLocation`, `PortableHandle`),
  `api/replay/renderdoc_replay.h` (the replay API used by the planned replay driver).
