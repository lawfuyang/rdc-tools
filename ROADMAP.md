# rdc-tools — expansion roadmap / TODO

Features that are **not implemented yet** in `rdc_analysis.py`, in rough priority order. Each item says what it
is, why it is wanted, how it would be built, and what blocks it.

Legend: **P0** = do next / unblocks current work · **P1** = high value, moderate effort · **P2** = useful,
opportunistic · **P3** = nice-to-have.

### What is deliberately *not* on this list

Anything RenderDoc's own **replay engine** answers directly is not tracked as offline work here: the replay
driver (§1) is the plan for it, and re-deriving it by hand would be building a worse version of a tool that
already exists. The offline parser's job is what replay is bad at — the container-level, no-device, sub-second
questions. Removed on those grounds: texture decoding (`GetTextureData` / `SaveTexture`), shader disassembly
(`GetShader` → `ShaderReflection`), the non-frame `.rdc` sections (`d3d12core`, `d3d12sdklayers` — the engine
reads them and the API surfaces what is in them), per-instance post-VS data (`GetPostVSData`), full pipeline
state (`GetPipelineState`), true EID mapping (`SetFrameEvent` and the action list), typed constant-buffer values
and the `DebugDumpState`-style helpers built on them (`GetCBufferVariableContents`), a per-pass summary (a fold
over the action list), Vulkan support (replay is API-agnostic), and texture-format identification (which landed
anyway: `resources` prints the DXGI format and dimensions of every texture).

The same rule was applied to what was already **built**: `float` and `pattern` (searching the raw stream for a
uniform's *value*), `sig` (decoding vertex signatures), `rootconst` (dumping root-constant values) and `report`
(an inventory of UE shader/policy strings) were removed, together with the `dxbc`/`dump-shaders` harvest that
picked GI-ish strings out of containers. Each one was a worse answer to a question `GetCBufferVariableContents`
or `ShaderReflection` answers exactly. What is left is deliberately offline: the container and the chunk stream,
`draws`/`resources`/`descriptors`/`rootsig`, the cache, `verify`, and the structural search commands.

The **D3D12 harness** (§2) absorbs nothing: it exists for the one question replay cannot answer — what a shader
does with inputs the capture does not contain — so no item here is "free with a harness".

Current state for reference: the tool parses the `.rdc` container, decompresses the frame-capture stream
(LZ4 in-file, Zstd optional) and caches it on disk so repeat commands are instant (README §4.8), walks the
SDChunk stream, decodes the main D3D12 draw/pipeline/CBV/vertex-buffer payloads, the resource table
(id → kind/size/name, README §4.9), the descriptor heaps (§4.10) and the root signatures (§3.4), inventories
the DXBC/DXIL containers, and can check its own parse (`verify`).
22 commands, see `README.md`. Since 2026-09-15 it also has a hermetic unittest suite
(`python rdc_analysis.py selftest`) and is clean under Pyright "Standard" (`npx --yes pyright@latest`);
`AGENTS.md` holds the coding rules, README §4.6/§4.7 how to run both. That suite is the safety net for
everything below — land the tests with the change, not after it.

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
values in typed form. It also answers, directly, everything the top of this file leaves to it.

**API surface** (verified in `renderdoc/api/replay/renderdoc_replay.h`):

| API | Line | Gives |
|---|---|---|
| `SetFrameEvent(eventId, force)` | 487 | select any draw as the current event |
| `GetPipelineState()` | 545 | full state at that event |
| `GetShader(pipeline, shader, entry)` | 893 | `ShaderReflection` — **constant-block names**, sizes, bind points, I/O signatures, disassembly |
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
* Replay version must be **≥ the capture's file-format version**; the captures in this project are D3D12 from
  this PC (the "Android" one is the editor's mobile preview) and the third is a hobby renderer, so they replay
  locally on the same device ✔
* Budget roughly the capture's resource footprint in memory (mobile: 92 MB buffer + 4 MB ring; PC: 631 MB
  stream; hobby: 1.47 GB stream).

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

**Deliverables.** `replay_dump.py` (or `.exe`) with sub-commands mirroring the offline tool: `passes`, `draws`,
`state <eid>`, `cb <eid> <cbufslot>` (named values), `textures <eid>` (decoded, `SaveTexture`),
`mesh <eid> <instance>`, `image <eid> <out.png>`, `shaders <eid>` (reflection + disassembly), `counters`, plus
`--json` output so results can be diffed. Between them these cover everything this file deliberately leaves to
replay (see the note at the top), which is the point: they are the reason those items are not tracked as
offline work.

**Blockers.** Building the Python module (or writing the C++ driver); version match with the capture.

**Effort.** ~1–2 days for a useful first version (the API is high-level; the work is plumbing and output
formatting).

---

## 2. P0/P1 — D3D12 harness (only for *synthetic inputs*)

**What.** A minimal standalone D3D12 program that creates its own device/PSO/buffers and runs a shader (the
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
shader's bind points (the offline tool can now print that layout: `rootsig`, README §4.1); upload a 256-byte
constant buffer; dispatch/draw to a small RTV; read back with `ReadBackResource`. Inputs: the `.dxil` files from
`dump-shaders` and a JSON of uniform values (which the replay driver can export directly).

**Blockers.** Needs the shader's exact root signature layout (available from the capture — `rootsig` — or the
replay driver) and DXIL compilation to a PSO — `dxc` is available with the UE install.

**Effort.** ~2–3 days for a single-purpose harness; scope it to one shader at a time.

---

## 3. P2 — Quality of life

* **Bundled chunk-name table** — embed the D3D12/SystemChunk enums (generated from
  `<root>/rdc-tools/renderdoc-src/`) so the tool still prints readable names when the `renderdoc-src` copy is
  absent or when analysing captures made by a different RenderDoc version. Keep the `renderdoc-src` lookup as
  the preferred source, with the bundled table as the fallback. (~2 h)
* **`--json` / CSV output** for `draws`, `summary`, `dxbc` — makes results diffable and scriptable *without
  loading a device*, which is the offline tool's whole advantage over the replay driver's own `--json`. (~2 h)
* **Diff two captures** (`diff <a.rdc> <b.rdc>`) — same draw index, what changed in PSO/CBVs/streams. This is
  the single most valuable feature for A/B investigations like mobile-vs-PC, and the offline tool is the right
  place for it because it needs no device: `draws` now carries the command-list state, resource names,
  descriptor resolution and the `rpN` type/register annotations, so a diff can say *"rp2 was a compute CBV at b0,
  now a vertex CBV at b1; `SkyViewLut` left the table"* rather than "the numbers differ". (~1 day)
* **HTML report generator** — one self-contained page per capture: marker tree, draw table, shader table, CB
  dumps. (~1 day)

## 4. P3 — Robustness and scope

* **Zstd without the dependency** — either vendor a decoder or fail with a clear message (today it needs
  `pip install zstandard`). (~4 h)
* **Golden output files for the decoders** — snapshot `summary` / `draws` / `rootsig` output per capture and
  diff it on every run, so a decoder change shows up as a reviewable diff instead of a silent drift. (~2 h)
* **Memory-mapped stream access** — avoid holding ~1.5 GB in RAM for the largest captures. (~4 h)
* **Non-D3D12 driver names** — `load_chunk_names(driver=...)` already takes a driver; expose it on the CLI.
  (~1 h)

---

## 5. Clear README §8 ("Pitfalls and known limitations")

One entry per bullet in README §8 that is *not* left to replay (see the note at the top), with the change that
closes it and the gate that proves it is closed.

**"Resolved" means** the bullet is deleted from README §8, the `CHARACTERIZATION` tests that pinned the old
behaviour are replaced by tests of the *correct* behaviour, and every doc that described the limitation is
updated in the same change (AGENTS.md: behaviour is the contract, so these are deliberate, tested fixes —
never silent ones).

| # | README §8 bullet | Plan | Priority | Effort |
|---|---|---|---|---|
| 5.1 | Chunk names need `renderdoc-src` | §3 bundled chunk-name table | P2 | ~2 h |

### Acceptance gates

* **5.1** (§3 bundled table): names resolve with `renderdoc-src` absent **and** when the capture's version is
  newer than the tree; the table is generated by a checked-in script and carries its RenderDoc version; the
  §1.1 warning becomes "using bundled names for RenderDoc X".

---

## 6. Suggested order

1. **Replay driver** (§1) — the big unblock: it answers the whole list at the top of this file, so every day
   spent on an offline version of one of those items is a day spent twice.
2. **Bundled chunk names** (§3, = §5.1) — ~2 h, removes the last environment dependency and closes the last
   README §8 bullet that is not replay's job.
3. **Golden output files** (§4) — ~2 h, the regression gate that makes the next big change safe.
4. **Diff two captures** (§3) — ~1 day, the highest-value new offline feature, and offline is where it belongs.
5. **D3D12 harness** (§2) — only when a shader must be run with inputs the capture does not contain.
