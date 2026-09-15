# rdc_analysis.py — offline RenderDoc `.rdc` analyser

A single-file, dependency-light analyser for RenderDoc captures. It parses the `.rdc` container, decompresses
the frame-capture stream, walks the structured-data (SDChunk) stream, and decodes the D3D12 command payloads —
**without `renderdoc.pyd`, without the GUI, and without a GPU**.

Written to answer graphics questions that the RenderDoc UI makes tedious: *which pipeline state does this draw
use, which constant buffers are bound and what is inside them, which shader permutations exist and what GI
uniforms do they read.* It is used from the command line and from scripts; every command prints plain text.

> ## Vibe coded — use at your own risk
>
> Every feature and every line of code here is **vibe coded**, written ad-hoc by me, for me, because I am lazy
> and this was the fastest way to get my own answers while debugging RenderDoc captures. It is not a product,
> not a library, not supported, and not reviewed. Commands exist because one specific capture needed them;
> heuristics and hard-coded assumptions (signature scoring, state-tracking guesses, "this layout worked once")
> are load-bearing throughout. **Use at your own risk** — validate anything you plan to rely on against the
> capture you are actually debugging, and read §8 for the known sharp edges.

```
rdc-tools/
  rdc_analysis.py     the tool (single file, ~1300 lines)
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

**Cost model.** Every command decompresses the frame-capture stream first (≈2–4 s and ≈400 MB RAM for a
374 MB capture, ≈7 s / ≈650 MB for the 631 MB one). Commands that only need the container (`sections`,
`blocks`) are instant. For repeated queries on the same capture, run the tool inside a Python session and keep
`stream` in a variable (see §7).

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
| What exactly is in chunk N (payload hex + decoded fields)? | `chunk <N>` |
| Is the parse trustworthy? | `verify` |
| Which shaders are in this capture and what GI uniforms do they read? | `dxbc`, `sig` |
| Where is this string / uniform name / float in the stream? | `grep`, `count`, `float`, `pattern` |
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
| `RDEF` | resource bindings (cbuffers, textures, samplers, bind points) |
| `RDAT` | reflection blob — cbuffer variable names, source file names, type info (the tool harvests strings from it) |
| `ISG1` / `OSG1` | input / output signatures (`parse_signature()` decodes them) |
| `ILDN` / `ILDB` | shader bytecode |
| `RTS0` | **root signature** (D3D12 serialised form; the tool currently only inspects it, see ROADMAP) |

`sig` decodes `ISG1`/`OSG1`: `u32 count`, then `count` × 24-byte elements (`nameOffset u32, semanticIndex u32,
register u32, ...`), with the string table after the array. This is how per-instance vertex streams show up
(`TEXCOORD6…TEXCOORD12`) and how PS/VS are told apart (`SV_Target` vs `SV_Position`).

---

## 4. Command reference

### 4.1 Container level (fast, no decompression)

| Command | Arguments | Output |
|---|---|---|
| `sections` | `<rdc>` | file size, rdc version, progVersion, thumbnail, driver name/id; every section (type, flags, version, compressed/uncompressed size, name); then the decompressed size of section 0 vs expected, and the method used |
| `verify` | `<rdc>` | walks the chunk stream and checks what would make a parse untrustworthy: frames claiming bytes the stream does not hold, and payload lengths that disagree with the layout the decoder expects (see §3.4). Also reports the alignment padding totals — non-zero padding is legal (stale buffer bytes) so it is a note, not a failure. Exit code 0/1, so it can gate a script |
| `blocks` | `<rdc>` | per section: name, flags, first 16 bytes hex — enough to identify compression (`28b52ffd` = Zstd) |

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

### 4.3 Numeric / pattern search

| Command | Arguments | Output |
|---|---|---|
| `float` | `<rdc> <value>` | exact float32 bit-pattern search; prints the packed bytes, hit count, and ±64 bytes of text context per hit |
| `pattern` | `<rdc> <f0,f1,…> [count=72]` | packs the float list as little-endian float32, finds **all** occurrences, and dumps the next `count` floats at each hit, 8 per row. Built for locating uniform buffers by a known prefix, e.g. the ILC buffer whose first 12 floats are `Add=(0,0,0) Scale=(1,1,1) MinUV=(0,0,0) MaxUV=(1,1,1)` → `pattern <rdc> 0,0,0,1,1,1,0,0,0,1,1,1` |

### 4.4 Shader level

| Command | Arguments | Output |
|---|---|---|
| `dxbc` | `<rdc> [verbose]` | one row per DXBC/DXIL container: offset, stage (`PS`/`VS`/`CS`/`root-sig`/`?`), hash, parts, and the **GI-related cbuffer variable names** found in the container (`IndirectLighting*`, `VolumetricLightmap*`, `DirectionalLightShadowing`, `LightmapResourceCluster`, `SkyBentNormal`, `PrecomputedIndirect*`). Then per container: GI vars, VS input semantics, PS outputs, HLSL source file names; `verbose` adds all strings |
| `sig` | `<rdc>` | decoded `ISG1`/`OSG1` per shader: `IN: nameN(regR)`, `OUT: nameN` |
| `dump-shaders` | `<rdc> <outdir>` | writes `shader_NN_<hash>.dxil` per container plus `shaders.txt` (hash, size, parts, GI vars) — feed the `.dxil` to `dxc`/`dxil-spirv`/RenderDoc for disassembly |

### 4.5 Chunk level

| Command | Arguments | Output |
|---|---|---|
| `summary` | `<rdc>` | chunk count, draw/dispatch count, marker count, chunk-type histogram (top 40), and all markers in order with their chunk index (= EID) |
| `markers` | `<rdc>` | every marker chunk: index, kind, up to 3 strings (handles ASCII and UTF-16LE) |
| `chunks` | `<rdc> [limit=200] [nameFilter]` | chunk index, offset, **name**, payload length, and a preview of the strings inside — the way to find a chunk by name |
| `chunk` | `<rdc> <index>` | full inspector: id/name/flags/length, payload offset **and header size**, decoded fields via `decode_chunk`, 160-byte hex dump, and the payload's strings |
| `draws` | `<rdc> [maxDraws=80]` | per-draw table (see below) |
| `rootconst` | `<rdc> [maxChunks=8]` | `SetGraphicsRoot32BitConstant(s)` payloads decoded to root param index, value count, dest offset, and float values |
| `dump-chunk` | `<rdc> <index> <outfile>` | writes the chunk payload to a file |

#### `draws` — the per-draw table

Maintains running state while walking the stream: marker stack (`PushMarker`/`PopMarker`), pipeline state
(`List_SetPipelineState`), vertex streams (`List_IASetVertexBuffers`), index buffer, and the list of root
constant-buffer bindings. On every draw/dispatch it prints:

```
#452    asicShapeMaterial Sphere 3042     idx=2880 inst=1 DrawIndexedInstanced
        CBV: rp10=res1907+0x120000  rp11=res342+0x3b000  rp6=res342+0x3b000  rp7=res342+0x8d200
        VB : res315+0x3f7400(sz6708,st12)  res315+0x3f5900(sz2236,st4)  res315+0x300(sz16,st0)
        IB : res315+0x3f2b00
```

`rp<n>=res<id>+0x<offset>` is a root-parameter CBV binding; `res<id>+0x<off>(sz,st)` is a vertex stream
(resource, byte offset, size, stride). A `res0+0x0(sz0,st0)` entry is a **NULL vertex buffer** — a useful
signature in itself. State lists reset when the PSO changes and after each draw, so the printed CBVs are the
ones bound *for that draw* (see §8: inherited bindings are not reported).

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
(`$env:RDC_TEST_CAPTURE = 'C:\path\capture.rdc'` — copies of the two captures used here live in the ignored
`renderdoc-src/` folder); they take about a minute, because each command re-decompresses the whole stream. A
third class parses the real `renderdoc-src` enums and is skipped when the tree is absent. Tests that pin
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

---

## 5. Worked examples

**Find the two sphere groups in a mobile base pass and see how they differ**

```powershell
& $py rdc_analysis.py markers 'mobile.rdc' | Select-String -Pattern 'BasePass' -Context 0,12
& $py rdc_analysis.py draws   'mobile.rdc' 40
```

**Read a constant buffer that a draw binds**

Not possible offline any more: `draws` tells you *which* buffer is bound (`rp7=res342+0x8d200`) but not what is
inside it. Reading the contents needs the replay driver (`GetCBufferVariableContents`, `ROADMAP.md` §1); the old
`initial` command guessed the data offset by scoring a known signature against candidate header sizes, which
did not survive contact with the captures it was pointed at, so it was removed rather than fixed.

**Locate a uniform buffer by content**

```powershell
# the ILC uniform always starts Add=(0,0,0) Scale=(1,1,1) MinUV=(0,0,0) MaxUV=(1,1,1)
& $py rdc_analysis.py pattern 'mobile.rdc' 0,0,0,1,1,1,0,0,0,1,1,1
```

**See which shader permutations read GI**

```powershell
& $py rdc_analysis.py dxbc 'mobile.rdc' | Select-String -Pattern 'IndirectLighting|VolumetricLightmap'
& $py rdc_analysis.py sig  'mobile.rdc' | Select-String -Pattern 'TEXCOORD1[0-2]'
```

**Dump shaders for external disassembly**

```powershell
& $py rdc_analysis.py dump-shaders 'mobile.rdc' '.\out\mobile'
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
* `D3D12BufferLocation` (CBV/SRV/UAV and vertex/index views) serialises as **(resourceId, byteOffset)** — this is
  the VA→resource mapping, obtained for free.
* `D3D12_GPU_DESCRIPTOR_HANDLE` serialises as a **`PortableHandle` (`u64 heapId, u32 descriptorIndex`)**, not as
  a pointer (`d3d12_manager.h`), which is why `List_SetGraphicsRootDescriptorTable` payloads are 24 bytes.
  Verified on both captures, and it is what `verify` caught: the decoder assumed 20.
* The serialised D3D12 root signature (`RTS0` part) is
  `u32 version=2 | u32 numRootParameters | u32 rootParametersOffset | u32 numStaticSamplers | u32 staticSamplersOffset | u32 flags`,
  followed by the parameters and descriptor ranges — and it contains **no parameter names**. Names live in the
  shader reflection (`RDAT`), so mapping `rpN` → uniform name needs reflection *and* the root signature, or the
  replay API (see ROADMAP).
* `Device_CreatePipelineState` embeds the DXBC/DXIL containers of the shaders it references, which is why
  `parse_dxil_containers()` finds shaders at offsets *inside* those chunks.
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

Every bullet below is tracked as a work item with an acceptance gate in `ROADMAP.md` §6 (6.1–6.7); the plan
is to clear the whole list, so a bullet here is a known defect, not a permanent design decision.

* **Chunk names need the RenderDoc source tree in the root folder.** The tool expects
  `<root>/rdc-tools/renderdoc-src/` (see §1.1); if it is absent, or if its version is older than the one that
  produced the capture, names degrade to numeric IDs. The framing itself is version-stable, so decoding still
  works — only the labels are missing.
* **Only section 0 is decompressed.** Additional sections are listed but not parsed.
* **No name resolution for root parameters.** The serialised root signature carries no names, so `rpN` cannot be
  mapped to a uniform name offline. This is the main reason for the replay driver in `ROADMAP.md`.
* **`draws` state tracking is a heuristic.** CBV/VB lists are "bindings since the previous draw", reset on PSO
  change; a draw that inherits state from earlier in the frame will show fewer bindings.
* **No texture decoding.** `GetTextureData`-style format decoding (BC/ASTC/float, mips, slices) is not
  implemented; only raw bytes can be dumped.
* **No shader disassembly.** `dump-shaders` extracts containers; disassembling the `ILDN`/`ILDB` bytecode needs
  an external tool.
* **Performance.** Decompression is single-threaded Python LZ4 (~2–4 s for 374 MB). Fine for interactive use,
  not for batch processing hundreds of captures.

## 9. See also

* `ROADMAP.md` — features not implemented yet, including the **replay driver** and the **D3D12 harness**.
* RenderDoc source, expected at `<root>/rdc-tools/renderdoc-src/` (§1.1). The files this tool and its docs rely
  on: `serialise/serialiser.cpp` (chunk framing), `serialise/rdcfile.cpp` (container), `core/core.h` and
  `driver/d3d12/d3d12_common.h` (chunk-name enums), `driver/d3d12/d3d12_command_list_wrap.cpp` (payload
  layouts), `driver/d3d12/d3d12_serialise.cpp` + `d3d12_manager.h` (`D3D12BufferLocation`, `PortableHandle`),
  `api/replay/renderdoc_replay.h` (the replay API used by the planned replay driver).
