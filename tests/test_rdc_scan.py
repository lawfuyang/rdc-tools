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
from typing import Dict, Tuple
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
        with mock.patch.object(rdc_scan.multiprocessing, 'Pool',
                               side_effect=OSError('no processes here')):
            counts, firsts = R.scan_runs(stream, 6, self.source_for(stream), procs=4)
        self.assertEqual((counts, firsts), serial_runs(stream, 6))

    def test_a_small_stream_does_not_start_a_pool(self):
        # `procs=None` means "use a pool only if the stream is big enough and there is a file to map"
        stream = sample_stream(20)
        with mock.patch.object(rdc_scan.multiprocessing, 'Pool',
                               side_effect=AssertionError('a pool was started for a small stream')):
            counts, firsts = R.scan_runs(stream, 6, self.source_for(stream))
        self.assertEqual((counts, firsts), serial_runs(stream, 6))

    def test_no_source_means_no_pool(self):
        stream = sample_stream()
        with mock.patch.object(rdc_scan.multiprocessing, 'Pool',
                               side_effect=AssertionError('a pool was started without a source')):
            counts, firsts = R.scan_runs(stream, 6, None)
        self.assertEqual((counts, firsts), serial_runs(stream, 6))

    def test_a_stream_shorter_than_the_file_check_needs_is_not_mapped(self):
        stream = b'\x00abcdefgh\x00' * 8        # under 128 bytes: the file check cannot compare ends
        with mock.patch.object(rdc_scan.multiprocessing, 'Pool',
                               side_effect=AssertionError('mapped a stream too short to check')):
            counts, firsts = R.scan_runs(stream, 6, self.source_for(stream, 'short.bin'))
        self.assertEqual((counts, firsts), serial_runs(stream, 6))

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
