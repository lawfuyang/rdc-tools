# AGENTS.md

Personal, vibe-coded tool for debugging RenderDoc `.rdc` captures offline. See `README.md` for what it
does; nothing here is a supported product.

## Always run the checks after any code change

Any change to `src/py/` (or to `tests/`, or to `src/cpp/` — which has its own gate: a warning-free
`cmake --build build --config Release` and `clang-format-check`) is not finished until **both** of these pass:

```powershell
python src\py\rdc_analysis.py selftest    # whole suite, ~7 s: exit 0 = pass, 1 = fail, 2 = bad option
npx --yes pyright@latest                  # must print: 0 errors, 0 warnings
```

- `selftest -v` for per-test output, `selftest -k Draws` to run only matching test ids.
- Other entry points, one file per area (`tests/rdc_testcase.py` holds what they share, and is not a test
  file — `discover` only collects `test_*.py`): `test_rdc_analysis.py` (container/compression/cache),
  `test_rdc_chunks.py` (chunk stream/payloads/shader containers), `test_rdc_resources.py` (resource
  table/heaps/enums), `test_rdc_commands.py` (commands/CLI), `test_rdc_report.py` (report, notables,
  recommendations, detectors), `test_rdc_renderdoc_src.py` (the source-tree fetch), `test_rdc_validate.py`
  (schemas), or all of them with `python -m unittest discover -s tests -t tests`.
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
  `renderdoc-src\PC Renderer.rdc` must still print `2132 events / 47 passes / 493 resources / 63 findings from
  20 detectors` and `engine   : Unreal Engine (31 concept(s) by name, 1 question(s))`, and the driver's text
  output must stay byte-identical.
- New behaviour needs tests in `tests/`; a bug fix needs a test that fails before the fix.
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
  fetching it) → `rdc_types` → `rdc_chunkmap` → `rdc_stream` → `rdc_cache`/`rdc_dxbc` → `rdc_resources` →
  `rdc_payloads` → `rdc_commands` → `rdc_analysis`,
  and on the report side `rdc_bundle` → `rdc_detect_common` → `rdc_passes`/the detectors →
  `rdc_notable`/`rdc_recommend` → `rdc_report_render` → `rdc_report` → `rdc_analysis`. A cycle breaks
  `from X import *` at import time (a partially initialised module exports only what it has defined so far), so
  put a shared helper *below* the modules that need it instead of importing upwards — that is why `_name_suffix`
  lives in `rdc_resources` and the loaders in `rdc_cache`.
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
  follows), **never** from the bound shaders. Measured: `PC Renderer.rdc` has a compute shader bound at every
  one of its 2132 events, so the shader-based guess called the whole frame compute and the report grouped a
  frame of draws into compute passes. A wrong value here is wrong everywhere downstream.
- The replay driver (`src/cpp/replay_dump.cpp`, REFERENCE §9) keeps the same rule: it prints what the engine returns and
  nothing else. It must keep doing the three things a replay host has to do — `REPLAY_PROGRAM_MARKER()` at file
  scope, `RENDERDOC_InitialiseReplay()` before opening, `RENDERDOC_ShutdownReplay()` on the way out — or it
  dies inside `OpenCapture` with no diagnostic at all. Its output is unbuffered on purpose, so a crash still
  leaves the output that was already produced, and `$RDC_REPLAY_DEBUG=1` traces each step on stderr.
- The state document's `rootParameters` array is rows, and the binding rules read them by shape: `rpN reg=R
  space=S vis=<stages> <target>` (the parameter as set; `vis=` is absent in older bundles and then means every
  stage), and, for a *set* table, `rpN <letter><reg> s<space> cat(N) type(N) <res…|none>` per resolved slot.
  `cat` is the range's declared category and `type` is the heap slot's own descriptor type; the mismatch rule
  compares those two and **never** the reflection's letter against a row of a different letter -- `b0` and
  `t0` are separate register spaces, and that mistake cost ~60 false positives on a real capture.
- Never assume an event id is a chunk index: `probe` is the authority (`REFERENCE.md` §9), and since
  2026-09-17 `draws` prints the engine's own ids too (`ActionDescription::eventId`, from `GetRootActions`),
  which is the numbering `SetFrameEvent`, `probe` and a bundle all use. The offline tool's `chunks`/`summary`
  keep their own chunk numbering; the two agreed on the Unreal captures and do not on the hobby-renderer one,
  and a wrong id silently returns an *empty* state rather than failing.
- A **marker path** is available with the ids: every event carries the markers it sits inside, written by
  `state`/`shaders`/`cb` as a `marker` field and by `dump` into `events.json`, and a bundle older than that
  member simply has none (read it with a default, never by index). Matching is by the *name inside* a path
  (`A > B` answers for `B`), because paths carry dynamic text no table could list.
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
  `rdc_chunkmap.py` the chunk-name enums, `rdc_stream.py` the container and the frame stream, `rdc_cache.py` the
  stream cache and the loaders, `rdc_dxbc.py` the shader containers, `rdc_resources.py` the resource table and
  everything read out of it (formats, heaps, root signatures, `RDEF`), `rdc_payloads.py` the chunk payload
  decoders, `rdc_commands.py` the commands, `rdc_report.py`/`rdc_bundle.py`/`rdc_passes.py`/`rdc_detect_*.py`/
  `rdc_report_render.py` the report, `rdc_engine_schema.py` the engine-name interpretation (`engine-schemas/`),
  `rdc_schemas.py` the JSON contract, `rdc_analysis.py` the CLI that re-exports
  them all. C++: `src/cpp/replay_dump.cpp` the entry point, `common.h` the modules' shared declarations,
  `text.cpp`/`output.cpp` the printing, `capture.cpp` the engine session, `actions.cpp` the action tree,
  `commands_frame.cpp`/`commands_state.cpp` the commands by area, `bundle.cpp` the bundle producer,
  `selftest.cpp`, and `schema.cpp`/`schema.h` for the schema table — *data only*, because the printing, writing
  and checking need the tool's `Fail`/log plumbing.
- **Split by what never changes together, not by size.** The Python split was done by cutting the original file
  at its own layer boundaries (and in dependency order: types → chunk-map → stream → cache/DXBC → resources →
  payloads → commands → CLI); the C++ split moved whole families out (`text`, `output`, `capture`, `actions`,
  the two command files, `bundle`, `selftest`) and touched no logic. A new module belongs beside the layer it
  serves, and its imports must point *down* the layering (see the Python structure rules above).
- A capture path with a space in it (`PC Renderer.rdc`) needs its quotes **inside** the argument:
  `Start-Process -ArgumentList` joins with spaces and does not quote, so `@('dump', $rdc)` arrives as
  `renderdoc-src\PC` and the run dies in under a second with `cannot open ...\renderdoc-src\PC`. Write it as
  `"`"$rdc`""` (or `'"' + $rdc + '"'`) — and read the driver's own stderr/log, which says exactly this. Two
  harness scripts in `build/` have now hit it.
- `dump`'s sweep cost is set by the id *range* it walks, and the range is capped by the capture's **chunk
  count**, not by `--until` (the log prints `sweeping ids A..B for bound state, at most N id(s) (the file's
  chunk count)`). For "look at a few events' state or reflection", use **`batch`** instead: one open capture,
  many commands (~20 s for 26 commands) against minutes for a bundle sweep.
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
  `states/<eid>.shaders.json` came out invalid for the hobby capture (a stage whose signature arrays were
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
- Never commit `renderdoc-src/` — upstream code fetched on demand, gitignored (`src/py/rdc_renderdoc_src.py`).
- Never run `git commit` or `git push` unless explicitly asked to.
- **An engine call's cost is measured, not assumed.** `$RDC_PROFILE=1` prints a per-call-site breakdown at the
  end of a driver run (REFERENCE §9 has the numbers): on the 1.4 GB capture `SetFrameEvent` is 83% of a bundle
  dump at 47 ms a call, and it costs the same whether the id changes or not. Anything that looks like redundant
  engine work there has been tried, measured and reverted — **the bundle's second state read per event is
  load-bearing** (a forward step gives an incomplete state; only a backwards one makes the engine replay the
  frame from its start), which is why `REFERENCE §9` carries the hashes of the bundles that proved it. Change
  it only with a byte-identical-bundle check against a cold run.

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
`renderdoc-src\PC Renderer.rdc`, text *and* `--json`, were captured before the rename and diffed after it, and
came out byte-identical. Do the same for the next one: `batch` a command list into a file, rename, rebuild,
diff.