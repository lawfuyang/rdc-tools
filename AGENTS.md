# AGENTS.md

Personal, vibe-coded tool for debugging RenderDoc `.rdc` captures offline. See `README.md` for what it
does; nothing here is a supported product.

## Always run the unit tests after any code change

Any change to `rdc_analysis.py` (or to `tests/`) is not finished until the full suite has been run.
It is hermetic — no capture file, GPU, `renderdoc.pyd` or `renderdoc-src` needed — and takes ~1 s.

```powershell
python rdc_analysis.py selftest            # full suite: exit 0 = pass, 1 = fail, 2 = bad option
python rdc_analysis.py selftest -v         # per-test output
python rdc_analysis.py selftest -k Draws   # only tests whose id contains "Draws"
```

Other entry points: `python tests/test_rdc_analysis.py` (parsers/decoders),
`python tests/test_rdc_commands.py` (commands/CLI), `python -m unittest discover -s tests -t tests`.

- New behaviour needs tests in `tests/`; a bug fix needs a test that fails before the fix.
- Never weaken an existing assertion to make a run pass. Tests pinning behaviour that looks wrong are
  marked `CHARACTERIZATION` — change code and test together, and say so.
- Report the real result in your reply (test count, pass/fail), not just "tests pass".

## Conventions

- `rdc_analysis.py` is a single file; a new command goes in the module docstring too (it is the help text).
- Docs: `README.md` (usage, internals, §8 pitfalls), `ROADMAP.md` (unimplemented work).
- Never commit `renderdoc-src/` — vendored upstream code, gitignored.
