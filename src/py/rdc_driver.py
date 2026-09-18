"""The replay driver's binary against the sources it is built from, and the build that catches it up.

`bin/replay_dump.exe` is written by `cmake --build build` from `src/cpp/*.cpp|h` and `CMakeLists.txt`, and
nothing in that arrangement notices when the binary is left behind: a replay host answers from the code it
was compiled with, so an exe older than its sources replies with the *previous* revision's behaviour and
looks exactly like one that is current. The driver says so itself when it happens (REFERENCE 9), which only
helps a reader of the log; this is the other half, so `build --check` can be a gate and `build` is the fix.

Why the driver cannot rebuild itself: on Windows a running image cannot be written to, so the link that
would replace `bin/replay_dump.exe` fails while that same exe is what is running -- `LNK1104: cannot open
file 'bin\\replay_dump.exe'`. Anything automatic would have to be somebody *else*, which is this module.

Exit codes for `build`: 0 = current (or the build succeeded), 1 = out of date (with `--check`) or the build
failed, 2 = no verdict -- no binary or no sources, which is a fresh clone rather than a mistake.
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

#: The build tree `CMakeLists.txt` documents (`cmake -S . -B build`), and the executable it writes.
BUILD_DIR = 'build'
EXE_PATH = os.path.join('bin', 'replay_dump.exe')

#: The build command, in the form the driver's own warning names it.
BUILD_COMMAND = ('cmake', '--build', BUILD_DIR, '--config', 'Release')

class DriverVerdict(TypedDict):
    """Whether the driver's binary is older than the sources it is built from.

    `source`/`source_time` are the newest of them (the driver's own rule), `newer_seconds` is how much
    newer it is than the binary (negative when the binary is the newer of the two), and `note` says why no
    comparison was possible -- an empty string when one was. `stale` is only ever true when both sides were
    actually read: a missing binary or a missing tree is not evidence that anything is out of date.
    """

    root: str
    exe: str
    exe_time: float
    source: str
    source_time: float
    newer_seconds: float
    stale: bool
    note: str

def repo_root() -> str:
    """The repository root, two folders above this file (`<root>/src/py/rdc_driver.py`)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def newest_source(root: str) -> Tuple[str, float]:
    """The newest thing the driver is built from, as `(path relative to root, mtime)`.

    `src/cpp`'s `.cpp` and `.h` files, and `CMakeLists.txt` -- the last one because it can change the
    binary without touching a source (`/WX` off, a new glob, a different `renderdoc.dll`). `('', 0.0)` when
    none of them could be read, which the caller has to treat as "no opinion" rather than "very old".
    """
    newest, newest_time = '', 0.0
    candidates: List[str] = [os.path.join(root, 'CMakeLists.txt')]
    source_dir = os.path.join(root, SOURCE_DIR)
    try:
        names = sorted(os.listdir(source_dir))
    except OSError:
        names = []
    for name in names:
        if name.endswith(SOURCE_SUFFIXES):
            candidates.append(os.path.join(source_dir, name))
    for path in candidates:
        try:
            when = os.path.getmtime(path)
        except OSError:
            continue
        if when > newest_time:
            newest, newest_time = os.path.relpath(path, root), when
    return newest, newest_time

def staleness(root: Optional[str] = None) -> DriverVerdict:
    """Compare the driver's binary with the newest of its sources.

    Both sides are read; when either is missing the verdict is `stale` false with a `note` saying which,
    because "there is no binary here" is a fact about this checkout and not a claim about freshness.
    """
    base = root or repo_root()
    exe_rel = EXE_PATH
    exe = os.path.join(base, exe_rel)
    source, source_time = newest_source(base)
    try:
        exe_time = os.path.getmtime(exe)
    except OSError:
        exe_time = 0.0

    note = ''
    if exe_time == 0.0:
        note = ('%s is not there, so there is nothing to compare: `%s` writes it'
                % (exe_rel, ' '.join(BUILD_COMMAND)))
    elif not source:
        note = ('no sources under %s, so there is nothing to compare: this is not a checkout of the '
                'driver, or `%s` is missing' % (SOURCE_DIR, os.path.join(base, 'CMakeLists.txt')))
    stale = bool(note == '' and source_time > exe_time)
    return DriverVerdict(root=base, exe=exe, exe_time=exe_time, source=source, source_time=source_time,
                         newer_seconds=source_time - exe_time, stale=stale, note=note)

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

def describe(verdict: DriverVerdict) -> List[str]:
    """The verdict as the lines `build` prints, in the shape the other environment commands use."""
    out = ['root       : %s' % verdict['root'],
           'driver     : %s (%s)' % (EXE_PATH, _when(verdict['exe_time']))]
    if verdict['source']:
        out.append('newest     : %s (%s)' % (verdict['source'].replace('\\', os.sep),
                                             _when(verdict['source_time'])))
    if verdict['note']:
        out.append('verdict    : no verdict -- %s' % verdict['note'])
    elif verdict['stale']:
        out.append('verdict    : out of date -- %s is %.1f s newer, so a run answers with the previous '
                   'build' % (verdict['source'].replace('\\', os.sep), verdict['newer_seconds']))
    else:
        out.append('verdict    : current -- no source is newer than the binary')
    return out

def cmd_build(argv: Sequence[str]) -> int:
    """`build [--check]` -- is `bin/replay_dump.exe` older than its sources, and build it if so.

    `--check` only reports, so it can be a gate (`exit 1` = out of date, `2` = nothing to compare, which a
    fresh clone is). Without it a stale or missing binary is built: the build is the whole point of the
    command, and it prints the compiler's own output, so a failed build is read rather than summarised.
    """
    unknown = [a for a in argv if a != '--check']
    if unknown:
        print('usage: rdc_analysis.py build [--check]')
        return 2
    check_only = '--check' in argv

    before = staleness()
    for line in describe(before):
        print(line)

    if check_only:
        return 2 if before['note'] else (1 if before['stale'] else 0)

    if before['note'] == '' and not before['stale']:
        print('nothing to build: the binary is newer than every source')
        return 0

    print('building  : %s (in %s)' % (' '.join(BUILD_COMMAND), before['root']))
    code = rebuild()
    if code != 0:
        print('error: the build failed with exit code %d; the binary is unchanged' % code)
        return 1

    after = staleness()
    for line in describe(after):
        print(line)
    return 0

__all__ = [
    'BUILD_COMMAND',
    'BUILD_DIR',
    'DriverVerdict',
    'EXE_PATH',
    'SOURCE_DIR',
    'SOURCE_SUFFIXES',
    'cmd_build',
    'describe',
    'newest_source',
    'rebuild',
    'repo_root',
    'staleness',
]
