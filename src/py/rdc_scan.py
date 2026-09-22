"""The whole-stream string scan, split across processes (REFERENCE 4.13).

Two commands -- `strings` and `names` -- scan every byte of the frame stream with one regex, and that
single `re` call is the whole cost of the command: measured on `desktop-2` (1.47 GB), 14.5 s of a
17.6 s run, at ~100 MB/s. Nothing around it matters (the container is 0.4 s, counting and
de-duplicating the 588,900 matches 0.3 s), and no cache can help a first look.
A third kind of scan is not a string run at all: the DXBC container search that `draws`, `rootsig`, `dxbc`
and `dump-shaders` each pay over the whole stream is a byte-pattern `find` (`find_all` below). The same shape
of problem with different arithmetic -- `find` runs at ~1.2 GB/s where the `re` pass runs at ~100 MB/s -- so
its pool has to save less before it pays, and it carries its own threshold (`FIND_MIN_BYTES`, a gigabyte,
measured) rather than borrowing `MIN_BYTES`. It carries its own worker cap too (`FIND_MAX_SLICES`, eight,
against `MAX_SLICES`' 32): a `find` is bandwidth bound, so past a handful of workers the spawn is the whole
cost of the extras. `find_all_many` is the same scan for a list of needles over one pool, which is what
keeps `count <rdc> a b c` at one pool and one mapping per slice instead of one of each per pattern.

Threads cannot help either: `re` holds the GIL for the length of the call, so the work is one core's.
Processes can, and this module is that: the stream is cut into slices at boundaries no match can cross,
each slice is scanned in its own process, and the per-slice results are merged in offset order. The
children do not receive the bytes -- they map the stream cache's file themselves, so a 1.47 GB scan
costs no copies, and a scan with no cache file to map falls back to the serial loop unchanged.

Measured on that capture: the serial scan of `strings` 15.0 s, the same scan in 16 slices 3.4 s, with
the two agreeing match for match (and the whole command 17.6 s -> 10.2 s; `names` 17.7 s -> 6.5 s).

A worker is a fresh interpreter, so the process that starts a pool must be importable without side
effects -- `rdc_analysis.py` guards its `main()` and is fine; a throwaway script that decompresses at
module level will be decompressed again by every worker, once per worker.
"""

from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
import rdc_profile

import mmap
import os

from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:       # `multiprocessing` is imported where a pool is made, not here: see `_pool`
    import multiprocessing.pool

#: Below this a pool costs more than it saves: the spawn is ~0.5 s, the scan of a smaller stream less.
MIN_BYTES = 64 << 20
#: Slices, i.e. the cap on workers -- never more than the cores there are, and the sweep that picked
#: this (REFERENCE 4.13) is on `desktop-2`: 8 slices 7.3 s, 16 slices 5.3 s, 32 slices 4.0 s for
#: `minlen=6`, and 4.9 / 3.1 / 2.2 s for `minlen=10`, each including the spawn.
MAX_SLICES = 32
#: How far a slice boundary may be pushed to find a byte no match can cross. A longer printable run
#: than this (a text blob) just means fewer slices, never a wrong answer.
CUT_SEARCH = 4 << 20
#: The serial path scans in windows this big, so a long scan can report progress at all -- one
#: `finditer` over 1.4 GB cannot say how far it is.
SERIAL_WINDOW = 128 << 20

#: A byte-pattern find costs a *pool* to parallelise, and a pool costs 0.48 s here (every worker is a
#: fresh interpreter, 0.37 s of that its own startup -- see `MIN_BYTES`), so it only pays where the find
#: is long enough. Measured on the desktop captures: `desktop-2`'s 1.47 GB stream goes
#: 1.25 s -> 0.67 s in 16 or 32 slices, while `desktop-1`'s 631 MB goes 0.53 s -> 0.62 s, i.e. *slower*.
#: Hence a gigabyte -- sixteen times the string scan's threshold, because that scan is 14 s where this
#: one is 1 s -- and the serial loop below it, which is what every caller got before this existed.
FIND_MIN_BYTES = 1 << 30

#: The byte find's own worker cap, and deliberately not `MAX_SLICES`. A `find` is memory-bandwidth
#: bound where the `re` pass is CPU-bound, so past a handful of workers the only thing still growing is
#: more interpreters: measured best-of-three on `desktop-2` (1.47 GB, `DXBC`, pages warm), 1 slice
#: 0.678 s, 4 slices 0.702 s, **8 slices 0.554 s**, 16 slices 0.613 s, 32 slices 0.741 s -- the 32-group
#: that used to be the default is the *worst* of the five, and 8 is 25% better than it. The string scan
#: keeps `MAX_SLICES`: the same session measured 16 slices 2.50 s against 32 slices 2.21 s there.
FIND_MAX_SLICES = 8

#: text -> times seen in the slice, text -> first offset in the slice (offset relative to the stream).
Counts = Dict[str, int]
Firsts = Dict[str, int]


def _printable(byte: int) -> bool:
    """True for a byte the string pattern matches (`[\\x20-\\x7e]`)."""
    return 0x20 <= byte <= 0x7E


def split_ranges(stream: Buffer, want: int) -> List[Tuple[int, int]]:
    """Cut `stream` into at most `want` ranges, each ending where no string run crosses.

    A boundary is only safe where the byte is *not* printable: a run cannot contain such a byte, so no
    match can span the cut and the concatenated per-range results are exactly the whole-stream ones.
    The search for one is bounded (`CUT_SEARCH`); if it finds none the range simply does not get cut,
    which loses parallelism but never correctness.
    """
    total = len(stream)
    if want < 2 or total <= 0:
        return [(0, total)]
    step = max(1, total // want)
    ranges: List[Tuple[int, int]] = []
    start = 0
    while start < total and len(ranges) < want - 1:
        want_at = min(total, start + step)
        if want_at >= total:
            break
        cut = _safe_cut(stream, want_at)
        if cut is None:
            break
        ranges.append((start, cut))
        start = cut
    ranges.append((start, total))
    return ranges


def _safe_cut(stream: Buffer, pos: int) -> Optional[int]:
    """The first offset at or after `pos` holding a non-printable byte, or None within `CUT_SEARCH`."""
    limit = min(len(stream), pos + CUT_SEARCH)
    at = pos
    while at < limit:
        if not _printable(stream[at]):
            return at
        at += 1
    return None


def _scan_range(buf: Buffer, start: int, end: int, minlen: int, base: int, counts: bool,
                into_counts: Optional[Counts] = None,
                into_firsts: Optional[Firsts] = None) -> Tuple[Counts, Firsts]:
    """Scan `buf[start:end]`: return (times seen, first offset) per text, offsets relative to `base`.

    Passing `into_counts`/`into_firsts` accumulates into the caller's dicts instead of new ones, which
    is what the serial path does: it scans in windows (so a long scan can report progress at all), and
    merging a window's 1.16 M keys into a running total afterwards cost more than the scan itself -- 7 s
    of a 22 s serial scan. Accumulating in place is exactly the loop the commands used to run, which is
    the loop this must not be slower than; ranges come in ascending offset order, so `setdefault` still
    keeps the lowest offset per text.
    """
    seen: Counts = {} if into_counts is None else into_counts
    firsts: Firsts = {} if into_firsts is None else into_firsts
    for offset, text in string_runs(buf, minlen, start, end):    # type: ignore[arg-type]
        if counts:
            seen[text] = seen.get(text, 0) + 1
        firsts.setdefault(text, offset - base)
    return seen, firsts


def _worker(job: Tuple[str, int, int, int, int, bool]) -> Tuple[Counts, Firsts]:
    """One slice, in a worker process: map the cache file and scan our range of it.

    The stream's first byte sits at `base` in the file, and the children map the file read-only, so the
    parent's copy of the stream is never sent anywhere.
    """
    path, base, start, end, minlen, counts = job
    with open(path, 'rb') as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            return _scan_range(mapped, base + start, base + end, minlen, base, counts)


def _source_holds_stream(stream: Buffer, source: CacheEntry) -> bool:
    """True when `source`'s cached stream is byte-identical to `stream` at both ends.

    A stale or foreign cache file is already rejected by the cache's own identity check; this is the
    cheap second look before a worker is allowed to read the bytes from it instead of being handed them.
    """
    if source['streamLen'] != len(stream) or len(stream) < 128:
        return False
    try:
        with open(source['file'], 'rb') as fh:
            fh.seek(source['hdrLen'])
            head = fh.read(64)
            fh.seek(source['hdrLen'] + len(stream) - 64)
            tail = fh.read(64)
    except OSError:
        return False
    return head == stream[:64] and tail == stream[-64:]


def _merge_counts(into: Counts, part: Counts) -> None:
    """Add one slice's occurrence counts into the running total."""
    for text, n in part.items():
        into[text] = into.get(text, 0) + n


def _merge_firsts(into: Firsts, part: Firsts) -> None:
    """Keep, per text, the lowest first offset across the slices."""
    for text, offset in part.items():
        seen = into.get(text)
        if seen is None or offset < seen:
            into[text] = offset


def _find_serial(stream: Buffer, needle: bytes) -> List[int]:
    """Every offset of `needle`, in one `find` loop -- what `find_all` is measured against."""
    hits: List[int] = []
    at = stream.find(needle)
    while at >= 0:
        hits.append(at)
        at = stream.find(needle, at + 1)
    return hits


def _pool(workers: int) -> multiprocessing.pool.Pool:
    """A worker pool, and the only place one is made.

    `multiprocessing` is imported *here* rather than at the top of the module: it is 11 ms of startup
    (measured with `python -X importtime`, which is 8% of a `summary`'s 0.13 s), and the commands that
    import this module -- every one of them, since `rdc_analysis` re-exports it -- are mostly commands
    that never start a pool: `summary`, `resources`, `descriptors`, `rootsig`, `vram`, `memory`. It is
    also the seam the tests use, so "a pool that cannot start" stays testable without one.
    """
    import multiprocessing
    return multiprocessing.Pool(workers)


def _find_range(mapped: mmap.mmap, base: int, start: int, end: int, needle: bytes) -> List[int]:
    """The offsets of `needle` whose *first byte* is inside `[start, end)`, read out of `mapped`.

    Two bounds matter, and both are off by `len(needle) - 1` in opposite directions. The search starts
    that far *before* the slice, so a needle beginning in the previous slice's tail is still findable
    here (and then dropped, because its first byte is not in this slice). And it runs that far *past*
    the slice, because `find` only reports a match that fits inside its bounds -- a needle starting in
    the last three bytes of a slice ends outside it, and this slice is the one that owns it. Without
    that second bound the needle is owned by nobody: measured by the test that plants one two bytes
    before each cut, which found 2 of 5 needles until the bound was fixed.
    """
    hits: List[int] = []
    limit = min(len(mapped), base + end + len(needle) - 1)
    at = mapped.find(needle, base + max(0, start - (len(needle) - 1)), limit)
    while at >= 0:
        offset = at - base
        if offset >= end:
            break
        if offset >= start:
            hits.append(offset)
        at = mapped.find(needle, at + 1, limit)
    return hits


def _find_worker(job: Tuple[str, int, int, int, bytes]) -> List[int]:
    """One slice, in a worker process: map the cache file and find one needle in it."""
    path, base, start, end, needle = job
    with open(path, 'rb') as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            return _find_range(mapped, base, start, end, needle)


def _find_worker_many(job: Tuple[str, int, int, int, List[bytes]]) -> List[List[int]]:
    """One slice, in a worker process: every needle, over one mapping and one pass each.

    What this saves is the *pool*, not the passes: `count` with three patterns used to start three
    pools (measured on `desktop-2`: 0.646 s for one pattern against 1.635 s for three), where the
    mapping, the spawn and the slice arithmetic are the same work three times over.
    """
    path, base, start, end, needles = job
    with open(path, 'rb') as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            return [_find_range(mapped, base, start, end, needle) for needle in needles]


def _find_ranges(stream: Buffer, source: Optional[CacheEntry],
                 procs: Optional[int]) -> Optional[List[Tuple[int, int]]]:
    """The slices a find should run in, or None when it has to be done in this process.

    None is the serial loop, and it is what `procs=1`, a stream under `FIND_MIN_BYTES` (unless `procs`
    forces the count) and a cache file that does not hold this stream all mean -- the three refusals
    the parallel path has always made. The worker count is `FIND_MAX_SLICES`, not `MAX_SLICES`: see the
    constant for why a byte find wants a handful of workers where the `re` pass wants every core.
    """
    total = len(stream)
    workers = min(os.cpu_count() or 1, FIND_MAX_SLICES) if procs is None else max(1, procs)
    if workers <= 1 or source is None:
        return None
    if procs is None and total < FIND_MIN_BYTES:
        return None
    if not _source_holds_stream(stream, source):
        return None
    cuts = [total * i // workers for i in range(workers + 1)]
    return [(cuts[i], cuts[i + 1]) for i in range(workers)]


def find_all(stream: Buffer, needle: bytes, source: Optional[CacheEntry] = None,
             procs: Optional[int] = None) -> List[int]:
    """Every offset of `needle` in `stream`, ascending: a `find` loop's answer, in slices.

    For the scans that are a *byte pattern* rather than a string run -- the DXBC container search that
    `draws`, `rootsig`, `dxbc` and `dump-shaders` each pay for over the whole stream. `split_ranges` is
    not used here: its cut rule exists because a string run may cross a boundary, and a byte pattern may
    start anywhere, so even cuts with a `len(needle) - 1` byte overlap give exactly the same answer with
    a cheaper split. The parallel path needs the same two things `scan_runs` does -- a stream big enough
    (`FIND_MIN_BYTES`, measured) and a cache file to map instead of copying the bytes -- and falls back
    to the serial loop when either is missing, or when the pool itself cannot start.

    No progress bar: where this is parallel at all the whole find is under a second, and the bar's own
    eight-second threshold would print nothing for it.
    """
    if not needle:
        return []
    ranges = _find_ranges(stream, source, procs)
    if ranges is None:
        return _find_serial(stream, needle)

    assert source is not None    # narrowed by `_find_ranges`
    jobs = [(source['file'], source['hdrLen'], start, end, needle) for start, end in ranges]
    try:
        with rdc_profile.phase('byte find (parallel)'):
            with _pool(len(jobs)) as pool:
                parts = pool.map(_find_worker, jobs)
    except Exception:    # noqa: BLE001 - any pool failure just means "do it here instead"
        return _find_serial(stream, needle)

    hits: List[int] = []
    for part in parts:
        hits.extend(part)
    return hits


def find_all_many(stream: Buffer, needles: Sequence[bytes],
                  source: Optional[CacheEntry] = None,
                  procs: Optional[int] = None) -> List[List[int]]:
    """`find_all` for several needles over one stream: the offsets of each, in the order asked for.

    The answer is the same as calling `find_all` once per needle -- the offsets of each needle are
    ascending and the needles are independent -- and the serial path *is* that loop, so nothing about
    the answer depends on how many needles there are. What changes is the parallel path: one pool, one
    mapping per slice, one pass per needle inside it (`_find_worker_many`), instead of a whole pool and
    a fresh mapping per pattern. That is `count`'s case (REFERENCE 4.13), and an empty needle keeps
    `find_all`'s answer for it: no offsets, because a zero-length pattern is not a `find` question.
    """
    if not needles:
        return []
    hits: List[List[int]] = [[] for _ in needles]
    # An empty needle is dropped before anything scans: `find_all` answers it with no offsets, and a
    # `find` loop cannot (an empty pattern matches at every offset). One list of the live needles and
    # the slots they came from keeps the answers where the caller asked for them.
    wanted = [index for index, needle in enumerate(needles) if needle]
    if not wanted:
        return hits
    active = [needles[index] for index in wanted]

    ranges = _find_ranges(stream, source, procs)
    if ranges is None:
        for index, needle in zip(wanted, active):
            hits[index] = _find_serial(stream, needle)
        return hits

    assert source is not None    # narrowed by `_find_ranges`
    jobs = [(source['file'], source['hdrLen'], start, end, active) for start, end in ranges]
    try:
        with rdc_profile.phase('byte find (parallel)'):
            with _pool(len(jobs)) as pool:
                parts = pool.map(_find_worker_many, jobs)
    except Exception:    # noqa: BLE001 - any pool failure just means "do it here instead"
        for index, needle in zip(wanted, active):
            hits[index] = _find_serial(stream, needle)
        return hits

    for part in parts:
        for slot, index in enumerate(wanted):
            hits[index].extend(part[slot])
    return hits


def scan_runs(stream: Buffer, minlen: int, source: Optional[CacheEntry] = None,
              procs: Optional[int] = None, counts: bool = True) -> Tuple[Counts, Firsts]:
    """Every string run of `minlen` or more: (times seen, first offset) per text, over `stream`.

    The result is identical to walking `string_runs(stream, minlen)` and counting, which is what the
    serial path here does and what the tests compare the parallel path against. `procs` forces a
    process count (the tests use it, and so does anyone measuring); left None the pool is used only for
    a stream big enough to pay for it (`MIN_BYTES`) with a `source` cache file to map, and `procs=1`
    always means the serial path. `counts=False` skips the counting, for a caller that only wants the
    offsets (`names` does).
    """
    total = len(stream)
    workers = min(os.cpu_count() or 1, MAX_SLICES) if procs is None else max(1, procs)
    parallel = (workers > 1 and source is not None and (procs is not None or total >= MIN_BYTES)
                and _source_holds_stream(stream, source))
    ranges = split_ranges(stream, workers if parallel else max(1, total // SERIAL_WINDOW))
    bar = rdc_profile.progress('string scan: %d slice(s)' % len(ranges), total, 'bytes')
    bar.begin()

    seen: Counts = {}
    firsts: Firsts = {}
    done = 0
    if parallel:
        assert source is not None    # narrowed by `parallel`
        jobs = [(source['file'], source['hdrLen'], start, end, minlen, counts) for start, end in ranges]
        slice_bytes = [end - start for start, end in ranges]
        try:
            with rdc_profile.phase('string scan (parallel)'):
                with _pool(len(jobs)) as pool:
                    for index, (part_counts, part_firsts) in enumerate(pool.imap(_worker, jobs)):
                        if counts:
                            _merge_counts(seen, part_counts)
                        _merge_firsts(firsts, part_firsts)
                        done = min(total, done + slice_bytes[index])
                        bar.tick(done)
        except Exception as exc:    # noqa: BLE001 - any pool failure just means "do it here instead"
            bar.note('process pool unavailable (%s), scanning in this process' % exc)
            seen, firsts, done, parallel = {}, {}, 0, False
    if not parallel:
        with rdc_profile.phase('string scan (serial)'):
            for start, end in ranges:
                _scan_range(stream, start, end, minlen, 0, counts, seen, firsts)
                done += end - start
                bar.tick(done)
    bar.done(total)
    return seen, firsts


__all__ = [
    'CUT_SEARCH',
    'FIND_MAX_SLICES',
    'FIND_MIN_BYTES',
    'MAX_SLICES',
    'MIN_BYTES',
    'SERIAL_WINDOW',
    'find_all',
    'find_all_many',
    'scan_runs',
    'split_ranges',
]
