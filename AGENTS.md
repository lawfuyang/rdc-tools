# AGENTS.md

Personal, vibe-coded tool for debugging RenderDoc `.rdc` captures offline. See `README.md` for what it
does; nothing here is a supported product.

## Always run the checks after any code change

Any change to `src/py/` (or to `tests/`, or to `src/cpp/` — which has its own gate: a warning-free
`cmake --build build --config Release` and `clang-format-check`) is not finished until **both** of these pass:

```powershell
python src\py\rdc_analysis.py selftest    # whole suite, ~11 s: exit 0 = pass, 1 = fail, 2 = bad option
npx --yes pyright@latest                  # must print: 0 errors, 0 warnings
```

- `selftest -v` for per-test output, `selftest -k Draws` to run only matching test ids.
- A change that can move what a **command prints** (a decoder, a table, `draws`/`resources`/`deps`/`memory`/
  `dxbc`, a report section, the driver's own text) is not finished until the corpus agrees either: `python
  src\py\rdc_analysis.py goldens --check` (REFERENCE §4.17) diffs every transcript under `goldens/`, compares
  the labels *and* runs each capture's `driverCommands` through one `batch` session, diffing the driver's text
  too — about a minute on this machine (half of it the driver half, which needs the built exe and a GPU), and
  it exits 2 when the captures are not here, which means "not compared", not "clean". When the change is
  deliberate, `goldens --write` refreshes the transcripts and the driver texts, and **that diff is the
  review**: read it, then keep it. The labels (`*.expect.json`) are hand-written and `--write` never touches
  them.
- **The driver's half of that check has two rules that keep it from crying wolf, and one that keeps it
  honest.** The header's `renderdoc` field is the *installed* engine and is written `<engine>` (the version
  guard judges that number, not a golden); the driver's stderr reaches the golden as findings only, because
  its per-step log lines carry seconds and a timestamped log path. And a run that failed for the machine's
  reasons (no DLL, no replay system, no device, a capture from a newer engine) is "not compared" while any
  other failure is compared — but a failure with *no output* is reported and **never** written as an
  expectation: a golden of a crash would be a golden that says a crash is correct.
- **`--format csv|markdown` may not change an answer, only its shape.** The five row commands build their
  rows once and print either form from that list, so the terminal and the CSV cannot drift; in the non-table
  forms stdout is the table *alone* and the prose moves to stderr, and every cap (a limit, the top 40 chunk
  types, the first 120 markers) still applies. If a format needs a different selection, that is a different
  command, not a flag.
- **The driver refuses a capture from a newer RenderDoc than the engine it loaded** (`--dll` over
  `$RDC_RENDERDOC_DLL` over the installed one), before `InitialiseReplay` — both versions named, exit 1, no
  device created. It reads the container's 32-byte header itself because the replay API has no accessor for
  the recording version, and it does not guess when a version does not parse. Do not "fix" a refusal by
  lowering the comparison: an older engine answering from another version's decoding is the failure this
  exists to prevent (REFERENCE §9 has the tested matrix).
- **The corpus is generic, and a path is never committed.** A capture is a key (`desktop-1`, `desktop-2`,
  `mobile-1`: platform, then ascending size for a *new* key — a key re-pinned against a different file keeps
  its name, and the measurements elsewhere that name it stay statements about the file it held then) plus a
  SHA-256; where *this* machine keeps the file is
  `goldens/captures.local.json`, which `.gitignore` keeps out of the repository because a path is local state
  and a file name may name the frame it came from. `redact` is what enforces it: everything a run records (a
  transcript's command line, both streams, an A/B document's `capture` member) says `<capture>` where the path
  was. So no doc, comment or test writes a capture's path or file name either — refer to it by its key, and
  say "Unreal Engine" when the engine matters (it is a public codebase; a private one has no name here).
  `tests/test_rdc_goldens.py` checks that the checked-in goldens carry no path (and skips where the local file
  is absent, which is CI).
- **A capture's own strings are a separate question from its path, and redaction cannot touch them**: a
  transcript *is* the frame's words, and so is a driver's text, so a capture whose author has not said they
  may be published is in the corpus by identity only — no transcript, no label, no document,
  `commands: []` **and** `driverCommands: []`. `desktop-2` is that capture, and REFERENCE §4.17 records both
  the rule and the scan that verifies nothing of it is left in the tree.
- Other entry points, one file per area (`tests/rdc_testcase.py` holds what they share, and is not a test
  file — `discover` only collects `test_*.py`): `test_rdc_analysis.py` (container/compression/cache),
  `test_rdc_chunks.py` (chunk stream/payloads/shader containers), `test_rdc_resources.py` (resource
  table/heaps/enums), `test_rdc_commands.py` (commands/CLI), `test_rdc_report.py`, `test_rdc_notable.py`, `test_rdc_report_detectors.py`, `test_rdc_detect_state.py`, `test_rdc_detect_stream.py` (the report and its detectors, whose shared fixtures are
  `rdc_report_fixtures.py`),   `test_rdc_renderdoc_src.py` (the source-tree fetch), `test_rdc_validate.py`
  (schemas),   `test_rdc_scan.py` (the slice scan and the phase/progress instrumentation), `test_rdc_ab.py`
  (the marker trees, the bundle A/B and the PNG/pixel maths), `test_rdc_goldens.py` (the corpus harness:
  transcripts, labels, the bundle half — it runs the CLI as a subprocess, which is most of the 11 s), or all
  of them with `python -m unittest discover -s tests -t tests`.
- **The suite never reaches the network.** The source-tree fetch is the one place the tool downloads anything,
  and it writes into exactly one folder — `renderdoc-src` at the root, the one `rdc_renderdoc_src.target_dir()`
  names — so a test that hands a command its own tree gets a check and a fallback, never a download. That is why
  `tests/test_rdc_renderdoc_src.py` patches `target_dir` to its scratch root to exercise the fetch, and why the
  other files can call `load_chunk_names` with a temp folder without thinking about it.
- A test fixture that the detectors read — the chunk-name map, the cache directory — is patched on the
  module that *owns* it, and the capture the test builds must come from the same map. Building a capture from
  the entry module's copy while the code reads the owner's is how two stream-detector tests silently passed
  their setup and found nothing.
- A refactor of this kind is checked against the *real* captures, not only the suite: `report` over
  `desktop-1` must still print `6994 events / 104 passes / 4843 resources / 115 findings from
  21 detectors` and `engine   : Unreal Engine (108 concept(s) by name, 1 question(s))`, and the driver's text
  output must stay byte-identical. (6,994 is every id with bound state up to the frame's last event; the
  sweep that produced 16,857 also collected the clamped tail past it, which is gone since REFERENCE §9's
  sweep bound.)
- New behaviour needs tests in `tests/`; a bug fix needs a test that fails before the fix. A new **detector**
  needs a fixture bundle from `tests/rdc_report_fixtures.py` that makes it fire (and one that makes it not fire),
  because a detector that is only ever exercised by a real capture is a detector nobody can falsify.
- Never weaken, skip or delete an assertion to make a run pass. Tests pinning behaviour that looks wrong
  are marked `CHARACTERIZATION` — change code and test together, and say so.
- Report the real numbers in your reply (test count, pyright error/warning counts), not just "passes".

## Python coding guidelines

Enforced by `pyrightconfig.json` (`"typeCheckingMode": "standard"`) plus the test suite. Target:
**Python 3.8+, standard library only** (plus the optional `zstandard`), **one module per layer under
`src/py/`**, CLI behaviour frozen.

### Types — Pyright/Pylance "Standard", zero errors and zero warnings

- `from __future__ import annotations` at the top of every module; annotate **every** parameter and return.
- Modern annotation syntax (`dict[str, int]`, `X | None`, `Sequence[T]`) is allowed *because* of that future
  import — but nothing that needs 3.9+/3.10+ **at runtime** (no `match`, no evaluated `X | Y`, no `NotRequired`).
- `Optional[T]` for nullable values, never an implicit `None` default; `Sequence`/`Iterable` for read-only
  inputs; `TypedDict` for dict-shaped records (`CaptureInfo`, `SectionInfo`, `ChunkInfo`, `DxbcRow`).
- No `Any` in `rdc_analysis.py`; if one is genuinely unavoidable, comment why.
- No `# type: ignore` without a bracketed rule name and a reason.
- Optional dependencies stay lazily imported inside the function that needs them, with a stub in `typings/`
  so the checker can resolve the import.

### Style

- PEP 8; 4-space indent; ≤100 columns. Pre-existing long string literals that are part of the CLI output are
  exempt — reflowing them changes output.
- PEP 257 docstrings on every public function. The module docstring **is** the CLI help, so keep its
  `Usage:` block in sync with the dispatch in `main()`.
- stdlib imports at the top, sorted; nothing imported inside a function except optional third-party deps.
- `UPPER_SNAKE_CASE` constants, `snake_case` functions, no mutable default arguments, no bare `except:`.
  The one broad `except Exception` in `decode_chunk` is deliberate (a corrupt payload is reported, not
  raised) and must stay.
- Keep the payload-layout comments (REFERENCE §3.4): they are the reason the decoders can be trusted.

### Structure and behaviour

- The tool is a **package-shaped folder, not a package**: `src/py/` holds plain modules with no `__init__.py`,
  imported by name (`import rdc_chunkmap`). `src/py/rdc_analysis.py` is the entry point and re-exports every
  module (`from rdc_commands import *`), so `R.<anything>` keeps working for the tests and for scripts. Do not
  turn it back into one file, and do not add an `__init__.py`.
- **A module may only import modules beneath it**, and the layering is: `rdc_renderdoc_src` (the tree, and
  fetching it), `rdc_chunknames` (the bundled enum table: generated data, a leaf) and
  `rdc_driver` (the driver's binary against the sources it is built from, and the build that
  catches it up) → `rdc_types` → `rdc_chunkmap` → `rdc_stream` → `rdc_cache`/`rdc_dxbc` → `rdc_resources` →
  `rdc_payloads` → `rdc_commands` → `rdc_analysis`,
  and on the report side `rdc_bundle` → `rdc_detect_common` → `rdc_passes`/the detectors →
  `rdc_notable`/`rdc_recommend` → `rdc_report_render` → `rdc_report` → `rdc_analysis`, with the A/B chain
  beside it: `rdc_image` (a leaf) → `rdc_ab_render` → `rdc_ab` → `rdc_analysis`, and `rdc_passdiff` on the
  capture side, above `rdc_payloads`. Two more sit beside `rdc_commands`: `rdc_replay` (the driver as a
  library, `ctypes` over `bin/rdc_replay.dll`; a leaf beyond `rdc_driver`) and `rdc_sweep` (a folder of
  captures, above `rdc_replay`/`rdc_schemas`), each imported by `rdc_analysis` by name rather than star-wise
  because its public surface is one `cmd_*`. A cycle breaks
  `from X import *` at import time (a partially initialised module exports only what it has defined so far), so
  put a shared helper *below* the modules that need it instead of importing upwards — that is why `_name_suffix`
  lives in `rdc_resources` and the loaders in `rdc_cache`.
- **The bundled enum table is generated, and the tree wins over it.** `src/py/rdc_chunknames.py` is written by
  `chunk-names --write` from a RenderDoc source tree and carries that tree's version; `load_chunk_names` lays it
  down as the floor and writes the tree's enums over it, so a tree that is present decides the spelling and a
  machine without one still prints names (with a warning naming the table's version). Never edit the table by
  hand: regenerate it when a tree moves on and keep the diff — `TestTheCheckedInTable` in
  `tests/test_rdc_renderdoc_src.py` fails until you do (it skips where no tree is on the machine, which is CI,
  the same rule `goldens --check` has).
- **A rule the report prints is a rule the document carries.** The notable ranking, its inputs and the severity
  each detector's findings are grouped by are in `report.json` (`notables`, `severityTable`), not baked into the
  renderer: the renderer lays them out and joins the flags to their severity by `detector` alone. Adding a
  detector means adding a line to `DETECTOR_SEVERITY` and a recipe to `DETECTOR_RECIPE` in `rdc_detect_common`,
  and a test walks the run list against the table so one cannot arrive without the other.
- **A notable list never turns "I do not know" into a fact.** `read by 0 passes` is printed only for a resource
  the engine *tracked*; an empty usage chain, the `eid 0, Unused` marker and a UAV row are each reported as what
  they are, because all three have been read as "nothing reads it" by mistake at least once. The units are the
  table's own, too: bytes for a buffer, pixels for a texture, and a stated `~` wherever one has to be estimated.
- **A shared helper is called through its owner** (`rdc_chunkmap.load_chunk_names(...)`), never by the bare name
  a star import copied — the qualified form is what makes an override, or a test's `mock.patch.object`, reach the
  code that uses it, and it keeps the dependency visible at the call site. The tests patch the owner for the same
  reason (`chunkmap.load_chunk_names`, `cache.cache_dir`, `resources.load_format_names`).
- Keep the documented public names (`parse_container`, `iter_chunks`, `decode_chunk`, `load_chunk_names`,
  `load_format_names`, `parse_resource_table`, `parse_descriptor_heaps`, `parse_root_signatures`, `parse_rdef`,
  `shader_bind_names`, `load_stream`, `cache_dir`, `cache_lookup`, `cache_store`, `DrawState`, `ResourceInfo`,
  `DescriptorInfo`, `RootSignature`, `RootParam`, `cmd_*`, `RENDERDOC_SRC`, `MARKER_CHUNKS`, `CHUNK_*`, ...) —
  tests, docs and scripts reference them.
- Descriptor resolution stays honest: only slots the capture actually wrote are recorded, a write or copy
  replaces what the slot held, and anything unknown is reported as the heap — never guessed at. See
  `parse_descriptor_heaps` and REFERENCE §4.10/§8.
- Root parameters stay honest the same way: `draws` annotates `rpN` with what the *signature* says (type,
  register, space, visibility, §3.4) and appends a name only when the capture's `RDEF` reflection offers one
  that every stage agrees on. Never invent a name, and never drop the annotation back to a bare index — an
  index alone is what produced the wrong conclusion in REFERENCE §9.
- The driver's `psoKind` is the *call kind*, and everything downstream groups by it: it comes from the
  capture's action tree (a `Dispatch*` chunk or not; an event that is not a call takes the kind of the call it
  follows), **never** from the bound shaders. Measured: `desktop-1` has a compute shader bound at every
  one of its 1,186 events, so the shader-based guess called the whole frame compute and the report grouped a
  frame of draws into compute passes. A wrong value here is wrong everywhere downstream.
- The replay driver (`src/cpp/replay_dump.cpp`, REFERENCE §9) keeps the same rule: it prints what the engine returns and
  nothing else. It must keep doing the three things a replay host has to do — `REPLAY_PROGRAM_MARKER()` at file
  scope, `RENDERDOC_InitialiseReplay()` before opening, `RENDERDOC_ShutdownReplay()` on the way out — or it
  dies inside `OpenCapture` with no diagnostic at all. Its output is unbuffered on purpose, so a crash still
  leaves the output that was already produced, and `$RDC_REPLAY_DEBUG=1` traces each step on stderr.
- **The driver says so in its log when it is older than the sources it was built from**
  (`WarnIfDriverIsStale` in `src/cpp/replay_dump.cpp`, over `FileWriteTime`/`NewestSourceTime` in
  `capture.cpp`): a replay host answers from the code it was compiled with, so a stale exe is
  indistinguishable from a current one from the outside — its answer looks exactly like an answer — and that
  has already cost this repository a whole verification pass. The other half is
  `python src\py\rdc_analysis.py build [--check]` (`rdc_driver.py`), which compares **both** artefacts the
  build writes — the exe against `src/cpp/*.cpp|h` and `bin\rdc_lz4.dll` against `src/cpp/third_party/lz4` —
  because the library *is* the offline tool's decoder, so a stale one decodes every capture with the code its
  source no longer says. The two verdicts are independent (neither tree is the other's input) and the driver's
  own warning stays about the exe, which is all it loads. Because a running image cannot be
  overwritten on Windows: the link that would replace `bin\replay_dump.exe` fails with `LNK1104` while that
  same exe is what is running. Do not turn the warning into an error — running an old build on purpose is how
  a bundle from the previous revision gets reproduced — and do not make the check a *test*: whether a binary
  has been built is the state of a working tree, not a property of the tool.
- The state document's `rootParameters` array is rows, and the binding rules read them by shape: `rpN reg=R
  space=S vis=<stages> <target>` (the parameter as set; `vis=` is absent in older bundles and then means every
  stage), and, for a *set* table, `rpN <letter><reg> s<space> cat(N) type(N) <res…|none>` per resolved slot.
  `cat` is the range's declared category and `type` is the heap slot's own descriptor type; the mismatch rule
  compares those two and **never** the reflection's letter against a row of a different letter -- `b0` and
  `t0` are separate register spaces, and that mistake cost ~60 false positives on a real capture.
- Never assume an event id is a chunk index: `probe` is the authority (`REFERENCE.md` §9), and since
  2026-09-17 `draws` prints the engine's own ids too (`ActionDescription::eventId`, from `GetRootActions`),
  which is the numbering `SetFrameEvent`, `probe` and a bundle all use. The offline tool's `chunks`/`summary`
  keep their own chunk numbering; the two agreed on the Unreal captures and do not on `desktop-2`,
  and a wrong id silently returns an *empty* state rather than failing.
- A **marker path** is available with the ids: every event carries the markers it sits inside, written by
  `state`/`shaders`/`cb` as a `marker` field and by `dump` into `events.json`, and a bundle older than that
  member simply has none (read it with a default, never by index). Matching is by the *name inside* a path
  (`A > B` answers for `B`), because paths carry dynamic text no table could list.
- **`--at-marker` supplies the event id *instead of* the positional one, and it is an error to give both.**
  `DispatchCommand` counts arguments against `MinArgs` to decide that, because the dispatcher cannot tell a
  missing id from a command's next positional: the version that erased the slot to make room took that
  positional with it (`image --at-marker X out.bmp` ran `image X` for a while, `cb --at-marker X ps 0` and
  `statediff --at-marker X 900` likewise). A new command whose id comes first needs a `MinArgs` line, and
  `pixelhistory` is the case that makes the point: a marker path resolves to a pass's *first* call, so
  `--at-marker <pass>` answers up to the start of it -- the pass's last eid is what asks about its end.
- The driver must not specialise RenderDoc's function templates (`DoStringise<...>`). RenderDoc defines them in
  its own `stringise.cpp`, unreachable from our translation unit
  (`[ifndr:temp.expl.spec.unreachable.declaration]`, `[basic.def.odr]`); use local, distinctly-named helpers
  instead, and read `ResultDetails` through its public `internal_msg`/`code` members rather than `Message()`.
  Anything that looks like it needs a specialisation to link is a reason to stop calling the header inline that
  pulled it in, not to supply the definition.
- The driver's output writer has two rules that are load-bearing for `--json`: every string that reaches the
  output goes through `JsonEscape` (a Windows path in `capture` broke every document once), and a separator is
  written *before* each item after the first, never after a last one — so a field which may be the object's
  last must say so with its `last` argument. After changing any command, validate:
  `bin\replay_dump.exe <cmd> "<capture>" --json | python -m json.tool`. Text-mode output is the contract for
  the offline tool's users: it must stay byte-identical unless the change is deliberate and recorded.
- Layout, so new code has an obvious home. Python: `src/py/rdc_types.py` the shapes and their constants,
  `rdc_profile.py` the phase timer and the progress lines (bottom of the layering, because every layer calls
  it), `rdc_chunkmap.py` the chunk-name enums, `rdc_stream.py` the container and the frame stream, `rdc_cache.py` the
  stream cache and the loaders, `rdc_scan.py` the slice scan over a whole stream, `rdc_dxbc.py` the shader containers, `rdc_resources.py` the resource table and
  everything read out of it (formats, heaps, root signatures, `RDEF`), `rdc_payloads.py` the chunk payload
  decoders, `rdc_commands.py` the commands, `rdc_report.py`/`rdc_bundle.py`/`rdc_passes.py`/`rdc_detect_*.py`/
  `rdc_report_render.py` the report, `rdc_passdiff.py` the two captures' marker trees, `rdc_image.py` the PNG
  reader/writer and the pixel maths (a leaf, like `rdc_profile`), `rdc_ab.py`/`rdc_ab_render.py` the A/B of
  two bundles and its Markdown, `rdc_engine_schema.py` the engine-name interpretation (`engine-schemas/`),
  `rdc_schemas.py` the JSON contract, `rdc_goldens.py` the capture corpus and its transcripts/labels
  (REFERENCE §4.17; a harness beside `rdc_driver.py`, not a test), `rdc_analysis.py` the CLI that re-exports
  them all. C++: `src/cpp/replay_dump.cpp` the entry point, `common.h` the modules' shared declarations,
  `text.cpp`/`output.cpp` the printing, `capture.cpp` the engine session, `actions.cpp` the action tree,
  `commands_frame.cpp`/`commands_state.cpp` the commands by area, `commands_image.cpp`/`commands_patch.cpp`
  the frame's pictures and the shader substitution, `image.cpp` the BMP/difference/hash maths, `bundle.cpp`
  the bundle producer, `selftest.cpp`, and `schema.cpp`/`schema.h` for the schema table — *data only*, because
  the printing, writing and checking need the tool's `Fail`/log plumbing.
- **Split by what never changes together, not by size.** The Python split was done by cutting the original file
  at its own layer boundaries (and in dependency order: types → chunk-map → stream → cache/DXBC → resources →
  payloads → commands → CLI); the C++ split moved whole families out (`text`, `output`, `capture`, `actions`,
  the two command files, `bundle`, `selftest`) and touched no logic. A new module belongs beside the layer it
  serves, and its imports must point *down* the layering (see the Python structure rules above).
- A capture path with a space in it (`desktop-1`) needs its quotes **inside** the argument:
  `Start-Process -ArgumentList` joins with spaces and does not quote, so `@('dump', $rdc)` arrives as
  `renderdoc-src\PC` and the run dies in under a second with `cannot open ...\renderdoc-src\PC`. Write it as
  `"`"$rdc`""` (or `'"' + $rdc + '"'`) — and read the driver's own stderr/log, which says exactly this. Two
  harness scripts in `build/` have now hit it.
- `dump`'s sweep cost is set by the id *range* it walks, and the range is capped by the capture's **chunk
  count**, not by `--until` (the log prints `sweeping ids A..B for bound state, at most N id(s) (the file's
  chunk count)`). For "look at a few events' state or reflection", use **`batch`** instead: one open capture,
  many commands (~20 s for 26 commands) against minutes for a bundle sweep.
- **A command moves the replay through `MoveToEvent(ctrl, eid)`, never `ctrl->SetFrameEvent` directly**
  (`capture.cpp`; there is one call left in the program, inside it). It sets the one bit `AnyEventReplayed()`
  reads, and `probe` is the command that needs it: its answer is only true on a *cold* engine, so a
  non-first probe warns on stderr and its answer is deliberately **not** cached (REFERENCE §9). A new
  command that calls `SetFrameEvent` itself silently breaks both.
- **`bin/rdc_replay.dll` is the same code as the exe, as a library**, and a change to `src/cpp` is a change to
  both (they share the source list in `CMakeLists.txt`, so `build --check` compares both against it). The
  ABI is `src/cpp/api.h`; the Python caller is `src/py/rdc_replay.py`; the first in-repo caller is `sweep`
  (REFERENCE §4.21). Two rules go with it: the **replay system is the process's** and is initialised once
  (`api.cpp`'s `Process()` — a session owns a capture and its controller, nothing more; doing it per session
  crashed every session after the first), and **one session means one batch file**: the library produces the
  same text as a `batch` run of the same lines, byte for byte, and never promises a fresh engine per command
  (REFERENCE §9).
- **Paths are `std::filesystem::path`, and every filesystem operation is `std::filesystem`'s** — with the
  `error_code` overloads, always: an exception must never leave a helper, and a `filesystem_error` out of a
  directory walk is a crash for a file that went away between two calls. A `std::string` is how a path is
  *printed* (`.string()`/`.generic_string()` at the `printf`), never how one is taken apart, joined, made
  relative or resolved; the one place a path becomes a `FILE *` is `FileOpen`. What stays Win32, and why:
  `GetModuleFileName` (a module, not a file), `CaptureStdout`'s `_dup2`, and `_wfopen` inside `FileOpen`
  (REFERENCE §9 has the whole account, including the encoding limit that is deliberately unchanged).
- **A bundle is local state: never commit one, and never let one be written where it can be committed.** It
  belongs to one `.rdc` on this machine, and it records that capture's **absolute path** (`capture.json`'s
  `absPath`), so a bundle in `git status` is a leak rather than an artefact. The destinations that need no
  argument are the ones to watch — `dump <rdc>` writes `bundle/` in the working directory, `sweep <dir>`
  writes `bundles/` — and both are in `.gitignore`, with a test that keeps them there
  (`tests/test_rdc_sweep.py`). A destination you pass yourself is yours to ignore. Two ways one has landed
  outside those names: a `dump` line whose path was **unquoted** (a key with a space becomes two arguments,
  and `dump` takes the last positional as its destination — that is how 257 files once appeared in
  `Renderer/` at the repository root), and a relative path resolved against whatever the working directory
  happened to be. Both are why `rdc_sweep.dump_line` quotes and why the sweep checks for the manifest it
  asked for rather than trusting the exit code.
- Every `--json` document carries `schemaVersion` (REFERENCE §9, and §4.12 for the validator), and the schema for it lives in the
  driver's `kSchemas` table. A new document, or a new member on an existing one, updates that schema **and**
  the checked-in `schema/` folder in the same change (`replay_dump schema --out schema`, then
  `replay_dump schema --check schema` — which fails a run when the two have drifted): the schemas are
  `additionalProperties: false`, so a document that drifts from its schema fails `validate` — which is the
  point, and only works if both halves move together. `selftest` covers the writer's own helpers (escaping,
  separators, balance) and the schema table's self-consistency; the *documents* are the offline validator's.
- The frame report (`report`, REFERENCE §4.11) is deterministic *by contract*: byte-stable for a fixed bundle —
  sorted tables, no timestamps, no paths in the prose — because that is what lets two runs be diffed and
  `tests/test_rdc_report.py` pin the document. A change that alters those bytes is deliberate or it is a bug.
  Its "what this report cannot tell you" section names what is not implemented yet (REFERENCE §4.11): whatever lands
  there must update that section in the same change, or the report starts lying about its own coverage.
- **An A/B is only as honest as the rule that made each match.** `replaydiff` (§4.16) aligns two passes by
  marker path, then by innermost name, then by the resources both write, then by call order *within one call
  kind*; every fallback writes itself into the row's note, so a reader can disagree with a match instead of
  having to guess how it was made. `passdiff` offers a rename only for a pair in the same slot, under the same
  parent marker, with the same call count, and says the file cannot prove a rename — the version built on
  position and call count alone suggested nineteen renames between two frames that share almost no passes. A
  bundle's `shaders` identity is the `hash` the driver stamps (a size is not an identity: two shaders can share
  one), and a bundle from before the hash says `no-hash-in-this-bundle` rather than calling equal reflection the
  same shader. **An image nobody looked at is not an unchanged image**: pairs are compared by bytes, decoded
  while `--image-detail` lasts, and the rest are named as differing with the reason they were not compared;
  decoding is pure Python at ~0.15 s/MB (`rdc_image`), which is why the cap exists and why the number is in the
  document.
- A marker's name offline is read from a run of at least `rdc_passdiff.MARKER_MINLEN` (3) printable
  characters, because a marker payload's own frame decodes as one- and two-character runs *before* the name
  does (measured on both real captures: `B` where the name is `MobileSceneRender`). `markers`/`summary` have
  always used the same floor; a marker genuinely called `A` reads as `<unnamed>` and that is the cost.
- The engine vocabulary (`engine-schemas/*.json`, `rdc_engine_schema.py`) keeps three rules, and they are what
  make it worth reading: a concept is claimed **only** because the capture contains a name the table lists (a
  constant block, a shader entry point, a resource, a marker, a pass *structure* string), every claim carries
  the name and the place it came from in the same row, and a frame whose names do not match gets **no**
  interpretation with the reason printed. Do not add value-based or timing-based inference, do not let a
  concept match on "some of the kinds it asks for" (a block that is merely bound in a pass is a leftover
  binding as often as a fact — the match is a conjunction), and do not read a value the table does not tag: a
  member's number is printed with the eid its document was written at, because a value without a place is not
  evidence. A member read while **nothing was bound** to its block is not a value: the engine returns every
  member's default, and the row says `*not bound*` rather than passing a zero off as data. The pilot — the
  mobile-vs-PC GI question answered in those words — is documented in REFERENCE §4.11 and pinned by
  `tests/test_rdc_engine_schema.py`.
- The writer's separator state nests with the arrays: `ArrayOpen` saves the enclosing array's `g_firstRow` and
  `ArrayClose` restores it. Without that, an *empty* nested array leaves the enclosing one looking like it had
  just started, the next item is written with no comma, and the document does not parse — which is exactly how
  `states/<eid>.shaders.json` came out invalid for `desktop-2` (a stage whose signature arrays were
  empty). Validate a writer change with a parse **and** a duplicate-key check.
- Undefined behaviour and IFNDR are treated as bugs here: no `memcpy` out of a class without a
  `static_assert` that it is trivially copyable and the right size, no signed overflow in size arithmetic
  (compute in `size_t` and check the product), no out-of-range `static_cast` to an enum without a fixed
  underlying type, no pointer arithmetic past the end of a buffer, and no unbounded recursion over
  engine-supplied data. The two annexes in the parent folder (`cpp-annex-F-ub-core-undefined-behavior.md`,
  `cpp-annex-G-ifndr-ill-formed-no-diagnostic-required.md`) are the checklist.
- `draws` reports the D3D12 command-list state in effect at each call (see `DrawState`): bindings survive
  `SetPipelineState` and draws, `Reset()` clears them, and only a *changed* root signature invalidates root
  arguments. Do not reintroduce a per-draw or per-PSO reset, and keep the state keyed per command list.
- CLI parsing stays hand-rolled: `main()` prints the module docstring for a missing/unknown command and lets
  `IndexError`/`ValueError` escape for bad arguments. Do not replace it with `argparse`. Commands that take no
  capture path (`selftest`, `cache`) are dispatched before the `len(argv) < 3` check.
- Behaviour is the contract. The quirks in REFERENCE §8 (6-character string floor, `first=0x-1`, the
  `decode_chunk` field labels, ...) are pinned by tests and must survive a refactor; change them only as a
  deliberate, separately-tested fix.
- Do not re-add a command that reconstructs *frame data* replay hands over directly (uniform values, shader
  signatures, which shader reads what, root-constant values): `float`, `pattern`, `sig`, `rootconst` and
  `report` were removed for exactly that reason, and the `dxbc`/`dump-shaders` GI-string harvest with them.
  The tool stays on what replay does not expose — the file's structure and the command stream.
- The disk cache must stay invisible: it may only change the `, cached` marker in a method label, never what a
  command prints. Do not add a cache-related output line, and keep the tests hermetic — `TempDirCase` points
  `RDC_CACHE_DIR` at a scratch directory, so nothing writes to the real user cache.
- No new runtime dependencies; `tests/` uses the stdlib only.

## Conventions

- Docs: `README.md` (setup, quick start, the playbook for an agent), `REFERENCE.md` (internals, the command
  reference, examples, pitfalls, the driver), `ROADMAP.md` (unimplemented work).
- **The scope is one machine, and the roadmap tracks only that** (2026-09-22): a `.rdc` on this machine, the
  offline Python, the driver exe, and the `renderdoc.dll` installed here. Work that would need a replay server,
  a capture from another API, a DLL inside the captured application, or a program of our own to feed the
  shaders is out of scope; `ROADMAP.md` names each of those with its reason, and none of them comes back as an
  item until the thing it needs is here. When proposing work, prefer the item that *extracts* an answer the
  engine already has, or decodes a payload the offline parser's siblings already decode.
- Never commit `renderdoc-src/` — upstream code fetched on demand, gitignored (`src/py/rdc_renderdoc_src.py`).
- Never run `git commit` or `git push` unless explicitly asked to.
- **An engine call's cost is measured, not assumed.** `$RDC_PROFILE=1` prints a per-call-site breakdown at the
  end of a driver run (REFERENCE §9 has the numbers): on the 1.4 GB capture `SetFrameEvent` is 83% of a bundle
  dump at 47 ms a call, and it costs the same whether the id changes or not. Anything that looks like redundant
  engine work there has been tried, measured and reverted — **the bundle's second state read per event is
  load-bearing** (a forward step gives an incomplete state; only a backwards one makes the engine replay the
  frame from its start), which is why `REFERENCE §9` carries the hashes of the bundles that proved it. Change
  it only with a byte-identical-bundle check against a cold run.
- **A change that alters the host's timing is not content-neutral** (measured 2026-09-18). Buffering the
  bundle's document writes took a 300-event dump from 36.5 s to 30.1 s and moved two files of the bundle:
  the engine's answer depends on how fast the host gets back to it. A document written *between* engine calls
  stays unbuffered; one written after the last `SetFrameEvent` may be buffered (`SetDocumentBuffering`, which
  the bundle writer turns on after the events loop — `resources.json`'s 3.8 s of syscalls becomes 0.1 s with
  the manifest still byte-identical). A driver change is checked against a bundle from the *previous* build,
  not against a fresh run of itself, and `CaptureStdout` carries the numbers.
- **Every call that costs real time has a `$RDC_PROFILE` slot, or it will hide.** `shader_bind_names` was
  1.2-1.7 s of a 2.6 s `draws` and invisible in the table for an hour; the scan itself is at the primitive's
  own rate (one `find` pass over the stream at 1.2 GB/s, and a map is not slower than `bytes` for it), so what
  the slot buys is not an optimisation but the ability to see that the cost is real and where it sits. A long
  call with no slot is a measurement that cannot be taken.
- **The offline tool maps rather than reads** (REFERENCE 4.14): the container and the cached stream are
  `mmap`s, which is 0.5-1.1 s and ~2 GB of allocation per command. A stream is an `rdc_types.Buffer` (bytes,
  bytearray or map -- never a view, so `chunk_strings` can still call `.decode`); a *slice* of any of them is
  `bytes`/`bytearray`, and `BufferLike` is the wider shape the `u16/u32/u64` primitives accept. A mapped file
  is locked against writing on Windows, so a test that rewrites a capture releases the map first, and the
  stream cache's format is versioned (v2 pads the header so the stream starts on an `mmap` boundary).
- **The JSON writer has no fractional field, on purpose.** A `double` overload of `Field` was tried and made
  every existing `Field(key, 0)` ambiguous -- an `int` converts to both `long long` and `double`, so it is a
  compile error in twenty call sites rather than a wrong number in one. A fractional value is written with
  `Fmt("%.3f", ...)` and typed as a string in its schema. A bare `const char *` is the other half of the same
  trap: it converts to both `string_view` and `rdcstr`, so wrap it in `std::string(...)` (which is why the rest
  of the driver does).
- **Synthesised pictures live in `image.cpp`** -- BMP in and out, thumbnails, a montage, a difference and a
  difference hash -- and are tested device-free in `selftest.cpp`, because a contact sheet is a picture nobody
  can check by reading the code. `WriteBMP` is the writer the bundle's `rt/` images go through, so its bytes are
  a contract there too: the bundle gate covers it, and a reader must round-trip what it writes.
- **`pixelhistory` answers with the engine's verdicts *and* their evidence, and says when a verdict is not a
  fact.** It refuses three things rather than faking them: a capture whose driver does not support pixel history
  (`APIProperties.pixelHistory`, printed by `info`); a pixel outside the texture (the engine answers an
  out-of-range pixel with an *empty* list, which is what "nothing wrote it" looks like); and an empty list the
  engine's own source explains -- a texture whose format it cannot read (`D3D12Replay::PixelHistory` returns
  before doing anything). An empty answer that *is* an answer carries its evidence: `usagesUpTo`, the events up
  to the scope that touch the texture at all. Every row prints the value before, from and after the fragment,
  because one verdict is not a closed case on D3D12: the `sample masked` test is an instrumented re-draw
  (RenderDoc marks the flag `TODO: figure out if we always need to check this`), and measured on desktop-2's
  1-sample targets every base-pass fragment is flagged while one of them carries a changed `postMod`
  in the same row -- so the document's `note` (printed as one of the header's key/value lines, in both formats)
  says which two members to compare.
  The vocabulary those rows are phrased in (`CastFromName`/`CastText`, `PixelValueText`, the
  `Modification*Text` family, `RejectionText`) is in `commands_state.cpp` and pinned device-free in
  `selftest.cpp`: a reason missing from the list a verdict is built from, or a value printed from the
  `0xdeadbeef` "invalid" sentinel, is a wrong answer that reading the code does not reveal.
- **The same rule on the offline side**: `$RDC_PROFILE=1` prints a phase table and `$RDC_PROGRESS=1` the live
  progress lines, both to **stderr** (stdout is the contract, and a command's stderr stays empty otherwise).
  The phase names are the `rdc_profile.timed(...)` decorators on the layers — one name per call site, so a
  command that calls a layer twice shows two calls in one slot — and a long loop ticks a `Progress`, which is
  time-based on purpose (a count-based line once stayed silent for a whole 164 s sweep).
- **A whole-stream scan is split across processes, because nothing else can help it**: `re` holds the GIL for
  the length of the call, so threads take turns, and there is no I/O for async to overlap. Three rules come
  with `rdc_scan`: a slice boundary is only legal where no match can cross it (a non-printable byte); the
  **serial path must stay exactly as fast as the loop it replaced** (the first version allocated a tuple per
  match and made it twice as slow — the parallel path hid it, which is why the numbers are checked with
  `procs=1` too); and anything that starts a pool must be importable without module-level side effects,
  because a worker re-imports `__main__` (see REFERENCE §4.13).
- **A measured number is quoted as a ratio within one session.** The same scan of the same stream measured
  14.5 s and 25.2 s an hour apart on this machine. An absolute number in a doc, a comment or a commit message
  is a liability unless it says what it was measured against; the pairwise tables in REFERENCE §4.13 and §9
  say so.

# Naming

* **Types**: `UpperCamelCase` (nouns)
* **Functions**: `UpperCamelCase` (nouns)
* **bool**: `b*` (e.g. `bIsValid`)
* **Local vars**: `lowerCamelCase`
* **Members**: `m_UpperCamelCase`
* **Static members**: `ms_UpperCamelCase`
* **Constants**: `SCREAMING_SNAKE_CASE` or `kLowerCamelCase`
* **Globals**: `g_UpperCamelCase`
* **Static globals**: `gs_UpperCamelCase`

- Avoid `auto` (except for container iterators)
- Max line length: 140 columns
- Prefer `std::string_view` over `const std::string&`
- Use `const char*` for guaranteed hardcoded string inputs

**How the two prefixes compose, and where the section applies.** This is for `src/cpp/`; the Python side is
PEP 8, where `m_`/`b` names would be wrong. Order is scope first, then bool: a global bool is `g_bJson`, a
struct's bool member is `m_bCall`, a function- or file-scope static is `gs_bWalkedMarkers`. A data member of a
struct or class takes `m_` (`ActionNode::m_Eid`), and the *same word* used as a parameter does not -- `depth`
stays `depth` in the walk that fills `m_Depth` -- which is the one thing a mechanical rename gets wrong: it
cannot tell a member from a local of the same name, so an access rename has to be aimed at the variable
(`row.m_Name`) and never at the word. `auto` is kept only where there is no type to write: a lambda, or the
element type of a container the engine declared. A path that reaches `fopen`/`CreateDirectoryA` stays
`const char*`/`const std::string &` rather than a view, because those want a NUL-terminated string.

**Text output is the contract this refactor had to keep**: `draws`, `state`, `shaders` and `cb` over
`desktop-1`, text *and* `--json`, were captured before the rename and diffed after it, and
came out byte-identical. Do the same for the next one: `batch` a command list into a file, rename, rebuild,
diff.