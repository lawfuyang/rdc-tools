"""The parallel slice scan and the phase/progress instrumentation.

Both halves are about the same thing -- a whole-stream scan costs one `re` call, and that call is the
whole command -- so they are tested together: `rdc_scan` for the split (a cut must never touch a match,
a worker must never read a file that is not this stream, and a pool that cannot start must not change
the answer), `rdc_profile` for the table and the lines around it, which must be silent unless asked for.

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_scan.py
    python -m unittest tests.test_rdc_scan
    python src/py/rdc_analysis.py selftest -k Scan
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from typing import Dict, List, Sequence, Tuple
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R            # noqa: E402
import rdc_profile                  # noqa: E402
import rdc_scan                     # noqa: E402

from rdc_testcase import *          # noqa: E402,F401,F403

def sample_stream(runs: int = 200) -> bytes:
    """A buffer with printable runs, junk between them and UTF-16 text, long enough to be split.

    Every piece of junk is generated so that it holds non-printable bytes: a slice boundary is only
    allowed where a run cannot cross, so a buffer without them cannot be cut at all.
    """
    parts = []
    for i in range(runs):
        parts.append(bytes(((i * 5 + j) * 3) % 32 for j in range(23)))
        parts.append(('Name_%04d_BasePass' % i).encode('ascii'))
        parts.append(('Wide%04d' % i).encode('utf-16-le'))
        parts.append(b'\x00\xff')
    return b''.join(parts)

def serial_runs(stream: bytes, minlen: int) -> Tuple[Dict[str, int], Dict[str, int]]:
    """The scan `rdc_scan` is measured against: one pass, no windows, no processes."""
    counts: Dict[str, int] = {}
    firsts: Dict[str, int] = {}
    for offset, text in R.string_runs(stream, minlen):
        counts[text] = counts.get(text, 0) + 1
        firsts.setdefault(text, offset)
    return counts, firsts

# =========================================================================== splitting
class TestSplitRanges(unittest.TestCase):
    def test_ranges_cover_the_stream_in_order(self):
        stream = sample_stream()
        ranges = R.split_ranges(stream, 5)
        self.assertGreater(len(ranges), 1)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], len(stream))
        for (start, end), (nstart, _nend) in zip(ranges, ranges[1:]):
            self.assertLess(start, end)
            self.assertEqual(end, nstart)

    def test_every_cut_is_a_byte_no_match_can_cross(self):
        stream = sample_stream()
        for start, _end in R.split_ranges(stream, 6)[1:]:
            self.assertFalse(0x20 <= stream[start] <= 0x7E,
                             'cut at %d is printable, a run could cross it' % start)

    def test_no_cut_is_possible_in_printable_text(self):
        stream = b'a' * 4096
        self.assertEqual(R.split_ranges(stream, 8), [(0, len(stream))])

    def test_the_search_for_a_cut_is_bounded(self):
        # a mixed stream, but with no room at all to look for a boundary: it stays one range rather
        # than cutting somewhere a run could cross.
        stream = sample_stream()
        with mock.patch.object(rdc_scan, 'CUT_SEARCH', 0):
            self.assertEqual(R.split_ranges(stream, 8), [(0, len(stream))])

    def test_a_single_range_is_asked_for_nothing(self):
        stream = sample_stream()
        self.assertEqual(R.split_ranges(stream, 1), [(0, len(stream))])

    def test_empty_stream(self):
        self.assertEqual(R.split_ranges(b'', 4), [(0, 0)])

# =========================================================================== scanning
class TestScanRuns(TempDirCase):
    def source_for(self, stream: bytes, name: str = 'stream.bin') -> R.CacheEntry:
        """A cache-file stand-in: a header the scan never reads, then exactly `stream`."""
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as fh:
            fh.write(b'RDCCACHE' + b'\x00' * 8)
            fh.write(stream)
        return {'file': path, 'hdrLen': 16, 'streamLen': len(stream), 'section': 0, 'method': 1,
                'blocks': 1, 'srcPath': 'test', 'srcSize': len(stream), 'srcMtime': 1}

    def test_the_pool_agrees_with_the_serial_scan(self):
        stream = sample_stream()
        expected = serial_runs(stream, 6)
        counts, firsts = R.scan_runs(stream, 6, self.source_for(stream), procs=4)
        self.assertEqual((counts, firsts), expected)
        self.assertGreater(len(firsts), 100)

    def test_the_pool_agrees_with_the_serial_scan_without_counting(self):
        stream = sample_stream()
        _counts, firsts = R.scan_runs(stream, 6, self.source_for(stream), procs=4, counts=False)
        self.assertEqual(firsts, serial_runs(stream, 6)[1])
        self.assertEqual(_counts, {})

    def test_a_file_that_is_not_this_stream_is_ignored(self):
        stream = sample_stream()
        source = self.source_for(stream)
        with open(source['file'], 'wb') as fh:      # same length, different bytes
            fh.write(b'RDCCACHE' + b'\x00' * 8)
            fh.write(b'z' * len(stream))
        counts, firsts = R.scan_runs(stream, 6, source, procs=4)
        self.assertEqual((counts, firsts), serial_runs(stream, 6))

    def test_a_pool_that_cannot_start_still_gives_the_same_answer(self):
        stream = sample_stream()
        with mock.patch.object(rdc_scan, '_pool',
                               side_effect=OSError('no processes here')):
            counts, firsts = R.scan_runs(stream, 6, self.source_for(stream), procs=4)
        self.assertEqual((counts, firsts), serial_runs(stream, 6))

    def test_a_small_stream_does_not_start_a_pool(self):
        # `procs=None` means "use a pool only if the stream is big enough and there is a file to map"
        stream = sample_stream(20)
        with mock.patch.object(rdc_scan, '_pool',
                               side_effect=AssertionError('a pool was started for a small stream')):
            counts, firsts = R.scan_runs(stream, 6, self.source_for(stream))
        self.assertEqual((counts, firsts), serial_runs(stream, 6))

    def test_no_source_means_no_pool(self):
        stream = sample_stream()
        with mock.patch.object(rdc_scan, '_pool',
                               side_effect=AssertionError('a pool was started without a source')):
            counts, firsts = R.scan_runs(stream, 6, None)
        self.assertEqual((counts, firsts), serial_runs(stream, 6))

    def test_a_stream_shorter_than_the_file_check_needs_is_not_mapped(self):
        stream = b'\x00abcdefgh\x00' * 8        # under 128 bytes: the file check cannot compare ends
        with mock.patch.object(rdc_scan, '_pool',
                               side_effect=AssertionError('mapped a stream too short to check')):
            counts, firsts = R.scan_runs(stream, 6, self.source_for(stream, 'short.bin'))
        self.assertEqual((counts, firsts), serial_runs(stream, 6))

# =========================================================================== the byte-pattern find
class TestFindAll(TempDirCase):
    """`find_all`: the sliced `find` the DXBC container search goes through.

    Its cut rule is not `split_ranges`' -- a byte pattern may start anywhere, so the slices are even
    cuts with a `len(needle) - 1` byte overlap -- and the whole answer depends on that overlap: a
    needle starting inside the overlap belongs to the *next* slice, and one that starts in a slice
    must not be reported by it twice. Both are planted below, right at the cuts.
    """

    def source_for(self, stream: bytes, name: str = 'find.bin') -> R.CacheEntry:
        """A cache-file stand-in: a header the find never reads, then exactly `stream`."""
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as fh:
            fh.write(b'RDCCACHE' + b'\x00' * 8)
            fh.write(stream)
        return {'file': path, 'hdrLen': 16, 'streamLen': len(stream), 'section': 0, 'method': 1,
                'blocks': 1, 'srcPath': 'test', 'srcSize': len(stream), 'srcMtime': 1}

    def planted(self, needles: Sequence[int], size: int = 4000) -> Tuple[bytes, List[int]]:
        """`size` bytes of filler with `DXBC` planted at offsets that sit on the 4-slice cuts."""
        data = bytearray(b'A' * size)
        for at in needles:
            data[at:at + 4] = b'DXBC'
        stream = bytes(data)
        expected = [i for i in range(0, len(stream) - 3) if stream[i:i + 4] == b'DXBC']
        return stream, expected

    def test_the_pool_finds_exactly_what_a_find_loop_finds(self):
        stream = sample_stream()
        needle = b'\x00\xff'
        expected = [i for i in range(0, len(stream) - 1) if stream[i:i + 2] == needle]
        self.assertGreater(len(expected), 10)
        self.assertEqual(R.find_all(stream, needle), expected)
        self.assertEqual(R.find_all(stream, needle, self.source_for(stream), procs=4), expected)

    def test_a_needle_across_a_cut_is_found_once(self):
        # With `procs=4` the cuts of this stream are at 1000, 2000 and 3000, and a needle starts one
        # or two bytes before each of them -- plus one at each end.
        stream, expected = self.planted([0, 998, 1998, 2999, 3996])
        self.assertEqual(len(expected), 5)
        self.assertEqual(R.find_all(stream, b'DXBC', self.source_for(stream), procs=4), expected)

    def test_a_run_of_needles_right_up_to_a_cut_is_found_once_each(self):
        # Four needles back to back, the last of them ending one byte past the cut at 2000: a slice
        # that reported a hit it does not hold, or dropped one it does, shows up here.
        stream, expected = self.planted([1988, 1992, 1996, 2000, 2004])
        self.assertEqual(len(expected), 5)
        self.assertEqual(R.find_all(stream, b'DXBC', self.source_for(stream), procs=4), expected)

    def test_no_source_means_no_pool(self):
        stream, expected = self.planted([100, 2000])
        with mock.patch.object(rdc_scan, '_pool',
                               side_effect=AssertionError('a pool was started without a source')):
            self.assertEqual(R.find_all(stream, b'DXBC'), expected)

    def test_a_small_stream_does_not_start_a_pool(self):
        # `procs=None` is "only if the stream is big enough to pay for the start", which a 4 KB
        # fixture never is: `FIND_MIN_BYTES` is a gigabyte, measured (a pool costs 0.48 s here).
        stream, expected = self.planted([100, 2000])
        with mock.patch.object(rdc_scan, '_pool',
                               side_effect=AssertionError('a pool was started for a small stream')):
            self.assertEqual(R.find_all(stream, b'DXBC', self.source_for(stream)), expected)

    def test_a_pool_that_cannot_start_still_answers(self):
        stream, expected = self.planted([0, 1998, 2999, 3996])
        with mock.patch.object(rdc_scan, '_pool',
                               side_effect=OSError('no processes here')):
            self.assertEqual(R.find_all(stream, b'DXBC', self.source_for(stream), procs=4), expected)

    def test_a_file_that_is_not_this_stream_is_ignored(self):
        stream, expected = self.planted([0, 1998, 3996])
        source = self.source_for(stream)
        with open(source['file'], 'wb') as fh:      # same length, different bytes
            fh.write(b'RDCCACHE' + b'\x00' * 8)
            fh.write(b'z' * len(stream))
        self.assertEqual(R.find_all(stream, b'DXBC', source, procs=4), expected)

    def test_an_empty_needle_matches_nothing(self):
        stream, _expected = self.planted([100])
        self.assertEqual(R.find_all(stream, b''), [])
        self.assertEqual(R.find_all(stream, b'', self.source_for(stream), procs=4), [])

    def test_the_default_worker_count_is_the_byte_find_cap(self):
        """`procs=None` uses `FIND_MAX_SLICES`, which is deliberately below the string scan's 32.

        Read through `_find_ranges`, which is the whole decision: a byte find is memory-bandwidth
        bound, so every worker past a handful costs a fresh interpreter and buys nothing (the curve is
        in the constant's own comment).
        """
        self.assertLess(rdc_scan.FIND_MAX_SLICES, rdc_scan.MAX_SLICES)
        stream, _expected = self.planted([100, 2000])
        with mock.patch.object(rdc_scan, 'FIND_MIN_BYTES', 1):     # "big enough to pay for a pool"
            ranges = rdc_scan._find_ranges(stream, self.source_for(stream), None)
        self.assertIsNotNone(ranges)
        self.assertEqual(len(ranges or []), min(os.cpu_count() or 1, rdc_scan.FIND_MAX_SLICES))

    def test_the_multi_needle_find_agrees_with_one_call_per_needle(self):
        stream = sample_stream()
        needles = [b'Name_00', b'\x00\xff', b'Wide0007']
        one_each = [R.find_all(stream, n) for n in needles]
        self.assertGreater(len(one_each[0]), 10)
        self.assertEqual(R.find_all_many(stream, needles), one_each)
        self.assertEqual(R.find_all_many(stream, needles, self.source_for(stream), procs=4), one_each)

    def test_the_multi_needle_find_keeps_each_needle_in_its_own_slice(self):
        # The failure a shared pool invites: the second needle's hits reported under the first. Both
        # plantings straddle a cut, and the second needle never occurs at all.
        stream, expected = self.planted([0, 1998, 3996])
        got = R.find_all_many(stream, [b'DXBC', b'CDXB', b'XBCD'],
                              self.source_for(stream), procs=4)
        self.assertEqual(got, [expected, [], []])

    def test_no_needles_is_no_answer(self):
        stream, _expected = self.planted([100])
        self.assertEqual(R.find_all_many(stream, []), [])

    def test_an_empty_needle_in_a_list_matches_nothing(self):
        stream, expected = self.planted([100, 2000])
        self.assertEqual(R.find_all_many(stream, [b'', b'DXBC'], self.source_for(stream), procs=4),
                         [[], expected])

    def test_the_multi_needle_find_falls_back_to_this_process(self):
        stream = sample_stream()
        needles = [b'Name_00', b'\x00\xff']
        with mock.patch.object(rdc_scan, '_pool', side_effect=OSError('no processes here')):
            self.assertEqual(R.find_all_many(stream, needles, self.source_for(stream), procs=4),
                             [R.find_all(stream, n) for n in needles])

# =========================================================================== profiling and progress
class TestProfile(unittest.TestCase):
    def setUp(self):
        for name in (rdc_profile.PROFILE_ENV, rdc_profile.PROGRESS_ENV):
            os.environ.pop(name, None)
        rdc_profile.clear()
        self.addCleanup(rdc_profile.clear)

    def table(self) -> str:
        buf = io.StringIO()
        rdc_profile.report(buf)
        return buf.getvalue()

    def test_nothing_is_recorded_or_printed_unless_asked(self):
        with rdc_profile.phase('quiet'):
            pass
        self.assertEqual(self.table(), '')
        self.assertFalse(rdc_profile.enabled())

    def test_the_table_counts_calls_and_orders_by_time(self):
        with mock.patch.dict(os.environ, {rdc_profile.PROFILE_ENV: '1'}):
            self.assertTrue(rdc_profile.enabled())
            self.assertTrue(rdc_profile.progress_enabled())
            for _ in range(3):
                with rdc_profile.phase('three times'):
                    pass
            rdc_profile.add('slower', rdc_profile.now() - 0.5)
            text = self.table()
        self.assertIn('three times', text)
        self.assertIn('3 call(s)', text)
        self.assertIn('$RDC_PROFILE', text)
        self.assertLess(text.index('slower'), text.index('three times'))

    def test_progress_alone_wants_lines_but_no_table(self):
        with mock.patch.dict(os.environ, {rdc_profile.PROGRESS_ENV: '1'}):
            self.assertTrue(rdc_profile.progress_enabled())
            self.assertFalse(rdc_profile.enabled())
            with rdc_profile.phase('unmeasured'):
                pass
            self.assertEqual(self.table(), '')

    def test_timed_records_the_call_and_returns_the_value(self):
        def double(value: int) -> int:
            return value * 2

        timed = rdc_profile.timed('doubling')(double)
        with mock.patch.dict(os.environ, {rdc_profile.PROFILE_ENV: '1'}):
            self.assertEqual(timed(21), 42)
            text = self.table()
        self.assertIn('doubling', text)
        self.assertIn('1 call(s)', text)

    def test_timed_is_transparent_when_disabled(self):
        def add(a: int, b: int = 0) -> int:
            return a + b

        timed = rdc_profile.timed('adding')(add)
        self.assertEqual(timed(2, b=3), 5)
        self.assertEqual(self.table(), '')

    def test_the_human_units(self):
        self.assertEqual(rdc_profile.human_time(12), '12 s')
        self.assertEqual(rdc_profile.human_time(110), '1 min 50 s')
        self.assertEqual(rdc_profile.human_time(7500), '2 h 5 min')

    def progress_lines(self, unit: str) -> str:
        every = mock.patch.object(rdc_profile, 'EVERY_SECONDS', 0.0)
        every.start()
        self.addCleanup(every.stop)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {rdc_profile.PROGRESS_ENV: '1'}):
            with contextlib.redirect_stderr(err):
                bar = rdc_profile.progress('scan', 200, unit)
                bar.begin()
                bar.tick(100)
                bar.done(200)
        return err.getvalue()

    def test_progress_reports_bytes_and_a_rate(self):
        text = self.progress_lines('bytes')
        self.assertIn('scan: 200 B', text)
        self.assertIn('MB/s', text)
        self.assertIn('done in', text)

    def test_progress_reports_items_and_a_rate(self):
        text = self.progress_lines('')
        self.assertIn('100/200 (50%)', text)
        self.assertIn('ms each', text)

    def test_progress_says_nothing_when_disabled(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            bar = rdc_profile.progress('scan', 200, 'bytes')
            bar.begin()
            bar.tick(100)
            bar.done(200)
            bar.note('a fallback was taken')
        self.assertEqual(err.getvalue(), '')

    def test_a_short_phase_gets_no_summary(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {rdc_profile.PROGRESS_ENV: '1'}):
            with contextlib.redirect_stderr(err):
                bar = rdc_profile.progress('quick', 10, '')
                bar.begin()
                bar.done(10)
        self.assertEqual(err.getvalue(), '')

if __name__ == '__main__':
    unittest.main()
