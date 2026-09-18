"""The build's artefacts against the sources they are built from, and the build that catches them up.

`cmake --build build` writes two things: `bin/replay_dump.exe` from `src/cpp/*.cpp|h`, and `bin/rdc_lz4.dll`
from the vendored decoder in `src/cpp/third_party/lz4` (`CMakeLists.txt` can change either one). Nothing in
that arrangement notices when one of them is left behind, and each is wrong in its own way: a replay host
answers from the code it was compiled with, so an exe older than its sources replies with the *previous*
revision's behaviour and looks exactly like one that is current -- the driver says so itself when it happens
(REFERENCE 9), which only helps a reader of the log; and the library *is* the offline tool's LZ4 decoder, so a
stale one decodes every capture with whatever its source said before the last change to it. This module is the
other half for both: `build --check` is the gate, `build` is the fix.

The two comparisons are **independent on purpose**: the exe is not built from the decoder's source, so a
changed `lz4.c` must not make the exe look stale, and the driver's own warning -- which reads its own directory
-- is not extended with a library it never loads.

Why the driver cannot rebuild itself: on Windows a running image cannot be written to, so the link that
would replace `bin/replay_dump.exe` fails while that same exe is what is running -- `LNK1104: cannot open
file 'bin\replay_dump.exe'`. Anything automatic would have to be somebody *else*, which is this module.

Exit codes for `build`: 0 = every artefact current (or the build succeeded), 1 = something out of date (with
`--check`) or the build failed, 2 = no verdict anywhere -- no artefacts or no sources, which is a fresh clone
rather than a mistake.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import List, Optional, Sequence, Tuple, TypedDict

#: The directory the driver's sources live in, relative to the repository root.
SOURCE_DIR = os.path.join('src', 'cpp')

#: What counts as a driver source: the same two suffixes the driver's own staleness check looks for.
SOURCE_SUFFIXES = ('.cpp', '.h')

#: The vendored LZ4 decoder's source, and the suffixes it uses (it is C, not C++): a *named* tree because the
#: scan above is one level deep -- the decoder's own directory is not somewhere a `src/cpp` listing reaches,
#: and the driver's comparison should not start reporting on a file it does not compile.
LIB_SOURCE_DIR = os.path.join('src', 'cpp', 'third_party', 'lz4')
LIB_SOURCE_SUFFIXES = ('.c', '.h')

#: The build tree `CMakeLists.txt` documents (`cmake -S . -B build`), and what it writes.
BUILD_DIR = 'build'
EXE_PATH = os.path.join('bin', 'replay_dump.exe')
LIB_PATH = os.path.join('bin', 'rdc_lz4.dll')

#: The build command, in the form the driver's own warning names it.
BUILD_COMMAND = ('cmake', '--build', BUILD_DIR, '--config', 'Release')

class BuildTarget(TypedDict):
    """One artefact the build writes, what it is built from, and what being stale costs.

    `stakes` finishes the sentence `build` prints for a stale artefact, because the two failures are not the
    same failure: an old exe answers wrong, an old decoder decodes with the previous code.
    """

    name: str
    artefact: str
    source_dir: str
    suffixes: Tuple[str, ...]
    stakes: str

#: What the build writes, in the order `describe` prints it. `CMakeLists.txt` is in every comparison because it
#: can change any artefact without touching a source (`/WX` off, a new glob, a new target, a different
#: `renderdoc.dll`).
TARGETS: Tuple[BuildTarget, ...] = (
    {'name': 'driver', 'artefact': EXE_PATH, 'source_dir': SOURCE_DIR, 'suffixes': SOURCE_SUFFIXES,
     'stakes': 'so every run answers with the previous build'},
    {'name': 'lz4 library', 'artefact': LIB_PATH, 'source_dir': LIB_SOURCE_DIR,
     'suffixes': LIB_SOURCE_SUFFIXES,
     'stakes': 'so every capture is decoded by the previous decoder'},
)

class TargetVerdict(TypedDict):
    """Whether one artefact is older than the sources it is built from.

    `source`/`source_time` are the newest of them, `newer_seconds` is how much newer it is than the artefact
    (negative when the artefact is the newer of the two), `stakes` is the target's own sentence for what a
    stale one costs, and `note` says why no comparison was possible -- an empty string when one was. `stale` is
    only ever true when both sides were actually read: a missing artefact or a missing source tree is not
    evidence that anything is out of date.
    """

    name: str
    root: str
    artefact: str
    artefact_time: float
    source: str
    source_time: float
    newer_seconds: float
    stakes: str
    stale: bool
    note: str

def repo_root() -> str:
    """The repository root, two folders above this file (`<root>/src/py/rdc_driver.py`)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def newest_source(root: str, source_dir: str = SOURCE_DIR,
                  suffixes: Sequence[str] = SOURCE_SUFFIXES) -> Tuple[str, float]:
    """The newest thing one artefact is built from, as `(path relative to root, mtime)`.

    `CMakeLists.txt` and that artefact's own source directory, scanned one level deep -- which is the whole
    depth of both trees. `('', 0.0)` when none of them could be read, which the caller has to treat as "no
    opinion" rather than "very old".
    """
    newest, newest_time = '', 0.0
    candidates: List[str] = [os.path.join(root, 'CMakeLists.txt')]
    tree = os.path.join(root, source_dir)
    try:
        names = sorted(os.listdir(tree))
    except OSError:
        names = []
    for name in names:
        if name.endswith(tuple(suffixes)):
            candidates.append(os.path.join(tree, name))
    for path in candidates:
        try:
            when = os.path.getmtime(path)
        except OSError:
            continue
        if when > newest_time:
            newest, newest_time = os.path.relpath(path, root), when
    return newest, newest_time

def target_staleness(target: BuildTarget, root: Optional[str] = None) -> TargetVerdict:
    """Compare one artefact with the newest of the things it is built from.

    Both sides are read; when either is missing the verdict is `stale` false with a `note` saying which,
    because "there is no binary here" is a fact about this checkout and not a claim about freshness.
    """
    base = root or repo_root()
    artefact_rel = target['artefact']
    artefact = os.path.join(base, artefact_rel)
    source, source_time = newest_source(base, target['source_dir'], target['suffixes'])
    try:
        artefact_time = os.path.getmtime(artefact)
    except OSError:
        artefact_time = 0.0

    note = ''
    if artefact_time == 0.0:
        note = ('%s is not there, so there is nothing to compare: `%s` writes it'
                % (artefact_rel, ' '.join(BUILD_COMMAND)))
    elif not source:
        note = ('no sources under %s, so there is nothing to compare: this is not a checkout of the %s, or '
                '`%s` is missing' % (target['source_dir'], target['name'],
                                     os.path.join(base, 'CMakeLists.txt')))
    stale = bool(note == '' and source_time > artefact_time)
    return TargetVerdict(name=target['name'], root=base, artefact=artefact, artefact_time=artefact_time,
                         source=source, source_time=source_time,
                         newer_seconds=source_time - artefact_time, stakes=target['stakes'],
                         stale=stale, note=note)

def verdicts(root: Optional[str] = None) -> List[TargetVerdict]:
    """Every artefact's verdict, in the order `describe` prints them."""
    return [target_staleness(target, root) for target in TARGETS]

def staleness(root: Optional[str] = None) -> TargetVerdict:
    """The driver's own verdict -- the artefact this module was about before the library existed, and the
    one its log mirrors. `verdicts()` is what `build` acts on."""
    return verdicts(root)[0]

def verdict_code(found: Sequence[TargetVerdict]) -> int:
    """The exit code one run answers with: 1 when anything is out of date (the actionable answer), else 2
    when nothing could be compared at all (a fresh clone is not a mistake), else 0.
    """
    if any(verdict['stale'] for verdict in found):
        return 1
    if any(verdict['note'] for verdict in found):
        return 2
    return 0

def rebuild(root: Optional[str] = None) -> int:
    """Run the build, with the compiler's own output going straight to the console.

    Returns the build's exit code, or 1 when `cmake` itself could not be started -- a missing `cmake` is a
    failure of this command, and the message says so rather than being swallowed as a bad build.
    """
    base = root or repo_root()
    try:
        return subprocess.run(list(BUILD_COMMAND), cwd=base, check=False).returncode
    except OSError as exc:
        print('error: cannot run %s: %s' % (' '.join(BUILD_COMMAND), exc))
        return 1

def _when(when: float) -> str:
    """A file time as a local timestamp, or `never` for the 0.0 a missing file reads as."""
    return 'never' if when == 0.0 else time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when))

def describe(found: Sequence[TargetVerdict]) -> List[str]:
    """The verdicts as the lines `build` prints, in the shape the other environment commands use: one block per
    artefact, because they can disagree and the reader has to see which one is behind.
    """
    out = ['%-11s: %s' % ('root', found[0]['root'])]
    for verdict in found:
        out.append('%-11s: %s (%s)' % (verdict['name'], verdict['artefact'],
                                       _when(verdict['artefact_time'])))
        if verdict['source']:
            out.append('%-11s: %s (%s)' % ('newest', verdict['source'], _when(verdict['source_time'])))
        if verdict['note']:
            out.append('%-11s: no verdict -- %s' % ('verdict', verdict['note']))
        elif verdict['stale']:
            out.append('%-11s: out of date -- %s is %.1f s newer, %s'
                       % ('verdict', verdict['source'], verdict['newer_seconds'], verdict['stakes']))
        else:
            out.append('%-11s: current -- no source is newer than the %s' % ('verdict', verdict['name']))
    return out

def cmd_build(argv: Sequence[str]) -> int:
    """`build [--check]` -- is anything `cmake --build build` writes older than its sources, and build it if so.

    `--check` only reports, so it can be a gate (`exit 1` = out of date, `2` = nothing to compare, which a
    fresh clone is). Without it a stale or missing artefact is built: the build is the whole point of the
    command, and it prints the compiler's own output, so a failed build is read rather than summarised.
    """
    unknown = [a for a in argv if a != '--check']
    if unknown:
        print('usage: rdc_analysis.py build [--check]')
        return 2
    check_only = '--check' in argv

    before = verdicts()
    for line in describe(before):
        print(line)
    code = verdict_code(before)

    if check_only:
        return code

    if code == 0:
        print('nothing to build: every artefact is newer than its sources')
        return 0

    print('building  : %s (in %s)' % (' '.join(BUILD_COMMAND), before[0]['root']))
    built = rebuild()
    if built != 0:
        print('error: the build failed with exit code %d; the artefacts are unchanged' % built)
        return 1

    after = verdicts()
    for line in describe(after):
        print(line)
    return 0

__all__ = [
    'BUILD_COMMAND',
    'BUILD_DIR',
    'BuildTarget',
    'EXE_PATH',
    'LIB_PATH',
    'LIB_SOURCE_DIR',
    'LIB_SOURCE_SUFFIXES',
    'SOURCE_DIR',
    'SOURCE_SUFFIXES',
    'TARGETS',
    'TargetVerdict',
    'cmd_build',
    'describe',
    'newest_source',
    'rebuild',
    'repo_root',
    'staleness',
    'target_staleness',
    'verdict_code',
    'verdicts',
]
