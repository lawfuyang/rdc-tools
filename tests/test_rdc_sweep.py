"""`sweep`: a folder of captures, one bundle each, and the index of what they are (REFERENCE §9).

Two halves, and the split is deliberate. The **parts** are pure and tested here directly: which files a
sweep picks up, what a capture's key is, what a command line's transcript is called, and what happens to a
capture whose bundle is already there -- that last one is the whole resumability story, and it needs no
engine at all (a manifest on disk is what says a bundle is complete). What does need an engine is a capture
being *swept*: that is `dump` through the library, and it is exercised end to end where a device exists --
the index it writes is validated against `SWEEP_SCHEMA` here, because a document this tool cannot validate
is a document a consumer cannot either.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(HERE)
for _p in (HERE, _ROOT, os.path.join(_ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_schemas as schemas     # noqa: E402
import rdc_sweep as sweep         # noqa: E402
from rdc_testcase import TempDirCase, capture_text    # noqa: E402


def write(path: str, document: object) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(document, handle)
    return path


class TestTheParts(TempDirCase):
    """The three pure functions, which are what the folder's shape is decided by."""

    def test_captures_are_found_recursively_and_in_a_stable_order(self) -> None:
        for name in ('b.rdc', 'a.rdc', 'sub/c.rdc', 'notes.txt', 'sub/deep/d.rdc'):
            path = os.path.join(self.tmp, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as handle:
                handle.write(b'x')
        found = [os.path.relpath(path, self.tmp).replace(os.sep, '/')
                 for path in sweep.capture_files(self.tmp)]
        self.assertEqual(found, ['a.rdc', 'b.rdc', 'sub/c.rdc', 'sub/deep/d.rdc'])

    def test_a_key_is_the_path_under_the_folder(self) -> None:
        """The key names where a capture sits, and not what it is called: a corpus's own word for it
        (`desktop-1`) is the corpus's to choose (REFERENCE §4.17)."""
        self.assertEqual(sweep.key_for(self.tmp, os.path.join(self.tmp, 'a.rdc')), 'a')
        self.assertEqual(sweep.key_for(self.tmp, os.path.join(self.tmp, 'sub', 'b.rdc')), 'sub-b')
        self.assertEqual(sweep.key_for(self.tmp, os.path.join(self.tmp, 'Android Renderer.rdc')),
                         'Android Renderer')

    def test_a_transcript_is_named_the_way_the_corpus_names_them(self) -> None:
        """`goldens/<key>/` holds `info.txt` and `draws-25.txt` for the same lines: a sweep's output can be
        read next to a corpus's without a translation step."""
        self.assertEqual(sweep.transcript_name('info'), 'info.txt')
        self.assertEqual(sweep.transcript_name('draws 12'), 'draws-12.txt')
        self.assertEqual(sweep.transcript_name('state 270 --json'), 'state-270.txt')

    def test_a_bundle_without_a_manifest_is_not_a_bundle(self) -> None:
        """`dump` writes the manifest last, so its absence is what "interrupted" means (REFERENCE §9)."""
        self.assertIsNone(sweep.read_manifest(os.path.join(self.tmp, 'nothing-here')))
        half = os.path.join(self.tmp, 'half')
        os.makedirs(half, exist_ok=True)
        write(os.path.join(half, 'capture.json'), {'schemaVersion': 1})
        self.assertIsNone(sweep.read_manifest(half))
        write(os.path.join(half, sweep.MANIFEST_NAME), {'schemaVersion': 1, 'captureBytes': 7})
        self.assertEqual(sweep.read_manifest(half), {'schemaVersion': 1, 'captureBytes': 7})

    def test_the_dump_line_quotes_the_bundle_path(self) -> None:
        """The key can hold a space, and the command language splits on whitespace.

        Unquoted, `dump out/Android Renderer` sends `Renderer` as a second argument -- and since `dump`
        takes the *last* positional as its destination, the bundle lands in a directory named after the
        capture, resolved against the process's working directory. That happened once: a sweep of a folder
        holding `Android Renderer.rdc` wrote 257 files into `Renderer/` at the repository root (REFERENCE
        §4.21).
        """
        key = os.path.join('out', 'Android Renderer')
        line = sweep.dump_line(key)
        self.assertEqual(line, 'dump "%s"' % key)
        self.assertEqual(len(line.split('"')[1].split()), 2)    # two words: one quoted token, not two
        self.assertEqual(sweep.dump_line('out/plain', overwrite=True), 'dump "out/plain" --overwrite')

    def test_the_default_output_directory_is_git_ignored(self) -> None:
        """A bundle is local state twice over -- it belongs to one `.rdc` on this machine, and it records
        the capture's absolute path -- so the directory a sweep writes to *by default* must not be
        committable. The driver's `dump` has the same default of its own (`bundle`, spelled in `DumpOptions`,
        bundle.cpp), and both names are in `.gitignore` for it.

        Checked here rather than trusted: this is the half of "do not commit a bundle" that a test can see,
        and the other half (the quoting above) is the accident that put one in the working tree.
        """
        with open(os.path.join(_ROOT, '.gitignore'), encoding='utf-8') as handle:
            lines = [line.strip() for line in handle if line.strip() and not line.startswith('#')]
        for name in (sweep.DEFAULT_OUT, 'bundle'):
            self.assertIn('%s/' % name, lines,
                          '`%s/` is a bundle directory the tools write to by default and it is not in '
                          '.gitignore' % name)


class TestTheCommand(TempDirCase):
    """`cmd_sweep` where it needs no engine: what it does with nothing to do, and with work already done."""

    def capture(self, name: str = 'a.rdc', size: int = 32) -> str:
        path = os.path.join(self.tmp, 'captures', name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(b'RDOC' + bytes(size - 4))
        return path

    def existing_bundle(self, key: str = 'a', **manifest: object) -> str:
        """A bundle that a previous sweep left behind: a directory and a manifest, nothing else."""
        document = {'schemaVersion': 1, 'captureBytes': 32, 'captureSha256': 'ab' * 32,
                    'renderdoc': '1.46', 'fileCount': 5, 'fileBytes': 1234}
        document.update(manifest)
        return write(os.path.join(self.tmp, 'out', key, sweep.MANIFEST_NAME), document)

    def test_a_folder_with_no_capture_is_exit_2(self) -> None:
        os.makedirs(os.path.join(self.tmp, 'empty'), exist_ok=True)
        out = capture_text(sweep.cmd_sweep, [os.path.join(self.tmp, 'empty')])
        self.assertIn('nothing to sweep', out)

    def test_a_folder_that_is_not_there_is_exit_2(self) -> None:
        out = capture_text(sweep.cmd_sweep, [os.path.join(self.tmp, 'not-here')])
        self.assertIn('is not a folder', out)

    def test_a_capture_whose_bundle_is_there_is_present_and_is_not_replayed(self) -> None:
        """Resumability, and the reason the index is built from manifests: a second sweep of the same
        folder answers in a moment, with the numbers the first one's bundle carries.

        No engine is touched -- if the command tried to open this capture (it is four bytes and a magic)
        the run would fail, so "present" is the proof that it did not.
        """
        self.capture()
        self.existing_bundle()
        code = sweep.cmd_sweep([os.path.join(self.tmp, 'captures'), '--out', os.path.join(self.tmp, 'out')])
        self.assertEqual(code, 0)
        with open(os.path.join(self.tmp, 'out', sweep.INDEX_NAME), encoding='utf-8') as handle:
            index = json.load(handle)
        self.assertEqual([row['status'] for row in index['captures']], ['present'])
        self.assertEqual(index['captures'][0]['key'], 'a')
        self.assertEqual(index['captures'][0]['bytes'], 32)
        self.assertEqual(index['captures'][0]['fileCount'], 5)
        self.assertEqual((index['swept'], index['present'], index['failed']), (0, 1, 0))

    def test_the_index_it_writes_validates_against_the_tools_own_schema(self) -> None:
        self.capture()
        self.existing_bundle()
        sweep.cmd_sweep([os.path.join(self.tmp, 'captures'), '--out', os.path.join(self.tmp, 'out')])
        with open(os.path.join(self.tmp, 'out', sweep.INDEX_NAME), encoding='utf-8') as handle:
            index = json.load(handle)
        self.assertEqual(schemas.validate_document(index, schemas.SWEEP_SCHEMA), [])
        # And the *validator* knows the file: a sweep's index is one of the documents the offline tool
        # writes, so `validate sweep.json <schemaDir>` needs no kind of its own.
        self.assertEqual(schemas.schema_for_file(sweep.INDEX_NAME), 'sweep')

    def test_min_bytes_filters_the_folder(self) -> None:
        self.capture('small.rdc', size=16)
        self.capture('big.rdc', size=64)
        self.existing_bundle('big')
        code = sweep.cmd_sweep([os.path.join(self.tmp, 'captures'), '--out', os.path.join(self.tmp, 'out'),
                                '--min-bytes', '32'])
        self.assertEqual(code, 0)
        with open(os.path.join(self.tmp, 'out', sweep.INDEX_NAME), encoding='utf-8') as handle:
            index = json.load(handle)
        self.assertEqual([row['key'] for row in index['captures']], ['big'])

    def test_an_unknown_option_is_refused_rather_than_ignored(self) -> None:
        out = capture_text(sweep.cmd_sweep, [os.path.join(self.tmp, 'captures'), '--nonsense'])
        self.assertIn('usage: rdc_analysis.py sweep', out)


__all__ = [
    'TestTheCommand',
    'TestTheParts',
]
