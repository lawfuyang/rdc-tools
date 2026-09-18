"""The whole-stream string scan, split across processes (REFERENCE 4.13).

Two commands -- `strings` and `names` -- scan every byte of the frame stream with one regex, and that
single `re` call is the whole cost of the command: measured on the 1.47 GB hobby capture, 14.5 s of a
17.6 s run, at ~100 MB/s. Nothing around it matters (the container is 0.4 s, counting and
de-duplicating the 588,900 matches 0.3 s), and no cache can help a first look.

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
import multiprocessing
import os

from typing import Dict, List, Optional, Tuple

#: Below this a pool costs more than it saves: the spawn is ~0.5 s, the scan of a smaller stream less.
MIN_BYTES = 64 << 20
#: Slices, i.e. the cap on workers -- never more than the cores there are, and the sweep that picked
#: this (REFERENCE 4.13) is on the hobby capture: 8 slices 7.3 s, 16 slices 5.3 s, 32 slices 4.0 s for
#: `minlen=6`, and 4.9 / 3.1 / 2.2 s for `minlen=10`, each including the spawn.
MAX_SLICES = 32
#: How far a slice boundary may be pushed to find a byte no match can cross. A longer printable run
#: than this (a text blob) just means fewer slices, never a wrong answer.
CUT_SEARCH = 4 << 20
#: The serial path scans in windows this big, so a long scan can report progress at all -- one
#: `finditer` over 1.4 GB cannot say how far it is.
SERIAL_WINDOW = 128 << 20

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
                with multiprocessing.Pool(len(jobs)) as pool:
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
    'MAX_SLICES',
    'MIN_BYTES',
    'SERIAL_WINDOW',
    'scan_runs',
    'split_ranges',
]
