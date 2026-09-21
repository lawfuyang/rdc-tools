"""Tests for the RenderDoc source bootstrap (`rdc_renderdoc_src`): the check, the fetch, and the refusals.

Two rules shape every test here. Nothing touches the network: the single HTTP call is `_fetch`, and these
tests replace it or the functions above it, so the suite runs on a machine with no internet and no GitHub
account. And nothing touches the real `renderdoc-src`: every test builds its own tree in a scratch directory,
because that folder holds this repository's captures.

The archive the tests serve is built in memory by the test itself, which is what makes the *extraction* rules
testable -- a member that points outside the tree, a symlink, a layout that moved -- rather than only the happy
path.

Run directly, via unittest, or through the tool:

    python tests/test_rdc_renderdoc_src.py
    python -m unittest tests.test_rdc_renderdoc_src
    python rdc_analysis.py selftest -k Bootstrap
"""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R             # noqa: E402
import rdc_chunkmap as chunkmap      # noqa: E402
import rdc_chunknames as chunknames  # noqa: E402
import rdc_renderdoc_src as src      # noqa: E402

#: What a populated tree needs, as paths under its root: the two enums the tool parses.
CORE = os.path.join('renderdoc', 'core', 'core.h')
D3D12 = os.path.join('renderdoc', 'driver', 'd3d12', 'd3d12_common.h')

#: The smallest file that is a usable `core.h`: `load_chunk_names` parses the enum out of it, so a test that
#: wants names rather than a mere presence check gets one worth parsing.
CORE_TEXT = """enum class SystemChunk : uint32_t
{
  DriverInit,
  InitialContents,
  CaptureBegin,
  PushMarker,
  SetMarker,
  Drawcall,
};
"""

D3D12_TEXT = """enum class D3D12Chunk : uint32_t
{
  Device_CreateCommandQueue,
  List_DrawInstanced,
  List_Dispatch,
};
"""


def build_tar(members: List[Tuple[str, Optional[bytes]]]) -> bytes:
    """A `.tar.gz` in memory: `(name, contents)`, with None meaning a directory and a symlink named by hand."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, contents in members:
            if contents is None:
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
                continue
            if contents == b'SYMLINK':
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = '../../etc/passwd'
                archive.addfile(info)
                continue
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
    return buffer.getvalue()


def renderdoc_archive(tag: str = 'v1.46') -> bytes:
    """What the real archive looks like: one top-level folder, the two enums inside it, and some filler."""
    top = 'renderdoc-%s/' % tag.lstrip('v')
    return build_tar([(top, None),
                      (top + 'renderdoc', None),
                      (top + 'renderdoc/core', None),
                      (top + 'renderdoc/core/core.h', CORE_TEXT.encode()),
                      (top + 'renderdoc/driver', None),
                      (top + 'renderdoc/driver/d3d12', None),
                      (top + 'renderdoc/driver/d3d12/d3d12_common.h', D3D12_TEXT.encode()),
                      (top + 'LICENSE.md', b'BSD\n')])


class SrcCase(unittest.TestCase):
    """A scratch root per test; the tree lives inside it, never in the repository's own folder.

    `target_dir` is pointed at the scratch root for the duration of each test, because that is the *only*
    folder the module fetches into: without the patch, a test that wants a download would be asking for one
    into this repository's real `renderdoc-src`, and the suite would reach the network. The tests that are
    about the real location use the unpatched function, captured in `setUp`.
    """

    tmp: str

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='rdc_src_')
        self.addCleanup(self._remove_tmp)
        self.real_target_dir = src.target_dir
        patch = mock.patch.object(src, 'target_dir', return_value=os.path.join(self.tmp, 'renderdoc-src'))
        patch.start()
        self.addCleanup(patch.stop)

    def _remove_tmp(self) -> None:
        for dirpath, _dirs, files in os.walk(self.tmp, topdown=False):
            for name in files:
                os.remove(os.path.join(dirpath, name))
            os.rmdir(dirpath)

    def tree(self, name: str = 'renderdoc-src', core: bool = True, d3d12: bool = True,
             extra: Optional[Dict[str, str]] = None) -> str:
        """A tree in the scratch root with the parts a test wants present."""
        root = os.path.join(self.tmp, name)
        for wanted, relative, text in ((core, CORE, CORE_TEXT), (d3d12, D3D12, D3D12_TEXT)):
            if wanted:
                path = os.path.join(root, relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write(text)
        for relative, text in (extra or {}).items():
            path = os.path.join(root, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(text)
        return root

    def fetch(self, payload: bytes, calls: Optional[List[str]] = None):
        """Replace the module's one network call; `calls` records the URLs it was asked for."""
        def fake(url: str, timeout: float = 0.0) -> bytes:
            if calls is not None:
                calls.append(url)
            return payload
        return mock.patch.object(src, '_fetch', side_effect=fake)

    def quiet(self) -> Tuple[List[str], Any]:
        """A log callback that collects lines instead of writing them, since the default writes to stderr."""
        lines: List[str] = []
        return lines, (lambda text: lines.append(text))


class TestTheCheck(SrcCase):
    """`missing_parts`/`is_populated`: what "the tree is there" means, and what it says when it is not."""

    def test_an_empty_folder_is_not_populated_and_names_what_is_missing(self):
        empty = os.path.join(self.tmp, 'renderdoc-src')
        os.makedirs(empty)
        self.assertFalse(src.is_populated(empty))
        self.assertEqual(src.missing_parts(empty),
                         ['renderdoc/core/core.h', 'renderdoc/driver/d3d12/d3d12_common.h'])

    def test_a_folder_that_does_not_exist_is_the_same_answer(self):
        self.assertFalse(src.is_populated(os.path.join(self.tmp, 'nothing-here')))

    def test_one_missing_file_is_enough_to_be_incomplete(self):
        # A tree that is half extracted, or one copied without a driver folder, is the case a marker file
        # would get wrong: the check is the contents, not a stamp.
        root = self.tree(core=True, d3d12=False)
        self.assertEqual(src.missing_parts(root), ['renderdoc/driver/d3d12/d3d12_common.h'])

    def test_a_whole_tree_is_populated(self):
        self.assertTrue(src.is_populated(self.tree()))

    def test_describe_reports_where_it_looked(self):
        described = src.describe(self.tree())
        self.assertTrue(described['populated'])
        self.assertEqual(described['missing'], [])
        self.assertEqual(described['required'],
                         ['renderdoc/core/core.h', 'renderdoc/driver/d3d12/d3d12_common.h'])

    def test_the_target_is_renderdoc_src_at_the_repository_root(self):
        # The tool writes where it looks: `_find_renderdoc_src` walks up from the same folder. The unpatched
        # function, since every other test here points the target at its own scratch folder.
        self.assertEqual(os.path.abspath(self.real_target_dir()),
                         os.path.abspath(os.path.join(ROOT, 'renderdoc-src')))


class TestTheFetch(SrcCase):
    """The download path, with the archive built by the test: what it writes, and what it refuses."""

    def test_the_latest_tag_comes_from_the_tags_api(self):
        payload = json.dumps([{'name': 'v1.46'}, {'name': 'v1.45'}]).encode()
        with self.fetch(payload):
            self.assertEqual(src.latest_tag(), 'v1.46')

    def test_a_tags_response_that_is_not_a_list_is_an_error(self):
        with self.fetch(b'{"message": "rate limited"}'):
            with self.assertRaises(src.BootstrapError):
                src.latest_tag()

    def test_a_tag_with_no_name_is_an_error_rather_than_a_guess(self):
        with self.fetch(json.dumps([{'commit': {'sha': 'abc'}}]).encode()):
            with self.assertRaises(src.BootstrapError):
                src.latest_tag()

    def test_the_tarball_url_is_the_tag_archive(self):
        self.assertEqual(src.tarball_url('v1.46'),
                         'https://codeload.github.com/baldurk/renderdoc/tar.gz/refs/tags/v1.46')

    def test_ensure_fetches_extracts_and_verifies(self):
        root = os.path.join(self.tmp, 'renderdoc-src')
        calls: List[str] = []
        lines, log = self.quiet()
        with self.fetch(renderdoc_archive(), calls):
            self.assertEqual(src.ensure(root=root, tag='v1.46', log=log), root)
        self.assertTrue(src.is_populated(root))
        self.assertEqual(calls, ['https://codeload.github.com/baldurk/renderdoc/tar.gz/refs/tags/v1.46'])
        self.assertTrue(any('extracted' in line for line in lines))
        # The top-level folder the archive carries is stripped: the tree is `renderdoc/...`, not
        # `renderdoc-1.46/renderdoc/...`, which is the layout `_find_renderdoc_src` looks for.
        self.assertTrue(os.path.isfile(os.path.join(root, CORE)))
        self.assertTrue(os.path.isfile(os.path.join(root, 'LICENSE.md')))

    def test_ensure_leaves_the_files_beside_the_tree_alone(self):
        # `renderdoc-src` is also where this repository keeps its captures: an extraction adds to that
        # folder and must not clear it.
        root = os.path.join(self.tmp, 'renderdoc-src')
        os.makedirs(root)
        with open(os.path.join(root, 'My Frame.rdc'), 'wb') as fh:
            fh.write(b'a capture, not a source file')
        with self.fetch(renderdoc_archive()):
            src.ensure(root=root, tag='v1.46', log=self.quiet()[1])
        self.assertTrue(os.path.isfile(os.path.join(root, 'My Frame.rdc')))
        self.assertTrue(src.is_populated(root))

    def test_a_populated_tree_is_a_silent_no_op(self):
        root = self.tree()
        lines, log = self.quiet()
        with mock.patch.object(src, '_fetch', side_effect=AssertionError('the network was used')):
            self.assertEqual(src.ensure(root=root, log=log), root)
        self.assertEqual(lines, [], 'a tree that is already there must not even print')

    def test_a_member_outside_the_tree_is_refused_and_nothing_else_is_lost(self):
        # The archive is untrusted input: `..`, an absolute path and a backslash are all refused, and the
        # files beside them are still extracted.
        nasty = build_tar([('renderdoc-1.46/', None),
                           ('renderdoc-1.46/renderdoc/core/core.h', CORE_TEXT.encode()),
                           ('renderdoc-1.46/renderdoc/driver/d3d12/d3d12_common.h', D3D12_TEXT.encode()),
                           ('renderdoc-1.46/../../escaped.txt', b'nope'),
                           ('/absolute.txt', b'nope'),
                           ('renderdoc-1.46/dir\\..\\..\\escaped2.txt', b'nope')])
        root = os.path.join(self.tmp, 'renderdoc-src')
        lines, log = self.quiet()
        with self.fetch(nasty):
            src.ensure(root=root, tag='v1.46', log=log)
        self.assertTrue(src.is_populated(root))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'escaped.txt')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'renderdoc-src', 'absolute.txt')))
        self.assertTrue(any('refused' in line for line in lines))

    def test_a_symlink_is_refused(self):
        sneaky = build_tar([('renderdoc-1.46/', None),
                            ('renderdoc-1.46/renderdoc/core/core.h', CORE_TEXT.encode()),
                            ('renderdoc-1.46/renderdoc/driver/d3d12/d3d12_common.h', D3D12_TEXT.encode()),
                            ('renderdoc-1.46/link', b'SYMLINK')])
        root = os.path.join(self.tmp, 'renderdoc-src')
        with self.fetch(sneaky):
            src.ensure(root=root, tag='v1.46', log=self.quiet()[1])
        self.assertFalse(os.path.lexists(os.path.join(root, 'link')), 'a link is not followed into the tree')

    def test_an_archive_that_does_not_contain_the_enums_is_an_error(self):
        # A layout that moved, or an archive cut short: what is there stays, and the caller is told -- the
        # fallback (numeric ids) is still better than an exception escaping into a command.
        moved = build_tar([('renderdoc-1.46/', None), ('renderdoc-1.46/README.md', b'moved\n')])
        root = os.path.join(self.tmp, 'renderdoc-src')
        lines, log = self.quiet()
        with self.fetch(moved):
            src.ensure(root=root, tag='v1.46', log=log)      # not strict: reported, not raised
        self.assertFalse(src.is_populated(root))
        self.assertTrue(any('could not fetch' in line for line in lines))
        self.assertFalse(os.path.isfile(os.path.join(root, CORE)))

    def test_a_network_failure_is_reported_and_not_raised(self):
        root = os.path.join(self.tmp, 'renderdoc-src')
        lines, log = self.quiet()
        with mock.patch.object(src, '_fetch', side_effect=OSError('no route to host')):
            self.assertEqual(src.ensure(root=root, tag='v1.46', log=log), root)
        self.assertTrue(any('no route to host' in line for line in lines))
        self.assertTrue(any('numeric ids' in line for line in lines))

    def test_strict_turns_the_same_failure_into_an_error(self):
        # The explicit `bootstrap` command is the one caller that asked for the download, so it is the one
        # caller that should be told why it did not happen.
        with mock.patch.object(src, '_fetch', side_effect=OSError('no route to host')):
            with self.assertRaises(src.BootstrapError):
                src.ensure(root=os.path.join(self.tmp, 'out'), tag='v1.46', log=self.quiet()[1],
                           strict=True)


class TestWhatItWillNotDo(SrcCase):
    """The two ways a reader says "do not download into that": `$RENDERDOC_SRC` and `RDC_NO_BOOTSTRAP`."""

    def test_renderdoc_src_is_never_written_into(self):
        root = os.path.join(self.tmp, 'my-tree')
        os.makedirs(root)
        lines, log = self.quiet()
        with mock.patch.dict(os.environ, {'RENDERDOC_SRC': root}):
            with mock.patch.object(src, '_fetch', side_effect=AssertionError('the network was used')):
                src.ensure(root=root, log=log)
        self.assertTrue(any('RENDERDOC_SRC' in line for line in lines))
        self.assertFalse(os.listdir(root), 'a tree the reader pointed at is reported, not written into')

    def test_a_tree_that_is_not_the_target_is_never_written_into_or_complained_about(self):
        # What keeps the suite hermetic: a command handed a tree of its own (a test's scratch folder) gets the
        # check and the fallback, never a download -- and no note, because the command already warns about a
        # missing tree once and a second voice saying the same thing is noise.
        other = os.path.join(self.tmp, 'somebody-elses-tree')
        os.makedirs(other)
        lines, log = self.quiet()
        with mock.patch.object(src, '_fetch', side_effect=AssertionError('the network was used')):
            self.assertEqual(src.ensure(root=other, log=log), other)
        self.assertEqual(lines, [])
        self.assertFalse(os.listdir(other))

    def test_no_bootstrap_means_no_network(self):
        root = os.path.join(self.tmp, 'renderdoc-src')
        lines, log = self.quiet()
        with mock.patch.dict(os.environ, {src.NO_BOOTSTRAP_ENV: '1'}):
            with mock.patch.object(src, '_fetch', side_effect=AssertionError('the network was used')):
                src.ensure(root=root, log=log)
        self.assertTrue(any(src.NO_BOOTSTRAP_ENV in line for line in lines))
        self.assertFalse(os.path.exists(root))


class TestVersionOfATree(SrcCase):
    """`rdc_renderdoc_src.version`: what the bundled table is stamped with, and what `bootstrap` reports.

    It is read out of the tree rather than remembered from a fetch's tag: a tree can be hand-copied, and the
    two numbers here are the ones RenderDoc's own build turns into the `progVersion` a capture header carries,
    so the two are comparable as strings (that comparison is the reason a reader can judge a name at all).
    """

    def test_reads_the_two_numbers_the_header_declares(self):
        root = self.tree(extra={'renderdoc/api/replay/version.h':
                                '#define RENDERDOC_VERSION_MAJOR 2\n'
                                '#define RENDERDOC_VERSION_MINOR 7\n'})
        self.assertEqual(src.version(root), '2.7')

    def test_a_tree_that_does_not_say_is_empty_rather_than_an_error(self):
        self.assertEqual(src.version(self.tree()), '')
        self.assertEqual(src.version(os.path.join(self.tmp, 'nothing-here')), '')


class TestTheBundledTable(SrcCase):
    """`chunk-names`: the table the tool falls back to, against the tree it is generated from."""

    def run_chunk_names(self, *argv: str) -> Tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(sys, 'stdout', out):
            code = chunkmap.cmd_chunknames(list(argv))
        return code, out.getvalue()

    def write_table(self, root: str) -> str:
        path = os.path.join(self.tmp, 'generated_table.py')
        code, out = self.run_chunk_names('--write', '--out', path, '--src', root)
        self.assertEqual(code, 0, out)
        return path

    def test_the_generated_file_is_python_and_holds_what_the_tree_has(self):
        """The table is a module the tool imports, so generating it has to produce valid Python that says
        exactly what the tree said -- and it carries the tree's version and the command that rewrites it."""
        root = self.tree(extra={'renderdoc/api/replay/version.h':
                                '#define RENDERDOC_VERSION_MAJOR 1\n'
                                '#define RENDERDOC_VERSION_MINOR 99\n'})
        path = self.write_table(root)
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        namespace: Dict[str, Any] = {}
        exec(compile(text, path, 'exec'), namespace)      # it is a Python module, or it is not a table
        self.assertEqual(namespace['VERSION'], '1.99')
        self.assertEqual(namespace['SYSTEM_CHUNKS'], chunkmap.parse_enum(root, 'system'))
        self.assertEqual(namespace['DRIVER_CHUNKS']['D3D12'], chunkmap.parse_enum(root, 'driver'))
        self.assertEqual(namespace['FORMAT_NAMES'], chunkmap.parse_enum(root, 'formats'))
        self.assertIn('chunk-names --write', text)

    def test_check_accepts_a_current_table_and_says_what_moved(self):
        root = self.tree()
        path = self.write_table(root)
        code, out = self.run_chunk_names('--check', '--out', path, '--src', root)
        self.assertEqual(code, 0)
        self.assertIn('current', out)
        # The tree moves on -- one chunk renamed, one added -- and the table is now out of date.
        with open(os.path.join(root, 'renderdoc', 'driver', 'd3d12', 'd3d12_common.h'), 'w',
                  encoding='utf-8') as handle:
            handle.write('enum class D3D12Chunk : uint32_t\n{\n  PushMarker = 1001,\n'
                         '  List_AddedLater = 1200,\n};\n')
        code, out = self.run_chunk_names('--check', '--out', path, '--src', root)
        self.assertEqual(code, 1)
        self.assertIn('out of date', out)
        self.assertRegex(out, r'driver: \d+ new, \d+ gone')     # which kind moved, and how much of it

    def test_no_tree_is_exit_2_and_writes_nothing(self):
        """A tree that is not on this machine is working-tree state, like a capture the goldens cannot read."""
        path = os.path.join(self.tmp, 'never-written.py')
        code, out = self.run_chunk_names('--check', '--out', path, '--src',
                                         os.path.join(self.tmp, 'nothing-here'))
        self.assertEqual(code, 2)
        self.assertIn('no tree to compare with', out)
        self.assertFalse(os.path.exists(path))

    def test_write_refuses_a_half_extracted_tree(self):
        """Writing from one would shrink the table to what that tree happens to have, which is backwards."""
        path = os.path.join(self.tmp, 'never-written.py')
        code, out = self.run_chunk_names('--write', '--out', path, '--src', self.tree(core=False))
        self.assertEqual(code, 1)
        self.assertIn('not a complete tree', out)
        self.assertFalse(os.path.exists(path))

    def test_an_unknown_option_is_a_usage_line_and_exit_2(self):
        code, out = self.run_chunk_names('--nonsense')
        self.assertEqual(code, 2)
        self.assertIn('usage: rdc_analysis.py chunk-names', out)


class TestTheCheckedInTable(unittest.TestCase):
    """The checked-in `src/py/rdc_chunknames.py` against the tree this machine has, when it has one.

    Skips without a tree (a fresh clone, CI) for the same reason `goldens --check` exits 2: whether the input
    is on this machine is working-tree state, and a check that could not read its input has not passed.
    """

    def test_the_table_is_what_the_tree_would_generate(self):
        root = chunkmap.RENDERDOC_SRC
        if not src.is_populated(root):
            self.skipTest('no RenderDoc source tree at %s' % root)
        with open(chunkmap.table_path(), encoding='utf-8', newline='') as handle:
            checked_in = handle.read()
        self.assertEqual(checked_in.replace('\r\n', '\n'), chunkmap.table_text(root),
                         'run `python src\\py\\rdc_analysis.py chunk-names --write` and keep the diff')


class TestTheHook(SrcCase):
    """The tree is asked for by `load_chunk_names`, which is the only place the tool needs an enum."""

    def test_load_chunk_names_asks_the_bootstrap_for_the_tree(self):
        root = self.tree()
        with mock.patch.object(src, 'ensure', wraps=src.ensure) as ensure:
            names = chunkmap.load_chunk_names(root)
        self.assertEqual(len(ensure.call_args_list), 1)
        self.assertEqual(ensure.call_args[0][0], root)
        # Both enums go into one map and their low values collide (value 1 is a system chunk *and* a D3D12
        # one), so the assertion is on a name only one of them has rather than on "the tree was read".
        self.assertIn('PushMarker', names.values())
        self.assertIn('Device_CreateCommandQueue', names.values())

    def test_a_command_fetches_the_tree_when_it_is_missing(self):
        # The hook end to end: the tree is not there, a command needs a name, the fetch happens, and the
        # command still returns the names it always would have.
        root = self.tree()
        os.remove(os.path.join(root, CORE))
        os.remove(os.path.join(root, D3D12))
        with mock.patch.object(src, 'latest_tag', return_value='v1.46'):
            with self.fetch(renderdoc_archive()):
                names = chunkmap.load_chunk_names(root)
        self.assertTrue(src.is_populated(root), 'the fetch filled the tree the caller named')
        self.assertIn('PushMarker', names.values())

    def test_a_missing_tree_still_produces_names_and_a_versioned_warning(self):
        """The case the bundled table exists for: no tree, no network, and chunk names anyway.

        The warning is what keeps that honest -- the names are a released RenderDoc's vocabulary while the
        capture may be another version's -- so it is asserted here with the version in it, not just "a warning".
        """
        root = self.tree(core=False, d3d12=False)
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {src.NO_BOOTSTRAP_ENV: '1'}):
            with mock.patch.object(chunkmap, '_SRC_WARNED', False):
                with mock.patch.object(sys, 'stderr', stderr):
                    names = chunkmap.load_chunk_names(root)
        self.assertIn('PushMarker', names.values())
        self.assertIn('using bundled names for RenderDoc %s' % chunknames.VERSION, stderr.getvalue())
        self.assertIn('bootstrap', stderr.getvalue())

    def test_the_bootstrap_command_reports_where_the_tree_is(self):
        # `bootstrap` runs the same step explicitly; it is also the only path that fails loudly.
        root = self.tree()
        with mock.patch.object(R, 'target_dir', return_value=root):
            with mock.patch.object(sys, 'stdout', io.StringIO()) as out:
                self.assertEqual(R.cmd_bootstrap([]), 0)
        self.assertIn('already populated', out.getvalue())

    def test_the_bootstrap_command_fails_loudly_when_it_cannot_fetch(self):
        root = os.path.join(self.tmp, 'renderdoc-src')
        with mock.patch.object(R, 'target_dir', return_value=root):
            with mock.patch.object(src, '_fetch', side_effect=OSError('no route to host')):
                with mock.patch.object(sys, 'stdout', io.StringIO()) as out:
                    self.assertEqual(R.cmd_bootstrap([]), 1)
        self.assertIn('error:', out.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
