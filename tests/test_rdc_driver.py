"""Tests for the driver's freshness check (`rdc_driver`): the comparison, its refusals, and the command.

Two rules shape every test here. Nothing builds anything: `rebuild` shells out to `cmake`, so the tests pin
the decision that decides *whether* to build and never call it. And nothing reads the real checkout: every
test builds its own `src/cpp`, `CMakeLists.txt` and `bin/replay_dump.exe` in a scratch directory, because a
test that measured this repository's working tree would pass or fail with whatever was last edited.

The times are set with `os.utime` rather than by writing files in an order. File times on this platform
advance with an interrupt that ticks every ~15 ms, so two writes in a row can carry the same timestamp and
the comparison under test would be a coin toss -- which is also why `selftest` in the driver sleeps between
its two writes.

Run directly, via unittest, or through the tool:

    python tests/test_rdc_driver.py
    python -m unittest tests.test_rdc_driver
    python rdc_analysis.py selftest -k Driver
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from typing import List, Tuple
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_driver as driver     # noqa: E402


def run_cmd(argv: List[str]) -> str:
    """`cmd_build(argv)` with its output captured, so a test can read what it printed."""
    out = io.StringIO()
    with redirect_stdout(out):
        driver.cmd_build(argv)
    return out.getvalue()


class CheckoutCase(unittest.TestCase):
    """A scratch checkout: the sources, the build file and the binary, each at a time the test chooses."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='rdc-driver-')
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, driver.SOURCE_DIR))
        os.makedirs(os.path.dirname(os.path.join(self.root, driver.EXE_PATH)))
        # Strictly ordered in time: `os.utime` writes exactly what it is given, and two files with the
        # same time would make "which one won" depend on the order the scan happens to look at them.
        self.write('CMakeLists.txt', 500)
        self.write(os.path.join(driver.SOURCE_DIR, 'one.cpp'), 1000)
        self.write(os.path.join(driver.SOURCE_DIR, 'notes.txt'), 9000)
        self.write(driver.EXE_PATH, 2000)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, when: float) -> str:
        """A one-byte file at `relative` under the checkout, last written at `when`."""
        path = os.path.join(self.root, relative)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('x')
        os.utime(path, (when, when))
        return path

    def verdict(self) -> driver.DriverVerdict:
        """The verdict for this checkout, with the module's own root overridden to point at it."""
        with mock.patch.object(driver, 'repo_root', lambda: self.root):
            return driver.staleness()


class Staleness(CheckoutCase):
    """The comparison itself: which file wins, and when there is no verdict to give."""

    def test_a_source_newer_than_the_binary_is_stale(self) -> None:
        self.write(os.path.join(driver.SOURCE_DIR, 'two.cpp'), 3000)
        verdict = self.verdict()
        self.assertTrue(verdict['stale'])
        self.assertEqual(verdict['source'], os.path.join(driver.SOURCE_DIR, 'two.cpp'))
        self.assertAlmostEqual(verdict['newer_seconds'], 1000.0, places=3)
        self.assertEqual(verdict['note'], '')

    def test_a_binary_newer_than_every_source_is_current(self) -> None:
        self.write(driver.EXE_PATH, 20000)
        verdict = self.verdict()
        self.assertFalse(verdict['stale'])
        self.assertTrue(verdict['newer_seconds'] < 0)
        self.assertEqual(verdict['note'], '')

    def test_the_newest_source_wins(self) -> None:
        # Written in an order that is neither the name order nor the time order, so a scan that took the
        # first or the last candidate it looked at answers with the wrong file.
        self.write(os.path.join(driver.SOURCE_DIR, 'zzz.h'), 4000)
        self.write(os.path.join(driver.SOURCE_DIR, 'aaa.cpp'), 3000)
        self.write(driver.EXE_PATH, 2000)
        self.assertEqual(self.verdict()['source'], os.path.join(driver.SOURCE_DIR, 'zzz.h'))

    def test_a_file_that_is_not_a_source_is_ignored(self) -> None:
        # `notes.txt` is the newest file in the folder by a wide margin; only the two suffixes count.
        verdict = self.verdict()
        self.assertEqual(verdict['source'], os.path.join(driver.SOURCE_DIR, 'one.cpp'))
        self.assertFalse(verdict['stale'])

    def test_the_build_file_counts_as_a_source(self) -> None:
        # `CMakeLists.txt` can change the binary without touching a translation unit.
        self.write('CMakeLists.txt', 5000)
        verdict = self.verdict()
        self.assertTrue(verdict['stale'])
        self.assertEqual(verdict['source'], 'CMakeLists.txt')

    def test_no_binary_is_no_verdict(self) -> None:
        os.remove(os.path.join(self.root, driver.EXE_PATH))
        verdict = self.verdict()
        self.assertFalse(verdict['stale'])
        self.assertIn('is not there', verdict['note'])
        self.assertIn('cmake --build', verdict['note'])
        self.assertEqual(verdict['exe_time'], 0.0)

    def test_a_tree_that_is_not_a_checkout_is_no_verdict(self) -> None:
        # Nothing to read on either side: `src/cpp` and the build file both gone, which is what a
        # directory that is not this repository looks like.
        shutil.rmtree(os.path.join(self.root, driver.SOURCE_DIR))
        os.remove(os.path.join(self.root, 'CMakeLists.txt'))
        verdict = self.verdict()
        self.assertFalse(verdict['stale'])
        self.assertIn('no sources', verdict['note'])

    def test_the_note_is_only_there_when_there_is_no_verdict(self) -> None:
        for when, stale in ((500.0, True), (20000.0, False)):
            with self.subTest(binary_time=when):
                self.write(driver.EXE_PATH, when)
                verdict = self.verdict()
                self.assertEqual(verdict['note'], '')
                self.assertEqual(verdict['stale'], stale)


class Command(CheckoutCase):
    """`cmd_build`: the exit codes a gate reads, and the one option it takes."""

    def build(self, argv: List[str]) -> Tuple[int, str]:
        """`cmd_build(argv)` against this checkout: its exit code, and what it printed."""
        out = io.StringIO()
        with mock.patch.object(driver, 'repo_root', lambda: self.root), redirect_stdout(out):
            code = driver.cmd_build(argv)
        return code, out.getvalue()

    def test_check_is_zero_when_current(self) -> None:
        self.write(driver.EXE_PATH, 20000)
        self.assertEqual(self.build(['--check'])[0], 0)

    def test_check_is_one_when_out_of_date(self) -> None:
        self.write(os.path.join(driver.SOURCE_DIR, 'two.cpp'), 3000)
        code, text = self.build(['--check'])
        # ...and it says which two files disagree, rather than only that something is wrong.
        self.assertEqual(code, 1)
        self.assertIn('out of date', text)
        self.assertIn('two.cpp', text)

    def test_check_is_two_when_there_is_nothing_to_compare(self) -> None:
        # A fresh clone: `2` rather than `1`, so a gate does not report a checkout with no binary as
        # "out of date" -- there is nothing there to be out of date.
        os.remove(os.path.join(self.root, driver.EXE_PATH))
        code, text = self.build(['--check'])
        self.assertEqual(code, 2)
        self.assertIn('no verdict', text)

    def test_check_never_builds(self) -> None:
        self.write(os.path.join(driver.SOURCE_DIR, 'two.cpp'), 3000)
        with mock.patch.object(driver, 'repo_root', lambda: self.root), \
                mock.patch.object(driver, 'rebuild') as rebuild:
            driver.cmd_build(['--check'])
        rebuild.assert_not_called()

    def test_a_stale_binary_is_built_and_the_verdict_reprinted(self) -> None:
        self.write(os.path.join(driver.SOURCE_DIR, 'two.cpp'), 3000)
        with mock.patch.object(driver, 'repo_root', lambda: self.root), \
                mock.patch.object(driver, 'rebuild', return_value=0) as rebuild:
            text = io.StringIO()
            with redirect_stdout(text):
                code = driver.cmd_build([])
        rebuild.assert_called_once()
        self.assertEqual(code, 0)
        self.assertIn('building', text.getvalue())
        # The verdict is printed again afterwards, so the reader sees what the build did rather than
        # having to re-run the check to find out.
        self.assertEqual(text.getvalue().count('verdict'), 2)

    def test_a_failed_build_is_reported_and_is_not_a_success(self) -> None:
        self.write(os.path.join(driver.SOURCE_DIR, 'two.cpp'), 3000)
        with mock.patch.object(driver, 'rebuild', return_value=1):
            code, text = self.build([])
        self.assertEqual(code, 1)
        # The compiler's own output went to the console; what this adds is that the binary was *not*
        # replaced, because a failed link leaves the previous one in place.
        self.assertIn('the build failed with exit code 1', text)

    def test_a_current_binary_is_not_built(self) -> None:
        self.write(driver.EXE_PATH, 20000)
        with mock.patch.object(driver, 'repo_root', lambda: self.root), \
                mock.patch.object(driver, 'rebuild') as rebuild:
            self.assertEqual(driver.cmd_build([]), 0)
        rebuild.assert_not_called()

    def test_an_unknown_option_is_refused(self) -> None:
        self.assertEqual(run_cmd(['--build']).count('usage:'), 1)
        with mock.patch.object(driver, 'repo_root', lambda: self.root):
            self.assertEqual(driver.cmd_build(['--build']), 2)


class Description(unittest.TestCase):
    """The three verdicts as text: one of them must never read like another."""

    def _lines(self, exe_time: float, source_time: float, note: str = '') -> str:
        verdict: driver.DriverVerdict = {
            'root': 'R', 'exe': 'R\\bin\\replay_dump.exe', 'exe_time': exe_time,
            'source': 'src\\cpp\\one.cpp', 'source_time': source_time,
            'newer_seconds': source_time - exe_time, 'stale': bool(not note and source_time > exe_time),
            'note': note}
        return '\n'.join(driver.describe(verdict))

    def test_current_stale_and_no_verdict_read_differently(self) -> None:
        self.assertIn('current', self._lines(2000.0, 1000.0))
        self.assertIn('out of date', self._lines(1000.0, 2000.0))
        self.assertIn('no verdict', self._lines(0.0, 1000.0, 'bin\\replay_dump.exe is not there'))

    def test_a_missing_time_is_named_rather_than_printed_as_a_number(self) -> None:
        self.assertIn('never', self._lines(0.0, 1000.0, 'not there'))


class ThisCheckout(unittest.TestCase):
    """The real repository, so a wrong `repo_root` is caught here rather than by a user."""

    def test_the_root_is_this_repository(self) -> None:
        root = driver.repo_root()
        self.assertTrue(os.path.isfile(os.path.join(root, 'CMakeLists.txt')))
        self.assertTrue(os.path.isdir(os.path.join(root, driver.SOURCE_DIR)))
        self.assertEqual(os.path.normcase(root), os.path.normcase(ROOT))

    def test_the_verdict_is_computed_without_raising(self) -> None:
        # No assertion on which verdict: it depends on whether the driver has been built, which is not
        # this suite's business. What it must not do is guess or throw.
        verdict = driver.staleness()
        self.assertIsInstance(verdict['stale'], bool)
        self.assertIsInstance(verdict['note'], str)


if __name__ == '__main__':
    unittest.main()
