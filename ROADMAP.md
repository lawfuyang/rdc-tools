# rdc-tools — expansion roadmap / TODO

Features that are **not implemented yet** in `rdc_analysis.py` or in its replay driver (`replay_dump.cpp`,
README §9), in rough priority order. Each item says what it is, why it is wanted, how it would be built, and
what blocks it.

Landed items are **removed** from this file rather than marked done: their reference moves to `README.md` (or to
the code), and the remaining sections are renumbered with every cross-reference updated in the same change.

Legend: **P0** = do next / unblocks current work · **P1** = high value, moderate effort · **P2** = useful,
opportunistic · **P3** = nice-to-have.

### The two tools and the line between them

`rdc_analysis.py` reads the **file**: the container, the chunk stream, payload layouts, resource tables,
descriptor writes. `replay_dump.exe` asks the **engine**: names, values, decoded textures, geometry, the
rendered image (README §9). Anything that spans the two is written here as a **pipeline** item, and the split
inside it follows the same line — the driver *extracts* what only the engine knows, the offline tool
*analyses and presents* it, because that half must be testable without a GPU, a capture or a driver (§7).

### What is deliberately *not* on this list

Anything RenderDoc's own **replay engine** answers directly is not tracked as offline work here: the replay
driver (**README §9**, landed 2026-09-15) answers it, and re-deriving it by hand would be building a worse
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

**The executive summary (§1) is the one exception, and it does not break the rule.** It adds no new extraction:
it consumes what `replay_dump` and the offline parser already produce, and adds analysis, ranking and
presentation on top. Every *detector* in it is offline code over a bundle of engine answers, which is what makes
it testable from fixtures and diffable between runs (§7) — the analysis is the part that must be verifiable, and
the verifiable place for it is Python with the existing suite.

The **D3D12 harness** (§8.5) absorbs nothing: it exists for the one question replay cannot answer — what a
shader does with inputs the capture does not contain — so no item here is "free with a harness".

Current state for reference: the offline tool parses the `.rdc` container, decompresses the frame-capture stream
(LZ4 in-file, Zstd optional) and caches it on disk so repeat commands are instant (README §4.8), walks the
SDChunk stream, decodes the main D3D12 draw/pipeline/CBV/vertex-buffer payloads, the resource table
(id → kind/size/name, README §4.9), the descriptor heaps (§4.10) and the root signatures (§3.4), inventories the
DXBC/DXIL containers, and can check its own parse (`verify`). 22 commands, see `README.md`. The replay driver
(README §9) adds 15 more (`info`, `draws`, `state`, `shaders`, `cb`, `textures`, `mesh`, `image`, `counters`,
`debug`, `usage`, `probe`, `batch`, and the bundle pair `dump` + `bundle-verify`) that ask the engine for what no
file read can answer — including the bundle the report generator reads. Since 2026-09-15 the
offline tool also has a hermetic unittest suite (`python rdc_analysis.py selftest`) and is clean under Pyright
"Standard" (`npx --yes pyright@latest`); the driver has a build-and-baseline harness in the (gitignored)
`build/` folder. `AGENTS.md` holds the coding rules, README §4.6/§4.7 how to run both. That suite is the safety
net for everything below — land the tests with the change, not after it.

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

## 1. P0 — Executive summary: one `.md` that explains a frame

**What.** `python rdc_analysis.py report <capture.rdc> --out <dir>` produces `report.md` (plus
`report.json` and, with `--html`, one self-contained page) that describes a frame end to end: what the frame
is, what every major pass does, which passes and resources matter, what looks wrong, what to look at next — and
what it could not determine. Every claim in it carries the event id and resource id that proves it, and the
appendix lists the commands that reproduce each one, so a reader can check the report rather than trust it.

**Why.** Everything the two tools can say is already reachable, but only one question at a time and only for
someone who knows which question to ask. The frame-level "what is going on here, and what is suspicious" is the
work actually being done by hand today, over a dozen commands, and it is the same work every time. It is also
the only way the expensive part (a replay session) is paid once for a whole frame instead of once per question:
today a 14-command pass is 28 process starts and ~3.5 minutes (README §9), and the open dominates.

**How — three stages, each independently useful.**

1. **Extract** — `replay_dump dump <rdc> --out <dir>` writes a *bundle*: one capture, one replay session, plain
   JSON/PNG files on disk (the bundle, README §9). The driver stays a data source: it answers, it does not judge.
2. **Analyse** — the offline tool reads the bundle (and the `.rdc` itself for the chunk stream, the resource
   table and the descriptor writes) and derives the pass structure, the per-pass roll-ups, the ranked lists and
   the red flags. All of this is pure functions over files, so it runs in milliseconds and is unit-testable
   without a GPU (§7).
3. **Present** — a deterministic Markdown report + its JSON twin + an optional HTML page. Deterministic means
   byte-stable for a fixed bundle: stable ordering everywhere (eids ascending, tables sorted by a stated key),
   no timestamps, no absolute paths in the prose — so two runs diff cleanly and a golden test can pin it (§7).

### 1.1 The bundle is the interface

Everything downstream depends on the bundle's shape, so it is versioned (`bundleVersion`) and hashed
(per-file SHA-256 in a manifest) — a report can then state exactly what it read, and refuse to guess if a file
is missing or was written by a different driver version. The contents are the bundle's (README §9); the report
needs all of them, but
must degrade gracefully when `--with-images` or `--with-counters` was not used: it says "no images in this
bundle" rather than inventing a visual section.

### 1.2 Pass reconstruction

Passes are *derived*, never assumed, and each boundary states why it is a boundary:

| Boundary because | Evidence |
|---|---|
| a marker begins/ends | the action list's `BeginEvent`/`EndEvent` pairs (marker path, nested) |
| the render targets change | RT set + depth target per event (ids, formats) |
| the command list changes | `ExecuteCommandLists` groupings in the action list, plus the eid order |
| a compute dispatch follows draws (or the reverse) | call kind per event |
| `BeginRenderPass`/`EndRenderPass` | the call itself, where the capture has it (D3D12 render passes) |

Nested markers become a path (`Frame/Shadow/Split0`), which is what the report names passes by — *not* by eid or
index, so the same names survive a re-capture. Draws outside any marker are reported as such (`<unmarked>`,
counted), because the summary cannot attribute them and must say so.

### 1.3 Per-pass roll-up

Per pass: first/last eid · draws, dispatches, vertex/index counts fed in, primitives out (topology-aware) ·
RT set with ids, formats and dimensions · depth target and its clear state · the first clear/copy events that
touch those targets · blend, depth-test and raster state in one line each · shaders per stage (id, entry point,
container hash, and the *names* of the constant blocks they read, from the reflection) · the key named values for
blocks the engine schema recognises (§1.7) · resources first touched by this pass · cost (counters if the bundle
has them, else blank) · the inferred *purpose* (shadow map, depth prepass, G-buffer, base pass, lighting,
post-process, UI — inferred from state like "depth-only, no colour target" or "full-screen triangle, one texture
in, no depth", and always labelled inferred).

### 1.4 Detectors — the red flags

Each detector states what it means, what proves it, and how certain it is. A row that has never fired on a
labelled capture is marked *unproven* (§7) rather than quietly shipped; heuristics that key off names carry
`[heuristic]` in their output. This is the list as designed (subject to being narrowed by evidence):

| Detector | What it means | Evidence | Certainty |
|---|---|---|---|
| Debug/validation messages ≥ warning | the API's own complaint, with the eid it happened at | `debug` bundle, grouped by message hash with counts and first/last eid | certain |
| Nothing bound where the reflection expects something | `rpN`/table slot the shader reads has a null descriptor | root parameters + reflection | certain |
| A CBV reads as all zeros | the uniforms the shader reads were never filled in, or the wrong buffer is bound | bundle cbuffer values (non-zero-majority check) | certain |
| Read before write | a texture/UAV read in a pass that no earlier pass wrote | usage chain from the bundle + descriptor writes offline | medium: legitimate for persistent resources — reported as a question, not a verdict |
| Write never read | an RT/UAV written and never read afterwards (and not presented) | usage chain + the final `Present` | medium |
| Allocation never used | a resource created and never referenced by any draw | resource table + usage | certain |
| VS out ≠ PS in | the vertex shader emits a semantic the pixel shader reads with a different index/width, or does not emit it at all | both reflections | certain |
| Binding kind mismatch | an SRV bound where the reflection wants a CBV/UAV, or register/space disagreement | root parameters + reflection | certain |
| Depth logic | depth write on with depth test off (or a depth test with no depth buffer bound) | pipeline state | certain |
| Empty scissor / degenerate viewport | draws that can only produce nothing | viewport/scissor state | certain |
| Mismatched MSAA | samples > 1 with no resolve before present, or a resolve of the wrong subresource | texture descriptions + `ResolveSubresource` events | certain |
| Load instead of clear | a pass whose first draw reads an RT that no clear and no earlier pass wrote | clear/copy events + usage | medium |
| Format/units suspicion | a float/HDR shader output written to an 8-bit `_UNORM` target, or sRGB/linear mismatch between write and read | RT format + PS output signature + the RT's later sampling | `[heuristic]` |
| Blend in an opaque pass | blending enabled where the pass name says base/GBuffer/depth | state + marker names | `[heuristic]` |
| Zero work | draws with 0 vertices/instances, dispatches with 0 groups, indexed draws with too few indices | call arguments + vertex stream sizes | certain |
| Marker imbalance | `EndEvent` without `BeginEvent`, or a pass that never closes | action list | certain |
| Unattributed draws | draws outside any marker (a hygiene note, not a bug) | action list | certain |
| Stencil without a writer | stencil test enabled where nothing wrote stencil in the frame | state + earlier passes | medium |
| Dead compute | a dispatch whose UAV output nothing reads | usage chain | medium |
| Peak vs total memory | what the frame holds, what it never reads, and what could alias (§5, the memory and aliasing report) | resource table + lifetimes | advisory |

### 1.5 Notable passes and notable resources

Notability is *stated as a rule*, so a reader can disagree with the ranking: passes are ranked by primitives,
draw count, RT footprint, resource churn, and counter cost when available; also listed whenever they are odd —
depth-only (shadow), single-draw, unmarked, dead (wrote and read by nobody), or the only pass touching a
resource. Resources are ranked by bytes, by how many passes read them, and by "never read at all"; the list also
carries the largest RTs, the formats that need special handling (float/HDR, compressed), and the ones the report
could not decode (say so, do not skip silently).

### 1.6 The report's shape

Header and provenance (capture hash, API, driver, RenderDoc version, bundle manifest, what was and was not
analysed) · the frame at a glance · the pipeline map (ordered pass list + a Mermaid graph of pass → RT edges) ·
pass by pass · notable passes · notable resources · red flags, grouped by severity · recommendations, ranked,
each with the command that reproduces it · **what this report cannot tell you** (undecoded formats, missing
reflection, unresolved bindless descriptors, no counters, no shader debug info, a stale or partial bundle, and
the fact that the frame was replayed on this machine's GPU rather than the device that recorded it) · the
appendix of reproduction commands.

### 1.7 The engine schema table

Names like `MobileBasePass`, `IndirectLightingCache` and `Material` are Unreal's, and the summary can only
speak that vocabulary if it is written down. A small, extendable table (`schemas/*.json`) maps a known engine's
constant-block, semantic and marker names onto concepts (`base pass`, `GI cache`, `light`, `material`,
`shadow pass`). Everything derived from it is labelled as name-based; a capture from an unknown engine simply
gets no interpretation rather than a wrong one. This is also where the project's original question finally gets
an answer in one place: the mobile-vs-PC GI investigation is the acceptance case for §1 as a whole (§1.8).

### 1.8 Acceptance gates

* Runs on all three captures in this project, in seconds once the bundle exists, with no unhandled exception and
  no warning on stderr.
* **Golden and deterministic**: a fixed bundle yields a byte-identical `report.md` (§7), and the JSON twin is
  schema-valid.
* **Testable without a GPU**: fixture bundles in `tests/` cover every detector and every report section; the
  suite grows the way the offline tool's does (`selftest`), and every detector added later lands with a fixture
  that fires it.
* **No claim without evidence**: a test walks the report's rows and fails if any claim is missing its eid/resId
  (the same rule the tool applies to itself — never invent a name, never print a bare index where a name is
  known).
* **Honest coverage**: every detector row is either demonstrated on a labelled capture in the corpus (§7, the
  capture corpus) or marked *unproven* in the report itself.
* **The pilot**: the summary explains the mobile-vs-PC GI difference in the words of the schema table — which
  pass, which cbuffer, which value — and a reader can follow its appendix commands and see the same thing.

**Effort.** ~10 days phased, now that the bundle it reads is landed: pass reconstruction and roll-ups (2 d),
detectors (2–3 d, the value is in getting them *right* rather than numerous), report + fixtures + gates (2 d),
schema table and the pilot (2 d).

**Blockers.** Bundle size on the 1.4 GB capture (mitigated by `--since`/`--until`/`--max-events` and by storing
full state only for distinct PSOs plus pass boundaries, README §9); counters are hardware/driver dependent and slow;
some formats cannot be decoded; shader debug info is usually absent from captured shaders; and a capture is
replayed on *this* machine's GPU, so device-specific behaviour is out of reach (§8, remote replay).

---

## 2. P1 — Replay driver: finding your way around a frame

* **Engine event ids in the driver's own output** — today `draws` numbers the structured file's command-list
  chunks, which is *not* the engine's event id: on the hobby capture the first draw is chunk 316 while the first
  event with pipeline state is 842, because RenderDoc numbers only what a command list recorded. `probe` lists
  the ids that really have state, so an id can be checked, but a draw number from `draws` should work everywhere:
  either derive the action list (no exported accessor exists) or calibrate once per capture and print both
  numbers side by side. (~4 h)
* **`--repl` (and `--stdin`)** — keep the capture open and take commands from the terminal or a pipe, so the
  3–10 s device setup is paid once and exploration becomes interactive instead of a sequence of processes. The
  building blocks exist (`batch` already runs a command list against one open capture); the work is prompt/loop
  plumbing, per-command error recovery, and not leaking the controller between commands. (~2–4 h)
* **`find <substring>` and `--at-marker <path>`** — find the events whose call name, marker path or resource
  name matches, and let every command take a marker path instead of an eid. Marker paths survive re-captures
  where eids do not, so this is also what the summary's appendix and any stored expectations should use. (~3 h)
* **`statediff <eidA> <eidB>`** — field-by-field diff of two events' state (RT set, depth, blend, raster, root
  parameters, shaders, viewport), printed as one changed-field-per-line list. "What changed between draw 40 and
  draw 41" is otherwise a manual read of two `state` dumps. (~4 h)
* **`buffer <resId> [offset] [len] [--as u32|f32|hex|ascii]`** — read a buffer's contents at the current event
  (`GetBufferData`), hexdump or typed rows, `--json` for scripts. The offline tool can print a buffer's *size*
  and *name*; only replay can show what is in it. Pairs with the resource table to answer "what is actually in
  that 4 MB uniform buffer". (~3 h)
* **`watch <name>`** — given a reflection member name (`Light.intensity`, `Material.Opacity`), print its value at
  every event of the frame as a small table, so a uniform that is right at one draw and wrong at the next is one
  command instead of forty `cb` calls. Slow by nature (a cbuffer read per event), so it takes a range. (~4–6 h)
* **`debug --group [--fail-on <severity>]`** — group messages by type/hash with counts and first/last eid, and
  exit non-zero when anything at or above a severity appears: the driver-side sanity gate, and the source of the
  summary's red-flag section. (~3 h)
* **`schema` and a published JSON contract** — print a JSON Schema for each command's `--json` output, stamp a
  `schemaVersion` into the documents, and validate the driver's own output against it in the harness. `--json`
  changed shape once already (flat stage members → a `stages` array, because repeats silently dropped data);
  consumers need a contract rather than reverse-engineering. (~4 h)
* **The driver as a library** — a thin C ABI over the same code, called from Python with `ctypes`, so the offline
  tool can query a frame in-process instead of one process per question. It makes §1's bundle an optimisation
  rather than a requirement ("ask the engine for just what the summary needs"), and removes the 28-process
  baseline pass. Cost: a real ABI (handles, error returns, no C++ types crossing), which is why the bundle comes
  first. (~2–3 d)
* **`sweep <dir> [--out <root>]`** — run a command set (or a `dump`) over every `.rdc` in a folder, writing one
  bundle per capture with a combined index: what makes a corpus (§7, the capture corpus) usable rather than
  heroic. (~4 h)

## 3. P1 — Replay driver: experiments on the frame (the "what if" tools)

* **Shader patching and differential replay** — `BuildTargetShader` compiles an edited shader (HLSL or the
  capture's own assembly), `ReplaceResource` substitutes it for the original, `Replay()` re-runs the draw, and
  `CreateOutput` gives the image. That turns "is this branch the problem" into an experiment: force the return
  value, disable a feature, substitute a flat texture — and diff the two renders (§4, render-target contact
  sheets, is where the comparison lives). This is the
  single highest-value item in this section: it answers questions about *that draw* without re-capturing the
  application. (~2–3 d)
* **Pixel history** — `PixelHistory(texture, x, y, subresource, typeCast)` returns one `PixelModification` per
  event that touched that pixel, with the reasons it was rejected (`depthTestFailed`, `stencilTestFailed`,
  `scissorClipped`, `viewClipped`, `shaderDiscarded`, `backfaceCulled`, `depthBoundsFailed`, `sampleMasked`) and
  the values before and after (`preMod`, `shaderOut`, `postMod`). "Why is this pixel this colour / why is it not
  drawn" stops being a guess. Gate it on `APIProperties.pixelHistory` and say plainly when the capture does not
  support it. (~1–2 d)
* **Shader debugging** — `DebugPixel(x, y, inputs)`, `DebugVertex(vertid, instid, idx, view)`,
  `DebugThread(group, thread)` and `DebugMeshThread(...)` return a `ShaderDebugTrace`; `ContinueDebug(debugger)`
  steps it and returns `ShaderDebugState`s; `FreeTrace` releases it. With a trace, print the inputs, the
  per-step variables and the outputs of one invocation. Only works for shaders built with debug info
  (`-Zi -Od`), which most captures do not have — the item is to *say that clearly* rather than fail obscurely,
  and to document how to re-capture with it. (~2 d)
* **Cross-checks between the reflections and the state** — cheap, deterministic, and they catch real bugs: VS
  output signature vs PS input signature (same semantics/index/width), each stage's expected bindings vs what
  the root signature and root parameters actually bind (kind, register, space), and the RT formats vs the PS
  output signature. Output as a checklist with the eid it applies to, so §1 can consume it directly. (~1 d)
* **Overlays as images** — `TextureDisplay` renders one texture; the overlay enum (`DebugOverlay`: `Drawcall`,
  `Wireframe`, `Depth`, `Stencil`, `BackfaceCull`, `ViewportScissor`, and the triangle-size / quad-overdraw
  overlays) annotates the targets themselves. A `--overlay wireframe|quad` switch on `image` gives the classic
  pictures for free — wireframe for topology, quad overdraw for a fragment-cost hunch. (~half a day once the
  display path is shared, which it nearly is)

## 4. P1/P2 — Replay driver: the frame's pictures, counters and geometry

* **Render-target contact sheets** — `image --every-pass <outdir>` (and `--every N`) writes one PNG per pass
  boundary plus a montage and an index Markdown; `image --diff <a.bmp> <b.bmp> --out <heat.png>` compares two
  renders (absolute difference plus a perceptual hash, so "did this actually change the picture" is one number).
  This is the visual half of the summary and the payoff for §3's experiments. (~1 d)
* **Per-pass counters** — `FetchCounters(counters)` returns results per event (`CounterResult.eventId`); there is
  no event-range parameter, so per-pass means folding the per-event list client-side between a pass's first and
  last eid. New command `counters --per-pass [--passes <path>]`, printing a table and the top-N cost passes, for
  the summary's performance section. Availability is hardware and driver specific (and the API says which
  counters exist), so the item is also to report "not available here" honestly rather than print zeros. (~1 d)
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

## 5. P1/P2 — Offline analysis: what the file knows that the frame does not

* **Dependency graph from the chunk stream** — who wrote what and who read it, from the descriptor writes and
  draw state the offline parser already decodes: a `deps` command emitting DOT/Mermaid plus a table, with
  read-before-write and write-never-read flagged. This is the evidence behind half of §1's detectors, and it is
  offline work because it is a fold over the stream — no device needed. (~1–2 d)
* **Memory and aliasing report** — resource lifetimes (creation, first/last use, destruction, `AliasingBarrier`
  pairs) and the placement/committed type from the creation payloads, then "these two could share memory, saving
  N MB" and "these N MB are never read". Sub-allocated UE page buffers complicate this (a CBV into a page is
  named after the page — README §4.9), so it reports what it can prove and says what it cannot. (~1 d)
* **CSV and Markdown tables** — `--format table|csv|markdown` on `draws`, `resources`, `descriptors`, `rootsig`,
  `summary`, so results can be pasted into an issue or opened in a spreadsheet. (The replay driver has `--json`;
  the offline tool is the cheap, device-free half.) (~2 h)
* **Structural capture diff** — `diff <a.rdc> <b.rdc>` for the file's own view: same draw index or marker path,
  what changed in PSO/CBVs/streams/descriptors. It can say *"rp2 was a compute CBV at b0, now a vertex CBV at b1;
  `SkyViewLut` left the table"* because `draws` now carries the command-list state, resource names, descriptor
  resolution and the `rpN` annotations. The mobile-vs-PC question is the reason this exists. (~1 d)
* **Root-signature vs chunk-stream cross-check** — verify that every root parameter the signature declares has the
  descriptor writes the stream shows, and that what replay reports at an eid agrees with what the file recorded.
  Two independent paths to the same fact is exactly what this project uses instead of trust. (~1 d)
* **VRAM budget and what-if** — total by category (RTs, textures, buffers, heaps), the peak of the widest pass,
  and "at half resolution" / "with this MRT dropped" arithmetic for a first-pass optimisation pass. (half a day)
* **Bundled chunk-name table** — embed the D3D12/SystemChunk enums (generated from `<root>/rdc-tools/renderdoc-src/`)
  so the tool still prints readable names when the `renderdoc-src` copy is absent or when analysing captures made
  by a different RenderDoc version. Keep `renderdoc-src` as the preferred source, the table as fallback. (~2 h)

## 6. P2 — A/B: two captures, one answer

* **`replaydiff <a.rdc> <b.rdc>`** — replay both, align by marker path (falling back to call order and the
  resource names present in both), then diff effective state, named cbuffer values, shader hashes, pass structure
  and — with `--with-images` — the renders of the passes with a matching name. The report is a table: which
  passes exist in one and not the other, which values moved, which images changed by more than a threshold. The
  offline `diff` (§5, the structural capture diff) compares the *file*; this compares what the *engine saw*,
  including values that come from
  memory rather than the stream. (~2–3 d)
* **Pass-list diff by path** — the cheap half of the same idea, offline: the marker trees of two captures side by
  side, with passes added/removed/renamed and eid ranges shifted, because pass names survive a re-capture and
  indices do not. (~4 h)
* **Image comparison** — absolute difference, mean/max delta, a perceptual hash and a heat-map PNG, so an A/B
  pair can be judged in one glance (and so a *regression* test can be "this render did not change"). (~half a day)
* **Golden A/B** — store a bundle per capture in the corpus (§7, the capture corpus) and make "the same capture,
  two builds of the tools" a diffable artefact: the regression net for the analy**ser**, where the goldens above
  are the net for the parsers. (~4 h)

## 7. P2 — Verification, regression and the bug atlas

* **Golden output files for both tools** — snapshot `summary`/`draws`/`rootsig`/`dxbc` output per capture (offline)
  and the driver's `state`/`shaders`/`cb` text output per eid, and diff on every run: a decoder or writer change
  then shows up as a reviewable diff instead of silent drift. The driver half already exists in the build folder
  (`run_baseline.ps1`/`diff_baseline.ps1`, 14 commands × text+JSON); it needs checking-in as expectations, with
  the JSON validated separately (a parse plus a duplicate-key check — a plain parse hides repeated keys, which is
  how a dropped vertex-shader block went unnoticed). (~half a day)
* **Fixture bundles** — small, hand-written bundles in `tests/` that exercise every summary detector and report
  section without a capture, a GPU or the driver. This is what makes §1 testable in CI and what keeps its
  heuristics from being unfalsifiable. (~1 d, and it grows with every detector)
* **Schema validation everywhere** — validate the driver's `--json` output and the bundle's files against the
  published schemas (§2), in the harness and in the offline tests. (~4 h)
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
* **A driver `selftest`** — DLL load, version, entry points, help text, and the writer's helpers (escaping,
  separator, `last`) as hermetic unit tests in the same spirit as the offline suite; plus `--json` output checked
  against the schema. (~half a day)

## 8. P2/P3 — Beyond the local desktop

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
* **D3D12 harness** (§1's predecessor, kept for completeness) — see §8.5 below.
* **Upstream** — the chunk-level findings (event ids vs chunk indices, what `InitialContents` really holds, the
  crash-handle server, the `TextureSave`/`GetTextureData` subresource split) are the kind of thing RenderDoc's
  own docs and tools benefit from. Not a work item, a standing intention: file them as they are confirmed.

### 8.5 The D3D12 harness (only for *synthetic inputs*)

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

**Verdict.** The replay driver is built (README §9), so the ✔ column above is no longer a plan — it is what
`replay_dump` does today, and §4.1's shader patching widens it. A harness is worth writing only for the ✖
column: running a shader with hand-built constants — e.g. feed the mobile base-pass pixel shader the HISM's baked
SH to prove the shader path in isolation — and even that can often be avoided by patching the shader in replay
instead.

**Sketch.** One `ID3D12Device` + a compute-style or full-screen-triangle PSO + a root signature matching the
shader's bind points (the offline tool can now print that layout: `rootsig`, README §4.1); upload a 256-byte
constant buffer; dispatch/draw to a small RTV; read back with `ReadBackResource`. Inputs: the `.dxil` files from
`dump-shaders` and a JSON of uniform values (which the replay driver can export directly).

**Blockers.** Needs the shader's exact root signature layout (available from the capture — `rootsig` — or the
replay driver) and DXIL compilation to a PSO — `dxc` is available with the UE install.

**Effort.** ~2–3 days for a single-purpose harness; scope it to one shader at a time.

## 9. P3 — Robustness and scope

* **Zstd without the dependency** — either vendor a decoder or fail with a clear message (today it needs
  `pip install zstandard`). (~4 h)
* **Memory-mapped stream access** — avoid holding ~1.5 GB in RAM for the largest captures. (~4 h)
* **Non-D3D12 driver names** — `load_chunk_names(driver=...)` already takes a driver; expose it on the CLI.
  (~1 h)
* **Format coverage in the offline view** — the same audit §4's format coverage does for the engine's textures,
  for the file's payloads: what was decoded, what was skipped, and why. (~2 h)

## 10. Clear README §8 ("Pitfalls and known limitations")

One entry per bullet in README §8 that is *not* left to replay (see the note at the top), with the change that
closes it and the gate that proves it is closed.

**"Resolved" means** the bullet is deleted from README §8, the `CHARACTERIZATION` tests that pinned the old
behaviour are replaced by tests of the *correct* behaviour, and every doc that described the limitation is
updated in the same change (AGENTS.md: behaviour is the contract, so these are deliberate, tested fixes —
never silent ones).

| # | README §8 bullet | Plan | Priority | Effort |
|---|---|---|---|---|
| 10.1 | Chunk names need `renderdoc-src` | §5 bundled chunk-name table | P2 | ~2 h |

### Acceptance gates

* **10.1** (§5 bundled table): names resolve with `renderdoc-src` absent **and** when the capture's version is
  newer than the tree; the table is generated by a checked-in script and carries its RenderDoc version; the
  §1.1 warning becomes "using bundled names for RenderDoc X".

---

## 11. Suggested order

Phased, and each phase stands on its own — nothing here is blocked on something later in the list.

**Phase 1 — the frame-level answer (its input already exists: `dump` writes the bundle, README §9)**
1. **`report` skeleton (§1.1–1.3)** — bundle in, pass structure and per-pass roll-ups out, deterministic
   Markdown, fixture-tested.
2. **`--json` schema (§2) + the driver `selftest` (§7)** — the contract the bundle and every later feature
   depends on, while the ink is still wet.
3. **Detectors, ranked by evidence (§1.4)** — start with the certain ones (unbound descriptors, zero work,
   mismatch checks) and only then the heuristics; each lands with a fixture.
4. **The engine schema table and the pilot (§1.7–1.8)** — the mobile-vs-PC GI question answered in the report's
   own words is the acceptance case for all of the above.

**Phase 2 — exploration and experiments**
5. **`--repl`, `find`/`--at-marker`, `statediff`, `buffer` (§2)** — the cheap commands that make a frame
   navigable; ~2 days for all four.
6. **Shader patching + differential replay, and RT contact sheets (§3, §4)** — the "what if" pair, and the
   honest way to answer "what does this branch contribute".
7. **Pixel history (§3)** — "why is this pixel this colour", gated on the capture supporting it.
8. **Cross-checks + per-pass counters (§3, §4)** — the deterministic bugs and the cost column.
9. **Dependency graph, memory/aliasing report (§5)** — the evidence behind the remaining detectors.

**Phase 3 — comparisons and the long tail**
10. **A/B: `replaydiff`, pass-list diff, image comparison (§6)** — the mobile-vs-PC workflow done properly.
11. **Golden outputs and the corpus (§7)** — the regression net under everything above.
12. **Bundled chunk names (§5, = §10.1)** — ~2 h, removes the last environment dependency and closes the last
    README §8 bullet that is not replay's job.
13. **Remote replay (§8)** — the honest fix for the desktop-GPU caveat, when a device is available.
14. **The D3D12 harness (§8.5)** — only when a shader must be run with inputs the capture does not contain.
