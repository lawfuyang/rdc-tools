# rdc-tools — expansion roadmap / TODO

Features that are **not implemented yet** in `rdc_analysis.py` or in its replay driver (`src/cpp/replay_dump.cpp`,
REFERENCE §9), in rough priority order. Each item says what it is, why it is wanted, how it would be built, and
what blocks it.

Landed items are **removed** from this file rather than marked done: their reference moves to `README.md`,
`REFERENCE.md` or the code, and the remaining sections are renumbered with every cross-reference updated in the
same change.

Legend: **P0** = do next / unblocks current work · **P1** = high value, moderate effort · **P2** = useful,
opportunistic · **P3** = nice-to-have.

### The two tools and the line between them

`rdc_analysis.py` reads the **file**: the container, the chunk stream, payload layouts, resource tables,
descriptor writes. `replay_dump.exe` asks the **engine**: names, values, decoded textures, geometry, the
rendered image (REFERENCE §9). Anything that spans the two is written here as a **pipeline** item, and the split
inside it follows the same line — the driver *extracts* what only the engine knows, the offline tool
*analyses and presents* it, because that half must be testable without a GPU, a capture or a driver (§6).

### What is deliberately *not* on this list

Anything RenderDoc's own **replay engine** answers directly is not tracked as offline work here: the replay
driver (**REFERENCE §9**, landed 2026-09-15) answers it, and re-deriving it by hand would be building a worse
version of a tool that already exists. The offline parser's job is what replay is bad at — the container-level,
no-device, sub-second questions. Removed on those grounds: texture decoding (`GetTextureData` / `SaveTexture`),
shader disassembly (`GetShader` → `ShaderReflection`), the non-frame `.rdc` sections (`d3d12core`,
`d3d12sdklayers` — the engine reads them and the API surfaces what is in them), per-instance post-VS data
(`GetPostVSData`), full pipeline
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

**The report generator was the one carve-out, and it did not break the rule.** It added no new extraction:
it consumes what `replay_dump` and the offline parser already produce, and puts analysis, ranking and
presentation on top (landed — REFERENCE §4.11). That is the shape any future analysis takes: offline code over
a bundle of engine answers, testable from fixtures and diffable between runs (§6), with its tests landed in the
same change.

The **D3D12 harness** (§7.5) absorbs nothing: it exists for the one question replay cannot answer — what a
shader does with inputs the capture does not contain — so no item here is "free with a harness".

Current state for reference: the offline tool parses the `.rdc` container, decompresses the frame-capture stream
(LZ4 through the built `bin/rdc_lz4.dll`, and Zstd optional) and caches it on disk so
repeat
commands are instant (REFERENCE §4.8), walks the
SDChunk stream, decodes the main D3D12 draw/pipeline/CBV/vertex-buffer payloads, the barriers, the render-target
bindings, the clears, the discards and the copies (the use ledger behind `deps` and `memory`, REFERENCE §4.15),
the resource table
(id → kind/size/name, REFERENCE §4.9), the descriptor heaps (REFERENCE §4.10) and the root signatures (REFERENCE §3.4), inventories the
DXBC/DXIL containers, and can check its own parse (`verify`). The replay driver (REFERENCE §9) is the other
half: it asks the engine what no file read can answer — names, values, decoded textures, geometry, the
rendered image, the cross-checks between a shader's reflection and the state it is given (`crosscheck`), the
per-pass counter fold (`counters --per-pass`), and the bundle the report generator reads (`dump` +
`bundle-verify`). The offline tool has a
hermetic unittest suite (`python src\py\rdc_analysis.py selftest`) and is clean under Pyright
"Standard" (`npx --yes pyright@latest`); the driver has a build-and-baseline harness in the (gitignored)
`build/` folder. `AGENTS.md` holds the coding rules, REFERENCE §4.6/§4.7 how to run both. That suite is the safety
net for everything below — land the tests with the change, not after it.

### Environment convention — `renderdoc-src` in the root folder

The tool refers to the **real implementation of RenderDoc** for anything that must match the capture's own
version — currently the chunk-name enums, which are parsed out of the RenderDoc source at runtime. It looks for
a `renderdoc-src` folder **in the root folder**, i.e. beside the tool:

```
<root>/rdc-tools/src/py/rdc_chunkmap.py             the module that reads the enums
<root>/rdc-tools/README.md
<root>/rdc-tools/ROADMAP.md
<root>/rdc-tools/renderdoc-src/                     <- a copy of the RenderDoc source tree MUST be here
    renderdoc/core/core.h                               SystemChunk enum
    renderdoc/driver/d3d12/d3d12_common.h               D3D12Chunk enum
```

The search walks *up* from that module, so the tree can also sit beside it or at any folder above — the
convention is the repository root, which is two levels up.

**The user must have a copy of the RenderDoc source tree at `<root>/rdc-tools/renderdoc-src/`.** Get it with:

```powershell
cd 'C:\Workspace WIth Spaces\rdc-tools'
git clone --depth 1 --branch v1.46 https://github.com/baldurk/renderdoc.git renderdoc-src
```

Match the version that produced the captures (RenderDoc **0.46** in this project). Resolution order is
`$RENDERDOC_SRC` → `<tool folder>/renderdoc-src` → `<parent>/renderdoc-src` → the historical absolute default; if
none of them exists the tool prints one warning and falls back to numeric chunk IDs. Details in README §1.1.

This tree is also the source of truth for every "how is this serialised?" question, so every item below assumes
it is present and kept at the version matching the captures being analysed. Any item that reads from it should
say so explicitly, and should degrade gracefully when it is missing.

---

## 1. P1 — Replay driver: finding your way around a frame

* **`watch <name>`** — given a reflection member name (`Light.intensity`, `Material.Opacity`), print its value at
  every event of the frame as a small table, so a uniform that is right at one draw and wrong at the next is one
  command instead of forty `cb` calls. Slow by nature (a cbuffer read per event), so it takes a range. (~4–6 h)
* **`debug --group [--fail-on <severity>]`** — group messages by type/hash with counts and first/last eid, and
  exit non-zero when anything at or above a severity appears: the driver-side sanity gate, and the source of the
  summary's red-flag section. (~3 h)
* **The driver as a library** — a thin C ABI over the same code, called from Python with `ctypes`, so the offline
  tool can query a frame in-process instead of one process per question. It makes the report's bundle an optimisation
  rather than a requirement ("ask the engine for just what the summary needs"), and removes the 28-process
  baseline pass. Cost: a real ABI (handles, error returns, no C++ types crossing), which is why the bundle comes
  first. (~2–3 d)
* **`sweep <dir> [--out <root>]`** — run a command set (or a `dump`) over every `.rdc` in a folder, writing one
  bundle per capture with a combined index: what makes a corpus (§6, the capture corpus) usable rather than
  heroic. (~4 h)

## 2. P1 — Replay driver: experiments on the frame (the "what if" tools)

* **Shader debugging** — `DebugPixel(x, y, inputs)`, `DebugVertex(vertid, instid, idx, view)`,
  `DebugThread(group, thread)` and `DebugMeshThread(...)` return a `ShaderDebugTrace`; `ContinueDebug(debugger)`
  steps it and returns `ShaderDebugState`s; `FreeTrace` releases it. With a trace, print the inputs, the
  per-step variables and the outputs of one invocation. Only works for shaders built with debug info
  (`-Zi -Od`), which most captures do not have — the item is to *say that clearly* rather than fail obscurely,
  and to document how to re-capture with it. (~2 d)
* **Overlays as images** — `TextureDisplay` renders one texture; the overlay enum (`DebugOverlay`: `Drawcall`,
  `Wireframe`, `Depth`, `Stencil`, `BackfaceCull`, `ViewportScissor`, and the triangle-size / quad-overdraw
  overlays) annotates the targets themselves. A `--overlay wireframe|quad` switch on `image` gives the classic
  pictures for free — wireframe for topology, quad overdraw for a fragment-cost hunch. (~half a day once the
  display path is shared, which it nearly is)

## 3. P1/P2 — Replay driver: the frame's pictures, counters and geometry

* **Geometry beyond the vertex shader's output** — `mesh` currently reads `MeshDataStage::VSOut`. The enum also
  has `VSIn`, `GSOut`, `TaskOut`/`AmpOut` and `MeshOut` (there is no separate HS/DS output stage), so a mesh
  shader's or GS's actual output is reachable — plus `--obj <file>` to export the vertices and indices for an
  external viewer, and a bounding box / vertex count sanity line per draw. (~1–2 d)
* **Texture subresources and formats** — `SaveTexture`'s `TextureSave` has no mip/slice/sample fields, but
  `GetTextureData(tex, Subresource{mip, slice, sample})` does, so the driver can read exactly one subresource
  (including a cubemap face or one MSAA sample) and write it with its own PNG/BMP writer; the extras worth having
  are `--mip`, `--slice`, `--sample`, `--raw` (undecoded bytes), and a tonemapping option for float/HDR formats
  (with `RENDERDOC_HalfToFloat` already in use). A cubemap → 6 faces + a cross layout is what makes an
  environment map reviewable. (~1–2 d)
* **Format coverage audit** — list every format present with how many resources use it and whether the format can
  be decoded; a summary that silently skips a texture is worse than one that says "12 textures use ASTC, not
  decoded". Also the RT-format audit the summary needs: which targets are UNORM/sRGB/float, and what the shader
  wrote into them. (~half a day)

## 4. P1/P2 — Offline analysis: what the file knows that the frame does not

* **CSV and Markdown tables** — `--format table|csv|markdown` on `draws`, `resources`, `descriptors`, `rootsig`,
  `summary`, so results can be pasted into an issue or opened in a spreadsheet. (The replay driver has `--json`;
  the offline tool is the cheap, device-free half.) (~2 h)
* **Structural capture diff** — `diff <a.rdc> <b.rdc>` for the file's own view: same draw index or marker path,
  what changed in PSO/CBVs/streams/descriptors. It can say *"rp2 was a compute CBV at b0, now a vertex CBV at b1;
  `SkyViewLut` left the table"* because `draws` now carries the command-list state, resource names, descriptor
  resolution and the `rpN` annotations. The mobile-vs-PC question is the reason this exists — and it is what
  `passdiff`/`replaydiff` (REFERENCE §4.16) cannot do: they read the marker trees and the engine's answers, this
  one would read the descriptor writes and the command payloads the stream holds. (~1 d)
* **Root-signature vs chunk-stream cross-check** — verify that every root parameter the signature declares has the
  descriptor writes the stream shows, and that what replay reports at an eid agrees with what the file recorded.
  Two independent paths to the same fact is exactly what this project uses instead of trust. (~1 d)
* **VRAM budget and what-if** — total by category (RTs, textures, buffers, heaps), the peak of the widest pass,
  and "at half resolution" / "with this MRT dropped" arithmetic for a first-pass optimisation pass. (half a day)
* **Bundled chunk-name table** — embed the D3D12/SystemChunk enums (generated from `<root>/rdc-tools/renderdoc-src/`)
  so the tool still prints readable names when the `renderdoc-src` copy is absent or when analysing captures made
  by a different RenderDoc version. Keep `renderdoc-src` as the preferred source, the table as fallback. (~2 h)

## 5. P2 — A/B: two captures, one answer

* **Golden A/B** — `replaydiff` (REFERENCE §4.16) already compares two bundles, and it names the case where the
  two sides carry one `captureSha256` as a tools A/B rather than a capture A/B. What is missing is the *storage*:
  a bundle per capture in the corpus (§6, the capture corpus), plus the checked-in `replaydiff.json` a run is
  compared against, so "the same capture through two builds of the tools changed nothing" is a command and not a
  reading. The regression net for the analy**ser**, where the golden outputs below are the net for the parsers.
  (~4 h)

## 6. P2 — Verification, regression and the bug atlas

* **Golden output files for both tools** — snapshot `summary`/`draws`/`rootsig`/`dxbc` output per capture (offline)
  and the driver's `state`/`shaders`/`cb` text output per eid, and diff on every run: a decoder or writer change
  then shows up as a reviewable diff instead of silent drift. The driver half already exists in the build folder
  (`run_baseline.ps1`/`diff_baseline.ps1`, 14 commands × text+JSON); it needs checking-in as expectations, with
  the JSON validated separately (a parse plus a duplicate-key check — a plain parse hides repeated keys, which is
  how a dropped vertex-shader block went unnoticed). (~half a day)
* **Fixture bundles** — small, hand-written bundles in `tests/` that exercise every summary detector and report
  section without a capture, a GPU or the driver. This is what makes the report testable in CI and what
  keeps its heuristics from being falsifiable. (~1 d, and it grows with every detector)
* **Schema validation in the harness** — in place: the driver publishes the schemas (`schema --out schema`),
  `schema --check` fails when the checked-in copies and the driver have drifted (every branch covered by the
  driver's `selftest`), and `validate` checks a bundle against them, with the offline tests covering the
  validator and the schemas themselves. What is left is the last mile inside the baseline pass: run the check
  and `validate` over its own output, so a driver change that breaks a document fails the pass instead of the
  next consumer. (~1 h)
* **Schemas for the tool's own documents** — `report.json` has one (`rdc_schemas.REPORT_SCHEMA`, built in
  Python because `schema/` is the driver's) and `replaydiff.json` (REFERENCE §4.16) carries a `schemaVersion`
  but no checked shape, so `validate` cannot tell a reader whether a document it is handed is one this tool
  wrote. The schema, plus a test that walks it against a document the tests write, closes the same gap for the
  A/B that the report closed for itself. (~2 h)
* **The capture corpus with labelled expectations** — a `captures/` index (path, provenance, API, engine, size,
  what is known to be wrong with it) and a `*.expect.json` per capture: "this frame has an unbound `rp7`, a dead
  4 MB UAV, a marker imbalance" — and the detectors must fire. Unlabelled captures are still useful (no crash,
  no unproven claim), but the labels are what let a heuristic be *trusted* rather than hoped for. This is also
  where the three captures in this project get their known-bug list written down. (~1 d, ongoing)
* **CI** — a workflow for the offline tool (Pyright "Standard" + `selftest`, hermetic, no GPU) on every push; the
  driver needs a GPU and a capture, so it stays a local/manual gate with the baseline artefacts attached, and CI
  at least builds it (compile + `--help`, no device). Record what CI cannot cover rather than implying coverage.
  (~half a day)
* **Driver version guard** — compare `RENDERDOC_GetVersionString` with the capture's file version and refuse
  clearly (replay must be ≥ the capture's version), with a `--dll <path>`/`$RDC_RENDERDOC_DLL` override and a
  documented matrix of what has been tested. Today a mismatch surfaces as whatever the engine does next. (~4 h)
## 7. P2/P3 — Beyond the local desktop

* **Remote replay** — capture on a phone, replay where the driver lives: RenderDoc's remote server plus
  `ReplayOptions`, so a mobile capture is replayed by the mobile driver on the device (real counters, real driver
  behaviour, real mobile quirks). Blocked by device access and server setup, but it is the honest fix for "the
  capture was recorded on a phone and replayed on a desktop GPU", which is a caveat every report currently has to
  carry. (~2–3 d + a device)
* **A capture-side annotation layer** — the same installed DLL exposes the *in-application* API through
  `RENDERDOC_GetAPI` (`RENDERDOC_API_1_7_0`): `StartFrameCapture`/`EndFrameCapture`/`TriggerCapture`,
  `SetCaptureOptionU32`, `SetCaptureTitle`, and `SetObjectAnnotation`/`SetCommandAnnotation` for rich labels. A
  small launcher could capture with *our own* markers and object names for the cases the engine's own names miss
  — which is exactly the class of thing that makes a summary sharper. (~2–3 d)
* **Other APIs' names** — the offline tool's chunk-name loader already takes a driver (`load_chunk_names(driver=...)`);
  expose it, and accept that a Vulkan capture's chunks will be read by the same code with different enums. The
  replay driver is API-agnostic already; the analysis is where the D3D12 assumptions live. (~1 d, plus evidence)
* **D3D12 harness** (kept for completeness) — see §7.5 below.
* **Upstream** — the chunk-level findings (event ids vs chunk indices, what `InitialContents` really holds, the
  crash-handle server, the `TextureSave`/`GetTextureData` subresource split) are the kind of thing RenderDoc's
  own docs and tools benefit from. Not a work item, a standing intention: file them as they are confirmed.

### 7.5 The D3D12 harness (only for *synthetic inputs*)

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

**Verdict.** The replay driver is built (REFERENCE §9), so the ✔ column above is no longer a plan — it is what
`replay_dump` does today. `patch` builds a replacement shader and substitutes it for the capture's own, which
is the same door one step further open; what it has *not* yet shown is the replacement reaching a draw, so a
harness is still worth writing for the ✖ column: running a shader with hand-built constants — e.g. feed the
mobile base-pass pixel shader the HISM's baked SH to prove the shader path in isolation — and the first thing
to settle either way is whether a patched shader changes a render (REFERENCE §9 has the measurement).

**Sketch.** One `ID3D12Device` + a compute-style or full-screen-triangle PSO + a root signature matching the
shader's bind points (the offline tool can now print that layout: `rootsig`, REFERENCE §4.1); upload a 256-byte
constant buffer; dispatch/draw to a small RTV; read back with `ReadBackResource`. Inputs: the `.dxil` files from
`dump-shaders` and a JSON of uniform values (which the replay driver can export directly).

**Blockers.** Needs the shader's exact root signature layout (available from the capture — `rootsig` — or the
replay driver) and DXIL compilation to a PSO — `dxc` is available with the UE install.

**Effort.** ~2–3 days for a single-purpose harness; scope it to one shader at a time.

## 8. P3 — Robustness and scope

* **Zstd without the dependency** — either vendor a decoder or fail with a clear message (today it needs
  `pip install zstandard`). (~4 h)
* **Memory-mapped stream access** — avoid holding ~1.5 GB in RAM for the largest captures. (~4 h)
* **Non-D3D12 driver names** — `load_chunk_names(driver=...)` already takes a driver; expose it on the CLI.
  (~1 h)
* **Format coverage in the offline view** — the same audit §3's format coverage does for the engine's textures,
  for the file's payloads: what was decoded, what was skipped, and why. (~2 h)

## 9. Clear REFERENCE §8 ("Pitfalls and known limitations")

One entry per bullet in REFERENCE §8 that is *not* left to replay (see the note at the top), with the change that
closes it and the gate that proves it is closed.

**"Resolved" means** the bullet is deleted from REFERENCE §8, the `CHARACTERIZATION` tests that pinned the old
behaviour are replaced by tests of the *correct* behaviour, and every doc that described the limitation is
updated in the same change (AGENTS.md: behaviour is the contract, so these are deliberate, tested fixes —
never silent ones).

| # | REFERENCE §8 bullet | Plan | Priority | Effort |
|---|---|---|---|---|
| 9.1 | Chunk names need `renderdoc-src` | §4 bundled chunk-name table | P2 | ~2 h |

### Acceptance gates

* **9.1** (§4 bundled table): names resolve with `renderdoc-src` absent **and** when the capture's version is
  newer than the tree; the table is generated by a checked-in script and carries its RenderDoc version; the
  chunk-name warning (README §1.1) becomes "using bundled names for RenderDoc X".

---

## 10. Suggested order

Phased, and each phase stands on its own — nothing here is blocked on something later in the list.

**Phase 1 — comparisons and the long tail**
1. **Golden outputs and the corpus (§6, and the golden A/B half of §5)** — the regression net under everything
   above, and what lets a detector be trusted rather than hoped for.
2. **Bundled chunk names (§4, = the README's playbook)** — ~2 h, removes the last environment dependency and closes the last
   REFERENCE §8 bullet that is not replay's job.
3. **Remote replay (§7)** — the honest fix for the desktop-GPU caveat, when a device is available.
4. **The D3D12 harness (§7.5)** — only when a shader must be run with inputs the capture does not contain.