"""Tests for the corpus harness (`goldens`): the corpus reader, the transcripts and the labels.

Hermetic, like the rest of the suite: the `capture` is a fixture built chunk by chunk, the corpus is
written into the scratch directory, and the fake chunk-name tree is handed to the child process through
`$RENDERDOC_SRC` -- the environment variable the tool's own resolution order reads first -- so a transcript
is compared against a run of the real CLI over a fixture, not against a call to a function.

The corpus under `goldens/` itself is *not* run here: it reads hundreds of megabytes and takes minutes,
and whether a capture is on this machine is the state of a working tree rather than a property of the tool
(the same reason `goldens --check` exits 2 when there is nothing to compare).

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_goldens.py
    python -m unittest tests.test_rdc_goldens
    python rdc_analysis.py selftest -k Goldens
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from typing import Any, Dict, List, Tuple
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402
import rdc_detect_stream          # noqa: E402
import rdc_fixtures as F          # noqa: E402
import rdc_goldens as goldens     # noqa: E402
import rdc_schemas as schemas     # noqa: E402
from rdc_testcase import CmdCase as _CmdCase   # noqa: E402


def write_json(path: str, document: Any) -> str:
    """Write one JSON document at `path`, creating its folder, and return the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(document, handle)
    return path


class GoldenCase(_CmdCase):
    """A scratch directory that is also the *root* a corpus is read from."""

    def corpus(self, captures: List[Dict[str, Any]], **extra: Any) -> str:
        document: Dict[str, Any] = {'schemaVersion': goldens.CORPUS_VERSION,
                                    'note': 'a fixture corpus', 'captures': captures}
        document.update(extra)
        return write_json(os.path.join(self.tmp, goldens.GOLDENS_DIR, goldens.CORPUS_NAME), document)

    def capture_entry(self, name: str = 'fixture', **extra: Any) -> Any:
        """One corpus entry as a plain dict: what a JSON file holds, which is what the reader is given."""
        entry: Dict[str, Any] = {'name': name, 'path': 'capture.rdc', 'commands': [['summary']]}
        entry.update(extra)
        return entry

    def run_goldens_code(self, *args: str) -> Tuple[int, str]:
        """Run the harness with `$RENDERDOC_SRC` set for the children, returning `(exit code, stdout)`.

        The variable matters: the harness's commands run as subprocesses, and a child without the fake
        source tree reads every chunk as a number -- the same capture, a different transcript.
        """
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {'RENDERDOC_SRC': self.src_root}), \
                contextlib.redirect_stdout(buf):
            code = R.cmd_goldens(list(args))
        return code, buf.getvalue()

    def run_goldens(self, *args: str) -> str:
        """The harness's stdout, run with `$RENDERDOC_SRC` set for the children."""
        return self.run_goldens_code(*args)[1]

    def write_fixture_capture(self) -> str:
        return self.cap(self.marker('Scene'), self.draw(), self.pop(),
                        self.marker('BasePass'), self.draw(), self.pop())

    def marker(self, name: str) -> bytes:
        return self.ch('PushMarker', name.encode('utf-8') + b'\x00')

    def pop(self) -> bytes:
        return self.ch('PopMarker', b'')

    def draw(self) -> bytes:
        return self.ch('List_DrawInstanced', F.pl_draw_instanced(1, 3, 1, 0, 0))

    def write_expect(self, name: str, document: Dict[str, Any]) -> str:
        return write_json(os.path.join(self.tmp, goldens.GOLDENS_DIR, name + goldens.EXPECT_SUFFIX),
                          document)


class TestCorpus(GoldenCase):
    def test_a_corpus_this_tool_does_not_know_is_refused(self):
        path = write_json(os.path.join(self.tmp, 'c.json'),
                          {'schemaVersion': goldens.CORPUS_VERSION + 1, 'captures': []})
        with self.assertRaises(schemas.SchemaError) as caught:
            goldens.load_corpus(path)
        self.assertIn('corpus version', str(caught.exception))

    def test_a_corpus_with_a_repeated_key_is_refused_rather_than_read(self):
        """The one check a schema cannot make: by the time a schema sees an object, the repetition is gone
        and the last value has already won."""
        path = os.path.join(self.tmp, 'c.json')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('{"schemaVersion": 1, "note": "one", "note": "two", "captures": []}')
        with self.assertRaises(schemas.SchemaError) as caught:
            goldens.load_corpus(path)
        self.assertIn('appears twice', str(caught.exception))

    def test_a_capture_without_a_name_is_refused(self):
        path = write_json(os.path.join(self.tmp, 'c.json'),
                          {'schemaVersion': 1, 'captures': [{'path': 'capture.rdc'}]})
        with self.assertRaises(schemas.SchemaError) as caught:
            goldens.load_corpus(path)
        self.assertIn('needs a `name`', str(caught.exception))

    def test_a_capture_without_a_path_is_kept_with_no_path(self):
        """The committed corpus carries no paths: the key is what the corpus has, the local file the rest."""
        path = write_json(os.path.join(self.tmp, 'c.json'),
                          {'schemaVersion': 1, 'captures': [{'name': 'x'}]})
        capture = goldens.load_corpus(path)['captures'][0]
        self.assertEqual(capture.get('path'), '')

    def test_the_local_file_supplies_the_paths_the_corpus_does_not_carry(self):
        path = write_json(os.path.join(self.tmp, 'c.json'),
                          {'schemaVersion': 1, 'captures': [{'name': 'x'}]})
        write_json(os.path.join(self.tmp, goldens.LOCAL_NAME),
                   {'capturePaths': {'x': 'elsewhere/x.rdc'}})
        capture = goldens.load_corpus(path)['captures'][0]
        self.assertEqual(capture.get('path'), 'elsewhere/x.rdc')

    def test_a_path_written_in_the_corpus_wins_over_the_local_file(self):
        """A corpus of one's own may carry paths; the local file fills in what it leaves out."""
        path = write_json(os.path.join(self.tmp, 'c.json'),
                          {'schemaVersion': 1, 'captures': [{'name': 'x', 'path': 'mine.rdc'}]})
        write_json(os.path.join(self.tmp, goldens.LOCAL_NAME),
                   {'capturePaths': {'x': 'elsewhere/x.rdc'}})
        self.assertEqual(goldens.load_corpus(path)['captures'][0].get('path'), 'mine.rdc')

    def test_a_local_file_that_names_an_unknown_capture_is_refused(self):
        path = write_json(os.path.join(self.tmp, 'c.json'),
                          {'schemaVersion': 1, 'captures': [{'name': 'x'}]})
        write_json(os.path.join(self.tmp, goldens.LOCAL_NAME),
                   {'capturePaths': {'x': 'x.rdc', 'typo': 'y.rdc'}})
        with self.assertRaises(schemas.SchemaError) as caught:
            goldens.load_corpus(path)
        self.assertIn('typo', str(caught.exception))

    def test_an_absent_local_file_is_no_paths_and_a_broken_one_is_an_error(self):
        path = write_json(os.path.join(self.tmp, 'c.json'),
                          {'schemaVersion': 1, 'captures': [{'name': 'x'}]})
        self.assertEqual(goldens.load_corpus(path)['captures'][0].get('path'), '')
        with open(os.path.join(self.tmp, goldens.LOCAL_NAME), 'w', encoding='utf-8') as handle:
            handle.write('{ not json')
        with self.assertRaises(schemas.SchemaError) as caught:
            goldens.load_corpus(path)
        self.assertIn('cannot read', str(caught.exception))

    def test_an_expect_file_that_is_absent_is_an_empty_label_set_and_not_an_error(self):
        expect = goldens.load_expect(os.path.join(self.tmp, 'nothing.expect.json'))
        self.assertEqual(expect.get('detectors'), {})
        self.assertEqual(expect.get('findings'), {})
        self.assertIn('no expect file', expect.get('why', [''])[0])

    def test_a_command_line_puts_the_path_after_the_command_and_repeats_it_for_the_token(self):
        entry = goldens.GoldenCapture(name='x', path='captures/My Frame.rdc', commands=[])
        self.assertEqual(goldens.command_line(entry, ['draws', '25']),
                         ['draws', 'captures/My Frame.rdc', '25'])
        self.assertEqual(goldens.command_line(entry, ['passdiff', goldens.CAPTURE_TOKEN]),
                         ['passdiff', 'captures/My Frame.rdc', 'captures/My Frame.rdc'])

    def test_a_recorded_text_says_the_key_where_the_path_was(self):
        """The three spellings a path arrives in: as given, its other slash, and JSON-escaped in a document."""
        path = 'captures/My Frame.rdc'
        self.assertEqual(goldens.redact('# summary %s\n' % path, path), '# summary <capture>\n')
        self.assertEqual(goldens.redact(r'captures\My Frame.rdc' + ' twice', path), '<capture> twice')
        self.assertEqual(goldens.redact('"capture": "captures\\\\My Frame.rdc"', path),
                         '"capture": "<capture>"')
        self.assertEqual(goldens.redact('My Frame.rdc alone', path), '<capture> alone')

    def test_a_name_without_its_extension_is_not_redacted(self):
        """Deliberate: `My Frame` cannot be told from the tool's own words, so it is measured, not rewritten."""
        self.assertEqual(goldens.redact('what the My Frame renderer did', 'captures/My Frame.rdc'),
                         'what the My Frame renderer did')

    def test_redaction_without_a_path_is_the_text_itself(self):
        self.assertEqual(goldens.redact('unchanged', ''), 'unchanged')

    def test_a_transcript_name_is_a_file_name(self):
        self.assertEqual(goldens.transcript_name(['passdiff', '<capture>']), 'passdiff-self.txt')
        self.assertEqual(goldens.transcript_name(['descriptors', '15']), 'descriptors-15.txt')


class TestTheCheckedInGoldensAreGeneric(unittest.TestCase):
    """No file under `goldens/` names a capture: the corpus carries a key and a digest, and the paths it
    does not carry live in the local file, which is not in git -- so this is checkable exactly where that
    file is, and *skips* where it is not (a fresh clone, CI). Skipping rather than passing is the same
    distinction `goldens --check` makes with exit 2: absent captures are working-tree state, and a check
    that cannot read them has not passed.
    """

    def setUp(self) -> None:
        self.corpus_path = os.path.join(ROOT, goldens.GOLDENS_DIR, goldens.CORPUS_NAME)
        if not os.path.isfile(os.path.join(ROOT, goldens.GOLDENS_DIR, goldens.LOCAL_NAME)):
            self.skipTest('%s is not here (it is local, and not in git)' % goldens.LOCAL_NAME)

    def test_no_committed_file_carries_a_captures_path_or_file_name(self):
        corpus = goldens.load_corpus(self.corpus_path)
        forms = set()
        for capture in corpus['captures']:
            path = str(capture.get('path', ''))
            if path:
                forms |= {path, path.replace('\\', '/'), path.replace('/', '\\'),
                          os.path.basename(path), json.dumps(os.path.basename(path))[1:-1]}
        self.assertTrue(forms, 'the local file names no capture, so there is nothing to check here')
        checked = 0
        for folder, _dirs, files in os.walk(os.path.join(ROOT, goldens.GOLDENS_DIR)):
            for name in files:
                if name == goldens.LOCAL_NAME:
                    continue      # the one file the paths belong in, and the reason it is not in git
                with open(os.path.join(folder, name), encoding='utf-8', errors='replace') as handle:
                    text = handle.read()
                checked += 1
                for form in sorted(forms):
                    self.assertNotIn(form, text, '%s carries a capture\'s path'
                                     % os.path.join(folder, name))
        self.assertGreater(checked, 10, 'the corpus should have transcripts to check')


class TestTranscripts(GoldenCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_fixture_capture()
        self.corpus_path = self.corpus([self.capture_entry()])

    def transcript_path(self, name: str = 'summary.txt') -> str:
        return os.path.join(self.tmp, goldens.GOLDENS_DIR, 'fixture', name)

    def test_write_produces_transcripts_that_check_then_accepts(self):
        code, written = self.run_goldens_code('--write', '--corpus', self.corpus_path)
        self.assertEqual(code, 0)
        self.assertIn('transcripts written', written)
        with open(self.transcript_path(), encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('# summary <capture>', text)
        self.assertIn('# exit: 0', text)
        # The path is recorded as the key: a transcript that carried it would fail the moment the capture
        # moved, and it is the one thing in a golden that is about this machine rather than about the tool.
        self.assertNotIn('capture.rdc', text)
        self.assertIn('--- stdout ---', text)
        self.assertIn('chunk count', text)
        code, checked = self.run_goldens_code('--check', '--corpus', self.corpus_path)
        self.assertIn('0 mismatch(es)', checked)
        self.assertEqual(code, 0)

    def test_a_transcript_that_changed_is_a_mismatch_with_a_readable_diff(self):
        self.run_goldens('--write', '--corpus', self.corpus_path)
        with open(self.transcript_path('summary.txt'), 'a', encoding='utf-8') as handle:
            handle.write('a line nobody wrote\n')
        code, out = self.run_goldens_code('--check', '--corpus', self.corpus_path)
        self.assertEqual(code, 1)
        self.assertIn('summary.txt', out)
        # `-` is what the checked-in transcript says, `+` what this run produced: the line was added to
        # the *golden*, so it reads as the expected side.
        self.assertIn('- a line nobody wrote', out)

    def test_a_transcript_that_is_missing_is_a_mismatch(self):
        out = self.run_goldens('--check', '--corpus', self.corpus_path)
        self.assertIn('no transcript', out)
        self.assertIn('1 mismatch(es)', out)

    def test_a_capture_that_is_not_here_is_not_compared_and_nothing_to_compare_is_exit_2(self):
        path = self.corpus([self.capture_entry('elsewhere', path='not-here.rdc')])
        out = self.run_goldens('--check', '--corpus', path)
        self.assertIn('not compared (the file it names is not here)', out)
        self.assertIn('nothing to compare', out)
        self.assertEqual(self.run_goldens_code('--check', '--corpus', path)[0], 2)

    def test_a_capture_with_no_path_at_all_is_not_compared_and_says_why(self):
        """The committed corpus carries no paths, so this is the ordinary case and not a failure.

        The reason names the local file rather than a path: this output is what a CI log keeps.
        """
        path = self.corpus([self.capture_entry('elsewhere', path='')])
        out = self.run_goldens('--check', '--corpus', path)
        self.assertIn('not compared (no path in %s)' % goldens.LOCAL_NAME, out)
        self.assertEqual(self.run_goldens_code('--check', '--corpus', path)[0], 2)

    def test_a_capture_that_is_not_the_file_the_goldens_were_written_for_is_a_mismatch(self):
        entry = self.capture_entry(sha256='00' * 32, bytes=4)
        path = self.corpus([entry])
        self.run_goldens('--write', '--corpus', path)
        out = self.run_goldens('--check', '--corpus', path)
        self.assertIn('sha256', out)
        self.assertIn('the file is', out)

    def test_the_transcript_pins_the_exit_code_and_the_command_line(self):
        self.corpus([self.capture_entry(commands=[['passdiff', goldens.CAPTURE_TOKEN]])])
        self.run_goldens('--write', '--corpus', self.corpus_path)
        with open(self.transcript_path('passdiff-self.txt'), encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('# passdiff <capture> <capture>', text)
        self.assertIn('# exit: 0', text)


class TestLabels(GoldenCase):
    def test_the_label_file_is_compared_detector_by_detector(self):
        capture = self.write_fixture_capture()
        entry = self.capture_entry(commands=[])
        self.write_expect('fixture', {'name': 'fixture', 'detectors': {'marker-imbalance': 0,
                                                                      'unattributed-draws': 0,
                                                                      'zero-work': 0}})
        corpus = self.corpus([entry])
        self.assertIn('0 mismatch(es)', self.run_goldens('--check', '--corpus', corpus))
        self.write_expect('fixture', {'name': 'fixture', 'detectors': {'marker-imbalance': 3}})
        out = self.run_goldens('--check', '--corpus', corpus)
        self.assertIn('marker-imbalance: 0 finding(s), the label says 3', out)
        self.assertEqual(self.run_goldens_code('--check', '--corpus', corpus)[0], 1)
        self.assertTrue(os.path.isfile(capture))

    def test_a_label_that_could_not_be_checked_is_not_a_passing_label(self):
        """No chunk-name map means the detector could not look, which is not the same answer as "clean"."""
        self.write_fixture_capture()
        entry = self.capture_entry(commands=[])
        self.write_expect('fixture', {'name': 'fixture', 'detectors': {'marker-imbalance': 0}})
        corpus = self.corpus([entry])
        blind = [mock.patch.object(rdc_detect_stream, name, return_value=None)
                 for name in ('detect_marker_balance', 'detect_unattributed_draws', 'detect_zero_work')]
        with blind[0], blind[1], blind[2]:
            checked, wrong, problems = goldens.check_labels(self.tmp, entry,
                                                           {'detectors': {'marker-imbalance': 0}})
        self.assertEqual((checked, wrong), (0, 1))
        self.assertIn('no chunk-name map', problems[0])
        self.assertIn('0 mismatch(es)', self.run_goldens('--check', '--corpus', corpus))


class TestBundleHalf(GoldenCase):
    def schemas(self) -> str:
        """A permissive schema per kind: what is under test is the *harness*, not the driver's contract."""
        directory = os.path.join(self.tmp, 'schemas')
        for kind in ('manifest', 'capture', 'events', 'resources', 'messages', 'state', 'shaders'):
            write_json(os.path.join(directory, kind + '.schema.json'), {'title': kind, 'type': 'object'})
        return directory

    def bundle(self, name: str = 'bundle', events_key: str = 'events') -> str:
        root = os.path.join(self.tmp, name)
        R_path = os.path.join(root, 'events.json')
        write_json(os.path.join(root, 'manifest.json'),
                   {'bundleVersion': R.BUNDLE_VERSION, 'capture': 'capture.rdc', 'captureSha256': 'ab' * 32,
                    'driver': 'replay_dump', 'renderdoc': '1.46'})
        write_json(os.path.join(root, 'capture.json'), {'capture': 'capture.rdc'})
        events = [{'eid': 1, 'marker': 'Scene > BasePass', 'pso': '100', 'psoKind': 'graphics',
                   'shaders': 'ps=7 ', 'targets': [], 'depth': '0', 'rootParameters': 1, 'state': 'x'}]
        if events_key == 'events':
            write_json(R_path, {'capture': 'capture.rdc', 'events': events})
        else:
            write_json(R_path, {'capture': 'capture.rdc', events_key: events})
        write_json(os.path.join(root, 'resources.json'), {'capture': 'capture.rdc', 'resources': []})
        write_json(os.path.join(root, 'messages.json'), {'capture': 'capture.rdc', 'messages': []})
        return root

    def entry_with_bundle(self, bundle: str) -> Any:
        return self.capture_entry(commands=[], bundle=os.path.relpath(bundle, self.tmp),
                                  path='capture.rdc')

    def test_the_bundle_half_validates_reports_and_compares_the_bundle_with_itself(self):
        self.write_fixture_capture()
        bundle = self.bundle()
        entry = self.entry_with_bundle(bundle)
        expect: Any = {'name': 'fixture', 'report': {'events': 1, 'passes': 1, 'resources': 0},
                  'selfAb': {'same': 1, 'structureChanges': 0, 'constantsChanged': 0}}
        checked, failed, notes = goldens.check_bundle(self.tmp, entry, expect, False,
                                                      schema_dir=self.schemas())
        self.assertEqual((checked, failed), (True, 0), notes)

    def test_a_count_that_moved_is_a_mismatch(self):
        self.write_fixture_capture()
        entry = self.entry_with_bundle(self.bundle())
        expect: Any = {'report': {'events': 2, 'passes': 1, 'resources': 0},
                  'selfAb': {'same': 1}}
        _checked, failed, notes = goldens.check_bundle(self.tmp, entry, expect, False,
                                                       schema_dir=self.schemas())
        self.assertEqual(failed, 1)
        self.assertIn('events is 1, the label says 2', notes[0])

    def test_a_detector_that_stopped_firing_is_a_mismatch_and_so_is_an_unlabelled_one(self):
        self.write_fixture_capture()
        entry = self.entry_with_bundle(self.bundle())
        expect: Any = {'findings': {'dead-compute': 0}}
        _checked, failed, notes = goldens.check_bundle(self.tmp, entry, expect, False,
                                                       schema_dir=self.schemas())
        self.assertEqual(failed, 0, notes)
        expect: Any = {'findings': {'dead-compute': 2}}
        _checked, failed, notes = goldens.check_bundle(self.tmp, entry, expect, False,
                                                       schema_dir=self.schemas())
        self.assertEqual(failed, 1)
        self.assertIn('dead-compute fired 0 time(s), the label says 2', notes[0])

    def test_a_bundle_with_a_repeated_json_key_fails_validation(self):
        """The class of bug a plain parse hides: two `vs` members where an array was meant, the second
        silently winning the first's place. `json.load` keeps the last one and says nothing, so the check
        is made on the text rather than on the object a parse would hand over."""
        self.write_fixture_capture()
        bundle = self.bundle()
        with open(os.path.join(bundle, 'events.json'), 'w', encoding='utf-8') as handle:
            handle.write('{"capture": "capture.rdc", "events": [], "vs": [], "vs": []}')
        entry = self.entry_with_bundle(bundle)
        expect: Any = {'report': {'events': 1, 'passes': 1, 'resources': 0}}
        _checked, failed, notes = goldens.check_bundle(self.tmp, entry, expect, False,
                                                       schema_dir=self.schemas())
        self.assertGreaterEqual(failed, 1)
        self.assertIn('appears twice', ' '.join(notes))

    def test_the_ab_document_is_written_then_compared_byte_for_byte(self):
        """A count can stay the same while the document moves, so the document itself is kept."""
        self.write_fixture_capture()
        entry = self.entry_with_bundle(self.bundle())
        expect: Any = {'selfAb': {'same': 1}, 'document': 'selfab.json'}
        _checked, failed, notes = goldens.check_bundle(self.tmp, entry, expect, False,
                                                       schema_dir=self.schemas())
        self.assertEqual(failed, 1)
        self.assertIn('not the document this run wrote', notes[0])
        _checked, failed, notes = goldens.check_bundle(self.tmp, entry, expect, False,
                                                       schema_dir=self.schemas(), write=True)
        self.assertEqual((failed, notes), (0, []))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, goldens.GOLDENS_DIR, 'fixture',
                                                   'selfab.json')))
        _checked, failed, _notes = goldens.check_bundle(self.tmp, entry, expect, False,
                                                        schema_dir=self.schemas())
        self.assertEqual(failed, 0)
        with open(os.path.join(self.tmp, goldens.GOLDENS_DIR, 'fixture', 'selfab.json'), 'a',
                  encoding='utf-8') as handle:
            handle.write(' ')
        _checked, failed, notes = goldens.check_bundle(self.tmp, entry, expect, False,
                                                       schema_dir=self.schemas())
        self.assertEqual(failed, 1)
        self.assertIn('selfab.json', notes[0])

    def test_a_bundle_that_is_not_here_is_a_note_and_not_a_mismatch(self):
        self.write_fixture_capture()
        entry = self.entry_with_bundle(os.path.join(self.tmp, 'nowhere'))
        checked, failed, notes = goldens.check_bundle(self.tmp, entry, {'report': {'events': 1}}, False,
                                                      schema_dir=self.schemas())
        self.assertEqual((checked, failed), (False, 0))
        self.assertIn('not here', notes[0])


if __name__ == '__main__':
    unittest.main()
