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
Six items came out of it on the *debugging* side rather than the performance side, each one an answer to a
question a person asks while chasing a bug or a crash.

**Phase 2a landed on 2026-09-24 and left this file**: the two audits §1 used to hold are `hazards` and
`samplers` in `README.md` (the command list) and REFERENCE §4.24 (the permission table, the two barrier forms,
the sampler layouts, the pairing bases and the measurements).

**Phase 3 landed on 2026-09-25 and left this file**: the memory questions §1 used to hold are
`memory`'s **overlapping placements** — placed resources whose byte ranges overlap while both are live, the
write-while-read conflicts among them, and whether an aliasing barrier declares each handover — and the
**`provenance`** command, the chain of writers behind one use, back through copies and resolves. Both are in
`README.md` (the command list) and REFERENCE §4.15; the overlap arithmetic is also the report's
`aliased-write` detector, and the report's `read-before-write` findings carry the walk as a one-line verdict
in their evidence. What is left here is the two probes (§1), plus the three candidates the survey killed —
their reasons are worth more where they are, under "what is deliberately *not* on this list".

**Phase 4 landed on 2026-09-28 and left this file**: the two smaller answers §1 used to hold are the
**`callstack <eid>`** command (`info` says whether a capture carries callstacks at all) and
**`mesh --bounds`** with `dump --bounds` — each draw's post-VS geometry folded into a bundle, and the report's
`geometry-offscreen` rule reading it (every vertex behind the eye, every vertex outside one clip plane, or a
whole draw projecting outside the rectangle it writes). They are in `README.md` (the command list), REFERENCE §9
(the two commands, the bundle's `bounds` member and what `--bounds` costs) and REFERENCE §4.11 (the rule, its
coverage gate and its measurements). The measurement the `callstack` item was waiting on is in REFERENCE §9:
`desktop-1` and `mobile-1` *were* recorded with callstacks on — every chunk carries the flag and the
`resolvedb` section is in the file — and the recorder collected **no frame for any of them**, so the item landed
with its "there is nothing here, and it is the capture rather than the query" path as the corpus's answer.

**Phase 5 landed on 2026-09-28 and left this file**: the two probes §1 used to hold, each of which ended in a
measurement rather than a feature. The **pixel path of `trace`** was not broken — what the engine looks for is
*coverage*: the named call's own fragments at the co-ordinate, so a fragment the frame rejected (`depth test
failed`, `shader discarded`) still traces, one the call never covers does not, and `pixelhistory`'s rows name
calls that do. The refusal now says that instead of guessing between two causes, and the runs behind it, the
`pixelhistory` route and the engine's own `No hit for this event` are in REFERENCE §9. The **patched shader** is
not steppable: the compile keeps its debug info (the engine's `BuildTargetShader` adds `D3DCOMPILE_DEBUG`), but a
trace with a replacement installed is byte-identical to one without it — and the picture side agrees, so the
substitution does not reach the draw at all. That is not a probe any more but a defect, and it is §1 below: the
one item left in this file.

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
  arithmetic `hazards` and `samplers` now do (REFERENCE §4.24), the aliased-placement overlap check
  `memory` does (REFERENCE §4.15), and the items in §1 below — catching the defects that cause them —
  and the messages `debug` prints when a capture has any.
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

## 1. P1 — the substitution does not reach the draw

* **What it is.** `patch` builds a replacement shader for this replay target, installs it with
  `ReplaceResource`, clears the replay cache and re-runs the frame — and nothing the frame does afterwards shows
  it: a replacement pixel shader that returns a fixed colour left the picture *identical* (`differing 0`, one
  non-zero difference hash for both images, `hashDistance 0`), and a trace of the same invocation with a
  replacement installed was byte-identical to one without it. Both measured 2026-09-28; REFERENCE §9 has the
  runs and the events. The replacement is registered (the command prints `replaced on`) and the *compile* is not
  the problem — it keeps its debug info (`BuildTargetShader` adds `D3DCOMPILE_DEBUG`) — so the break is between
  "installed" and "bound by the draw".
* **Why it is wanted.** Two commands depend on the experiment: `patch --compare` can only ever answer "nothing
  changed", and "change the shader, then step it" — the one answer for a capture whose shaders were built
  without debug data — does not exist while the substitution is inert. It is also the tool's only way to ask
  what a frame *would* do, where every other command reports what it did.
* **How.** Read what the engine does with a replacement instead of what our side assumes: `ReplaceResource`
  (`d3d12_replay.cpp`) registers the *shader*, and then `RefreshDerivedReplacements` walks
  `WrappedID3D12Device::GetPipelineList()` and, for every pipeline whose stages include a replaced shader,
  creates a new pipeline and registers that as the *pipeline's* replacement. So the first question is whether
  that pipeline exists and is bound: log the PSO id the render state names and whether the engine reports a
  replacement for it (a few lines in `patch`, removed afterwards), then check whether the draw used it — the
  picture at an event whose readback is known to have content (`sheet`'s index names one per pass, and
  `desktop-2` eid 928 is one that measured content) is the instrument. If no replacement pipeline is created,
  the difference is in `GetPipelineList()`'s contents or in the shader id the pipeline names; if it is created
  and not bound, it is in the render-state path a `eReplay_OnlyDraw` replay takes.
* **The other end of the same investigation.** At some events the readback is *entirely black*: `desktop-1` eid
  4235 and `mobile-1` eid 692 both compared two all-zero 256×256 pictures, while the engine reports real content
  for that same target at that same event (`histogram --eid 4235` on `desktop-1`: max `5.30, 1.86, 2.59`) and the
  same `ReadTargetImage` call writes pictures with content in a `dump` and in `sheet`. That may be the same
  cause seen from the other side, or its own; either way a `patch --compare` at such an event compares nothing.
* **What blocks it.** Nothing this machine lacks — no capture, no device, no tree. It is one question answered by
  reading the engine and running `patch` twice, and it ends either in a fix or in a paragraph saying a
  replacement cannot be replayed on this engine. **~0.5–1 d.**
