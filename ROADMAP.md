# rdc-tools — expansion roadmap / TODO

Features that are **not implemented yet** in `rdc_analysis.py`, in rough priority order. Each item says what it
is, why it is wanted, how it would be built, and what blocks it.

Legend: **P0** = do next / unblocks current work · **P1** = high value, moderate effort · **P2** = useful,
opportunistic · **P3** = nice-to-have.

Current state for reference: the tool parses the `.rdc` container, decompresses the frame-capture stream
(LZ4 in-file, Zstd optional), walks the SDChunk stream, decodes the main D3D12 draw/pipeline/CBV/vertex-buffer
payloads, extracts DXBC/DXIL containers with their GI-related reflection strings, and can read a resource's
`InitialContents` by `(resourceId, byteOffset)`. 22 commands, see `README.md`.

### Environment convention — `renderdoc-src` in the root folder

The tool refers to the **real implementation of RenderDoc** for anything that must match the capture's own
version — currently the chunk-name enums, which are parsed out of the RenderDoc source at runtime. It looks for
a `renderdoc-src` folder **in the root folder**, i.e. beside the tool:

```
<root>/rdc-tools/rdc_analysis.py
<root>/rdc-tools/README.md
<root>/rdc-tools/ROADMAP.md
<root>/rdc-tools/renderdoc-src/                     <- a copy of the RenderDoc source tree MUST be here
    renderdoc/core/core.h                               SystemChunk enum
    renderdoc/driver/d3d12/d3d12_common.h               D3D12Chunk enum
```

**The user must have a copy of the RenderDoc source tree at `<root>/rdc-tools/renderdoc-src/`.** Get it with:

```powershell
cd 'C:\Workspace WIth Spaces\rdc-tools'
git clone --depth 1 --branch v1.46 https://github.com/baldurk/renderdoc.git renderdoc-src
```

Match the version that produced the captures (RenderDoc **1.46** in this project). Resolution order is
`$RENDERDOC_SRC` → `<tool folder>/renderdoc-src` → `<parent>/renderdoc-src` → the historical absolute default; if
none of them exists the tool prints one warning and falls back to numeric chunk IDs. Details in README §1.1.

This tree is also the source of truth for every "how is this serialised?" question, so every item below assumes
it is present and kept at the version matching the captures being analysed. Any item that reads from it should
say so explicitly, and should degrade gracefully when it is missing.

---

## 1. P0 — Replay driver (headless RenderDoc replay as a *data source*)

**What.** A second tool (`replay_dump.py`, or `replay_dump.exe`) that opens the `.rdc` in RenderDoc's replay
engine and interrogates the frame programmatically, instead of re-deriving byte layouts by hand.

**Why.** Every hard problem encountered so far was a *layout guess*: which uniform is bound at root parameter 7,
how long the `InitialContents` header is, whether a constant buffer's tail is SH or lightmap bias. Replay
removes that class of problem — the shader reflection supplies the names, and the replay engine supplies the
values in typed form. It also unlocks things an offline parser cannot do at all: decoded textures, post-VS
per-instance data, per-event rendered images, and shader patching.

**API surface** (verified in `renderdoc/api/replay/renderdoc_replay.h`):

| API | Line | Gives |
|---|---|---|
| `SetFrameEvent(eventId, force)` | 487 | select any draw as the current event |
| `GetPipelineState()` | 545 | full state at that event |
| `GetShader(pipeline, shader, entry)` | 893 | `ShaderReflection` — **constant-block names**, sizes, bind points, I/O signatures |
| `GetCBufferVariableContents(pipe, shader, stage, entry, cbufslot, buffer, offset, len)` | 1080 | **named** uniform members and their values |
| `GetStructuredFile()` | 765 | the chunk tree with **typed** parameters (validates the hand-written decoders) |
| `GetBufferData(buff, offset, len)` | 1114 | buffer contents at any event |
| `GetTextureData(tex, sub)` / `SaveTexture(TextureSave, path)` | 1126 / 1094 | **format-decoded** texture data, incl. complex-mapping saves |
| `GetPostVSData(instance, view, stage)` | 1104 | post-VS/GS geometry — per-vertex **and per-instance** |
| `CreateOutput` + `SetTextureDisplay` / `SetMeshDisplay` | 450 / 236 / 242 | the rendered image at any event; the mesh/attribute view |
| `FetchCounters` / `GetUsage(resource)` | 797 / 1062 | GPU counters; every event touching a resource |
| `BuildTargetShader` + `ReplaceResource` + `Replay()` | 659 / 726 / 487 | patch a shader/resource, re-run the draw |
| `OpenCapture` / `CloseCapture` | 1561 / 1569 | open/close a capture |

**Prerequisites (checked on this machine).**

* RenderDoc **1.46** installed at `C:\Program Files\RenderDoc`; `renderdoc.dll` present (x64 + x86) ✔
* `renderdoccmd.exe` exposes `capture | replay | convert | thumb | test | remoteserver` ✔ — a CLI smoke-test path
* **`renderdoc.pyd` / `pymodules` is NOT installed** ✖ → pick one:
  * **A.** Build the Python module from `<root>/rdc-tools/renderdoc-src/qrenderdoc/Code/pyrenderdoc`
    (CMake target `pyrenderdoc_module`), then `import renderdoc as rd`. If the `renderdoc-src` copy was cloned
    with `--depth 1` it does contain `qrenderdoc/`; if a partial copy is used, re-clone to get it.
  * **B.** Write a small C++ driver against `renderdoc.dll` (`RENDERDOC_OpenCaptureFile` →
    `cap.OpenCapture(ReplayOptions, &controller)`), which needs no Python at all and is the more robust option
    for automation.
* Replay version must be **≥ the capture's file-format version**; both captures in this project are D3D12 from
  this PC (the "Android" one is the editor's mobile preview), so they replay locally on the same device ✔
* Budget roughly the capture's resource footprint in memory (mobile: 92 MB buffer + 4 MB ring; PC: 631 MB stream).

**Sketch (Python, option A).**

```python
import renderdoc as rd

cap = rd.OpenCaptureFile()
cap.OpenFile(r'C:\path\mobile.rdc', '', None)
controller = cap.OpenCapture(rd.ReplayOptions(), None)

controller.SetFrameEvent(452, True)                 # the draw we care about
pipe = controller.GetPipelineState()
ps = pipe.GetShader(rd.ShaderStage.Pixel)
refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)

for i, cb in enumerate(refl.constantBlocks):
    print(i, cb.name, cb.byteSize, cb.bindPoint)   # <- names, finally

# values of the buffer bound at res342 + 0x8d200, parsed with the reflection's layout
for var in controller.GetCBufferVariableContents(
        pipe.GetGraphicsPipelineObject(), ps, rd.ShaderStage.Pixel, 'main',
        0 /* cbufslot */, rd.ResourceId(342), 0x8d200, 0):
    print(var.name, var.value.fv)
```

**Deliverables.** `replay_dump.py` (or `.exe`) with sub-commands mirroring the offline tool: `passes`,
`draws`, `state <eid>`, `cb <eid> <cbufslot>`, `textures <eid>`, `mesh <eid> <instance>`, `image <eid> <out.png>`,
`counters`, plus `--json` output so results can be diffed.

**Blockers.** Building the Python module (or writing the C++ driver); version match with the capture.

**Effort.** ~1–2 days for a useful first version (the API is high-level; the work is plumbing and output
formatting).

---

## 2. P0/P1 — D3D12 harness (only for *synthetic inputs*)

**What.** A minimal standalone D3D12 program that creates its own device/PSO/buffers and runs a UE shader (the
DXIL extracted by `dump-shaders`) with constants that we choose.

**Why (and why it is *not* a replacement for replay).** Replay can only re-run the captured commands with the
captured resources — there is **no `SetBufferData`**, and `ReplaceResource` needs an existing replacement — so it
cannot answer "what would this shader do with *different* inputs?". A harness can, and it is also far faster to
iterate with than re-capturing a frame.

| Need | Replay | Harness |
|---|---|---|
| What is bound, and its named values, at this draw | ✔ | ✖ |
| What the VS emitted for instance 0 | ✔ | ✖ |
| What this draw actually renders | ✔ | ✖ |
| What the shader does if I change it | ✔ (`BuildTargetShader` + `Replay`) | ✔ |
| What the shader does with inputs the capture does not contain | ✖ | ✔ |
| Standalone shader bisecting / permutation unit tests | ✖ | ✔ |
| Fast iteration without a capture round-trip | ✖ | ✔ |

**Verdict.** Build the replay driver first. A harness becomes worth writing only when we need to run a shader
with hand-built constants — e.g. feed the mobile base-pass pixel shader the HISM's baked SH to prove the shader
path in isolation — and even that can often be avoided by patching the shader in replay instead.

**Sketch.** One `ID3D12Device` + a compute-style or full-screen-triangle PSO + a root signature matching the
shader's bind points; upload a 256-byte constant buffer; dispatch/draw to a small RTV; read back with
`ReadBackResource`. Inputs: the `.dxil` files from `dump-shaders` and a JSON of uniform values (which the replay
driver can export directly).

**Blockers.** Needs the shader's exact root signature layout (available from the capture or the replay driver)
and DXIL compilation to a PSO — `dxc` is available with the UE install.

**Effort.** ~2–3 days for a single-purpose harness; scope it to one shader at a time.

---

## 3. P1 — Offline parser gaps

### 3.1 Root signature decode + `rpN` → uniform name mapping
**What.** Finish parsing the `RTS0` part (parameter types, registers, spaces, visibility, descriptor ranges) and
combine it with the shader reflection's constant-block bind points to name every root parameter.
**Why.** Today `draws` prints `rp7=res342+0x8d200` with no name, which is what caused a wrong conclusion.
**How.** `RTS0` layout is already decoded in the investigation notes:
`u32 version=2 | u32 numRootParameters | u32 rootParametersOffset | u32 numStaticSamplers | u32 staticSamplersOffset | u32 flags`,
params at `header+0x18`, ranges at `header+0xc0`; determine the per-parameter stride empirically (candidate
12/16/20 bytes — validate that the range region starts exactly where the parameter array ends), then map
`(register, space)` against the `RDEF` bindings harvested from the DXIL containers.
**Note.** Names are *not* in the root signature; they come from the reflection, so this item and 3.2 belong
together — and replay (item 1) answers it directly.
**Effort.** ~half a day, or free once replay works.

### 3.2 Descriptor heap parsing
**What.** Decode `Device_CreateDescriptorHeap` / `Device_CopyDescriptors` / `Device_CopyDescriptorsSimple` and the
descriptor-table root bindings, so SRV/UAV/CBV tables can be resolved to resources.
**Why.** Many UE bindings are descriptor tables, not root CBVs; without this, only root-bound resources are
visible.
**Effort.** ~1 day (descriptor increments, heap types, per-range mapping).

### 3.3 Resource table (id → name / type / description)
**What.** Build a table from `Device_CreateCommittedResource*` / `CreatePlacedResource*` / `CreateReservedResource*`
(and the resource-name chunks) so `res342` can be reported as e.g. "buffer 4 MB, name `SceneUniformBuffer`".
**Why.** `draws` currently speaks only in numeric resource ids, which makes cross-referencing painful.
**Effort.** ~half a day.

### 3.4 Generalised `InitialContents` parsing
**What.** Parse the resource/subresource description properly instead of scoring candidate header sizes.
**Why.** `initial` currently guesses the data start; a real parse removes the guess and enables mip/slice
selection for textures.
**Effort.** ~half a day (read `Serialise_InitialState` in the D3D12 resource manager).

### 3.5 Texture dumping (format decoders)
**What.** Decode BC1–7, ASTC, and float formats; write PNG/EXR; select mip/slice.
**Why.** "Which lightmap/VLM brick is actually bound?" is a recurring question; today only raw bytes are
dumpable.
**Effort.** ~2–3 days (or delegate entirely to replay's `GetTextureData`/`SaveTexture` — recommended).

### 3.6 Other `.rdc` sections
**What.** Decompress and index the non-zero sections (init data / resource records) rather than only section 0.
**Why.** Large captures can keep resource payloads outside the frame-capture stream; today they are invisible.
**Effort.** ~half a day.

### 3.7 Shader disassembly
**What.** Disassemble `ILDN`/`ILDB` bytecode to text, or shell out to `dxc`/`dxil-spirv`.
**Why.** `dxbc` shows which uniforms a shader reads, but not the code path that consumes them.
**Effort.** ~1 day if shelling out; much more if implemented in Python.

### 3.8 Per-instance mesh data offline
**What.** Reconstruct post-VS per-instance data from the instance vertex streams.
**Why.** This is the "what instance SH did the VS actually emit" question; replay's `GetPostVSData` answers it,
offline it needs the vertex-factory layout.
**Effort.** ~2 days; replay is the better route.

### 3.9 Full pipeline state reconstruction
**What.** Track blend/rasterizer/depth-stencil/render-target state per draw so `draws` can print the complete
state, not just PSO/CBVs/streams.
**Effort.** ~1 day.

### 3.10 True EID mapping
**What.** Map chunk indices to real RenderDoc EIDs (the current assumption "chunk index ≈ EID" holds in the
captures tested but is not guaranteed).
**Why.** So output can be cross-referenced with the GUI and the replay API.
**Effort.** ~half a day (find the EID-assignment chunks/order).

---

## 4. P2 — Quality of life

* **Bundled chunk-name table** — embed the D3D12/SystemChunk enums (generated from
  `<root>/rdc-tools/renderdoc-src/`) so the tool still prints readable names when the `renderdoc-src` copy is
  absent or when analysing captures made by a different RenderDoc version. Keep the `renderdoc-src` lookup as
  the preferred source, with the bundled table as the fallback. (~2 h)
* **Parallel / cached decompression** — decompress LZ4 blocks with `multiprocessing`, and cache the
  decompressed stream to a temp file keyed by (path, mtime, size) so repeated commands are instant. (~4 h)
* **`--json` / CSV output** for `draws`, `summary`, `dxbc` — makes results diffable and scriptable. (~2 h)
* **Diff two captures** (`diff <a.rdc> <b.rdc>`) — same draw index, what changed in PSO/CBVs/streams. This is
  the single most valuable feature for A/B investigations like mobile-vs-PC. (~1 day)
* **HTML report generator** — one self-contained page per capture: marker tree, draw table, shader table, CB
  dumps. (~1 day)
* **UE-specific helpers** — resolve `FShader` hashes to shader type/permutation names, material instance and
  primitive names, `LightmapType`/`ShouldUseVLM` per primitive, i.e. bring `LM.DebugDumpState`-style data into
  the capture report. (~1–2 days)
* **Pass summary** — draw count, triangle count and state changes per pass, so a frame can be triaged in one
  screen. (~4 h)
* **Texture-format identification** — at least report the DXGI format and dimensions of every texture. (~2 h)

## 5. P3 — Robustness and scope

* **Vulkan support** — the container/framing code is API-agnostic; needs `VulkanChunk` enums plus payload
  decoders for `vkCmdDraw*`, descriptor sets, etc. (~3–5 days)
* **Zstd without the dependency** — either vendor a decoder or fail with a clear message (today it needs
  `pip install zstandard`). (~4 h)
* **Unit tests + fixtures** — small synthetic `.rdc`-like streams for the container/chunk walkers, plus golden
  output files for the decoders, so refactors are safe. (~1 day)
* **Memory-mapped stream access** — avoid holding ~650 MB in RAM for the largest captures. (~4 h)
* **`draws` state fidelity** — track state per PSO properly (and root-signature-aware), so inherited bindings
  are reported instead of omitted. (~4 h)
* **Non-D3D12 driver names** — `load_chunk_names(driver=...)` already takes a driver; expose it on the CLI.
  (~1 h)

---

## 6. Suggested order

1. **Replay driver** (§1) — unblocks `rpN` naming, typed CB values, decoded textures, per-instance data.
2. **Diff two captures** (§4) — the fastest path to mobile-vs-PC and before-vs-after answers.
3. **Root signature / descriptor decode** (§3.1, §3.2) and **resource table** (§3.3) — make the offline output
   self-explanatory.
4. **Bundled chunk names + caching** (§4) — remove the two environment dependencies.
5. **D3D12 harness** (§2) — only when a shader must be run with inputs the capture does not contain.
