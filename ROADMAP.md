# rdc-tools — expansion roadmap / TODO

Features that are **not implemented yet** in `rdc_analysis.py` or in its replay driver (`replay_dump.cpp`,
README §9), in rough priority order. Each item says what it is, why it is wanted, how it would be built, and
what blocks it.

Legend: **P0** = do next / unblocks current work · **P1** = high value, moderate effort · **P2** = useful,
opportunistic · **P3** = nice-to-have.

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

The **D3D12 harness** (§1) absorbs nothing: it exists for the one question replay cannot answer — what a shader
does with inputs the capture does not contain — so no item here is "free with a harness".

Current state for reference: the tool parses the `.rdc` container, decompresses the frame-capture stream
(LZ4 in-file, Zstd optional) and caches it on disk so repeat commands are instant (README §4.8), walks the
SDChunk stream, decodes the main D3D12 draw/pipeline/CBV/vertex-buffer payloads, the resource table
(id → kind/size/name, README §4.9), the descriptor heaps (§4.10) and the root signatures (§3.4), inventories
the DXBC/DXIL containers, and can check its own parse (`verify`).
22 commands, see `README.md`. The replay driver is the other half: `replay_dump.exe` (README §9) adds 12 more
that ask the engine for the uniform names and values, textures, disassembly and geometry no file read can
answer. Since 2026-09-15 the tool also has a hermetic unittest suite
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

## 1. P0/P1 — D3D12 harness (only for *synthetic inputs*)

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
`replay_dump` does today. A harness is worth writing only for the ✖ column: running a shader with hand-built
constants — e.g. feed the mobile base-pass pixel shader the HISM's baked SH to prove the shader path in
isolation — and even that can often be avoided by patching the shader in replay instead.

**Sketch.** One `ID3D12Device` + a compute-style or full-screen-triangle PSO + a root signature matching the
shader's bind points (the offline tool can now print that layout: `rootsig`, README §4.1); upload a 256-byte
constant buffer; dispatch/draw to a small RTV; read back with `ReadBackResource`. Inputs: the `.dxil` files from
`dump-shaders` and a JSON of uniform values (which the replay driver can export directly).

**Blockers.** Needs the shader's exact root signature layout (available from the capture — `rootsig` — or the
replay driver) and DXIL compilation to a PSO — `dxc` is available with the UE install.

**Effort.** ~2–3 days for a single-purpose harness; scope it to one shader at a time.

---

## 2. P1/P2 — Quality of life

* **Engine event ids in the replay driver** (P1) — the driver's `draws` numbers the structured file's command-list
  chunks, which is **not** the engine's event id: on the hobby capture the first draw is chunk 316 while the
  first event with pipeline state is 842, because RenderDoc numbers only what a command list recorded. `probe`
  lists the ids that really have state, but a draw number from `draws` should work everywhere. Either derive the
  action list (no exported accessor for it exists) or calibrate once per capture and print both numbers. (~4 h)
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

## 3. P3 — Robustness and scope

* **Zstd without the dependency** — either vendor a decoder or fail with a clear message (today it needs
  `pip install zstandard`). (~4 h)
* **Golden output files for the decoders** — snapshot `summary` / `draws` / `rootsig` output per capture and
  diff it on every run, so a decoder change shows up as a reviewable diff instead of a silent drift. (~2 h)
* **Memory-mapped stream access** — avoid holding ~1.5 GB in RAM for the largest captures. (~4 h)
* **Non-D3D12 driver names** — `load_chunk_names(driver=...)` already takes a driver; expose it on the CLI.
  (~1 h)

---

## 4. Clear README §8 ("Pitfalls and known limitations")

One entry per bullet in README §8 that is *not* left to replay (see the note at the top), with the change that
closes it and the gate that proves it is closed.

**"Resolved" means** the bullet is deleted from README §8, the `CHARACTERIZATION` tests that pinned the old
behaviour are replaced by tests of the *correct* behaviour, and every doc that described the limitation is
updated in the same change (AGENTS.md: behaviour is the contract, so these are deliberate, tested fixes —
never silent ones).

| # | README §8 bullet | Plan | Priority | Effort |
|---|---|---|---|---|
| 4.1 | Chunk names need `renderdoc-src` | §2 bundled chunk-name table | P2 | ~2 h |

### Acceptance gates

* **4.1** (§2 bundled table): names resolve with `renderdoc-src` absent **and** when the capture's version is
  newer than the tree; the table is generated by a checked-in script and carries its RenderDoc version; the
  §1.1 warning becomes "using bundled names for RenderDoc X".

---

## 5. Suggested order

1. **Bundled chunk names** (§2, = §4.1) — ~2 h, removes the last environment dependency and closes the last
   README §8 bullet that is not replay's job.
2. **Golden output files** (§3) — ~2 h, the regression gate that makes the next big change safe.
3. **Diff two captures** (§2) — ~1 day, the highest-value new offline feature, and offline is where it belongs.
4. **Engine event ids in the driver** (§2) — ~4 h, and the one thing that makes the driver's own output
   self-consistent.
5. **D3D12 harness** (§1) — only when a shader must be run with inputs the capture does not contain.
