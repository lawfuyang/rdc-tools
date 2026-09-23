"""Offline RenderDoc .rdc analyser (no renderdoc.pyd needed).

Container layout (from renderdoc/serialise/rdcfile.cpp):
  FileHeader(32) | BinaryThumbnail(8 + jpg) | CaptureMetaData(13 + name) | CaptureTimeBase(16)
  then N x { BinarySectionHeader(40) | name | data }

Section flags observed: 0x2 = LZ4 (u32 block-size prefixes), 0x4 = Zstd (u32 prefix + zstd frames),
0x0 = uncompressed.

The frame-capture section is one structured-data (SDChunk) stream; we decompress it and analyse the
readable content (D3D12: resource names, shader debug names, cbuffer reflection strings, ...).

Usage:
  python rdc_analysis.py sections <rdc>
  python rdc_analysis.py blocks   <rdc>          # per-section compression accounting
  python rdc_analysis.py resources <rdc> [limit] [nameFilter]   # id -> kind/size/name
  python rdc_analysis.py descriptors <rdc> [limit] [heapFilter] # descriptor heap contents
  python rdc_analysis.py verify   <rdc>          # framing/padding/payload checks, exit 1 on problems

# `--driver D3D11|D3D12|Vulkan|OpenGL|GLES` names the capture's API for every command's chunk names (the
# bundled table is D3D12's, so another driver needs the renderdoc-src tree; `bootstrap` fetches it). The same
# thing for a whole shell is `$RDC_DRIVER`, and the flag wins over it.

  python rdc_analysis.py summary  <rdc>
  python rdc_analysis.py markers  <rdc>
  python rdc_analysis.py chunks   <rdc> [limit] [nameFilter]
python rdc_analysis.py formats  <rdc>          # which formats the file holds, and what the parse decoded
  python rdc_analysis.py chunk    <rdc> <chunkIndex>
  python rdc_analysis.py draws    <rdc> [maxDraws]
  python rdc_analysis.py deps     <rdc> [maxResources] [table|dot|mermaid]
                                                         # who writes what, who reads it (offline)
  python rdc_analysis.py memory   <rdc> [maxRows]        # placement, capture-relative lifetimes,
                                                         #   aliasing pairs and never-read bytes
  python rdc_analysis.py rootsig  <rdc> [maxSigs]        # decoded root signatures: parameter types,
                                                         #   registers, spaces, descriptor ranges
  python rdc_analysis.py strings  <rdc> [minlen] [maxlines]
  python rdc_analysis.py names    <rdc> [minlen]
  python rdc_analysis.py grep     <rdc> <pattern> [context]
  python rdc_analysis.py dump     <rdc> <start> <length> [minlen]
  python rdc_analysis.py count    <rdc> <pattern> [pattern ...]
  python rdc_analysis.py hex      <rdc> <start> <length>
  python rdc_analysis.py dxbc     <rdc> [verbose]
  python rdc_analysis.py dump-chunk <rdc> <chunkIndex> <outfile>
  python rdc_analysis.py dump-shaders <rdc> <outdir>
  python rdc_analysis.py report   <rdc> <bundleDir> [outDir]   # frame report from `replay_dump dump`
  python rdc_analysis.py passdiff <a.rdc> <b.rdc> [--all]     # the two files' pass lists, by marker path
  python rdc_analysis.py vram     <rdc> [maxPasses] [--drop <nameFilter>] [--format table|csv|markdown]
                                                        # the frame's memory by role, the widest pass
                                                        #   and the "at half resolution" arithmetic
  python rdc_analysis.py rootsig-check <rdc> [bundleDir] [--format table|csv|markdown]
                                                        # what the root signatures declare against
                                                        #   what the stream binds and the heaps hold,
                                                        #   and (with a bundle) against the engine
  python rdc_analysis.py diff     <a.rdc> <b.rdc> [--all] [--format table|csv|markdown]
                                                        # the two streams' own calls, compared: marker
                                                        #   path, arguments, the state chunks that
                                                        #   changed before each call, and every binding
  python rdc_analysis.py replaydiff <bundleA> <bundleB> [--out <dir>] [--with-images]
                                   [--image-detail N] [--threshold N]
                                                         # A/B of two bundles: passes, state, values,
                                                         #   shaders and (with --with-images) the renders
  python rdc_analysis.py validate <file|bundleDir> <schemaDir> [kind]  # documents vs the schemas
  python rdc_analysis.py cache    [list|dir|clear]         # decompressed-stream cache
  python rdc_analysis.py bootstrap [tag]                   # fetch the RenderDoc source the chunk names come from
  python rdc_analysis.py chunk-names [--check|--write] [--out <file>] [--src <tree>]
                                                         # the bundled enum table (src/py/rdc_chunknames.py)
                                                         #   against a source tree: --write regenerates it
  python rdc_analysis.py build    [--check]                # are the built artefacts (bin/replay_dump.exe,
                                                          #   bin/rdc_lz4.dll) older than their sources,
                                                          #   and build them (`--check` only reports)
  python rdc_analysis.py goldens  [--check|--write] [--capture <name>] [--corpus <file>] [--verbose]
                                                        # the corpus's transcripts and labels (goldens/)
  python rdc_analysis.py sweep    <dir> [--out <root>] [--commands <file>] [--overwrite]
                                  [--min-bytes N] [--limit N] [--exe]
                                                        # a bundle per capture in a folder, plus an index
                                                        #   of them: the corpus's GPU half, one process
  python rdc_analysis.py selftest [-v] [-k <substring>]   # run the unit-test suite
  python rdc_analysis.py <cmd> <rdc> ... [--format table|csv|markdown]
                                                         # `draws`, `resources`, `descriptors`,
                                                         #   `rootsig` and `summary` print a CSV or a
                                                         #   Markdown table instead of the terminal's
                                                         #   own: the same rows, with the prose around
                                                         #   them on stderr so stdout stays a table

Speed (REFERENCE 4.13): the one decode a capture needs is 0.5 s, through `bin/rdc_lz4.dll` -- the same
`cmake --build build` writes it -- and it is free after that from the stream cache. A tree without that
library cannot read an LZ4 section, and says so. `$RDC_PROFILE=1` prints a per-phase table on
stderr when the run ends, and
`$RDC_PROGRESS=1` adds the live progress lines (neither ever touches stdout). The whole-stream scans
(`strings`, `names`) split the stream across processes, each mapping the cached stream -- so a capture whose
stream is not cached (`$RDC_NO_CACHE`) runs the same scan in one process, several times slower. `chunks` and
`chunk` stop scanning a payload as soon as they have enough strings for the preview.
"""
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

if TYPE_CHECKING:       # `unittest` is only needed by `selftest`, and importing it costs ~120 ms
    import unittest

# The two offline layers that grew out of this file. Imported rather than moved silently, so the CLI, the
# tests and anything else that already says `rdc_analysis.cmd_report` keep working; the heavy lifting is in
# the modules, which is where the line counts are for a reason. `X as X` is the re-export form PEP 484
# defines: it says "this name is part of this module's interface", so the type checker does not report the
# import as unused while still checking that the module it comes from exports it.
from rdc_report import (BUNDLE_VERSION as BUNDLE_VERSION, DEAD_ALLOCATION_LIMIT as DEAD_ALLOCATION_LIMIT,
                        KIND_ORDER as KIND_ORDER, NOTABLE_LIMIT as NOTABLE_LIMIT,
                        ODDITY_LIMIT as ODDITY_LIMIT, RECOMMENDATION_LIMIT as RECOMMENDATION_LIMIT,
                        REPORT_VERSION as REPORT_VERSION, SEVERITY_ORDER as SEVERITY_ORDER,
                        BundleData as BundleData,

                        BundleError as BundleError, DetectorRun as DetectorRun,
                        RedFlag as RedFlag, ReportDocument as ReportDocument, ReportPass as ReportPass,
                        _command as _command, _refs as _refs,
                        apply_known as apply_known,
                        cmd_report as cmd_report, detect_all as detect_all,
                        detect_dead_allocations as detect_dead_allocations,
                        detect_marker_balance as detect_marker_balance,
                        detect_messages as detect_messages,
                        detect_shader_io_mismatch as detect_shader_io_mismatch,
                        detect_srgb_view_mismatch as detect_srgb_view_mismatch,
                        detect_unattributed_draws as detect_unattributed_draws,
                        detect_unbound_root_parameters as detect_unbound_root_parameters,
                        detect_zero_constant_blocks as detect_zero_constant_blocks,
                        detect_zero_work as detect_zero_work,
                        frame_facts as frame_facts, load_bundle as load_bundle,
                        notable_passes as notable_passes, notable_resources as notable_resources,
                        notables as notables,
                        reconstruct_passes as reconstruct_passes,
                        recommendations as recommendations,
                        render_report_markdown as render_report_markdown,
                        report_caveats as report_caveats,
                        severity_of as severity_of, severity_table as severity_table)
from rdc_chunknames import VERSION as BUNDLED_NAMES_VERSION  # the bundled table's RenderDoc version
from rdc_renderdoc_src import (BootstrapError as BootstrapError,
                               NO_BOOTSTRAP_ENV as NO_BOOTSTRAP_ENV, describe as describe,
                               ensure as ensure, is_populated as is_populated, latest_tag as latest_tag,
                               missing_parts as missing_parts, target_dir as target_dir,
                               version as tree_version)
from rdc_schemas import (AB_SCHEMA as AB_SCHEMA, BUNDLE_SCHEMAS as BUNDLE_SCHEMAS,
                         REPORT_SCHEMA as REPORT_SCHEMA, SWEEP_SCHEMA as SWEEP_SCHEMA,
                         SCHEMA_KEYWORDS as SCHEMA_KEYWORDS,
                         SchemaError as SchemaError, cmd_validate as cmd_validate,
                         load_document as load_document, load_schemas as load_schemas,
                         no_duplicate_keys as no_duplicate_keys,
                         schema_for_file as schema_for_file,
                         validate_document as validate_document)
# `sweep` is the one command here that asks the *engine* a folder's worth of questions, through the
# library (`rdc_replay`); it is imported by name rather than star-wise because `cmd_sweep` is the whole
# list of what it offers the CLI (its constants are reachable as `rdc_sweep.<name>`).
from rdc_sweep import cmd_sweep as cmd_sweep

from rdc_types import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_stream import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_passdiff import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_filediff import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_sigcheck import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_vram import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_ab import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_ab_render import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_image import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_cache import *  # noqa: F401,F403  (re-exported for the CLI and tests)
import rdc_chunkmap  # `_take_driver` asks it for the driver list
from rdc_chunkmap import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_payloads import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_resources import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_dxbc import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_commands import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_formats import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_table import *  # noqa: F401,F403  (re-exported for the CLI and tests)
import rdc_table  # noqa: F401  (used qualified: the format the entry point parsed and checked)
from rdc_uses import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_profile import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_scan import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_driver import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_goldens import *  # noqa: F401,F403  (re-exported for the CLI and tests)

def _filter_suite(suite: unittest.TestSuite, patterns: Sequence[str]) -> unittest.TestSuite:
    """Keep only the tests whose id contains one of `patterns` (like `unittest -k`)."""
    import unittest
    out = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            out.addTest(_filter_suite(test, patterns))
        elif any(p in test.id() for p in patterns):
            out.addTest(test)
    return out

def cmd_selftest(args: Optional[Sequence[str]] = None) -> int:
    """Run the unit-test suite in the `tests` folder next to this file.

    Extra arguments: `-v` for verbose, `-k <substring>` to run matching tests only.
    Returns a process exit code (0 = all passed).
    """
    import unittest
    argv = list(args or [])
    verbosity, patterns, unknown = 1, [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('-v', '--verbose'):
            verbosity = 2
        elif a in ('-q', '--quiet'):
            verbosity = 1
        elif a in ('-k', '--pattern') and i + 1 < len(argv):
            i += 1
            patterns.append(argv[i])
        else:
            unknown.append(a)
        i += 1
    if unknown:
        print('usage: rdc_analysis.py selftest [-v] [-k <substring>]')
        return 2
    # This module lives in src/py/ and the suite in tests/, both under the repository root. The tool's own
    # folder goes on sys.path so the suite can `import rdc_analysis` / `import rdc_report` by name -- which
    # is what the tests do, and what the re-exports above exist for.
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    tests_dir = os.path.join(root, 'tests')
    if here not in sys.path:
        sys.path.insert(0, here)
    if not os.path.isdir(tests_dir):
        print('no tests folder at %s (it belongs to the repository root above %s)' % (tests_dir, os.path.basename(__file__)))
        return 1
        return 1
    print('running tests from %s' % tests_dir)
    suite = unittest.TestLoader().discover(tests_dir, pattern='test_*.py', top_level_dir=tests_dir)
    if patterns:
        suite = _filter_suite(suite, patterns)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1

def _arg(argv: Sequence[str], index: int, default: Optional[int] = None,
         base: int = 10) -> int:
    """Return `int(argv[index], base)`, or `default` when the argument is absent.

    Mirrors the original hand-rolled `int(sys.argv[n]) if len(sys.argv) > n else default` indexing:
    a missing argument uses the default, a present-but-unparsable one raises `ValueError` and a
    missing required argument (no default) raises `IndexError` -- both pinned by the CLI tests.
    """
    if index < len(argv):
        return int(argv[index], base)
    if default is None:
        raise IndexError('missing command argument #%d' % index)
    return default

def _take_format(cmd: str, argv: Sequence[str]) -> Tuple[List[str], str]:
    """The command's arguments with `--format <fmt>` taken out, and the format it named.

    `--format` may sit anywhere after the command, so it is removed *before* the positional arguments
    are read: written as `resources <rdc> --format csv 40`, the flag would otherwise be read as the
    limit. A missing or unknown value prints that command's usage and exits 2, like every other bad
    argument here -- and the exit code matters, because a script that asked for CSV and silently got
    a terminal table has an answer that parses into something wrong.
    """
    rest = list(argv)
    fmt = 'table'
    if '--format' in rest:
        index = rest.index('--format')
        if index + 1 >= len(rest) or not rdc_table.valid(rest[index + 1]):
            print('usage: rdc_analysis.py %s <rdc> [args] [--format %s]'
                  % (cmd, '|'.join(rdc_table.FORMATS)))
            sys.exit(2)
        fmt = rest[index + 1]
        del rest[index:index + 2]
    return rest, fmt

def _take_driver(cmd: str, argv: Sequence[str]) -> List[str]:
    """The command's arguments with `--driver <name>` taken out, after setting this run's driver.

    The driver is the *capture's* API rather than the command's -- a Vulkan capture has Vulkan chunks whatever
    is asked of it -- so it belongs to the run, and this is the one place it can be set from: `rdc_chunkmap`
    reads it for every command that prints a chunk name. `$RDC_DRIVER` says the same thing for a shell that
    runs many commands; this flag wins over it.

    An unknown name is refused with the list of the ones that exist. The loader treats a driver whose header
    it cannot find as "a half-extracted tree" and quietly falls back to numeric ids -- the right answer for a
    missing folder and exactly the wrong one for `vukan`, which would look like a capture nobody can name.
    `--driver` is taken out *before* the positional arguments are read, for the reason `--format` is: written
    as `chunks <rdc> --driver vulkan 20`, the value would otherwise be read as the limit.
    """
    rest = list(argv)
    if '--driver' in rest:
        index = rest.index('--driver')
        named = None
        for known in rdc_chunkmap.KNOWN_DRIVERS:
            if index + 1 < len(rest) and rest[index + 1].lower() == known.lower():
                named = known    # canonical: the path and the driver table are looked up by this spelling
        if named is None:
            print('usage: rdc_analysis.py %s <rdc> [args] [--driver %s]'
                  % (cmd, '|'.join(rdc_chunkmap.KNOWN_DRIVERS)))
            sys.exit(2)
        rdc_chunkmap.set_driver(named)
        del rest[index:index + 2]
    return rest

def _take_drops(cmd: str, argv: Sequence[str]) -> Tuple[List[str], List[str]]:
    """`(argv without the --drop pairs, the filters they named)`, in the order they were written.

    Repeatable because a what-if is asked one resource at a time ("what if this MRT went away, and that
    debug buffer"), and taken out *before* the positional arguments are read for the same reason
    `_take_format` is: written as `vram <rdc> --drop SceneColor 4`, the filter would otherwise be read
    as the pass limit. A `--drop` with nothing after it is a usage error, like a bad `--format`.
    """
    rest: List[str] = []
    drops: List[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == '--drop':
            if index + 1 >= len(argv):
                print('usage: rdc_analysis.py %s <rdc> [maxPasses] [--drop <nameFilter>] ... [--format '
                      '%s]' % (cmd, '|'.join(rdc_table.FORMATS)))
                sys.exit(2)
            drops.append(argv[index + 1])
            index += 2
            continue
        rest.append(argv[index])
        index += 1
    return rest, drops

def cmd_bootstrap(argv: Sequence[str]) -> int:
    """`bootstrap [tag]` -- put the RenderDoc source tree where the tool reads its chunk names from.

    Every command fetches it on demand through `rdc_chunkmap.load_chunk_names`; this is the same step run
    explicitly, for a fresh clone that wants it up front, for a script, or to pin a version. It is also the
    only path where a failure is an *error*: the automatic one keeps the documented fallback (numeric chunk
    ids and a warning), because a command that could not download should still analyse the capture.
    """
    tag = argv[0] if argv else None
    root = target_dir()
    already = is_populated(root)
    try:
        ensure(root=root, tag=tag, log=lambda text: print(text), strict=True)
    except BootstrapError as exc:
        print('error: %s' % exc)
        return 1
    print('renderdoc-src : %s (%s)' % (root, 'already populated' if already else 'fetched'))
    # What the *tree* contributes, which is what this command is about: `load_chunk_names` would count the
    # bundled table as well, and the point of that number here is "how much of the vocabulary came from the
    # tree you just fetched". The table's own version is reported next to it, so a tree that has moved on
    # says so (`chunk-names --write` is what closes that gap).
    print('version       : %s' % (tree_version(root) or 'not stated by the tree'))
    print('chunk names   : %d system + %d driver name(s) parsed from it'
          % (len(parse_enum(root, 'system')), len(parse_enum(root, 'driver'))))
    if BUNDLED_NAMES_VERSION and tree_version(root) != BUNDLED_NAMES_VERSION:
        print('bundled table : RenderDoc %s -- `chunk-names --write` regenerates it from this tree'
              % BUNDLED_NAMES_VERSION)
    return 0

def main() -> None:
    """CLI entry point: dispatch `sys.argv[1]` to the matching `cmd_*` function.

    A section that cannot be decompressed is the one failure here the user can act on -- the message says
    which library to build and a traceback would bury it -- so `FrameError` is what this turns into an
    `error:` line and exit code 1. Every other failure keeps its traceback, because it is a bug.
    """
    try:
        _dispatch()
    except FrameError as exc:
        print('error: %s' % exc)
        sys.exit(1)

def _dispatch() -> None:
    """`main()`'s body: the command table, one `cmd_*` per command."""
    argv = sys.argv
    if len(argv) > 1 and argv[1] in ('test', 'selftest'):
        sys.exit(cmd_selftest(argv[2:]))
    if len(argv) > 1 and argv[1] == 'cache':
        sys.exit(cmd_cache(argv[2:]))
    if len(argv) > 1 and argv[1] == 'bootstrap':
        sys.exit(cmd_bootstrap(argv[2:]))
    if len(argv) > 1 and argv[1] == 'chunk-names':
        sys.exit(cmd_chunknames(argv[2:]))
    if len(argv) > 1 and argv[1] == 'build':
        sys.exit(cmd_build(argv[2:]))
    if len(argv) > 1 and argv[1] == 'goldens':
        sys.exit(cmd_goldens(argv[2:]))
    if len(argv) > 1 and argv[1] == 'sweep':
        sys.exit(cmd_sweep(argv[2:]))
    if len(argv) < 3:
        print(__doc__)
        return
    cmd, path = argv[1], argv[2]
    # `--driver` is taken out before any command reads its own positionals, the way `--format` is: it is a
    # property of the run -- the capture's API, not the command's -- and every command that prints a chunk name
    # reads it back through `rdc_chunkmap.default_driver`.
    argv = _take_driver(cmd, argv)
    if cmd == 'chunk':
        cmd_chunk_detail(path, int(argv[3]))
    elif cmd == 'draws':
        rest, fmt = _take_format('draws', argv)
        cmd_draws(path, _arg(rest, 3, 80), fmt)
    elif cmd == 'deps':
        deps_fmt = argv[4] if len(argv) > 4 else 'table'
        if deps_fmt not in ('table', 'dot', 'mermaid'):
            print('usage: rdc_analysis.py deps <rdc> [maxResources] [table|dot|mermaid]')
            sys.exit(2)
        cmd_deps(path, _arg(argv, 3, 40), deps_fmt)
    elif cmd == 'memory':
        cmd_memory(path, _arg(argv, 3, 20))
    elif cmd == 'formats':
        rest, fmt = _take_format('formats', argv)
        cmd_formats(path, fmt)
    elif cmd == 'chunks':
        cmd_chunks(path, _arg(argv, 3, 200), argv[4] if len(argv) > 4 else None)
    elif cmd == 'verify':
        sys.exit(cmd_verify(path))
    elif cmd == 'summary':
        rest, fmt = _take_format('summary', argv)
        cmd_summary(path, fmt)
    elif cmd == 'markers':
        cmd_markers(path)
    elif cmd == 'rootsig':
        rest, fmt = _take_format('rootsig', argv)
        cmd_rootsig(path, _arg(rest, 3, 40), fmt)
    elif cmd == 'dump-chunk':
        cmd_dump_chunk(path, int(argv[3]), argv[4])
    elif cmd == 'dump-shaders':
        cmd_dump_shaders(path, argv[3])
    elif cmd == 'report':
        sys.exit(cmd_report(path, argv[3], argv[4] if len(argv) > 4 else None))
    elif cmd == 'passdiff':
        if len(argv) < 4:
            print('usage: rdc_analysis.py passdiff <a.rdc> <b.rdc> [--all]')
            sys.exit(2)
        sys.exit(cmd_passdiff(path, argv[3], '--all' in argv[4:]))
    elif cmd == 'diff':
        rest, fmt = _take_format('diff', argv)
        if len(rest) < 4:
            print('usage: rdc_analysis.py diff <a.rdc> <b.rdc> [--all] [--format %s]'
                  % '|'.join(rdc_table.FORMATS))
            sys.exit(2)
        sys.exit(cmd_filediff(rest[2], rest[3], '--all' in rest[4:], fmt))
    elif cmd == 'rootsig-check':
        rest, fmt = _take_format('rootsig-check', argv)
        sys.exit(cmd_rootsig_check(path, rest[3] if len(rest) > 3 else None, fmt))
    elif cmd == 'vram':
        rest, drops = _take_drops('vram', argv)
        rest, fmt = _take_format('vram', rest)
        cmd_vram(path, _arg(rest, 3, 8), drops, fmt)
    elif cmd == 'replaydiff':
        if len(argv) < 4:
            print('usage: rdc_analysis.py replaydiff <bundleA> <bundleB> [--out <dir>] [--with-images] '
                  '[--image-detail N] [--threshold N]')
            sys.exit(2)
        sys.exit(cmd_replaydiff(path, argv[3], argv[4:]))
    elif cmd == 'validate':
        if len(argv) < 4:
            print('usage: rdc_analysis.py validate <file|bundleDir> <schemaDir|one.schema.json> [kind]')
            sys.exit(2)
        sys.exit(cmd_validate(path, argv[3], argv[4] if len(argv) > 4 else None))
    elif cmd == 'sections':
        cmd_sections(path)
    elif cmd == 'blocks':
        cmd_blocks(path)
    elif cmd == 'resources':
        rest, fmt = _take_format('resources', argv)
        cmd_resources(path, _arg(rest, 3, 200), rest[4] if len(rest) > 4 else None, fmt)
    elif cmd == 'descriptors':
        rest, fmt = _take_format('descriptors', argv)
        cmd_descriptors(path, _arg(rest, 3, 200), rest[4] if len(rest) > 4 else None, fmt)
    elif cmd == 'strings':
        cmd_strings(path, _arg(argv, 3, 6), _arg(argv, 4, 200))
    elif cmd == 'names':
        cmd_names(path, _arg(argv, 3, 10))
    elif cmd == 'grep':
        cmd_grep(path, argv[3], _arg(argv, 4, 200))
    elif cmd == 'dump':
        cmd_dump(path, int(argv[3], 0), int(argv[4], 0), _arg(argv, 5, 4))
    elif cmd == 'dxbc':
        cmd_dxbc(path)
    elif cmd == 'count':
        cmd_count(path, argv[3:])
    elif cmd == 'hex':
        cmd_hex(path, argv[3], argv[4])
    else:
        print(__doc__)

if __name__ == '__main__':
    main()
