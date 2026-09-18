"""Phase timing and live progress for the long commands: off unless `$RDC_PROFILE`/`$RDC_PROGRESS` asks (REFERENCE 4.13)."""

from __future__ import annotations

import atexit
import functools
import os
import sys
import time

from typing import Callable, Dict, List, Optional, TextIO, Tuple, TypeVar, cast

#: `RDC_PROFILE=1` prints a per-phase table on stderr when the run ends. `RDC_PROGRESS=1` prints the
#: live lines as well; `RDC_PROFILE` alone implies them, because a table with no narration is only
#: useful after the fact. Neither ever touches stdout: the commands' output is the contract.
PROFILE_ENV = 'RDC_PROFILE'
PROGRESS_ENV = 'RDC_PROGRESS'

#: One line per ten seconds -- long enough not to spam a pipe, short enough that a stuck run is
#: visible. A phase shorter than `SUMMARY_SECONDS` says nothing at all: a summary line for two
#: seconds of work is noise.
EVERY_SECONDS = 10.0
SUMMARY_SECONDS = 5.0

#: phase name -> (seconds, calls), plus the order they were first seen in.
_PHASES: Dict[str, Tuple[float, int]] = {}
_ORDER: List[str] = []


def enabled() -> bool:
    """True when the phase table was asked for (`$RDC_PROFILE`)."""
    return bool(os.environ.get(PROFILE_ENV))


def progress_enabled() -> bool:
    """True when the live lines were asked for (`$RDC_PROGRESS`, or `$RDC_PROFILE` which implies them)."""
    return enabled() or bool(os.environ.get(PROGRESS_ENV))


def now() -> float:
    """`time.perf_counter()`, named so call sites read the same way the driver's `Millis()` does."""
    return time.perf_counter()


def add(name: str, since: float, calls: int = 1) -> None:
    """Record `calls` call(s) of the phase `name` that started at `since` seconds."""
    if not enabled():
        return
    seconds, seen = _PHASES.get(name, (0.0, 0))
    if seen == 0:
        _ORDER.append(name)
    _PHASES[name] = (seconds + (now() - since), seen + calls)


def clear() -> None:
    """Forget everything measured -- for the tests, and for an embedder that runs two phases of work."""
    _PHASES.clear()
    del _ORDER[:]


def report(stream: Optional[TextIO] = None) -> None:
    """Print the phase table to stderr, longest phase first; nothing at all when disabled or empty."""
    out = sys.stderr if stream is None else stream
    if not enabled() or not _PHASES:
        return
    width = max(len(name) for name in _PHASES)
    print('profile: where the time went (this run has $%s set)' % PROFILE_ENV, file=out)
    for name in sorted(_ORDER, key=lambda n: -_PHASES[n][0]):
        seconds, calls = _PHASES[name]
        print('profile:   %-*s %8.2fs in %6d call(s)' % (width, name, seconds, calls), file=out)

# The table is printed on the way out, whichever way that is: a command calls `sys.exit`, an exception
# propagates, or the interpreter just finishes. `report` is silent unless `$RDC_PROFILE` was set.
atexit.register(report)


class phase:
    """Context manager timing one phase: `with phase('lz4 decode'): ...`.

    Use it around a whole loop rather than around each item: a per-item timer is a clock read per
    item, which is exactly the thing being measured.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.started = 0.0

    def __enter__(self) -> 'phase':
        self.started = now()
        return self

    def __exit__(self, *exc: object) -> None:
        add(self.name, self.started)


F = TypeVar('F', bound=Callable[..., object])


def timed(name: str) -> Callable[[F], F]:
    """Decorator timing every call of a function as the phase `name`.

    This is how the layers time themselves -- `parse_container`, the cache read, the decompressors,
    the table builders, the payload decoder -- so a profile line names the *work* rather than a command
    that happens to do it, and a command that calls into a layer twice adds two calls to one slot. Only
    plain functions: wrapping a generator would time its creation, not its consumption.
    """
    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> object:
            if not enabled():
                return fn(*args, **kwargs)
            started = now()
            try:
                return fn(*args, **kwargs)
            finally:
                add(name, started)
        return cast(F, wrapper)
    return decorate


def human_time(seconds: float) -> str:
    """`12 s`, `1 min 50 s` or `2 h 5 min`, for a rate line a reader has to act on."""
    whole = int(seconds + 0.5)
    if whole < 90:
        return '%d s' % whole
    if whole < 5400:
        return '%d min %d s' % (whole // 60, whole % 60)
    return '%d h %d min' % (whole // 3600, (whole // 60) % 60)


class Progress:
    """Time-based progress for a loop that can run for minutes, printed on stderr.

    Time-based, not count-based: a count-based line stayed silent through a whole 164 s sweep in the
    driver, because the bound it counted towards was not the bound the loop actually stopped at. A
    line carries the rate and what is left, which is what a reader wants while waiting.
    """

    def __init__(self, what: str, total: int = 0, unit: str = '') -> None:
        self.what = what
        self.total = total
        self.unit = unit          # 'bytes' prints MB and MB/s; anything else prints plain counts
        self.started = now()
        self.last = self.started
        self.logged = False

    def begin(self) -> 'Progress':
        """Start the phase; returns self so a call site can write `progress('x').begin()`.

        The opening line names the size of the job, which is the one thing a reader cannot infer from
        the later lines -- and it is printed only when progress was asked for: nothing here may write
        to stderr otherwise, because "stderr is empty" is part of what the commands promise.
        """
        self.started = self.last = now()
        self.logged = False
        if self.total and self.unit and progress_enabled():
            print('progress: %s: %s' % (self.what, _amount(self.total, self.unit)), file=sys.stderr)
        return self

    def tick(self, done: int) -> None:
        """Emit a line if `EVERY_SECONDS` have passed since the last one."""
        if not progress_enabled():
            return
        current = now()
        if current - self.last < EVERY_SECONDS:
            return
        self.last = current
        self.logged = True
        elapsed = current - self.started
        each = (elapsed / done) if done > 0 else 0.0
        if self.total and 0 < done < self.total:
            left = each * (self.total - done)
            print('progress: %s: %s/%s (%d%%), %s, ~%s left'
                  % (self.what, _amount(done, self.unit), _amount(self.total, self.unit),
                     (100 * done) // self.total, _rate(each, self.unit), human_time(left)),
                  file=sys.stderr)
        else:
            print('progress: %s: %s done, %s' % (self.what, _amount(done, self.unit),
                                                 _rate(each, self.unit)), file=sys.stderr)

    def done(self, done: int) -> None:
        """Emit the closing line -- silent for a short phase that never ticked."""
        if not progress_enabled():
            return
        elapsed = now() - self.started
        if not self.logged and elapsed < SUMMARY_SECONDS:
            return
        print('progress: %s: %s done in %.1fs (%s)'
              % (self.what, _amount(done, self.unit), elapsed, _rate(elapsed / done if done else 0.0,
                                                                     self.unit)),
              file=sys.stderr)

    def note(self, text: str) -> None:
        """One extra line (a fallback taken, a decision made), printed when progress is on."""
        if progress_enabled():
            print('progress: %s: %s' % (self.what, text), file=sys.stderr)


def progress(what: str, total: int = 0, unit: str = '') -> Progress:
    """A `Progress` for the phase `what`; call `.begin()` when the work starts."""
    return Progress(what, total, unit)


def _amount(value: int, unit: str) -> str:
    """`1.47 GB`, `12.0 MB`, `200 B` or the plain count, depending on the phase's unit."""
    if unit == 'bytes':
        if value < 1024:
            return '%d B' % value
        mb = value / 1048576.0
        return '%.1f GB' % (mb / 1024.0) if mb >= 1024 else '%.1f MB' % mb
    return str(value)


def _rate(each: float, unit: str) -> str:
    """`88.0 MB/s` for a byte phase, `0.4 ms each` otherwise: `each` is seconds per unit."""
    if unit == 'bytes':
        return '%.1f MB/s' % (1.0 / (each * 1048576.0)) if each > 0 else '0 MB/s'
    return '%.1f ms each' % (each * 1000.0)


__all__ = [
    'EVERY_SECONDS',
    'PROFILE_ENV',
    'PROGRESS_ENV',
    'Progress',
    'SUMMARY_SECONDS',
    'add',
    'clear',
    'enabled',
    'human_time',
    'now',
    'phase',
    'progress',
    'progress_enabled',
    'report',
    'timed',
]
