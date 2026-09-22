"""The replay driver as a *library*: `bin/rdc_replay.dll` called in this process (REFERENCE §9).

`replay_dump.exe` has one command per process, which is the right shape for a command line and the wrong
one for a tool that asks several questions about one frame: standing the engine up is ~4 s on a small
capture and ~11 s on a 1.4 GB one, and every process pays it again. `bin/rdc_replay.dll` is the same code
with the session held open across calls -- the ABI is `src/cpp/api.h`, its implementation `api.cpp`, and
the two build from the same sources -- so N questions cost one standup and one open capture.

Three things are worth knowing about the split, and they are all the same rule: **the library is the
driver, not a second copy of it**.

* The command surface is the command line's. `Session.command('state 270 --json')` runs exactly what a
  batch file's line runs, through the same dispatcher, so an answer here and an answer from
  `replay_dump.exe` are the same text (the `goldens` corpus compares the executable's text byte for byte,
  and that is what makes this binding trustable).
* A command's *document* is returned; its progress lines and its own failure messages still go to this
  process's stderr, exactly as they do on the command line. `command()` returns the engine's exit code
  beside the text, so a failed command is visible rather than raised (the code is the CLI's contract).
* What raises is the **ABI** refusing the call: no session, an empty command, a command that needs a
  capture in a session opened without one, or a session that could not be opened at all. Those come back
  as `ReplayError` carrying the library's own sentence.

The DLL is loaded lazily, once per process, and only when something actually asks for it -- a run of the
offline tool that never touches the engine must not pay for a DLL load or need RenderDoc installed.
`available()` is the check for that, and it is honest about its own limits: it says whether the library
can be *loaded*, not whether a device exists (`probe` or `info` answers that, with a capture in hand).

Windows only, because the driver is: `available()` is False elsewhere rather than pretending otherwise.
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Dict, List, Optional, Tuple, cast

import rdc_driver

#: The ABI this module speaks. The library reports its own and a mismatch is refused rather than
#: guessed at: `api.h` and this constant are the two halves of one contract.
ABI_VERSION = 'rdc_replay/1'

#: Where the build writes the library, relative to the repository root (`CMakeLists.txt`), and the
#: environment variable that names another one -- the same convention `$RDC_LZ4_DLL` and
#: `$RDC_RENDERDOC_DLL` use.
DLL_PATH = os.path.join('bin', 'rdc_replay.dll')
DLL_ENV = 'RDC_REPLAY_DLL'

#: How much room a call's error sentence gets. The library truncates rather than overruns, and every
#: message it writes is a line of prose: 1 KB is far more than any of them needs.
ERROR_BYTES = 1024

class ReplayError(RuntimeError):
    """The library refused a call -- opening a session, or a call it could not run at all.

    A command that *ran* and answered with a failure is not this: that comes back as its exit code with
    the document, because a failed command's partial output is what a reader wants to see.
    """

def dll_path(root: Optional[str] = None) -> str:
    """Where the library is: `$RDC_REPLAY_DLL`, else `bin/rdc_replay.dll` under the repository root."""
    named = os.environ.get(DLL_ENV, '')
    if named:
        return named
    return os.path.join(root or rdc_driver.repo_root(), DLL_PATH)

def stale_note(dll: Optional[str] = None, root: Optional[str] = None) -> str:
    """What `build --check` would say about this library, as a line to warn with, or an empty string.

    A session answers from the code it loaded, and this is the one place that can say so: the driver's own
    warning (`WarnIfDriverIsStale`) asks about the **host** process -- under a library session that is
    `python.exe`, which has no `src\\cpp` beside it, so it stays quiet exactly when it would be useful.
    `build --check` still covers the library (REFERENCE §4.16), and that gate is what a change is verified
    with; this is the same fact said to the run that is about to trust the answers.

    The comparison is the gate's own, through `rdc_driver.target_staleness`, rather than a second
    reimplementation of it: a run-time warning that disagreed with the build-time verdict would be worse
    than no warning at all.
    """
    target = _replay_target(dll)
    if target is None:
        return ''
    verdict = rdc_driver.target_staleness(target, root)
    if not verdict['stale']:
        return ''
    # The driver's own two lines, in its own words (replay_dump.cpp `WarnIfDriverIsStale`), because a reader
    # who has seen the exe's warning should recognise this one as the same warning.
    return ('warning: this replay library is older than its sources: %s was written %d s later, so every '
            'answer from this session is the previous build\'s\n'
            '         build it with: cmake --build build --config Release'
            % (verdict['source'], int(verdict['newer_seconds'])))

def _replay_target(dll: Optional[str]) -> Optional[rdc_driver.BuildTarget]:
    """The gate's entry for the replay library, with `artefact` pointed at the DLL this process will load.

    Found by the artefact it names rather than by position, so reordering `rdc_driver.TARGETS` cannot make
    this warn about the wrong file; and `None` when there is no such entry, which turns the warning off
    rather than inventing one.
    """
    for target in rdc_driver.TARGETS:
        if target['artefact'] == DLL_PATH:
            # `dict(...)` of a TypedDict is what the update needs, and it is still a `BuildTarget`: the one
            # member replaced is the path, and only when `$RDC_REPLAY_DLL` names another file.
            return cast(rdc_driver.BuildTarget, dict(target, artefact=dll or dll_path()))
    return None

_LOADED: Dict[str, Optional[ctypes.CDLL]] = {}

def _load(path: str) -> Optional[ctypes.CDLL]:
    """The library, loaded once per path, or None when it cannot be loaded at all.

    Cached because a `CDLL` per call would map the DLL and run its initialisers again for every question,
    which is the one cost this module exists to remove; and because loading is what fails on a machine
    without RenderDoc beside it -- `renderdoc.dll` is a load-time dependency -- which must be a `False`
    from `available()` rather than an exception from every caller.
    """
    if path in _LOADED:
        return _LOADED[path]
    loaded: Optional[ctypes.CDLL] = None
    if os.name == 'nt' and os.path.isfile(path):
        try:
            loaded = _bind(ctypes.CDLL(path))
        except OSError:
            loaded = None
        else:
            # Only for a library that actually loaded: a stale one that cannot load at all is a missing
            # build, which is the error the caller reports, not a warning about staleness.
            note = stale_note(path)
            if note:
                print(note, file=sys.stderr)
    _LOADED[path] = loaded
    return loaded

def _bind(lib: ctypes.CDLL) -> ctypes.CDLL:
    """Declare the ABI's five entry points so ctypes checks what it passes and what it gets back."""
    lib.RdcReplayAbi.restype = ctypes.c_char_p
    lib.RdcReplayOpen.restype = ctypes.c_void_p
    lib.RdcReplayOpen.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    lib.RdcReplayCommand.restype = ctypes.c_int
    lib.RdcReplayCommand.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p),
                                     ctypes.c_char_p, ctypes.c_int]
    lib.RdcReplayClose.restype = None
    lib.RdcReplayClose.argtypes = [ctypes.c_void_p]
    lib.RdcReplayFree.restype = None
    lib.RdcReplayFree.argtypes = [ctypes.c_void_p]
    return lib

def abi(root: Optional[str] = None) -> str:
    """The library's own ABI string, or '' when there is no usable library here."""
    lib = _load(dll_path(root))
    if lib is None:
        return ''
    return (lib.RdcReplayAbi() or b'').decode('utf-8', 'replace')

def available(root: Optional[str] = None) -> bool:
    """True when the library is here, loadable, and speaks an ABI this module knows."""
    return abi(root) == ABI_VERSION

class Session:
    """One open capture (or none), held open across `command` calls.

    A session is a *resource*: it holds a replay device, and the engine creates one per process, so a
    caller keeps one open and closes it (`with` does both). Two sessions at once on one GPU is the thing
    the driver's own documentation warns about, and nothing here prevents it -- the caller decides.
    """

    def __init__(self, capture: Optional[str] = None, log: Optional[str] = None,
                 root: Optional[str] = None) -> None:
        lib = _load(dll_path(root))
        if lib is None:
            raise ReplayError('no replay library at %s (build it: `cmake --build build --config Release`)'
                              % dll_path(root))
        self._lib = lib
        self.capture_path = capture or ''
        err = ctypes.create_string_buffer(ERROR_BYTES)
        self._handle = lib.RdcReplayOpen(_bytes(self.capture_path), _bytes(log), err, ERROR_BYTES)
        if not self._handle:
            raise ReplayError(err.value.decode('utf-8', 'replace') or 'the library refused the session')
        self._open = True

    @property
    def abi_version(self) -> str:
        """The ABI the loaded library implements (`api.h`); the caller compares it once."""
        return (self._lib.RdcReplayAbi() or b'').decode('utf-8', 'replace')

    def command(self, line: str) -> Tuple[int, str]:
        """Run one command line (`state 270 --json`), returning `(exit code, document)`.

        The exit code is the driver's own: 0 answered, 1 failed, 2 bad arguments. The document is what
        the command printed to stdout, exactly as the command line would have printed it, and is empty
        when the command produced nothing (a failure, usually -- its message is on stderr).
        """
        if not self._open:
            raise ReplayError('this session is closed')
        err = ctypes.create_string_buffer(ERROR_BYTES)
        out = ctypes.c_char_p()
        code = self._lib.RdcReplayCommand(self._handle, _bytes(line), ctypes.byref(out), err, ERROR_BYTES)
        message = err.value.decode('utf-8', 'replace')
        if message:
            if out:
                self._lib.RdcReplayFree(out)
            raise ReplayError(message)
        try:
            return code, (out.value or b'').decode('utf-8', 'replace')
        finally:
            if out:
                self._lib.RdcReplayFree(out)

    def close(self) -> None:
        """Close the session: the controller, the capture file and the replay system, in that order."""
        if self._open:
            self._open = False
            self._lib.RdcReplayClose(self._handle)
            self._handle = None

    def __enter__(self) -> 'Session':
        return self

    def __exit__(self, _kind: object, _value: object, _trace: object) -> None:
        self.close()

    def __del__(self) -> None:
        """Close on collection, so a caller that forgot `with` does not leave a device behind."""
        try:
            self.close()
        except Exception:    # noqa: BLE001 - an interpreter shutting down has nothing left to report
            pass

def _bytes(text: Optional[str]) -> Optional[bytes]:
    """A `char *` argument: None stays None (the library reads that as "not given")."""
    return None if text is None else text.encode('utf-8')

def run(capture: Optional[str], lines: List[str], log: Optional[str] = None,
        root: Optional[str] = None) -> List[Tuple[int, str]]:
    """One session, several lines, `(exit code, document)` each -- the whole point of the library."""
    with Session(capture, log, root) as session:
        return [session.command(line) for line in lines]

__all__ = [
    'ABI_VERSION',
    'DLL_ENV',
    'DLL_PATH',
    'ReplayError',
    'Session',
    'abi',
    'available',
    'dll_path',
    'run',
]
