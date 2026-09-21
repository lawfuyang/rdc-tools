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

The same `cmake --build` writes `bin\rdc_lz4.dll`, and that one is the offline tool's decoder (§1), so
`python src\py\rdc_analysis.py build [--check]` compares **both** artefacts against their own sources — each in
its own block, and independently, because a new `lz4.c` does not make the exe stale. The driver's own warning
stays about the exe: it never loads the library.

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

## 2. Quick start — pick the command by what you are asking

| Question | Command |
|---|---|
| What is this file, what sections does it have? | `sections` |
| Why is my capture unreadable / what compression is used? | `blocks` |
| What is the shape of the frame (passes, draws, chunk histogram)? | `summary` |
| What does this whole frame do, pass by pass, and what is off about it? | `report <rdc> <bundleDir>` — after `replay_dump dump` (REFERENCE §4.11) |
| How do two captures differ (mobile vs PC, before vs after)? | `passdiff <a.rdc> <b.rdc>`, then `replaydiff <bundleA> <bundleB>` after a `dump` of each (REFERENCE §4.16) |
| Do the driver's documents still match their contract? | `validate <bundleDir> schema` (REFERENCE §4.12) |
| Show me the pass/primitive tree | `markers` |
| Which draw is the one I care about? | `markers`, then `chunks <limit> List_Draw` |
| What pipeline state, constant buffers and vertex streams does draw N use? | `draws` |
| Who writes this resource, and who reads it? | `deps` (offline, with `read-before-write` / `write-never-read` flagged — REFERENCE §4.15) |
| What does the frame's memory add up to, and what could share it? | `memory` (placement, capture-relative lifetimes, aliasing barriers — REFERENCE §4.15) |
| What is `res342`, and which buffers/textures exist at all? | `resources`, `resources <limit> <nameFilter>` |
| What does the descriptor heap hold that this table binding points into? | `descriptors`, `descriptors <rdc> <heapId>` |
| What exactly is in chunk N (payload hex + decoded fields)? | `chunk <N>` |
| Is the parse trustworthy? | `verify` |
| Which shaders are in this capture, and where? | `dxbc`, then `dump-shaders` |
| What does a shader read? (uniform names, signatures) | the replay driver — REFERENCE §9, not this tool (REFERENCE §8) |
| Where is this string / name in the stream? | `grep`, `count`, `names`, `strings` |
| Dump all shaders to disk for disassembly | `dump-shaders <outdir>` |
| Dump one chunk to disk | `dump-chunk <N> <outfile>` |

A typical triage session:

```powershell
$py = 'C:\Program Files\Python311\python.exe'
& $py src\py\rdc_analysis.py sections 'capture.rdc'
& $py src\py\rdc_analysis.py summary  'capture.rdc' | Select-Object -First 60
& $py src\py\rdc_analysis.py draws    'capture.rdc' 40
& $py src\py\rdc_analysis.py chunk    'capture.rdc' 452
```

The frame-level answer needs one replay session first (`replay_dump dump`, REFERENCE §9), and then it is offline:

```powershell
.\bin\replay_dump.exe dump 'capture.rdc' bundle    # the engine's answers, to disk
& $py src\py\rdc_analysis.py report 'capture.rdc' bundle    # report.md + report.json (REFERENCE §4.11)
```

---

## A playbook for an AI agent — working with both tools

The two tools split one job: **the engine extracts, the files get analysed, and only open questions go back to
the engine.** This section is the workflow that follows from that split, written for an agent (or a human) who
has to produce an analysis rather than run a command.

It is written against the roadmap as implemented: anything marked *(roadmap §N)* is a `ROADMAP.md` item and does
not exist yet, everything else runs today.

* **Offline today** — `sections`, `blocks`, `resources`, `descriptors`, `verify`, `summary`, `markers`,
  `chunks`, `chunk`, `draws`, `deps`, `memory`, `rootsig`, `strings`, `names`, `grep`, `dump`, `count`, `hex`,
  `dxbc`, `dump-chunk`, `dump-shaders`, `report`, `passdiff`, `replaydiff`, `validate`, `goldens`, `chunk-names`,
  `bootstrap`, `cache`, `selftest` (REFERENCE §4; the A/B pair is §4.16 and the corpus harness §4.17).
* **Driver today** — `info`, `draws`, `find`, `state`, `statediff`, `buffer`, `pixelhistory`, `shaders`, `cb`,
  `textures`,
  `mesh`, `image`, `sheet`, `imgdiff`, `patch`, `counters` (`--per-pass`), `crosscheck`,
  `debug`, `usage`, `probe`, `batch`, `--repl`/`--stdin`, the bundle pair `dump` + `bundle-verify`, and the
  contract pair `schema` + `selftest` (REFERENCE §9). `find` and an event id argument both take a **marker
  path** as well as a number (and `last` for the frame's own last event), so a command can be pointed at a
  pass rather than at an id.
* **Roadmap** — `watch`, `debug --group`, `sweep` (ROADMAP §1), shader debugging, overlays
  (ROADMAP §2), `mesh --stage/--obj`, texture subresources (ROADMAP §3), `--format`, structural `diff`,
  the root-signature/chunk cross-check, the VRAM budget (ROADMAP §4), the driver-version guard, and the
  driver's own golden text (ROADMAP §5).

### The rule, and why it is the rule

* **Opening a capture is the expensive thing.** It creates a device and takes ~2 s on a small capture and ~6 s
  on a 1.4 GB one, and only one replay may run at a time. A question the *files* can answer must never be
  asked of the engine: one `dump` (REFERENCE §9) or one `batch` file pays the open once for the whole frame.
* **The offline half is the verifiable half.** No device, sub-second once the stream is cached, deterministic,
  and covered by the unittest suite — which is why the roadmap puts the analysis *heuristics* there (the report
  generator is offline code over a bundle, REFERENCE §4.11) and keeps the driver a data source.
* **Only the engine knows frame *data*** (names, values, decoded pixels, geometry, the rendered image); only
  the file knows *structure* (chunk stream, resource table, descriptor writes, lifetimes). A claim that needs
  both is assembled offline, from both.
* **An answer without evidence is not an answer.** Every claim should carry the event id or resource id it came
  from, plus the command that shows it — the same rule the tools follow themselves (`AGENTS.md`), and the
  reason an agent's answer can be checked rather than believed.

### Extract once, to disk, and keep it

Extract to **files**, not to a terminal. A bundle can be re-read, grepped, diffed, hashed and handed to the
offline tool, and it survives the process that produced it; stdout does not, and a crash loses it.

| What to extract | Command | Why an agent wants it |
|---|---|---|
| the action list and markers | `replay_dump draws <rdc> 100000` (or the bundle's `events.json`, REFERENCE §9) | the frame's structure: what is a pass, what is a draw, which eids are real |
| per-event state | `state <eid>`, or `states/<eid>.json` in the bundle (REFERENCE §9) | render targets, depth, shaders, root parameters — the "what was bound" half of every claim |
| shader reflection | `shaders <eid>` (add `--disasm` when the shader itself is the question) | the **names** (`MobileBasePass`, `IndirectLightingCache`) and the bind points that turn a root parameter into a meaning |
| named constant values | `cb <eid> <stage> <slot>`, or the bundle's `cbuffers/` (REFERENCE §9) | what the shader actually read: the numbers behind "the light is too bright" |
| buffer contents | `buffer <resId> [offset] [len] [--as u32\|f32\|hex\|ascii]` (REFERENCE §9) | what is really in a buffer that reflection cannot describe (index data, structured buffers) |
| textures | `textures --save <dir>` today; subresources and raw/HDR options *(ROADMAP §3)* | decoded pixels to look at, plus the format/dimension facts for the audit |
| render targets | `image <eid> <out.bmp>`, or `sheet <rdc> <dir>` for every pass at once; bundle `rt/` (REFERENCE §9) | what the pass produced — the fastest way to see "this pass drew nothing" |
| one pixel's history | `pixelhistory <eid\|last> <resId> <x> <y>` (REFERENCE §9) | "why is this pixel this colour": every event up to the scope that tried to write it, the test that rejected each, and the value before, from and after it |
| geometry | `mesh <eid>` today; other stages and `--obj` *(ROADMAP §3)* | what the VS/GS emitted, which is where vertex bugs show themselves |
| GPU counters | `counters`, and `counters --per-pass` for the fold over each pass | where the time went, where the driver supports it |
| reflection vs state | `crosscheck` | the deterministic bugs: a ps reading what the vs never wrote, a binding outside the ranges the root signature declares, a target whose format is not what the shader writes |
| debug messages | `debug` | the API's own complaints — the highest-value red flags there are |
| usage chains | `usage <resId>`, or `resources.json` (REFERENCE §9) | who writes and who reads a resource: the evidence for "dead" and "uninitialised" |
| resource identity | `resources <rdc>` (offline) | names and sizes for every id, so output speaks in names instead of `res342` |
| the file's own view | `sections`, `verify`, `markers`, `draws`, `rootsig`, `descriptors`, `deps`, `memory` (offline) | structure, integrity, the descriptor writes the engine does not report, and who touched what |
| the `.rdc` itself | keep it next to the bundle | the offline commands read it directly; the engine's output is a *cache* of what it said, never the only copy |

### The order of operations

1. **Offline first, because it is free.** `verify` (is the file intact?), `summary`/`markers` (what is in it?),
   `resources` (ids → names), `draws` (the command stream's own account). No device, ~0.3 s each once the
   stream is cached (REFERENCE §4.8).
2. **One replay session for everything the engine alone can answer.** `dump` (REFERENCE §9), or a
   `batch` file holding the specific questions (REFERENCE §9). Never open the capture twice for the same
   question, and call `probe` first if the eids are not certain.
3. **Analyse offline over the bundle *and* the `.rdc`**: the report generator *(REFERENCE §4.11)* for the map, then targeted
   offline commands (`resources`, `deps` and `memory` *(REFERENCE §4.15)*, `diff` *(ROADMAP §4)*, `rootsig`) for the
   specific thing.
4. **Targeted engine follow-ups only** for what is still open, using the eids the offline step produced — not a
   second fishing expedition. Files narrow the question, the engine answers it, files again.
5. **Assemble the answer with evidence**, and state plainly what could not be determined (the bar for an answer, below).

### Recipes

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
the report. Offline-only fallback: `sections`, `summary`, `markers`, `draws`, `resources`.

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

**C. "Why do mobile and PC look different?"** The project's original question, and the reason `passdiff` and
`replaydiff` exist (REFERENCE §4.16).

```powershell
python src\py\rdc_analysis.py passdiff mobile.rdc pc.rdc                      # the two files' pass lists, no device
.\bin\replay_dump.exe dump mobile.rdc ab\mobile --with-images        # the engine's answers for each side
.\bin\replay_dump.exe dump pc.rdc     ab\pc     --with-images        #   (one replay at a time: §9)
python src\py\rdc_analysis.py replaydiff ab\mobile ab\pc --out ab\diff --with-images
```

`passdiff` first, because it is free and it answers "is the same pass even there": the marker trees are
aligned by **path**, then by the innermost name (a path carries dynamic text — `CullLights 32x20x8` against
`22x14x8`), with the passes only one side has listed. `replaydiff` then compares what the engine saw for the
passes that matched: their structure, the state rows at each pass's first event, every named constant value
member by member, each shader's **hash** (so "a different shader" is a fact), and — with `--with-images` —
each pass's readback, by bytes first and pixel by pixel up to `--image-detail`, with a heat map per compared
pair. It writes `replaydiff.md` and `replaydiff.json` and prints a summary. Then narrow by name rather than
by index: the named cbuffer values that moved (`watch <name>` *(ROADMAP §1)* turns that into a table over the
whole frame), and the tables in `engine-schemas/` *(REFERENCE §4.11)* to say which *concept* the differing
block is (`IndirectLightingCache`, `Material`, …). Where the two engines' reflections disagree on names
entirely, the offline `resources` and `rootsig` views are the fallback: they compare what the *file* recorded.
State the GPU caveat (the pitfalls above) in the answer: both frames were replayed on *this* machine's GPU.

**D. "Is this texture the problem?"**

```powershell
python src\py\rdc_analysis.py resources 'capture.rdc' 0 SkyViewLut       # name → id, size, format
.\bin\replay_dump.exe usage 'capture.rdc' <resId>              # every event that touches it
.\bin\replay_dump.exe textures 'capture.rdc' Sky --save .\tex  # decode it and look at it
```

Three things to check, in this order: is the *content* right (the decoded PNG), is the *format* right for how
it is sampled (the RT-format audit, *ROADMAP §3*), and was it *written* before it was read (`deps` *(REFERENCE §4.15)*:
the write→read chain). To prove its contribution rather than argue about it, substitute a flat texture for it and
diff the renders (`imgdiff`, or `replaydiff --with-images` for a whole frame — REFERENCE §4.16) — if the picture
does not change, the texture is not the problem.

**E. "What is in this uniform — and is it ever what we expect?"**

```powershell
.\bin\replay_dump.exe cb 'capture.rdc' 27931 ps 3     # named values, structs and arrays expanded
.\bin\replay_dump.exe watch 'capture.rdc' Light.intensity   # roadmap §1: the value at every event
```

`cb` answers "what is bound here"; `watch` answers "is it ever different" — the difference between a constant
that is wrong and a constant that is never set at all. An all-zero buffer where the reflection says the shader
reads it is a red flag the report generator looks for *(REFERENCE §4.11)*, and `buffer <resId>` *(ROADMAP §1)* shows the raw bytes
when the reflection is not enough (structured buffers, index data, hand-built tables).

**F. "What does the shader actually do?"** Four independent views, cheapest first.

```powershell
.\bin\replay_dump.exe shaders 'capture.rdc' <eid> --disasm   # the code, with the reflection next to it
python src\py\rdc_analysis.py dxbc 'capture.rdc' verbose              # which containers exist, and their hashes
.\bin\replay_dump.exe mesh 'capture.rdc' <eid> 0 20          # what the VS emitted (and, ROADMAP §3, the rest)
```

Cross-check the signatures before reading the maths: VS output vs PS input (same semantic, index and width —
a mismatch is a real bug and a *certain* finding), and each stage's expected bindings vs what the root
signature actually binds. When debug info exists in the capture, the shader debugger *(ROADMAP §2)* steps one
invocation and prints the variables; when it does not (usually), say so — that is a limitation to report, not
a puzzle to keep grinding at.

**G. "Where does the time and the bandwidth go?"**

```powershell
.\bin\replay_dump.exe counters 'capture.rdc'                 # what the driver can measure
.\bin\replay_dump.exe counters 'capture.rdc' --per-pass      # folded per pass (fetch is per event)
.\bin\replay_dump.exe crosscheck 'capture.rdc'               # reflection vs state, every pass
.\bin\replay_dump.exe image 'capture.rdc' <eid> out.bmp      # what each pass produced (contact sheet, ROADMAP §3)
```

The offline half supplies the parts the GPU cannot: `deps` *(REFERENCE §4.15)* for writes nobody reads and reads nobody
wrote, `memory` *(REFERENCE §4.15)* for "these N MB could be shared" and for which resources nothing reads, and the
VRAM budget *(ROADMAP §4)* for "what if this were half resolution". Counters are hardware and driver dependent — if they are unavailable, the honest
answer is "not measurable here", not zero.

**H. "This looks uninitialised, or garbage."**

```powershell
python src\py\rdc_analysis.py draws 'capture.rdc'                     # offline: clears, copies and the order of writes
.\bin\replay_dump.exe usage 'capture.rdc' <resId>            # engine: the same question, from the device's side
.\bin\replay_dump.exe buffer 'capture.rdc' <resId> 0 256     # roadmap §1: the actual bytes
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
2. `replay_dump info <rdc>` — API, driver, GPU, feature flags (`pixelHistory`, `shaderDebugging`), counts.
3. `replay_dump debug <rdc>` — the API's own complaints; validation errors outrank any self-made hypothesis.
4. `replay_dump draws <rdc> 200` — the marker map: which passes exist, and which eids are real.
5. `python src\py\rdc_analysis.py resources <rdc>` — names for the ids, and the sizes that tell you what is big.
6. `replay_dump probe <rdc> <maxEid>` if the numbers look wrong: a wrong eid returns an *empty* state rather
   than an error, so "nothing is bound" must be checked before it is believed.
7. Then the report *(REFERENCE §4.11)* or the specific recipe above.

**K. "Answer a shader question the capture cannot."** Some questions are not in the frame: what the shader does
with *different* inputs. Replay has no `SetBufferData`, and `ReplaceResource` needs an existing replacement, so
this is the one case for the standalone harness (ROADMAP §6.5):

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

* **CI, no GPU and no capture:** `selftest` (778 hermetic cases) and Pyright — that is the whole gate in
  `.github/workflows/checks.yml`, and the fixture bundles in `tests/` are what make even the report generator
  and the A/B testable there.
* **A machine with the captures but no GPU:** `goldens --check` (REFERENCE §4.17) — the transcripts and labels
  for the corpus in `goldens/`, plus the driver's device-free `schema --check`. It exits **2** when no capture
  of the corpus is present, which means "nothing compared" and not "clean".
* **A machine with the GPU and the capture:** the driver, gated — `probe` alone, one replay at a time,
  `debug --fail-on error` *(ROADMAP §1)* as the pass/fail line, `bundle-verify` over the artefacts, and the
  before/after comparison of recipe I. Record what CI cannot cover rather than implying coverage.

### Pitfalls an agent must not walk into

* **A wrong eid is not an error.** `state`/`shaders` on an id that has no pipeline state return an *empty*
  result. Check with `probe`, or use a marker path *(ROADMAP §1)* instead of a number.
* **A chunk index is not an event id.** The offline tool numbers chunks; the engine numbers what a command list
  recorded. On one capture the first draw was chunk 316 while the first event with state was 842 (REFERENCE §9).
* **`probe` runs alone** — it forces non-events on purpose and leaves the last real event's state behind, so
  mixing it into a batch of other commands makes one answer wrong whichever order they run in.
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
  up in `cache list` as unusable until `cache clear` removes them.

### What "best analysis" means here

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
