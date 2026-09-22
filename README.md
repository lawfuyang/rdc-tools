# rdc_analysis.py — offline RenderDoc `.rdc` analyser

A single-file, dependency-light analyser for RenderDoc captures. It parses the `.rdc` container, decompresses
the frame-capture stream, walks the structured-data (SDChunk) stream, and decodes the D3D12 command payloads —
**without `renderdoc.pyd`, without the GUI, and without a GPU**.

Written to answer graphics questions that the RenderDoc UI makes tedious: *which pipeline state does this draw
use, which constant buffers are bound, what each root parameter is, what a descriptor table resolves to, and
where a resource id points.* It is used from the command line and from scripts; every command prints plain text.

What it deliberately does **not** do is reconstruct frame data that RenderDoc's own replay engine hands over
directly — uniform values, shader signatures, disassembly, decoded textures. Those are the replay driver's job
(REFERENCE §9); this tool stays on the file's structure and the command stream, where it is fast and needs no
device. REFERENCE §8 lists the sharp edges that follow from that.

> ## Vibe coded — use at your own risk
>
> Every feature and every line of code here is **vibe coded**, written ad-hoc by me, for me, because I am lazy
> and this was the fastest way to get my own answers while debugging RenderDoc captures. It is not a product,
> not a library, not supported, and not reviewed. Commands exist because one specific capture needed them;
> heuristics and hard-coded assumptions (chunk names parsed out of a source tree, descriptor-write and
> state-tracking heuristics, "this layout worked once") are load-bearing throughout. **Use at your own risk** —
> validate anything you plan to rely on against the capture you are actually debugging, and read REFERENCE §8
> for the known sharp edges.

---

## 1. Requirements and setup

| Requirement | Notes |
|---|---|
| Python 3.8+ | tested with `C:\Program Files\Python311\python.exe` |
| `zstandard` (optional) | only for Zstd-compressed sections. Not needed for the captures used so far (they are LZ4, which the next row covers). `pip install zstandard` if `sections` reports `zstd`. |
| **`bin/rdc_lz4.dll`** | **the decoder, and the build writes it** (`cmake --build build`, from `src/cpp/third_party/lz4`) — so a tree that has not built it cannot read a capture's stream, and says so, naming the command. Nothing to install and no `pip` package involved; `$RDC_LZ4_DLL` points at a system `liblz4` (`lz4.dll` / `liblz4.so.1` / `liblz4.dylib`) instead if one is there. Decodes `desktop-2`'s 1.47 GB stream in **0.54 s** (REFERENCE §3.2). |
| **RenderDoc source tree** | **Preferred, optional, and fetched for you** — chunk names are the *capture's* vocabulary when the tool can read the **real implementation of RenderDoc** (a `renderdoc-src` folder in the root folder, downloaded from the latest tag the first time a command needs a name, §1.1). When it cannot — no network, `$RDC_NO_BOOTSTRAP`, a tree that is absent, older, or half-extracted — the names come from a **bundled table** generated from a released RenderDoc, so a chunk still reads `List_DrawIndexedInstanced` rather than `1040`, and the warning says which version those names are. `bootstrap` fetches the tree up front. |
| A `.rdc` capture | any D3D12 capture; Vulkan captures parse at container level but the chunk decoders are D3D12-specific |

Run it as:

```powershell
& 'C:\Program Files\Python311\python.exe' src\py\rdc_analysis.py <command> <file.rdc> [args...]
```

Running with no arguments prints the command list (the module docstring).

### 1.1 Chunk names — the source tree, and the bundled table behind it

The tool does **not** guess chunk names: it parses the chunk-name enums out of the **real RenderDoc
implementation** at runtime, so the names it prints are the vocabulary of the RenderDoc version that produced
the capture. That tree used to be a manual step. It is now **fetched on demand**: the first command that needs
a chunk name downloads the latest tagged RenderDoc source from GitHub and extracts it into `renderdoc-src` in
the root folder. Nothing to clone, nothing to unzip.

```
<root>/rdc-tools/                      <- the root folder of this tool
    src/py/                            the tool
    README.md
    ROADMAP.md
    renderdoc-src/                     <- fetched here on first use (and it holds your captures)
        renderdoc/
            core/core.h                        SystemChunk enum      (PushMarker, InitialContents, ...)
            driver/d3d12/d3d12_common.h        D3D12Chunk enum       (List_DrawIndexedInstanced, ...)
            common/dds_readwrite.cpp           DXGI_FORMAT           (R8G8B8A8_UNORM, ...)
        renderdoccmd/
        ...
```

Only those three files are actually *read* — "populated" means the first two exist, which is also how a
half-extracted tree is recognised — but the whole tree is kept, because the rest is what you consult while
extending the tool (REFERENCE §6).

**A tree is preferred, not required.** `src/py/rdc_chunknames.py` holds the same three enums, generated from a
released RenderDoc and checked in. It is the *floor*, and a tree's enums are written over the top of it:

* a tree that is there names what it knows, in the capture's own version's vocabulary — which is the whole
  reason to read one, and why a tree matching the capture beats the table;
* a machine with **no tree at all** — a fresh clone with no network, a locked-down box — still prints
  `List_DrawIndexedInstanced`, and the warning on stderr says which RenderDoc those names are from, because
  the capture may be another version's;
* a tree that is **older than the capture**, or half-extracted, no longer costs the names of the ids it does
  not have: the table covers them, and the warning says so.

The table is regenerated from a tree and never edited by hand:

```powershell
& $py src\py\rdc_analysis.py chunk-names            # is the table what the tree would generate?
& $py src\py\rdc_analysis.py chunk-names --write    # regenerate it from renderdoc-src, then read the diff
```

`--check` exits **2** when there is no tree on this machine to compare against, like `goldens --check`: a tree
that is not there is the state of a working tree, not a failure.

**Doing it explicitly.** Every command fetches on demand; these run the same step up front, which is what a
fresh clone, a script or a pinned version wants:

```powershell
& $py src\py\rdc_analysis.py bootstrap            # fetch the latest tagged source, or do nothing
& $py src\py\rdc_analysis.py bootstrap v1.46      # pin a tag, e.g. to match the installed RenderDoc
```

It downloads ~54 MB, extracts ~6,000 files (~900 MB on disk — the full source tree, docs included) in about
13 seconds, and prints where it put them, the version the tree declares and how many chunk names it parsed
out of it. A second run is a silent no-op. The extracted tree is RenderDoc's own checkout, so the version it
reports is the version of the enums; when that differs from the RenderDoc that recorded the capture — or from
the bundled table's release — the tool says so when it names a chunk.

**The driver's own build.** `bin\replay_dump.exe` is written by `cmake --build build` from `src/cpp`, and a
binary older than its sources answers with the *previous* revision's behaviour while looking exactly like a
current one. Both halves of that are covered: the driver compares its own write time against `src/cpp/*.cpp|h`
and `CMakeLists.txt` at startup and says so in its log when it is behind (never an error — running an old
build on purpose is how a bundle from the previous revision gets reproduced), and the tool reports and
rebuilds it, which is the only place that can: a running image cannot be overwritten on Windows.

The same `cmake --build` writes two more artefacts. `bin\rdc_lz4.dll` is the offline tool's decoder (§1), and
`bin\rdc_replay.dll` is **the same driver as a library** — the same sources, one command language, a session
you can hold open and ask several questions of (REFERENCE §9) — which is what `sweep` (§2.2) uses. So
`python src\py\rdc_analysis.py build [--check]` compares **all three** against their own sources, each in its
own block and independently, because a new `lz4.c` makes neither of the others stale; the exe and the replay
library share a source tree and are compared against the same one on purpose, since a library answering from
a different revision than the command line is worse than no library at all. The driver's own warning stays
about the exe: it never loads either library.

```powershell
& $py src\py\rdc_analysis.py build --check       # is bin\replay_dump.exe older than src\cpp? (exit 1 = yes)
& $py src\py\rdc_analysis.py build               # build it, if so
```

**Where the tree is looked for** (`_find_renderdoc_src()` in `src/py/rdc_chunkmap.py`), in order:

1. the `RENDERDOC_SRC` environment variable, if set;
2. `<the folder holding a module>/renderdoc-src`, and then the same name in every folder above it — this is
   the search that finds the documented `<root>/rdc-tools/renderdoc-src`, and it also finds a tree beside the
   tool if you keep one there (**the documented convention**, and the one place the fetch ever writes);
3. `C:\Workspace WIth Spaces\rdc-tools\renderdoc-src` — the historical absolute default.

The walk upwards is deliberate: the tool used to sit at the repository root and now sits in `src/py`, and one
search that works for both is better than a second convention to remember.

**When it will not fetch.** Three cases, all deliberate:

| case | what happens |
|---|---|
| `$RENDERDOC_SRC` is set | it is used and **never written into**: if it has no usable tree, that is reported and the run continues with numeric ids. Overwriting a path you set by hand is not the tool's business. |
| `$RDC_NO_BOOTSTRAP` is set | no network at all: the tool checks, warns and falls back, exactly as it did before the fetch existed. For an offline machine, or a reader who wants no surprise downloads. |
| the command names its own tree (a script, the test suite) | checked and reported, never filled — the fetch only ever writes into `renderdoc-src` in the root folder. |

In every one of those cases, and when the download fails (no network, GitHub unreachable, disk full), the
tool prints one warning to stderr and continues with **numeric chunk IDs** (`1040` instead of
`List_DrawIndexedInstanced`). Everything else — container parsing, decompression, payload decoding, `draws`,
`dxbc`, `verify` — is unaffected, and a capture is always analysed.

To point at a tree somewhere else for a single run:

```powershell
$env:RENDERDOC_SRC = 'D:\src\renderdoc'
& $py src\py\rdc_analysis.py chunks 'capture.rdc' 40 List_Draw
Remove-Item Env:\RENDERDOC_SRC
```

**Cost model.** The *first* command on a capture decompresses the frame-capture stream (≈3.2 s for the 374 MB
one, ≈3.6 s for the 631 MB one, plus the stream's size in RAM); every later command is served from the
decompressed-stream cache in ≈0.3 s (REFERENCE §4.8), and commands that only need the container (`blocks`) are instant
either way. For scripted queries inside one Python session, keep `stream` in a variable (see REFERENCE §7).

---

## 2. Quick start and the playbook — every command, with an example

The two tools split one job: **the engine extracts, the files get analysed, and only open questions go back to
the engine.** Five rules follow, and every recipe below keeps to them:

* **Opening a capture is the expensive thing.** It creates a device — seconds per capture (REFERENCE §9) — and
  only one replay may run at a time. A question the *files* can answer must never be asked of the engine: one
  `dump` or one `batch` file pays the open once for the whole frame.
* **The offline half is the verifiable half.** No device, sub-second once the stream is cached, deterministic,
  and covered by the hermetic unittest suite — which is why the analysis *heuristics* live there (the report
  generator is offline code over a bundle, REFERENCE §4.11) and the driver stays a data source.
* **Only the engine knows frame *data*** (names, values, decoded pixels, geometry, the rendered image); only the
  file knows *structure* (chunk stream, resource table, descriptor writes, lifetimes). A claim that needs both
  is assembled offline, from both.
* **Extract to files, not to a terminal.** A bundle can be re-read, grepped, diffed, hashed and handed to the
  offline tool, and it survives the process that produced it; stdout does not, and a crash loses it.
* **An answer without evidence is not an answer.** Every claim carries the event id or resource id it came from,
  plus the command that shows it — the rule the tools follow themselves (`AGENTS.md`).

The examples run as written against a capture called `capture.rdc` in the current folder, with `python` standing
for the interpreter of §1 and everything else spelled out. Items marked *(ROADMAP §N)* do not exist yet;
everything else does, and REFERENCE §4 (offline) and §9 (the driver) are the full reference for each command.

### 2.1 Pick the command by what you are asking

| Question | Where to start |
|---|---|
| What is this file, what sections does it have? | `sections`, `blocks` |
| Can I trust a parse of this file? | `verify` |
| What is the shape of the frame (passes, draws, chunk histogram)? | `summary` |
| Show me the pass/primitive tree | `markers` |
| Which draw is the one I care about? | `draws`, then `find` |
| What pipeline state, constant buffers and vertex streams does draw N use? | `draws` (the file's account), `state` (the engine's) |
| Who writes this resource, and who reads it? | `deps`; `usage` for the engine's side |
| What does the frame's memory add up to, and what could share it? | `memory`, `vram` (REFERENCE §4.15, §4.20) |
| Is what the root signatures declare what the stream binds? | `rootsig-check` (REFERENCE §4.19) |
| What is `res342`, and which buffers/textures exist at all? | `resources` |
| What does the descriptor heap hold that this binding points into? | `descriptors` |
| What does this root signature declare? | `rootsig` |
| What exactly is in chunk N (payload hex + decoded fields)? | `chunk <N>`, `dump-chunk <N> <file>` |
| Which shaders are in this capture, and where? | `dxbc`, then `dump-shaders` |
| What does a shader read, and is it given what it reads? | `shaders`, then `crosscheck` |
| Where is this string / name in the stream? | `grep`, `count`, `names`, `strings` |
| What did the engine complain about? | `debug` |
| Which event ids are real? | `probe`, `draws`, `find` |
| What was bound at eid E? | `state <eid>`, by number or marker path |
| What changed between two events? | `statediff <eidA> <eidB>` |
| What are the values behind "the light is too bright"? | `cb`, `buffer` |
| Is this uniform right here and wrong at the next draw? | `watch <name>` — one row per change over a range |
| Why is this pixel this colour? | `pixelhistory <eid\|last> <resId> <x> <y>` |
| What did the pass render? | `image <eid> <out.bmp>`, `sheet` |
| Is this texture the problem? | `textures --save`, `usage <resId>` |
| What did the vertex shader emit? | `mesh <eid>` |
| How far apart are two images? | `imgdiff <rdc> <a.bmp> <b.bmp>` |
| Where does the time and the bandwidth go? | `counters`, `counters --per-pass`, `crosscheck` |
| What if the shader did something else? | `patch <eid> <stage> --from <file>` |
| What does this whole frame do, pass by pass, and what is off about it? | `report` — after a `dump` (REFERENCE §4.11) |
| How do two captures differ (mobile vs desktop, before vs after)? | `diff` (the streams' own record), `passdiff`, then `replaydiff` (REFERENCE §4.18, §4.16) |
| Do the documents still match their contract? | `validate <bundle> schema`, `schema --check` |
| How do I run any of this headless (no GPU, no captures)? | `selftest`, `goldens --check`, `bundle-verify`, `schema --check` |

Every command in that table has a worked example in §2.2 (offline) or §2.3 (driver); §2.4 is the order to run
them in, and §2.5 is the recipes that use several at once.

**Three commands to start with**, and the rest of §2.2 for the long tail:

```powershell
python src\py\rdc_analysis.py sections 'capture.rdc'      # what is in the file
python src\py\rdc_analysis.py summary  'capture.rdc'      # the frame's shape
python src\py\rdc_analysis.py draws    'capture.rdc' 40   # the stream's own account of the draws
```

---

### 2.2 The offline tool — every command, with an example

`python src\py\rdc_analysis.py <command> <capture.rdc> [args…]`; no arguments prints the list (§1). It reads the
**file** — no device, no `renderdoc.dll`, no GPU — so it is the half that works anywhere, that costs
sub-seconds once the stream is cached (REFERENCE §4.8), and that the hermetic test suite covers. Each table
below gives the tool's own usage with one concrete capture. The five row commands (`resources`,
`descriptors`, `summary`, `draws`, `rootsig`) also take `--format csv|markdown`, which prints the same rows
as a spreadsheet or an issue wants them and moves the prose around them to stderr (REFERENCE §4).

#### The container and the stream

| Command | What it answers | Example |
|---|---|---|
| `sections` | file size, rdc and prog version, the driver that recorded it, and every section's type, flags and compressed/uncompressed size | `sections 'capture.rdc'` |
| `blocks` | per section: name, flags and the first 16 bytes hex — enough to identify the compression (`28b52ffd` = Zstd), which is what makes an unreadable capture explain itself | `blocks 'capture.rdc'` |
| `verify` | walks the chunk stream and checks what would make a parse untrustworthy — frames claiming bytes the stream does not hold, payload lengths that disagree with the documented layout; **exit 1** on problems | `verify 'capture.rdc'` |
| `chunks` | chunk index, offset, **name**, payload length and a string preview — find a chunk by name | `chunks 'capture.rdc' 40 List_Draw` |
| `chunk` | one chunk in full: id/name/flags/length, decoded fields, a 160-byte hex dump and the payload's strings | `chunk 'capture.rdc' 452` |
| `hex` | hex + ASCII dump of a stream window (accepts `0x…`) | `hex 'capture.rdc' 0x1c40 256` |
| `dump` | every ASCII string inside a byte window (decimal offsets only — this is **not** the driver's `dump`) | `dump 'capture.rdc' 1000 4096 4` |
| `count` | how many times each pattern occurs, and where the first one is | `count 'capture.rdc' List_Draw SetPipelineState` |
| `grep` | every byte-occurrence of one ASCII pattern, with ±context bytes rendered as text | `grep 'capture.rdc' IndirectLightingCache 200` |
| `strings` | unique ASCII strings, ranked by occurrence count then first offset | `strings 'capture.rdc' 6 100` |
| `names` | the strings that look like engine names (`Shader`, `BasePass`, `StaticMesh`, …) — how a capture's own vocabulary surfaces without replaying it | `names 'capture.rdc' 10` |
| `cache` | the decompressed-stream cache: `list`, `dir` or `clear`; needs no capture | `cache list` |

#### The resource table, its bindings and its memory

| Command | What it answers | Example |
|---|---|---|
| `resources` | the resource table: id, kind, byte size or dimensions + DXGI format, and the name the application gave it — the filter is a substring, and a limit of `0` means no limit | `resources 'capture.rdc' 0 Sky` |
| `descriptors` | the written slots of every descriptor heap: heap, slot, kind (cbv/srv/uav/rtv/dsv/sampler) and the resource it points at — the filter matches a heap's id or its name | `descriptors 'capture.rdc' 200 0` |
| `rootsig` | every root signature the capture creates: version, cost in root-argument DWORDs, static samplers, flags, and each parameter's type, register, space and descriptor ranges | `rootsig 'capture.rdc' 8` |
| `deps` | who writes what and who reads it, from the stream itself: per resource, writes and reads with their first and last event, with `read-before-write` and `write-never-read` flagged; `table` (default), `dot` or `mermaid` | `deps 'capture.rdc' 40 mermaid` |
| `memory` | what the frame's memory adds up to: placement and kind with byte totals, capture-relative lifetimes, the aliasing barriers memory is handed over with, heaps ranked by size, never-read bytes | `memory 'capture.rdc' 40` |
| `vram` | the same ledger as a *budget*: the resources the frame references by role (render targets, textures, buffers, acceleration structures), the pass with the largest peak live inside it, and the what-if arithmetic — this frame at half resolution, or with the resources a name filter matches dropped | `vram 'capture.rdc' --drop GBuffer` |
| `rootsig-check` | what the root signatures declare against what the stream binds and the heaps hold: an index the signature does not have, a table slot the frame never wrote, a slot holding another kind than the range declares, a resource the capture never creates — and, with a bundle, the same facts against the engine's own rows (exit 1 on a certain disagreement) | `rootsig-check 'capture.rdc' bundle` |

#### The frame as the file sees it

| Command | What it answers | Example |
|---|---|---|
| `summary` | chunk count, draw/dispatch and marker counts, the chunk-type histogram and every marker in order — the frame's shape with no device | `summary 'capture.rdc'` |
| `markers` | every marker chunk: index, kind and up to three strings (ASCII and UTF-16LE) — the pass/primitive tree | `markers 'capture.rdc'` |
| `draws` | per draw from the *command stream*: pipeline state, CBVs, vertex streams, the `rpN` root-parameter annotations and resource names | `draws 'capture.rdc' 60` |

#### Shaders

| Command | What it answers | Example |
|---|---|---|
| `dxbc` | one row per DXBC/DXIL container: index, offset, size, stage, hash and the parts it carries — an inventory, not a disassembler | `dxbc 'capture.rdc' verbose` |
| `dump-shaders` | writes `shader_NN_<hash>.dxil` per container plus `shaders.txt` — for `dxc`, `dxil-spirv`, RenderDoc, or the D3D12 harness (ROADMAP §3.5) | `dump-shaders 'capture.rdc' .\shaders` |
| `dump-chunk` | writes one chunk's payload to a file, for a hex editor or a bug report | `dump-chunk 'capture.rdc' 452 452.bin` |

#### The report, the A/B and the corpus

| Command | What it answers | Example |
|---|---|---|
| `report` | the frame report from a bundle: frame at a glance, the engine's vocabulary, the pipeline map, pass by pass, notable passes and resources, red flags with their evidence, ranked recommendations, and its own caveats. A finding whose cause the corpus knows (`goldens/captures.json`'s `known`, matched by the capture's SHA-256) is printed **proven** with that cause; every other one is **unproven** (REFERENCE §4.11, §4.17) | `report 'capture.rdc' bundle` → `bundle\report.md` + `.json` |
| `diff` | the two *streams'* own calls, compared: the marker path, the call's arguments, the state chunks that changed before it, and every binding in force — by name, and by what each slot is, so a re-numbered root parameter is not a difference (REFERENCE §4.18) | `diff ab\mobile.rdc ab\desktop.rdc --all` |
| `passdiff` | the two captures' marker trees side by side, aligned by path — the free first half of "why do these two differ" | `passdiff ab\mobile.rdc ab\desktop.rdc --all` |
| `replaydiff` | the A/B of two bundles: structure, state rows, every named constant member by member, each shader's hash, and with `--with-images` the renders, per pixel up to `--image-detail` | `replaydiff ab\mobile ab\desktop --with-images --out ab\diff` |
| `validate` | documents against the schemas the driver publishes (plus the report's own): a whole bundle, or one saved `--json` file with the `kind` it is | `validate bundle schema`, `validate t.json schema textures` |
| `goldens` | the checked-in corpus: re-runs each capture's pinned command list and compares the transcripts, the A/B documents and the **driver's own text** for that capture byte for byte (**0** compared and matched, **1** a mismatch, **2** nothing to compare). It also carries what is *known* about each capture — notes, and the **causes** the report matches findings against (REFERENCE §4.17) | `goldens --check`, `goldens --write` |
| `sweep` | a folder of captures, swept: one bundle per `.rdc` and an index of what they are (key, size, SHA-256, what each bundle holds). One process, one replay session per capture — the driver as a library (REFERENCE §4.21, §9) — and a capture whose bundle is already there is reported `present` rather than replayed, so it is resumable | `sweep captures --out bundles`, `sweep captures --commands driver.txt` |

#### Upkeep

| Command | What it answers | Example |
|---|---|---|
| `bootstrap` | fetches the RenderDoc source tree the chunk names come from, or checks it, optionally pinned to a tag; every command does this on demand (§1.1) | `bootstrap v1.46` |
| `chunk-names` | the bundled enum table against a source tree: `--check` reports drift, `--write` regenerates `src/py/rdc_chunknames.py` | `chunk-names --check` |
| `build` | whether `bin\replay_dump.exe`, `bin\rdc_replay.dll` and `bin\rdc_lz4.dll` are older than their sources, and builds them unless `--check` | `build --check` |
| `selftest` | the hermetic unittest suite — no GPU, no capture, seconds long | `selftest`, `selftest -k goldens -v` |

### 2.3 The replay driver — every command, with an example

`.\bin\replay_dump.exe <command> <capture.rdc> [args…] [--json]`; no arguments prints the list. It is the
engine's half: opening the capture stands a device up — seconds, and one replay at a time (REFERENCE §9) — which
is where §2's rules come from. An **event id argument may be a marker path** (`state BasePass`,
`state "Scene > BasePass"`) or `last` for the frame's own last event, and `--at-marker <path>` does the same for
a command whose eid is not positional (`cb --at-marker BasePass ps 0`). Every command takes `--json`; `schema`
prints the shape of each document kind, and the offline `validate` checks real documents against it.
`--dll <path>` (or `$RDC_RENDERDOC_DLL`) names which `renderdoc.dll` to replay with, and the driver refuses a
capture recorded by a **newer** RenderDoc than that engine, with both versions named, instead of replaying it
badly (REFERENCE §9).

#### Opening the capture, and finding your way

| Command | What it answers | Example |
|---|---|---|
| `info` | renderdoc version, driver, GPU, API properties, feature flags (`pixelHistory`, `shaderDebugging`) and counts — the cheapest sanity check there is | `info 'capture.rdc'` |
| `probe` | which event ids actually have pipeline state; a wrong eid returns an *empty* state rather than an error, so this is what runs before "nothing is bound" is believed. The whole frame by default (a number caps the range, `last` spells the default), it must be the session's **first** command, and the answer is cached — a repeat costs the session's startup | `probe 'capture.rdc'`, `probe 'capture.rdc' 3000` |
| `debug` | the API's own complaints (validation layer, etc.) — they outrank any self-made hypothesis; `--group` folds each distinct message into one row with its count and eid range, and `--fail-on` exits **1** when anything at or above that severity was reported (a pass/fail line for a script) | `debug 'capture.rdc' --group --fail-on medium` |
| `draws` | the action tree with event ids: markers and calls, optionally filtered | `draws 'capture.rdc' 200 Shadow` |
| `find` | events whose call name or marker path matches, and resources whose name matches (case-insensitive) | `find 'capture.rdc' SkyViewLut` |
| `state` | the bound shaders, outputs and D3D12 root parameters at one event | `state 'capture.rdc' 27931`, `state 'capture.rdc' BasePass` |
| `statediff` | one changed field per line between two events' states | `statediff 'capture.rdc' 270 last` |

#### Values, bindings and usage

| Command | What it answers | Example |
|---|---|---|
| `shaders` | reflection: cbuffers, bindings and signatures, plus the disassembly with `--disasm` — the names that turn a root parameter into a meaning | `shaders 'capture.rdc' 27931 --disasm` |
| `cb` | the named values of one constant buffer, structs and arrays expanded, member by member | `cb 'capture.rdc' 27931 ps 3` |
| `watch` | one reflection member's value **at every event** of a range, as one row per *change* — a uniform that is right at one draw and wrong at the next, in one command instead of forty `cb` calls. A name is a dotted path (`Light.intensity`) or a bare member; `--since`/`--until`/`--max-events`/`--stage` bound the read, `--all` prints every event, and the counters say what was actually read (`found` false means the name matched nothing, which is not the same as "it never changed") | `watch 'capture.rdc' Light.intensity --since 1000 --until 1300` |
| `buffer` | a buffer's contents, read through the engine at the current event | `buffer 'capture.rdc' res1234 0 256 --as f32` |
| `usage` | every event that touches a resource: who writes it, who reads it, and when | `usage 'capture.rdc' res1234` |
| `crosscheck` | what the reflections say a shader wants against what the state says it was given: the vs→ps link, each stage's bindings against the root signature, the render targets against the pixel shader's outputs | `crosscheck 'capture.rdc' --max 40` |

#### The frame's pictures and geometry

| Command | What it answers | Example |
|---|---|---|
| `textures` | the texture list; `--save` decodes them to PNG at full size (the engine decodes but does not resize) | `textures 'capture.rdc' Sky --save .\tex` |
| `image` | the texture display at one event, as a BMP | `image 'capture.rdc' 27931 pass1.bmp` |
| `sheet` | one image per pass, a montage of them and an index; `--list` writes nothing | `sheet 'capture.rdc' .\sheet --max 40` |
| `mesh` | post-VS vertices for one instance | `mesh 'capture.rdc' 27931 0 20` |
| `imgdiff` | how two images differ: how many pixels, how far, and a perceptual hash of each — a number, not an opinion | `imgdiff 'capture.rdc' before.bmp after.bmp --out heat.bmp` |
| `pixelhistory` | why this pixel is this colour: every event up to the scope that tried to write it, the test that rejected each, and the value before, from and after it | `pixelhistory 'capture.rdc' last res1234 640 360` |

#### Cost, and experiments on the frame

| Command | What it answers | Example |
|---|---|---|
| `counters` | GPU counters per event; `--per-pass` folds one counter over each pass (or over `--passes <file>`) and lists the dearest; a replay with no counter results says so rather than printing zeros | `counters 'capture.rdc' --per-pass --top 10` |
| `patch` | builds a shader for this replay target out of a file you edited, substitutes it for the capture's own and replays the frame; `--compare` renders before and after and writes the diff map | `patch 'capture.rdc' 27931 ps --from edited.hlsl --compare` |

#### One session, many questions

| Command | What it answers | Example |
|---|---|---|
| `dump` | the whole frame to disk as a bundle the offline tool reads: `events.json`, `states/`, `cbuffers/`, `resources.json`, `messages.json` and a manifest with every hash; `--with-images`, `--textures`, `--with-counters`, `--since`/`--until`/`--max-events` bound the work | `dump 'capture.rdc' bundle --with-images` |
| `bundle-verify` | re-hashes a bundle with no device and no DLL, so it can be checked anywhere | `bundle-verify bundle` |
| `batch` | runs every command in a file against one open capture, paying the open once; each line's output is preceded by `#=== <line>` so a stream can be split again | `batch 'capture.rdc' run.txt > before.txt` |
| `schema` | the JSON Schema of each `--json` document kind (24 of them); `--out <dir>` writes them, `--check <dir>` fails when the checked-in folder and the driver disagree | `schema --check schema`, `schema state` |
| `selftest` | the driver checking itself — JSON writer, schema table, help text, the DLL it loads — with no capture | `selftest` |
| `--repl` / `--stdin` | keep the capture open and read commands from the terminal or a pipe, one per line, exactly as a batch file spells them; `help` and `quit` work | `.\bin\replay_dump.exe 'capture.rdc' --repl` |

### 2.4 The order of operations

1. **Offline first, because it is free.** `verify` (is the file intact?), `summary` and `markers` (what is in
   it?), `resources` (ids → names), `draws` (the stream's own account). No device, ~0.3 s each once the stream
   is cached (REFERENCE §4.8).
2. **One replay session for everything only the engine can answer** — `dump` into a bundle, or one `batch`
   file holding the specific questions. Never open the capture twice for the same question; `probe` first if
   the eids are not certain, and `info`/`debug` when something already looks wrong.
3. **Analyse offline over the bundle *and* the `.rdc`**: `report` for the map, then the targeted commands for
   the specific thing — `resources` and `rootsig` for what the file recorded, `deps` and `memory` for who
   touched what, `passdiff`/`replaydiff` when there is a second capture to compare against (REFERENCE §4.11,
   §4.15, §4.16).
4. **Targeted engine follow-ups only**, with the eids the offline step produced (`state`, `cb`, `buffer`,
   `pixelhistory`, `image`): files narrow the question, the engine answers it, files again — never a second
   fishing expedition.
5. **Assemble the answer with evidence**, and state plainly what could not be determined (§2.7).

### 2.5 Recipes

Each recipe is a session — the commands in the order to run them, and what to do when the first answer is "not
that". Every command here has its own example in §2.2/§2.3; the recipes are how they compose.

**A. "Explain this frame to me."** The five-minute pass, and the one to run before any other recipe.

```powershell
.\bin\replay_dump.exe dump 'capture.rdc' bundle --with-images   # REFERENCE §9: one replay, everything
python src\py\rdc_analysis.py report 'capture.rdc' bundle                # REFERENCE §4.11: the frame report
python src\py\rdc_analysis.py validate bundle schema                     # REFERENCE §4.12: the documents vs their schemas
.\bin\replay_dump.exe schema --check schema                     # REFERENCE §4.12: the schemas vs the driver
```

Read it in this order: frame at a glance → the engine's vocabulary → pipeline map → pass by pass → notable
passes and resources → red flags → **recommendations**, which is the section that says what to look at first and
gives the command for each row (REFERENCE §4.11) → the caveats, which are the honest statement of what the
report does not know → the appendix of reproduction commands. Each later recipe is a follow-up on one line of
the report. With no GPU at all, the fallback is §2.2's offline five: `sections`, `summary`, `markers`, `draws`,
`resources`.

**Dumping a big capture takes minutes, and says so.** The driver prints a line every ten seconds with the rate
and what is left (`bundle: sweep: 641/3000 (21%), 47 ms each, ~1 min 50 s left`) — the cost is one
`SetFrameEvent` per id, measured at ~47 ms on the 601 MB capture, so nothing else in the run matters. Two
things make repeats cheap: the sweep's answer is **cached** (`%LOCALAPPDATA%\rdc-tools\cache`, one
`SetFrameEvent` re-warms it — measured 94.7 s cold against 36.9 s warm, byte-identical bundles), and
`--max-events`/`--since`/`--until` bound the work. `$RDC_NO_CACHE=1` turns the cache off, `$RDC_NO_BOOTSTRAP`
turns off the source fetch (§1.1), and `$RDC_PROFILE=1` prints where the time went, per call site
(REFERENCE §9).

**B. "Why is this object missing, black, or the wrong colour?"** The pixel-level route, in order of cost.

```powershell
.\bin\replay_dump.exe draws 'capture.rdc' 200 Shadow          # find the pass and the eids (markers first)
.\bin\replay_dump.exe state 'capture.rdc' <eid>              # was it even drawn? RTs, shaders, root params
.\bin\replay_dump.exe shaders 'capture.rdc' <eid>            # names + bind points: what the shader reads
.\bin\replay_dump.exe cb 'capture.rdc' <eid> ps 3            # ... and the values it read
.\bin\replay_dump.exe pixelhistory 'capture.rdc' last res1234 640 360   # every write to that pixel, and why not
```

If the draw is there and the values look right, `pixelhistory` *(REFERENCE §9)* is the next step and usually the
answer: it lists every event that touched that pixel **and the reason each was rejected** — `depth test failed`,
`stencil test failed`, `scissor clipped`, `view clipped`, `shader discarded`, `backface culled`, `sample masked` —
with the value before, from and after each one. "Nothing drew it" then becomes "the scissor was 0×0 at eid 812". If the pixel
history says the shader itself is responsible, patch it *(REFERENCE §9)* — force the return value, disable the branch —
and re-render the draw to see what changes. That experiment is often faster than reasoning about the
disassembly.

**C. "Why do mobile and PC look different?"** The project's original question, and the reason `diff`,
`passdiff` and `replaydiff` exist (REFERENCE §4.16, §4.18).

```powershell
python src\py\rdc_analysis.py diff     mobile.rdc desktop.rdc          # the two streams' own calls, no device
python src\py\rdc_analysis.py passdiff mobile.rdc desktop.rdc          # the two files' pass lists, no device
.\bin\replay_dump.exe dump mobile.rdc  ab\mobile  --with-images      # the engine's answers for each side
.\bin\replay_dump.exe dump desktop.rdc ab\desktop --with-images      #   (one replay at a time, REFERENCE §9)
python src\py\rdc_analysis.py replaydiff ab\mobile ab\desktop --out ab\diff --with-images
```

The two offline steps come first because they are free. `diff` reads what the *stream* recorded: the same
call under the same marker path, and whether its arguments, the state chunks that changed before it, or any
binding in force differ — by name, and by what each slot *is* (`cbv b0 s0`), so a root parameter renumbered
by a different signature is not reported as a difference. `passdiff` then answers "is the same pass even
there": the marker trees are aligned by **path**, then by the innermost name (a path carries dynamic text —
`CullLights 32x20x8` against `22x14x8`), with the passes only one side has listed. `replaydiff` then compares
what the engine saw for the passes that matched: their structure, the state rows at each pass's first event,
every named constant value member by member, each shader's **hash** (so "a different shader" is a fact), and —
with `--with-images` — each pass's readback, by bytes first and pixel by pixel up to `--image-detail`, with a
heat map per compared pair. It writes `replaydiff.md` and `replaydiff.json` and prints a summary. Then narrow
by name rather than by index: the named cbuffer values that moved (`watch <name>` §2.2 turns that
into a table over the whole frame), and the tables in `engine-schemas/` *(REFERENCE §4.11)* to say which
*concept* the differing block is (`IndirectLightingCache`, `Material`, …). Where the two engines' reflections
disagree on names entirely, the offline `resources`, `rootsig` and `rootsig-check` views are the fallback:
they compare what the *file* recorded. State the GPU caveat (§2.6) in the answer: both frames were replayed on
*this* machine's GPU.

**D. "Is this texture the problem?"**

```powershell
python src\py\rdc_analysis.py resources 'capture.rdc' 0 SkyViewLut       # name → id, size, format
.\bin\replay_dump.exe usage 'capture.rdc' <resId>              # every event that touches it
.\bin\replay_dump.exe textures 'capture.rdc' Sky --save .\tex  # decode it and look at it
```

Three things to check, in this order: is the *content* right (the decoded PNG), is the *format* right for how
it is sampled (the RT-format audit, *ROADMAP §2*), and was it *written* before it was read (`deps` *(REFERENCE §4.15)*:
the write→read chain). To prove its contribution rather than argue about it, substitute a flat texture for it and
diff the renders (`imgdiff`, or `replaydiff --with-images` for a whole frame — REFERENCE §4.16) — if the picture
does not change, the texture is not the problem.

**E. "What is in this uniform — and is it ever what we expect?"**

```powershell
.\bin\replay_dump.exe cb 'capture.rdc' 27931 ps 3     # named values, structs and arrays expanded
.\bin\replay_dump.exe watch 'capture.rdc' Light.intensity   # that member at every event, one row per change
```

`cb` answers "what is bound here"; `watch` answers "is it ever different" — the difference between a constant
that is wrong and a constant that is never set at all. An all-zero buffer where the reflection says the shader
reads it is a red flag the report generator looks for *(REFERENCE §4.11)*, and `buffer <resId>` shows the raw
bytes when the reflection is not enough (structured buffers, index data, hand-built tables).

**F. "What does the shader actually do?"** Four independent views, cheapest first.

```powershell
.\bin\replay_dump.exe shaders 'capture.rdc' <eid> --disasm   # the code, with the reflection next to it
python src\py\rdc_analysis.py dxbc 'capture.rdc' verbose              # which containers exist, and their hashes
.\bin\replay_dump.exe mesh 'capture.rdc' <eid> 0 20          # what the VS emitted (and, ROADMAP §2, the rest)
```

Cross-check the signatures before reading the maths: VS output vs PS input (same semantic, index and width —
a mismatch is a real bug and a *certain* finding), and each stage's expected bindings vs what the root
signature actually binds. When debug info exists in the capture, the shader debugger *(ROADMAP §1)* steps one
invocation and prints the variables; when it does not (usually), say so — that is a limitation to report, not
a puzzle to keep grinding at.

**G. "Where does the time and the bandwidth go?"**

```powershell
.\bin\replay_dump.exe counters 'capture.rdc'                 # what the driver can measure
.\bin\replay_dump.exe counters 'capture.rdc' --per-pass      # folded per pass (the fetch is per event)
.\bin\replay_dump.exe crosscheck 'capture.rdc'               # reflection vs state, every pass
.\bin\replay_dump.exe sheet 'capture.rdc' .\sheet --max 40   # one image per pass, montaged, with an index
.\bin\replay_dump.exe image 'capture.rdc' <eid> out.bmp      # ... or one event's target on its own
```

The offline half supplies the parts the GPU cannot: `deps` *(REFERENCE §4.15)* for writes nobody reads and reads nobody
wrote, `memory` *(REFERENCE §4.15)* for "these N MB could be shared" and for which resources nothing reads, and the
VRAM budget (`vram`, REFERENCE §4.20) for "what if this were half resolution". Counters are hardware and driver dependent — if they are unavailable, the honest
answer is "not measurable here", not zero.

**H. "This looks uninitialised, or garbage."**

```powershell
python src\py\rdc_analysis.py draws 'capture.rdc'                     # offline: clears, copies and the order of writes
.\bin\replay_dump.exe usage 'capture.rdc' <resId>            # engine: the same question, from the device's side
.\bin\replay_dump.exe buffer 'capture.rdc' <resId> 0 256     # the actual bytes, through the engine
```

The class of bug where the answer is a *question*: a resource read in a pass that no earlier pass wrote
(legitimate for persistent resources, so it is reported as read-before-write rather than as a verdict), a
render target loaded instead of cleared, a constant buffer that reads as all zeros, a descriptor that points at
a resource the file shows was never filled in. Say which of those it is, and whether it can be *proved* from an
earlier eid.

**I. "Did my change fix it — or break something else?"**

```powershell
.\bin\replay_dump.exe batch 'capture.rdc' run.txt > before.txt   # or `dump`, REFERENCE §9
# ... rebuild / recapture ...
.\bin\replay_dump.exe batch 'capture.rdc' run.txt > after.txt
```

Compare the **text** output byte-for-byte (it is the contract: this is how the driver's own regression pass is
run, REFERENCE §9), and validate the JSON separately — `validate` now refuses a repeated key as well as a
wrong shape, because a plain parse hides it and that is exactly how a dropped vertex-shader block went
unnoticed once. With images, compare with a difference threshold rather than by eye: "did the picture change"
should be a number (`imgdiff`, or `replaydiff --with-images` for a whole frame — REFERENCE §4.16).

For the **offline** half the same idea is checked in: `python src\py\rdc_analysis.py goldens` re-runs a fixed
command list over the captures in `goldens/` and diffs the output against the transcripts kept there
(REFERENCE §4.17). When the change is to the analyser rather than to a parser, the check that matters is the
self-A/B in that same run: `replaydiff` of one bundle against *itself* must find nothing.

**J. "Triage a capture someone sent me."** A fixed order, because each step can end the investigation.

1. `python src\py\rdc_analysis.py verify <rdc>` — is the file itself intact? (framing, padding, payload checks)
2. `.\bin\replay_dump.exe info <rdc>` — API, driver, GPU, feature flags (`pixelHistory`, `shaderDebugging`), counts.
3. `.\bin\replay_dump.exe debug <rdc>` — the API's own complaints; validation errors outrank any hypothesis of
   your own.
4. `.\bin\replay_dump.exe draws <rdc> 200` — the marker map: which passes exist, and which eids are real.
5. `python src\py\rdc_analysis.py resources <rdc>` — names for the ids, and the sizes that say what is big.
6. `.\bin\replay_dump.exe probe <rdc> <maxEid>` if the numbers look wrong: a wrong eid returns an *empty* state
   rather than an error, so "nothing is bound" must be checked before it is believed.
7. Then the report *(REFERENCE §4.11)*, or the recipe that matches the question — A, B, D or H.

**K. "Answer a shader question the capture cannot."** Some questions are not in the frame: what the shader does
with *different* inputs. Replay has no `SetBufferData`, and `ReplaceResource` needs an existing replacement, so
this is the one case for the standalone harness (ROADMAP §3.5):

```powershell
python src\py\rdc_analysis.py dump-shaders 'capture.rdc' .\shaders   # the DXIL containers
python src\py\rdc_analysis.py rootsig 'capture.rdc'                  # the exact binding layout to reproduce
.\bin\replay_dump.exe cb 'capture.rdc' <eid> ps 0           # realistic constants to start from
```

Feed those three into the harness with hand-built constants, and compare its result against what replay reports
for the same draw (`cb`, `mesh`, `image`) — the capture is the reference implementation.

**L. "Write the answer."** An agent's report should look like the tools' own output in one respect: evidence or
silence.

* Every claim cites the eid and/or resource id, and the command that reproduces it.
* Distinguish *certain* (the engine returned it, the file recorded it) from *heuristic* (a name matched a
  pattern) from *unknown* (undecoded format, missing reflection, unresolved bindless descriptor, no debug info,
  no counters).
* Prefer the exact answer: `shaders` names a cbuffer, `cb` gives its values — do not infer either from the
  stream, and never invent a name.
* Keep the numbers with their units and their source (`1.6 MB` from `resources`, not from a guess).
* State the environment caveats: replayed on this machine's GPU (not the device that recorded it), counters may
  be absent, and a stale bundle is stale (its manifest has the capture hash).

**M. "Run it headless."** Three regimes, and they should not be confused:

* **A machine with neither the GPU nor the captures:** `selftest` (the hermetic suite — seconds, no device) and
  Pyright. Those two are the whole gate, and **nothing in the repository runs them for you** — there is no CI
  configuration and no hook, so this is what to run before pushing. The fixture bundles in `tests/` are what
  make even the report generator and the A/B testable without a device.
* **A machine with the captures but no GPU:** `goldens --check` (REFERENCE §4.17) — the transcripts, the labels
  and the self-A/B for the corpus in `goldens/`, plus the driver's device-free `schema --check`. The driver's own
  text needs a device, so without one that half is reported **not compared** rather than passed, and the command
  exits **2** when no capture of the corpus is present — "nothing compared", not "clean".
* **A machine with the GPU and the capture:** the driver, gated — `probe` alone, one replay at a time,
  `debug --fail-on error` (REFERENCE §9) as the pass/fail line, `bundle-verify` over the artefacts, and the
  before/after comparison of recipe I. Record what no gate covers rather than implying coverage.

**N. "Keep the repo honest."** The gates, cheapest first, and what each one can and cannot see:

```powershell
python src\py\rdc_analysis.py selftest                # the hermetic suite: no GPU, no capture, seconds
npx --yes pyright@latest                              # the type checker ("standard"), no runtime at all
python src\py\rdc_analysis.py chunk-names --check     # the bundled names vs the tree (exit 2 = no tree here)
python src\py\rdc_analysis.py build --check           # are bin\replay_dump.exe and bin\rdc_lz4.dll current?
python src\py\rdc_analysis.py goldens --check         # the corpus and the driver's text (exit 2 = none here)
.\bin\replay_dump.exe schema --check schema           # the checked-in schemas vs this driver
.\bin\replay_dump.exe selftest                        # the driver checking itself: writer, schemas, help, DLL
cmake --build build --config Release --target clang-format-check
```

The first two run anywhere; an exit code of **2** from `chunk-names`, `build` or `goldens` means "nothing to
compare on this machine", which is working-tree state and not a pass. Nothing in that list replays a capture —
that is recipe I and recipe M's third regime, and it is the part a reader has to be told about rather than
shown.

### 2.6 Pitfalls an agent must not walk into

* **A wrong eid is not an error.** `state`/`shaders` on an id that has no pipeline state return an *empty*
  result. Check with `probe`, or use a marker path (§2.3) instead of a number.
* **A chunk index is not an event id.** The offline tool numbers chunks; the engine numbers what a command list
  recorded. On one capture the first draw was chunk 316 while the first event with state was 842 (REFERENCE §9).
* **`probe` runs alone** — it forces non-events on purpose and leaves the last real event's state behind, so
  mixing it into a batch of other commands makes one answer wrong whichever order they run in. A `probe`
  that is not the session's first command now says so on stderr, and its answer is not cached (the cache is
  for a *first* run's answer, REFERENCE §9).
* **`watch` is slow by nature** — it reads every bound constant block at every event of its range, measured
  at ~15 ms per id on the PC capture — so give it `--since`/`--until`/`--max-events`, and read the counters
  it prints: `found` false means the *name* matched nothing (the log then says which commands list a block's
  names), which is not the same as "the value never changed".
* **A bundle is local state, so never commit one.** It belongs to one `.rdc` on this machine and it records
  the capture's **absolute path** (`capture.json`'s `absPath`), which is exactly the kind of thing that must
  not end up in a shared repository. The two defaults the tools write to when they are given no destination —
  `replay_dump dump <rdc>` writes `bundle/`, `sweep <dir>` writes `bundles/` — are in `.gitignore`, and a
  test in `tests/test_rdc_sweep.py` keeps them there; a destination you name yourself is yours to ignore.
* **One replay at a time.** The engine creates a device per process; two at once is what makes a run look
  stuck, and a force-killed replay can leave the driver slow to create the next device.
* **Validate the JSON.** `--json | python -m json.tool`, plus a duplicate-key check; and know that the driver's
  log is per run, its last line decisive (`failed: …` = it stopped and says why, `done: exit N` = it finished,
  neither = killed).
* **Text output is the contract.** It stays byte-identical unless a change is deliberate and recorded; the
  JSON is what a consumer should parse (REFERENCE §9).
* **Three names, three tools.** `replay_dump dump` (a bundle, REFERENCE §9) is not `rdc_analysis.py dump` (a raw stream
  range); `replay_dump draws` (events, with engine state) is not `rdc_analysis.py draws` (chunk-level); and the
  report generator is `report` *(REFERENCE §4.11)*, because `summary` is already the structural histogram.
* **Formats and features are conditional.** Not every texture format can be decoded, shader debugging needs
  debug info that captures usually lack, counters need driver support, and pixel history needs the capture to
  support it. Report the gap; do not synthesise around it.
* **A long scan is split across processes, and only when the stream is cached.** `strings` and `names` scan
  the whole stream in slices, each in its own process, each mapping the *cached* stream rather than being
  handed the bytes — so `$RDC_NO_CACHE`, or a capture whose stream has never been stored, runs the same scan
  in one process, several times slower. `$RDC_PROFILE=1` prints which path was taken and what it cost
  (REFERENCE §4.13).
* **Whatever starts a pool must be importable without side effects.** On Windows a worker re-imports
  `__main__`, so a script or test module that decompresses at module level does it once per worker. The tool
  itself guards `main()`; a throwaway harness has to do the same.
* **A command maps the capture; it does not read it** (REFERENCE §4.14). That is where a walk command's last
  second went, but it also means the file is locked against writing while the command runs — you cannot
  replace a capture mid-analysis, and `cache clear` in another process reports the file in use. The stream
  cache's format is versioned: after an upgrade the first run decompresses once more, and the old files show
  up in `cache list` as unusable until `cache clear` removes them. Beside a stream the cache may hold a
  **derived** file instead — a small answer *about* that stream, like the DXBC container search (§4.13) — and
  `cache list` counts those on their own line, with `cache clear` taking them too.

### 2.7 What "best analysis" means here

The bar is not the number of findings, it is that every finding is **checkable** and **ranked**: what the frame
does (from the report), what looks wrong (each red flag with its evidence and its certainty), what it costs
(counters, budgets, dead work), what to do next (the ranked recommendations with the command that reproduces
each), and what remains unknown. An answer that ends with "here is what I could not determine, and how you
could" is a better answer than one that ends with a confident guess — that is the rule the whole project is
built on.

## Where the rest of the manual lives

* **`REFERENCE.md`** — the detail behind this file: §3 how the capture is decoded, §4 the full command
  reference, §5 worked examples, §6 verified payload facts, §7 how to add a command, §8 pitfalls and known
  limitations, §9 the replay driver (`replay_dump`). Its section numbers are the ones the code cites.
* **`ROADMAP.md`** — what is not implemented yet, in priority order: the driver's navigation and experiment
  commands (§1–§2), its picture/counter/geometry work (§3), the offline analysis still to come (§4),
  verification, regression and the bug atlas (§5), the work beyond the local desktop (§6, with the standalone
  D3D12 harness as §6.5), robustness and scope (§7), and the suggested order (§8), which places every item of
  §1–§7 in one phased list.
* **`AGENTS.md`** — the rules for an AI agent changing this repo: the invariants, the payload-layout
  comments, the determinism contract, and the pitfalls that have already bitten.
* **`renderdoc-src/`** — fetched into the root folder on first use (§1.1), and also where this project keeps
  its captures. The files the decoders were written against:
  `serialise/serialiser.cpp` (chunk framing), `serialise/rdcfile.cpp` (container), `core/core.h` and
  `driver/d3d12/d3d12_common.h` (chunk-name enums), `driver/d3d12/d3d12_command_list_wrap.cpp` (payload
  layouts), `driver/d3d12/d3d12_serialise.cpp` + `d3d12_manager.h`, `api/replay/renderdoc_replay.h`.
* **The tools print their own help**: `rdc_analysis.py` with no arguments prints every command, and
  `bin\replay_dump.exe` with no arguments prints the driver's.
