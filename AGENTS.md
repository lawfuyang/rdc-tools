# AGENTS.md

Personal, vibe-coded tool for debugging RenderDoc `.rdc` captures offline. See `README.md` for what it
does; nothing here is a supported product.

## Always run the checks after any code change

Any change to `rdc_analysis.py` (or to `tests/`) is not finished until **both** of these pass:

```powershell
python rdc_analysis.py selftest    # whole suite, ~1 s: exit 0 = pass, 1 = fail, 2 = bad option
npx --yes pyright@latest           # must print: 0 errors, 0 warnings
```

- `selftest -v` for per-test output, `selftest -k Draws` to run only matching test ids.
- Other entry points: `python tests/test_rdc_analysis.py` (parsers/decoders),
  `python tests/test_rdc_commands.py` (commands/CLI), `python -m unittest discover -s tests -t tests`.
- New behaviour needs tests in `tests/`; a bug fix needs a test that fails before the fix.
- Never weaken, skip or delete an assertion to make a run pass. Tests pinning behaviour that looks wrong
  are marked `CHARACTERIZATION` — change code and test together, and say so.
- Report the real numbers in your reply (test count, pyright error/warning counts), not just "passes".

## Python coding guidelines

Enforced by `pyrightconfig.json` (`"typeCheckingMode": "standard"`) plus the test suite. Target:
**Python 3.8+, standard library only** (plus the optional `zstandard`), **single file**, CLI behaviour frozen.

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
- Keep the payload-layout comments (README §3.4): they are the reason the decoders can be trusted.

### Structure and behaviour

- `rdc_analysis.py` stays a single file — do not split it into a package or add a shim module.
- Keep the documented public names (`parse_container`, `iter_chunks`, `decode_chunk`, `load_chunk_names`,
  `cmd_*`, `RENDERDOC_SRC`, `MARKER_CHUNKS`, `CHUNK_*`, ...) — tests, docs and scripts reference them.
- CLI parsing stays hand-rolled: `main()` prints the module docstring for a missing/unknown command and lets
  `IndexError`/`ValueError` escape for bad arguments. Do not replace it with `argparse`.
- Behaviour is the contract. The quirks in README §8 (6-character string floor, `first=0x-1`, `rootconst`
  offsets, heuristic `InitialContents` header scan, ...) are pinned by tests and must survive a refactor;
  change them only as a deliberate, separately-tested fix.
- No new runtime dependencies; `tests/` uses the stdlib only.

## Conventions

- Docs: `README.md` (usage, internals, §8 pitfalls), `ROADMAP.md` (unimplemented work).
- Never commit `renderdoc-src/` — vendored upstream code, gitignored.
- Never run `git commit` or `git push` unless explicitly asked to.
