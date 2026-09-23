"""`sweep <dir> [--out <root>]`: one bundle per capture in a folder, and an index of what they are.

The corpus (`goldens/`, REFERENCE §4.17) names captures, hashes them and pins their transcripts; this is
the half of that which needs a GPU to rebuild in bulk, and it is why the driver is also a *library*: one
process sweeps a folder, opening and closing one replay session per capture. A folder of twenty-eight
captures is then twenty-eight device creations in one program instead of twenty-eight programs -- the
engine allows many captures per process (its own application opens them that way) and does *not* allow the
replay system to be built twice, which is why the library owns it (`src/cpp/api.h`).

What it writes under `--out` (default `bundles`):

    <key>/              the bundle `dump` writes, unchanged -- the same file tree `report` and the A/B read
    <key>/<line>.txt    one file per extra command line in `--commands`, named the way the corpus names
                        its transcripts (`info.txt`, `draws-12.txt`): the line's own tokens joined by `-`
    sweep.json          every capture's key, bundle, size, SHA-256 and what its bundle holds

The index is built from the bundles' **manifests** rather than from the run, which is what makes an
interrupted sweep recoverable: a capture whose manifest is there is `present` and needs no replay, and the
numbers a corpus entry wants (`bytes`, `sha256`, `renderdoc`) come from the bundle that was actually
written rather than from a second reading of the file.

A *key* is the capture's path under `<dir>`, without the extension and with the separators replaced (so
`sub/a.rdc` is `sub-a`), and the index therefore names local files -- which is local state, like
`goldens/captures.local.json`: the corpus keeps a key and a hash in git and the path out of it
(REFERENCE §4.17). Nothing here decides what a capture is *called* in a corpus; that is the caller's word.
"""

from __future__ import annotations

import json
import os
import subprocess       # the exe path's one session per capture (`multi`)
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import rdc_driver
import rdc_replay
import rdc_schemas

#: Where the bundles go when `--out` does not say, and the index's own name beside them.
DEFAULT_OUT = 'bundles'
INDEX_NAME = 'sweep.json'

#: The manifest `dump` writes last, and the evidence that a bundle is complete: it is written after every
#: other file, so a bundle without one was interrupted (REFERENCE §9).
MANIFEST_NAME = 'manifest.json'

class SweepError(Exception):
    """This machine cannot sweep: no library, no driver, or neither."""

def capture_files(root: str) -> List[str]:
    """Every `.rdc` under `root`, recursively and in sorted order -- a sweep that reports the same list
    twice, so a second run over a folder that only gained a capture sweeps only that one."""
    found: List[str] = []
    for folder, _dirs, names in os.walk(root):
        for name in names:
            if name.lower().endswith('.rdc'):
                found.append(os.path.join(folder, name))
    return sorted(found)

def key_for(root: str, path: str) -> str:
    """A capture's key: its path under `root`, extension dropped, separators replaced."""
    rel = os.path.relpath(path, root)
    stem = rel[:-4] if rel.lower().endswith('.rdc') else rel
    return stem.replace('\\', '-').replace('/', '-')

def transcript_name(line: str) -> str:
    """The file one command line's output goes to, named the way the corpus names transcripts.

    `info` is `info.txt` and `draws 12` is `draws-12.txt`, which is what `goldens/<key>/` holds for the
    same commands -- so a sweep's output can be read next to a corpus's without a translation step.
    """
    tokens = [token for token in line.split() if token and not token.startswith('--')]
    slug = '-'.join(tokens).replace('"', '') or 'command'
    return slug + '.txt'

def read_manifest(bundle_dir: str) -> Optional[Dict[str, Any]]:
    """A bundle's manifest as a dict, or None when the bundle is not there or not finished."""
    path = os.path.join(bundle_dir, MANIFEST_NAME)
    try:
        with open(path, encoding='utf-8') as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None

def index_entry(root: str, path: str, bundle: str, manifest: Dict[str, Any], status: str,
                seconds: float = 0.0, note: str = '') -> Dict[str, Any]:
    """One capture's row in the index, from its own manifest."""
    return {
        'key': key_for(root, path),
        'bundle': bundle.replace('\\', '/'),
        'status': status,
        'seconds': round(seconds, 2),
        'bytes': int(manifest.get('captureBytes', 0) or 0),
        'sha256': str(manifest.get('captureSha256', '')),
        'renderdoc': str(manifest.get('renderdoc', '')),
        'fileCount': int(manifest.get('fileCount', 0) or 0),
        'fileBytes': int(manifest.get('fileBytes', 0) or 0),
        'note': note,
    }

def build_index(source: str, out_dir: str, entries: List[Dict[str, Any]], renderdoc: str) -> Dict[str, Any]:
    """The `sweep.json` document: what was swept, what each capture is, and what could not be."""
    swept = [entry for entry in entries if entry['status'] in ('swept', 'present')]
    return {
        'schemaVersion': 1,
        'source': source.replace('\\', '/'),
        'out': out_dir.replace('\\', '/'),
        'renderdoc': renderdoc,
        'captures': entries,
        'swept': len([entry for entry in entries if entry['status'] == 'swept']),
        'present': len([entry for entry in entries if entry['status'] == 'present']),
        'failed': len([entry for entry in entries if entry['status'] == 'failed']),
        'bytes': sum(entry['bytes'] for entry in swept),
    }

def write_index(out_dir: str, document: Dict[str, Any]) -> str:
    """Write the index, and check it against the tool's own schema first: a document this tool writes and
    cannot validate is a document a consumer cannot either (`validate sweep.json`)."""
    rdc_schemas.validate_document(document, rdc_schemas.SWEEP_SCHEMA)
    path = os.path.join(out_dir, INDEX_NAME)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(document, handle, indent=2, sort_keys=False)
        handle.write('\n')
    return path

def dump_line(bundle_dir: str, overwrite: bool = False) -> str:
    """The command line that writes one bundle: `dump "<dir>"`, with the path in quotes.

    A key can hold a space -- a capture's file name is not ours to rewrite -- and the command language groups
    a quoted token. Unquoted, `dump out/Android Renderer` sends `Renderer` as a second argument, and since
    `dump` takes the *last* positional as its destination the bundle lands in a directory named after the
    capture at whatever the process's working directory is. That happened once: a sweep of a folder holding
    `Android Renderer.rdc` wrote a bundle into `Renderer/` at the repository root, which is exactly the
    accident this function (and `tests/test_rdc_sweep.py`) exists to keep from happening again.
    """
    return 'dump "%s"%s' % (bundle_dir, ' --overwrite' if overwrite else '')

def run_lines(path: str, root: str, bundle_dir: str, lines: Sequence[str], overwrite: bool,
              use_library: bool, log: str) -> Tuple[int, float, str]:
    """Sweep one capture: its bundle, then the extra lines, each in one session.

    Returns `(exit code, seconds, note)`; the note is the CLI's own last line of stderr for a failure,
    which is the part a reader wants when a capture cannot be replayed at all.
    """
    first_line = dump_line(bundle_dir, overwrite)
    all_lines = [first_line] + [line for line in lines if line.strip()]

    if use_library:
        started = time.perf_counter()
        try:
            with rdc_replay.Session(path, log=log) as session:
                last = 0
                # The first line writes the bundle and leaves no transcript: it is the sweep's own step,
                # and what it produced is the bundle's files. The rest are the caller's, one file each.
                for position, line in enumerate(all_lines):
                    code, text = session.command(line)
                    if code != 0:
                        return code, time.perf_counter() - started, '%s: exit %d' % (line, code)
                    if position > 0:
                        with open(os.path.join(bundle_dir, transcript_name(line)), 'w',
                                  encoding='utf-8') as handle:
                            handle.write(text)
                    last = code
            return last, time.perf_counter() - started, ''
        except rdc_replay.ReplayError as exc:
            return 1, time.perf_counter() - started, str(exc)

    # The exe path used to run `dump` and *nothing else*, so `--commands` was silently dropped here while the
    # library path honoured it -- one question with two answers depending on how the sweep was started.
    # `multi` (one session, many command lines) is what closes that: the dump line and every extra line go
    # to one process, whose stdout carries the `#=== <line>` markers the transcripts are split on.
    started = time.perf_counter()
    proc = subprocess.run([os.path.join(root, rdc_driver.EXE_PATH), 'multi', path] + all_lines,
                          capture_output=True)
    seconds = time.perf_counter() - started
    if proc.returncode != 0:
        message = (proc.stderr or b'').decode('utf-8', 'replace').strip().splitlines()
        return proc.returncode, seconds, message[-1][:160] if message else 'exit %d' % proc.returncode

    # The transcripts, split back out of the one stream: the marker line is exactly what a batch file's
    # output carries, and the library path's per-line files are the same documents without it.
    text = (proc.stdout or b'').decode('utf-8', 'replace')
    for position, line in enumerate(all_lines):
        if position == 0:
            continue        # the dump line is the sweep's own step, and what it produced is the bundle
        body = _transcript_body(text, line)
        if body is None:
            return 1, seconds, 'the exe session did not print a transcript for `%s`' % line
        with open(os.path.join(bundle_dir, transcript_name(line)), 'w', encoding='utf-8') as handle:
            handle.write(body)
    return 0, seconds, ''

def _transcript_body(text: str, line: str) -> Optional[str]:
    """One command's own output out of a `multi`/`batch` stream: the text between its marker and the next.

    `None` when the marker is not there at all, which is how a caller learns the session did not reach that
    line rather than writing an empty transcript as if it had.
    """
    marker = '#=== %s\n' % line
    at = text.find(marker)
    if at < 0:
        return None
    start = at + len(marker)
    end = text.find('\n#=== ', start)
    return text[start:] if end < 0 else text[start:end + 1]

def cmd_sweep(argv: Sequence[str]) -> int:
    """`sweep <dir> [--out <root>] [--commands <file>] [--overwrite] [--min-bytes N] [--limit N] [--exe]`.

    A bundle per capture, then `sweep.json`. A capture whose bundle is already there is `present` and is
    not replayed unless `--overwrite`, which is what makes a sweep over a folder resumable and what makes
    a second run cheap. Exit 0 when every capture is swept or present, 1 when any failed, 2 when there is
    nothing to sweep or no way to sweep it.
    """
    source = ''
    out_dir = DEFAULT_OUT
    commands_file = ''
    overwrite = False
    min_bytes = 0
    limit = 0
    use_library: Optional[bool] = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--overwrite':
            overwrite = True
        elif arg == '--exe':
            use_library = False
        elif arg == '--library':
            use_library = True
        elif arg in ('--out', '--commands', '--min-bytes', '--limit') and index + 1 < len(argv):
            index += 1
            value = argv[index]
            if arg == '--out':
                out_dir = value
            elif arg == '--commands':
                commands_file = value
            elif arg == '--min-bytes':
                min_bytes = int(value) if value.isdigit() else 0
            else:
                limit = int(value) if value.isdigit() else 0
        elif arg.startswith('-'):
            print('usage: rdc_analysis.py sweep <dir> [--out <root>] [--commands <file>] [--overwrite] '
                  '[--min-bytes N] [--limit N] [--exe]')
            return 2
        elif not source:
            source = arg
        else:
            print('usage: rdc_analysis.py sweep <dir> [--out <root>] [--commands <file>] [--overwrite] '
                  '[--min-bytes N] [--limit N] [--exe]')
            return 2
        index += 1

    if not source:
        print('usage: rdc_analysis.py sweep <dir> [--out <root>] [--commands <file>] [--overwrite] '
              '[--min-bytes N] [--limit N] [--exe]')
        return 2
    if not os.path.isdir(source):
        print('error: %s is not a folder' % source)
        return 2

    root = rdc_driver.repo_root()
    if use_library is None:
        use_library = rdc_replay.available()
    if not use_library and not os.path.isfile(os.path.join(root, rdc_driver.EXE_PATH)):
        print('error: neither %s nor %s is here: build them (`cmake --build build --config Release`)'
              % (rdc_replay.dll_path(), rdc_driver.EXE_PATH))
        return 2

    lines: List[str] = []
    if commands_file:
        try:
            with open(commands_file, encoding='utf-8') as handle:
                lines = [line.strip() for line in handle
                         if line.strip() and not line.lstrip().startswith('#')]
        except OSError as exc:
            print('error: cannot read %s: %s' % (commands_file, exc))
            return 2

    captures = capture_files(source)
    if min_bytes:
        captures = [path for path in captures if os.path.getsize(path) >= min_bytes]
    if limit:
        captures = captures[:limit]
    if not captures:
        print('nothing to sweep: no .rdc under %s%s' % (source, ' over %d byte(s)' % min_bytes
                                                        if min_bytes else ''))
        return 2

    log = os.path.join(out_dir, 'sweep.log.txt')
    os.makedirs(out_dir, exist_ok=True)
    print('sweep   : %s (%d capture(s)) -> %s, %s' % (source, len(captures), out_dir,
                                                      'the library' if use_library
                                                      else rdc_driver.EXE_PATH))
    entries: List[Dict[str, Any]] = []
    renderdoc = ''
    failed = 0
    for path in captures:
        name = key_for(source, path)
        bundle = os.path.join(out_dir, name)
        manifest = read_manifest(bundle)
        if manifest is not None and not overwrite:
            entries.append(index_entry(source, path, bundle, manifest, 'present'))
            print('%-24s present  %s' % (name, str(manifest.get('captureSha256', ''))[:12]))
            renderdoc = renderdoc or str(manifest.get('renderdoc', ''))
            continue

        os.makedirs(bundle, exist_ok=True)
        code, seconds, note = run_lines(path, root, bundle, lines, overwrite, bool(use_library), log)
        manifest = read_manifest(bundle)
        if code == 0 and manifest is not None:
            entries.append(index_entry(source, path, bundle, manifest, 'swept', seconds))
            renderdoc = renderdoc or str(manifest.get('renderdoc', ''))
            print('%-24s swept  %6.1f s  %d file(s), %.1f MB'
                  % (name, seconds, manifest.get('fileCount', 0),
                     float(manifest.get('fileBytes', 0) or 0) / 1048576.0))
        else:
            failed += 1
            entries.append({'key': name, 'bundle': bundle.replace('\\', '/'), 'status': 'failed',
                            'seconds': round(seconds, 2), 'bytes': os.path.getsize(path), 'sha256': '',
                            'renderdoc': '', 'fileCount': 0, 'fileBytes': 0,
                            'note': note or 'exit %d, and no manifest was written' % code})
            print('%-24s FAILED %s' % (name, note or 'no manifest'))

    document = build_index(source, out_dir, entries, renderdoc)
    index_path = write_index(out_dir, document)
    print('index   : %s (%d swept, %d present, %d failed, %.1f MB of captures)'
          % (index_path, document['swept'], document['present'], document['failed'],
             document['bytes'] / 1048576.0))
    for entry in entries:
        if entry['status'] == 'failed':
            print('  failed: %s: %s' % (entry['key'], entry['note']))
    return 1 if failed else 0

__all__ = [
    'DEFAULT_OUT',
    'INDEX_NAME',
    'MANIFEST_NAME',
    'SweepError',
    'build_index',
    'capture_files',
    'cmd_sweep',
    'index_entry',
    'key_for',
    'read_manifest',
    'run_lines',
    'transcript_name',
    'write_index',
]
