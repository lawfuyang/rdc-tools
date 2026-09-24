# rdc-tools — expansion roadmap / TODO

Features that are **not implemented yet** in `rdc_analysis.py` or in its replay driver (`src/cpp/replay_dump.cpp`,
REFERENCE §9), in rough priority order. Each item says what it is, why it is wanted, how it would be built, and
what blocks it.

Landed items are **removed** from this file rather than marked done: their reference moves to `README.md`,
`REFERENCE.md` or the code, and the remaining sections are renumbered with every cross-reference updated in the
same change.

**Scope: one machine (2026-09-22).** This file tracks work that a `.rdc` on this machine, the offline Python
and the driver exe can do *here* — no replay server, no capture from another API, no DLL inside the captured
application, no program of our own feeding the shaders. Those are not defects of this tool, and five items left
the file on those grounds alone; each is named with its reason in "what is deliberately *not* on this list"
below, so the reasoning survives the deletion.

**Phase 1 landed on 2026-09-23 and left this file**: the five questions a person actually asks — a pass's cost,
a target's statistics, a comparison over those costs, the frame's permutations, and what a dispatch writes —
are in `README.md` (the command list) and `REFERENCE.md` (§9 for the driver commands, §4.23 for the offline
ones and the documents they read).

**Phase 2 was written on 2026-09-24, out of a survey of what the other tools do** — Microsoft PIX, NVIDIA
Nsight Graphics (its feature list, Aftermath, and the shader-debugger/pixel-history pair), Sony's Razor where
anything is public at all, and the Khronos Vulkan tutorial's AI-assisted debugging chapter —
`docs.vulkan.org/tutorial/latest/AI_Assisted_Vulkan/06_debugging/03_renderdoc_ai_integration.html` is the page
of it that is reachable; its siblings answer 403/404 — each checked against what this tool already answers.
What came out of
it is §1–§3 below: six items on the *debugging* side rather than the performance side, each one an answer to a
question a person asks while chasing a bug or a crash.

The survey also cut three candidates, and the reasons are kept in "what is deliberately *not* on this list"
below, because a measurement that removes an item is worth as much as one that adds it. Half of what the
survey suggested was already answerable here — `pixelhistory`, `mesh`, `usage`, `patch`, `debug`, `watch`,
`crosscheck`, `imgdiff`, `histogram` and `trace` cover pixel history, post-VS geometry, a resource's event
history, live shader editing, validation messages and single-stepping — so none of those appears as an item,
and the items below are the ones the tools have that this one does not.

Legend: **P0** = do next / unblocks current work · **P1** = high value, moderate effort · **P2** = useful,
opportunistic · **P3** = nice-to-have.

### The two tools and the line between them

`rdc_analysis.py` reads the **file**: the container, the chunk stream, payload layouts, resource tables,
descriptor writes. `replay_dump.exe` asks the **engine**: names, values, decoded textures, geometry, the
rendered image (REFERENCE §9). Anything that spans the two is written here as a **pipeline** item, and the split
inside it follows the same line — the driver *extracts* what only the engine knows, the offline tool
*analyses and presents* it, because that half must be testable without a GPU, a capture or a driver (REFERENCE §4.6).

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
anyway: `resources` prints the DXGI format and dimensions of every texture, and `formats` now audits them).

The same rule was applied to what was already **built**: `float` and `pattern` (searching the raw stream for a
uniform's *value*), `sig` (decoding vertex signatures), `rootconst` (dumping root-constant values) and `report`
(an inventory of UE shader/policy strings) were removed, together with the `dxbc`/`dump-shaders` harvest that
picked GI-ish strings out of containers. Each one was a worse answer to a question `GetCBufferVariableContents`
or `ShaderReflection` answers exactly. What is left is deliberately offline: the container and the chunk stream,
`draws`/`resources`/`descriptors`/`rootsig`, the cache, `verify`, `formats`, and the structural search commands.

**The report generator was the one carve-out, and it did not break the rule.** It added no new extraction:
it consumes what `replay_dump` and the offline parser already produce, and puts analysis, ranking and
presentation on top (landed — REFERENCE §4.11). That is the shape any future analysis takes: offline code over
a bundle of engine answers, testable from fixtures and diffable between runs (REFERENCE §4.11), with its tests landed in the
same change.

**The five items that left on 2026-09-22, on scope and not on merit.** Each needs something this machine does
not have, and each keeps its reasoning in the clause that says so:

* **Remote replay** (was §1) — needs the device itself: RenderDoc's remote server plus `ReplayOptions`, so a
  mobile capture is replayed by the *mobile* driver (real counters, real driver behaviour, real mobile quirks).
  It remains the honest fix for the "recorded on a phone, replayed on a desktop GPU" caveat every report carries.
* **A capture-side annotation layer** (was §1) — needs an installed DLL inside the captured application, which
  is a capture-time tool rather than an analysis one.
* **Other APIs' names and payloads** (was §1) — naming them is done (`--driver`), and reading them needs a
  capture that is not D3D12 in hand; the analysis is where the D3D12 assumptions live, so this is a project with
  a file this repository does not have.
* **The D3D12 harness** (was §1.5) — needs a compiled program of our own: it exists for the one question replay
  cannot answer (what a shader does with inputs the capture does not contain), so no item here is "free with a
  harness", and the harness itself is not a `.rdc` + script it is a second program to build and maintain.
* **Upstream** (was §1) — filing the chunk-level findings with RenderDoc is a conversation with another
  project, not a command.

Any of them can come back the day the thing it needs is here; each was deleted rather than left as a stub, so
nothing below is waiting on something this machine cannot do.

**The three candidates the 2026-09-24 debugging survey killed, each by a measurement rather than by taste.**
They were looked at, and the reasons they are not items are worth more than the items would have been:

* **Reading a capture's own debug-layer log.** There is no such log to read: the `d3d12sdklayers` and
  `d3d12core` sections are copies of the SDK's own DLLs — `d3d12_device.cpp` reads `d3d12sdklayers.dll` and
  stores it, "which will be needed for debug on replay" — not the messages the debug layer printed. So
  `debug`'s measured **0 messages** on `desktop-1` is the true answer about that capture rather than a gap in
  the tool, and the sentence above about the non-frame sections holds for a better reason than the one it
  gave: the API surfaces nothing from them because there is nothing of that kind in them.
* **DRED / Aftermath-style GPU crash dumps.** A `.rdc` cannot carry one. DRED's settings must be configured
  before the device is created and its data — auto-breadcrumbs, page-fault information — is read from the
  *removed* device inside the faulting process, and the replay API exposes none of it (`DRED` appears nowhere
  in `api/replay` in 1.46). A capture is a healthy frame; the crash that follows it belongs to another tool in
  another process, and no analysis of this file recovers it. What this tool can do about crashes is the
  arithmetic of §1–§2 — catching the defects that cause them — plus the messages `debug` prints when a capture
  has any.
* **Asking the engine to replay with the debug layer on, so that a capture whose application ran clean gets
  validation anyway.** The engine keeps the SDK's DLL in the capture precisely for debugging at replay time,
  but nothing in the public replay API asks for the layer to be enabled — that is a request to RenderDoc
  rather than a command to write here. If a future engine version exposes it, the item comes back as one flag
  on `debug`.

Current state for reference: the offline tool parses the `.rdc` container, decompresses the frame-capture stream
(LZ4 through the built `bin/rdc_lz4.dll` — and a Zstd section without the optional decoder refuses with the fix
in the message rather than a traceback — and caches it on disk so
repeat
commands are instant (REFERENCE §4.8; a cache *hit* is an `mmap`, and a cold run is served from the map it just
wrote rather than holding the 1.5 GB stream on the heap), walks the
SDChunk stream, names chunks for the capture's driver (`--driver`/`$RDC_DRIVER`, D3D12 by default), decodes the
main D3D12 draw/pipeline/CBV/vertex-buffer payloads, the barriers, the render-target
bindings, the clears, the discards, the copies and the resolves, plus the view format each descriptor write
declares (the use ledger behind `deps` and `memory`, REFERENCE §4.15),
the resource table
(id → kind/size/name, REFERENCE §4.9), the descriptor heaps (REFERENCE §4.10) and the root signatures (REFERENCE §3.4), inventories the
DXBC/DXIL containers and joins them to the pipelines that bind them, by any of a container's three
identities (`psos`, REFERENCE §4.22), audits the formats it finds (`formats`, the offline half of the
driver's audit), and can
check its own parse (`verify`). The replay driver (REFERENCE §9) is the other
half: it asks the engine what no file read can answer — names, values, decoded textures, geometry, the
rendered image, what each call asked for (`volume`: a draw's vertices/instances/triangles, a dispatch's
groups and threads, from the engine's action list), the cross-checks between a shader's reflection and the
state it is given (`crosscheck`), the per-pass counter fold (`counters --per-pass`), the picture commands
with their subresources and overlays (`image`, `textures --save`, `cubemap`, `sheet`), the format coverage
audit (`formats`), one shader invocation stepped from its inputs through every step to its outputs (`trace`),
and the bundle the report generator reads (`dump` +
`bundle-verify`). The offline tool has a
hermetic unittest suite (`python src\py\rdc_analysis.py selftest`) and is clean under Pyright
"Standard" (`npx --yes pyright@latest`); the driver has a build-and-baseline harness in the (gitignored)
`build/` folder. `AGENTS.md` holds the coding rules, REFERENCE §4.6/§4.7 how to run both. That suite is the safety
net for everything below — land the tests with the change, not after it.

### Environment convention — `renderdoc-src` in the root folder

The tool refers to the **real implementation of RenderDoc** for anything that must match the capture's own
version — currently the chunk-name and `DXGI_FORMAT` enums, which are parsed out of the RenderDoc source at
runtime. It looks for a `renderdoc-src` folder **in the root folder**, i.e. beside the tool:

```
<root>/rdc-tools/src/py/rdc_chunkmap.py             the module that reads the enums
<root>/rdc-tools/src/py/rdc_chunknames.py           the bundled table it falls back to (generated)
<root>/rdc-tools/README.md
<root>/rdc-tools/ROADMAP.md
<root>/rdc-tools/renderdoc-src/                     <- a copy of the RenderDoc source tree belongs here
    renderdoc/core/core.h                               SystemChunk enum
    renderdoc/driver/d3d12/d3d12_common.h               D3D12Chunk enum
    renderdoc/common/dds_readwrite.cpp                  DXGI_FORMAT
```

The search walks *up* from that module, so the tree can also sit beside it or at any folder above — the
convention is the repository root, which is two levels up.

**Having a tree is preferred, not required** (it was required until the bundled table landed). Get it with:

```powershell
cd 'C:\Workspace WIth Spaces\rdc-tools'
git clone --depth 1 --branch v1.46 https://github.com/baldurk/renderdoc.git renderdoc-src
```

Match the version that produced the captures (RenderDoc **1.46** in this project — the bundled table is 1.46
too). Resolution order is `$RENDERDOC_SRC` → `<tool folder>/renderdoc-src` → `<parent>/renderdoc-src` → the
historical absolute default; when none of them exists the tool prints one warning naming the version the
bundled names are from, and a chunk the table does not have prints as a number. Details in README §1.1.

This tree is also the source of truth for every "how is this serialised?" question, so every item below assumes
it is present and kept at the version matching the captures being analysed. Any item that reads from it should
say so explicitly, and should degrade gracefully when it is missing.

---

## 1. P1 — the frame's own hazards

The two items that answer "why is this wrong" rather than "why is this slow", and both are **offline**: they
are arithmetic over what the parser already decodes, so they run with no GPU, no driver and no replay, and they
work on a capture whose application ran without the debug layer — which is every capture here, as
"what is deliberately *not* on this list" below now records with numbers.

* **`hazards` — the state and binding conflicts the frame commits, from its own arithmetic.** *What*: for
  every use the ledger already records, the state the resource was last transitioned into checked against what
  that use requires (a read of a resource left in a `*_WRITE` or `COPY_DEST` state, a write with no transition
  in front of it, a resource used as a depth target while it is already bound as one), plus the conflicts at a
  single event (a resource bound as RTV/DSV at an event and reachable as SRV/UAV through a table at the same
  event — the read-write loop). Each finding names the two eids it became true between and the chunk that set
  the state; the report gains a count and `sweep` prints it per capture, so a folder of frames can be ranked by
  how much it fights its own barriers. *Why*: this is the class that shows as "fine on my GPU, wrong on the
  phone", as an intermittent frame, or as a hang — and the engine can only report it when the application ran
  with the D3D12 debug layer at capture time. Measured on this corpus: `debug --group` reports **0 messages**
  on `desktop-1`, and its `d3d12sdklayers` section is not a log at all but a copy of `d3d12sdklayers.dll`
  (`d3d12_device.cpp`, stored so *replay* can load a debug layer). So nothing recorded the hazards for any
  capture here, and the file is the only witness. *How*: over the barrier decode, the RT/DS bindings, the
  clears/discards/copies/resolves and the use ledger (`deps`, REFERENCE §4.15), with the binding half joined
  the way `rootsig-check` already joins declared against bound. The permit table — which state each use
  requires — is written from the D3D12 documentation and cited in REFERENCE, and gets the treatment the
  report's other detectors get: a finding is an observation until a corpus `known` verdict confirms or refutes
  it (REFERENCE §4.17). *Blocks*: nothing; every input decodes today. **~2 d.**

* **`samplers` — every sampler the frame reads a texture through, and the mip range it can reach.** *What*:
  one row per (sampler, texture) pair actually bound at an event: where the sampler comes from (a static
  sampler in a root signature — `rootsig` counts them today, six per signature in `desktop-1`, and does not
  decode their values — or a heap slot), its filter, address mode and mip range, the texture's format and
  `mips=N` beside them (already in the resource table), and the mismatches that are *arithmetic*: `minLod` at
  or past the mip count (the level it asks for does not exist — sampling black), a `maxLod` that never reaches
  the base level, a bias that spends the whole range. *Why*: it is the Vulkan tutorial's own worked example, a
  black texture explained by `minLod = 10` on a five-level texture, and it is the same arithmetic in D3D12.
  Unlike "wrong address mode for this target", these are not heuristics: a level either exists or it does not.
  *How*: extend the root-signature decode past counting static samplers (the blob is parsed already;
  `D3D12_SAMPLER_DESC`'s layout comes from `renderdoc-src`, the source of truth for every "how is this
  serialised" question), decode sampler heap slots, and join to the textures bound through each table — the
  path `rootsig-check` walks. The first step is a measurement rather than code: `descriptors` prints **no
  sampler rows** for `desktop-1`, so settle whether that frame has none or the decode skips them.
  *Blocks*: nothing. **~1.5 d.**

## 2. P2 — the memory questions behind a wrong pixel

* **`alias` — the placed resources living in each other's memory.** *What*: from the placement rows the memory
  ledger already carries — `memory` reports **776 placed resources in 33 heaps** for `desktop-1` — plus the
  capture-relative lifetime, the pairs whose byte ranges overlap *while both are live*, and among those the
  pairs where one is written while the other is still read, with whether a transition between the two uses
  exists. Printed as `memory`'s own section and counted in the report. *Why*: aliased memory is a top cause of
  frames that are black, stale or garbage, of crashes that move when a debug print is added, and of "works on
  the desktop, wrong on the phone"; the ledger already knows every number needed, with one obstacle in the way.
  PIX's debug layer flags this class too — this is the part of that family reachable without it. *How*:
  arithmetic over the ledger; the obstacle is that the per-resource rows do not carry the heap offset yet —
  `memory` reports the placement *class* and the heap totals but not each resource's offset — so carrying it
  into the row is the first step and the overlap test the second. *Blocks*: nothing but that first step.
  **~1 d.**

* **`provenance` — where a resource's bytes came from, however many hops back.** *What*: for a resource and an
  eid, the chain of writers back through copies, resolves, clears and draws, ending at a clear, at the
  resource's creation, or at "no writer in the frame this capture covers" — printed as a path, and as a
  one-line verdict on the report's pass rows where `read-before-write` fires today. *Why*: `read-before-write`
  says *that* nothing wrote it, not *what* it was supposed to hold; `memory` already prints each resource's
  last write and its kind (`res2263[SceneDepthZ]: last write #15601 (dsv)`) and `deps`' own
  `read-before-write` list is the same single hop. The multi-hop walk is what answers "why is this buffer
  zero" and "why is this readback stale" — the question behind the detector. *How*: offline; the ledger gains
  a predecessor map over the writes it already records, and the walk terminates with the honesty the report
  uses elsewhere ("the writer is outside this frame"). *Blocks*: nothing. **~1.5 d.**

## 3. P3 — the smaller answers, and the two probes

* **`mesh --bounds` — is this instance's geometry even on screen.** *What*: the post-VS bounds the driver
  already extracts (`mesh` prints `boundsMin`/`boundsMax`, with the vertex and primitive counts) checked
  against the pass's target: wholly outside the viewport, wholly behind the near plane, `w <= 0` at every
  vertex — each said as a verdict in the report, beside the pass whose target it could not have written.
  *Why*: "the draw is in the frame and nothing appears" is the missing-mesh question, and this half of it is
  arithmetic, where the other half — the *application's* own frustum decision — cannot be read at all:
  `cullFlags` appears nowhere in the public replay API in 1.46, so the engine cannot be asked why it dropped
  the instance. *How*: the numbers come from the driver; the check belongs offline in the report, which knows
  the target size. The first step is a measurement: `desktop-1`'s `vsout` bounds for eid 2731 read
  `-1230.42 -729.432 10` to `-1194.85 -670.276 10`, which is plainly not NDC, so settle which space each
  stage reports before writing the comparison. *Blocks*: nothing. **~0.5 d.**

* **`callstack <eid>` — which line of the application issued this call.** *What*: `info` gains whether the
  capture carries callstacks (`HasCallstacks`, `renderdoc_replay.h`), and the command prints the stack for an
  event or an action. *Why*: it is the shortest path from a frame to a source line — "who issued this barrier
  or this copy" — and it is what turns "the state is wrong here" into a file to open. *How*: a small driver
  addition; the *dependency* is a capture recorded with callstacks on (`captureCallstacks`,
  `capture_options.h`), and none of the three in the corpus is — their sections end at `resolvedb`. So the
  item lands and is tested on its "this capture has none" path here, and becomes useful the first time a
  capture is recorded with the option. *Blocks*: one capture recorded with callstacks on — a capture-side
  choice rather than a new program, so it is not the D3D12-harness case. **~0.5 d + the capture.**

* **Why the pixel path of `trace` produces nothing.** Measured again on the re-pinned corpus: on both new
  captures every shader carries its own debug data (258 of 258 bindings, 196 of 196) and `--vertex 0` steps
  200 steps with source lines — while `--pixel 640,360` is refused at both pinned eids, with the engine's
  reason now in the text: `no trace for pixel 640,360 at eid 2731 (ps, res166972): sourceDebugInfo is 1 / its
  shader loading log says: Found debug data in the shader`. The debug data is there and it is the *invocation*
  that cannot run, because no fragment at that co-ordinate passed the depth test. That is a better answer than
  the one this probe was written against ("the engine says nothing about why", on a capture whose shaders had
  no debug data at all). What is left of it: whether *any* pixel on these captures traces — the engine's
  search is depth- and stencil-shaped, so a pixel behind or discarded geometry is the wrong candidate — and
  whether the message can name that search. *How*: `--pixel` at a co-ordinate a pass provably wrote (a
  readback's own non-zero pixels, via `histogram`), then read the refusal paths in `d3d12_shaderdebug.cpp`.
  **~0.5 d to a conclusion.**

* **Does a patched shader carry debug info?** — `patch` compiles a replacement shader locally
  (`BuildTargetShader`) and substitutes it for the capture's own. If that compile keeps its debug info, the
  debugger can step the *edited* shader in a capture whose own shaders were stripped of PDBs — which is every
  UE capture here (their shaders carry `ILDB`, which the engine steps through, but no `.pdb` beside them) —
  and would make `trace` usable where it is most wanted ("change the shader, then see what it computes").
  *How*: one run settles it — patch a shader in `mobile-1`, then `trace` the patched draw. Then either build
  the link between the two commands, or write the one paragraph in REFERENCE that says a replacement carries
  none. **~0.5 d.**

## 4. Suggested order

Phased, and each phase stands on its own — nothing here is blocked on something later in the list. Every work
item of §1–§3 appears exactly once, so this is the whole list in one place rather than a selection of it; each
item keeps its section's **P** label and its own effort figure, so this file stays the place to read what an
item *is*.

**First — the two audits (§1, ~3.5 d)**, which are the bug-hunters: `hazards` first, because it is what
catches a defect before it becomes a crash, it needs no new extraction, and its findings are the ones a corpus
verdict can confirm; `samplers` next, opening with a measurement rather than code.

1. **`hazards` (§1, ~2 d)** — the state and binding conflicts, offline, with the permit table cited.
2. **`samplers` (§1, ~1.5 d)** — the mip-range arithmetic, and the sampler decode it needs first.

**Then — the memory questions (§2, ~2.5 d)**, in this order because `alias` is shorter and its numbers are
already printed:

3. **`alias` (§2, ~1 d)** — carry the heap offset into the row, then the overlap test.
4. **`provenance` (§2, ~1.5 d)** — the multi-hop walk behind `read-before-write`.

**Then — the smaller answers and the probes (§3, ~2 d)**, where the bounds check is the one that pays per
line of code and each probe ends in a sentence either way:

5. **`mesh --bounds` (§3, ~0.5 d)** — settle the space first, then the verdict.
6. **`callstack <eid>` (§3, ~0.5 d)** — with `info` reporting availability; useful from the first capture
   that carries one.
7. **Why the pixel path of `trace` produces nothing (§3, ~0.5 d)** — fix the message, or document the
   engine's limit in the words of the measurement.
8. **Does a patched shader carry debug info? (§3, ~0.5 d)** — if it does, "change the shader, then step it"
   is the next feature; if not, it is one paragraph in REFERENCE.
