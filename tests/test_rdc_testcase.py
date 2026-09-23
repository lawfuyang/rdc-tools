"""The shared fixtures' own check: the scratch-directory removal, which five copies of used to get wrong.

A capture or a bundle that cannot be written is not what this covers -- the *removal* is, because the suite
left `%TEMP%` holding 10,695 `rdc_*` entries (96 MB) while every run of it reported success. Two things made
that possible: a removal that swallowed its failures (`ignore_errors=True` in three of the five copies, no
removal at all in a fourth), and a hold that no retry can outwait -- a file that is still mapped. Both are
pinned here, and the second one is the one the cache tests hit: `parse_container` answers out of an `mmap`
(REFERENCE 4.14), so a test that keeps the info keeps the capture open, and Windows will not unlink a mapped
file.

Run through the suite's entry points like the rest: `python -m unittest discover -s tests -t tests`, or
`src/py/rdc_analysis.py selftest`.
"""
from __future__ import annotations

import mmap
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_testcase as TC          # noqa: E402  (patched by name: the constants live on the module)

class TestRemoveTree(unittest.TestCase):
    """`remove_tree` in the two cases that matter: an ordinary directory, and a held file."""

    def setUp(self) -> None:
        # The waits are the point of the helper, not of its test: shorten them so a real hold takes
        # milliseconds to give up on rather than four seconds.
        for name, value in (('REMOVE_TRIES', 2), ('REMOVE_PAUSE', 0.0)):
            patch = mock.patch.object(TC, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def directory(self) -> str:
        tmp = tempfile.mkdtemp(prefix='rdc_unit_')
        with open(os.path.join(tmp, 'c.rdc'), 'wb') as fh:
            fh.write(b'x' * 4096)
        return tmp

    def test_an_ordinary_directory_goes(self):
        tmp = self.directory()
        TC.remove_tree(tmp)
        self.assertFalse(os.path.exists(tmp))

    def test_a_directory_that_is_already_gone_is_not_a_failure(self):
        tmp = self.directory()
        TC.remove_tree(tmp)
        TC.remove_tree(tmp)      # a cleanup that runs twice must not turn into an error

    @unittest.skipUnless(os.name == 'nt', 'a hold is per-platform: POSIX unlinks an open file happily')
    def test_a_held_file_is_reported_rather_than_swallowed(self):
        """The failure the leak hid: a *mapping* a test kept alive cannot be waited out, so it must raise."""
        tmp = self.directory()
        with open(os.path.join(tmp, 'c.rdc'), 'rb') as fh:
            held = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                with self.assertRaises(OSError) as caught:
                    TC.remove_tree(tmp)
                self.assertIn('could not be removed', str(caught.exception))
                self.assertTrue(os.path.exists(tmp), 'the directory is still there, and it said so')
            finally:
                held.close()
        TC.remove_tree(tmp)
        self.assertFalse(os.path.exists(tmp))

if __name__ == '__main__':
    unittest.main()
