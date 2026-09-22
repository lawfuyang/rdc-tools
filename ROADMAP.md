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
below, so the reasoning survives the deletion. What is left is analysis, in three shapes: the **engine answer
that is not extracted yet** (the replay API has it, no command asks for it, and the report has been carrying a
caveat about its absence), the **offline rule whose input is not decoded yet**, and **presentation that puts
two of them side by side** where a reader has to look at a picture or run one command and then another.

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

Current state for reference: the offline tool parses the `.rdc` container, decompresses the frame-capture stream
(LZ4 through the built `bin/rdc_lz4.dll` — and a Zstd section without the optional decoder refuses with the fix
in the message rather than a traceback — and caches it on disk so
repeat
commands are instant (REFERENCE §4.8; a cache *hit* is an `mmap`, and a cold run is served from the map it just
wrote rather than holding the 1.5 GB stream on the heap), walks the
SDChunk stream, names chunks for the capture's driver (`--driver`/`$RDC_DRIVER`, D3D12 by default), decodes the
main D3D12 draw/pipeline/CBV/vertex-buffer payloads, the barriers, the render-target
bindings, the clears, the discards and the copies (the use ledger behind `deps` and `memory`, REFERENCE §4.15),
the resource table
(id → kind/size/name, REFERENCE §4.9), the descriptor heaps (REFERENCE §4.10) and the root signatures (REFERENCE §3.4), inventories the
DXBC/DXIL containers, audits the formats it finds (`formats`, the offline half of the driver's audit), and can
check its own parse (`verify`). The replay driver (REFERENCE §9) is the other
half: it asks the engine what no file read can answer — names, values, decoded textures, geometry, the
rendered image, the cross-checks between a shader's reflection and the state it is given (`crosscheck`), the
per-pass counter fold (`counters --per-pass`), the picture commands with their subresources and overlays
(`image`, `textures --save`, `cubemap`, `sheet`), the format coverage audit (`formats`), one shader
invocation stepped from its inputs through every step to its outputs (`trace`), and the bundle the report
generator reads (`dump` +
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

## 1. P1 — the extract the tool already knows how to do

Four items, each one an *existing* answer that is not being asked for: the API call is in the header, the
offline decode is a sibling of one already written, or the caveat that says it is missing names the reason
that has since expired. None of them is blocked on anything.

* **The work volume of every event, in a bundle** — how many vertices/instances/triangles a draw asked for and
  how many threads a dispatch started. *Why*: the report's own caveat says "a draw's vertex count is the first
  input of the rule and no bundle has it, because the replay API exposes no action list" — and that reason has
  expired: `draws` walks the action list today, and `ActionDescription` carries `numIndices`, `numInstances` and
  `dispatchDimension` (`control_types.h`, `data_types.h`). *How*: `dump` writes a volume row per event, and the
  report's pass table and call ranking — today a rank over *counts*, and honest about it — rank by geometry
  instead, which is the difference between "the pass with the most draw calls" and "the pass with the most
  triangles". *Blocks*: nothing. **~0.5 d.**

* **A picture's statistics: `histogram` and the min/max of a target** — `GetMinMax(textureId, sub, typeCast)`
  and `GetHistogram(textureId, sub, typeCast, minval, maxval, channels)` are in the replay API
  (`renderdoc_replay.h` 929/953) and no command calls either. *Why*: the report states "nothing here samples or
  decodes a texture or a render target: formats are named, pixels are not read", and the picture commands
  (`image`, `textures --save`, `cubemap`, `sheet`) make a person *look* at a PNG to answer "is this target
  black? blown out? clipping?" — three questions with exact numeric answers, and the first thing anyone checks
  when a frame renders wrong or a bloom looks flat. *How*: `--stats` on the picture commands, or a
  `histogram <rdc> <resId|eid> [--mip] [--slice] [--sample] [--cast]`, reusing the `Subresource`/`CompType`
  plumbing `textures --save` already has; min/max always, the buckets as bars in text and as numbers under
  `--json`, so a bundle can carry them and the report can rank by exposure. *Blocks*: nothing. **~1 d.**

* **The frame's time, per pass, in the report** — the counters folded into the pass table. *Why*: the caveat
  "counters are not folded into the pass sections… no pass roll-up prints them", while `counters --per-pass`
  folds them in the driver already: the number a reader wants first is "where does the frame's time go", and
  today that means running `counters` beside the report and joining two documents by hand. *How*: the driver's
  per-pass fold written into the bundle under `--with-counters`, and a report column for each pass's share and
  its dearest event — with the counter that *is* the cost named, as `counters` already does. *Blocks*: nothing.
  **~1 d.**

* **The corpus pins the commands landed since** — `goldens/` compares the driver on four commands per capture
  (`info`, `draws`, `textures`, `debug`), so everything landed after that — `state`, `shaders`, `cb`, `formats`,
  `trace`, `cubemap`, `sheet`, `counters`, `crosscheck`, `pixelhistory`, `patch` — would drift unnoticed: the
  corpus is the only gate that catches "this command prints something different now", and it does not look at
  them. *Why*: it is the safety net every change in this repository leans on, and it is cheapest to extend
  right after the commands exist. *How*: add the commands whose output is stable on the three local captures
  (and for `trace`: the refusal on a capture without debug data, and the 31-step trace on the HobbyRenderer
  one). *Blocks*: nothing. **~0.5 d.**

## 2. P2 — new analysis on top of them

* **`replaydiff` over counters** — "did my change help, and where". The project's own regression workflow is two
  bundles of the same capture (`replaydiff`, `rdc_filediff.py`), and it compares states, shaders and resources
  today; both bundles can carry counters (`--with-counters`), so the comparison can say which pass got cheaper
  and which got dearer instead of leaving a person to diff two `counters` runs by eye. *Blocks*: none, though
  the per-pass roll-up above makes it much more useful. **~0.5–1 d.**

* **What a dispatch writes** — the UAV side of the state. *Why*: "what a dispatch *does* write (its UAVs) is not
  in a bundle at all", which is why a compute pass has "targets and depth not applicable" and why the usage
  chain has no write side for compute — the half of a mobile frame that is usually the interesting half.
  *How*: `PipeState::GetReadWriteResources(stage)` exists and is unused (`GetConstantBlocks`,
  `GetReadOnlyResources` and `GetSamplers` are the other three; the driver resolves bindings through
  `GetDescriptorAccess` + `GetDescriptors` instead), so the read-write set is one call, or a filter of the
  access list it already walks — `DescriptorAccess` carries the stage and the slot but no direction, and the
  reflection's `readWriteResources` is what supplies it. Then the report can name a dispatch's targets and the
  ledger gains compute writes. **~1 d.**

* **The two offline gaps the report names, decoded** — the `ResolveSubresource` payload (so the MSAA rule can
  say *which* subresource was resolved, not that a resolve happened) and the sampling view's sRGB flag (so the
  sRGB half of the format rule stops being unchecked). *Why*: the caveat names both precisely, and both are
  more of the work the offline parser already does for the sibling payloads (barriers, render-target bindings,
  clears, discards, copies) and the descriptor heaps. *How*: offline only, fixtures and hermetic tests, no
  device — the two rules then report a verdict instead of "not looked at". **~0.5–1 d.**

* **A permutation table for the frame** — which shaders this frame actually uses: per stage, the hash, the
  entry point, how many events bind it, the first and last eid, and the reflection summary. *Why*: feature
  study on a UE capture is mostly "how many permutations does this frame really contain, and is the one I am
  editing even in it?" — the bundles carry `states/<eid>.shaders.json` with a `hash` per stage already, so the
  table is a dedup and a count, and `replaydiff` already compares two frames' shaders by that hash. *How*:
  offline over a bundle (state events), presented as a report section or a `permutations <bundle>` command;
  the raw `.rdc` side is `dxbc`'s container inventory. **~1 d.**

## 3. P3 — probes whose answer is not known yet

Both of these are one session long, and their value is in the answer rather than in the code: they decide
whether a bigger item exists at all.

* **Why the pixel path of `trace` produces nothing on the one debuggable capture here.** Measured: on the
  HobbyRenderer capture `--vertex 0` traces 31 steps with source lines, while `--pixel` at four co-ordinates on
  three of its draws produces no trace and the engine says nothing about why (REFERENCE §9). The command exists
  to answer "what did this pixel's shader compute", and on the only capture in this tree with debug data that
  half does not work. *How*: try the selectors against each other on the same draw, `--sample 0`, and read the
  engine's own refusal paths (`d3d12_shaderdebug.cpp`: the input fetcher's compile, the depth-test hit search,
  `derivValid`); then either fix the message so the reason is said, or document the engine's limit. **~0.5 d to
  a conclusion.**

* **Does a patched shader carry debug info?** — `patch` compiles a replacement shader locally
  (`BuildTargetShader`) and substitutes it for the capture's own. If that compile keeps its debug info, the
  debugger can step the *edited* shader in a capture whose own shaders were stripped of PDBs — which is every
  UE capture here, and would make `trace` usable where it is most wanted ("change the shader, then see what it
  computes"). *How*: one run settles it — patch a shader in the Android capture, then `trace` the patched draw.
  Then either build the link between the two commands, or write the one paragraph in REFERENCE that says a
  replacement carries none. **~0.5 d.**

## 4. Suggested order

Phased, and each phase stands on its own — nothing here is blocked on something later in the list. Every work
item of §1–§3 appears exactly once, so this is the whole list in one place rather than a selection of it; each
item keeps its section's **P** label and its own effort figure, so this file stays the place to read what an
item *is*.

**Phase 1 — the cheap extracts, one sitting each.** Nothing blocked, nothing invented, and each either
unblocks an analysis that is already shipped or protects what is:

1. **The work volume of every event (§1, ~0.5 d)** — the report's ranking has been waiting for it, and the
   action list is already being walked for `draws`.
2. **The corpus pins the commands landed since (§1, ~0.5 d)** — while `formats`/`trace`/`cubemap`/`sheet` are
   fresh and their output is stable on the three local captures.
3. **The MSAA resolve and the sRGB flag, decoded offline (§2, ~0.5–1 d)** — hermetic, and it turns two "not
   looked at" rows into verdicts.

**Phase 2 — the analysis they feed.** These are the questions a person actually asks of a frame, which today
take two commands and a hand-join:

4. **The frame's time per pass (§1, ~1 d)** — then "the pass with the most triangles" has a cost beside it.
5. **A picture's statistics (§1, ~1 d)** — the numeric form of "look at the PNG".
6. **`replaydiff` over counters (§2, ~0.5–1 d)** — after 4, because the per-pass share is what a comparison
   differs over.
7. **A permutation table for the frame (§2, ~1 d)** — feature study, and it costs a dedup of what the bundles
   already carry.
8. **What a dispatch writes (§2, ~1 d)** — the largest P2 item, and the one that opens compute.

**Phase 3 — settle the two probes, then decide.** One session each, and the answer decides whether a bigger
item exists:

9. **Why the pixel path of `trace` produces nothing (§3, ~0.5 d)** — fix the message, or document the engine's
   limit in the words of the measurement.
10. **Does a patched shader carry debug info? (§3, ~0.5 d)** — if it does, "change the shader, then step it"
    is the next feature; if not, it is one paragraph in REFERENCE.
