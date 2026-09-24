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
ones and the documents they read). What is left here is the two probes: one session each, and the answer
decides whether a bigger item exists at all.

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

## 1. P3 — probes whose answer is not known yet

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

## 2. Suggested order

Phased, and each phase stands on its own — nothing here is blocked on something later in the list. Every work
item of §1–§3 appears exactly once, so this is the whole list in one place rather than a selection of it; each
item keeps its section's **P** label and its own effort figure, so this file stays the place to read what an
item *is*.

**Phase 1 — settle the two probes, then decide.** One session each, and the answer decides whether a bigger
item exists:

1. **Why the pixel path of `trace` produces nothing (§1, ~0.5 d)** — fix the message, or document the engine's
   limit in the words of the measurement.
2. **Does a patched shader carry debug info? (§1, ~0.5 d)** — if it does, "change the shader, then step it"
   is the next feature; if not, it is one paragraph in REFERENCE.
