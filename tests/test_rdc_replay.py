"""The replay library: `bin/rdc_replay.dll` through `rdc_replay.py` (REFERENCE §9).

Everything here is **device-free**, which is what makes it a test rather than a manual check: a session with
no capture loads the engine and answers `schema` (no device), a session on a file that is not there fails
before a device would be created, and a capture the version guard refuses is refused before one is created
too. What a device *is* needed for -- a document from a real frame -- is checked where it belongs: the
sweep command's own tests, and the corpus (which compares the executable's text, and the library's promise
is that it produces the same text: measured over 15 commands on `desktop-1`, and byte for byte against a
`batch` file with the same lines).

Skipped where the library is not built, which is a checkout that has not run `cmake --build build` -- and
that is not a silent skip: `available()` says why, and the message names the build command.
"""
from __future__ import annotations

import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(HERE)
for _p in (HERE, _ROOT, os.path.join(_ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_driver     # noqa: E402
import rdc_replay     # noqa: E402
from rdc_testcase import ROOT, TempDirCase    # noqa: E402

#: Every test that touches the ABI goes through this, so a machine without the built library runs the
#: file's *other* half -- the module's own contract -- and reports the rest as skipped rather than failed.
LIBRARY = rdc_replay.available()
EXE = os.path.join(ROOT, rdc_driver.EXE_PATH)
HAVE_EXE = os.path.isfile(EXE)

class TestTheModuleItself(TempDirCase):
    """The parts of `rdc_replay.py` that are about this checkout rather than about the engine."""

    def test_the_library_path_follows_the_environment_first(self) -> None:
        """`$RDC_REPLAY_DLL` names another build, the way `$RDC_LZ4_DLL` does for the decoder."""
        os.environ.pop(rdc_replay.DLL_ENV, None)
        self.assertEqual(rdc_replay.dll_path(), os.path.join(ROOT, rdc_replay.DLL_PATH))
        os.environ[rdc_replay.DLL_ENV] = 'elsewhere/rdc_replay.dll'
        try:
            self.assertEqual(rdc_replay.dll_path(), 'elsewhere/rdc_replay.dll')
        finally:
            os.environ.pop(rdc_replay.DLL_ENV, None)

    def test_a_library_that_is_not_there_is_reported_rather_than_raised(self) -> None:
        """`available()` is the check a caller makes before offering a library path at all."""
        missing = os.path.join(self.tmp, 'nothing.dll')
        self.assertEqual(rdc_replay.abi(missing), '')
        self.assertFalse(rdc_replay.available(missing))
        with self.assertRaises(rdc_replay.ReplayError) as caught:
            rdc_replay.Session(None, root=self.tmp)
        self.assertIn('no replay library at', str(caught.exception))

    def test_the_build_checks_the_replay_library_beside_the_driver(self) -> None:
        """Three artefacts, and the two that share sources are compared separately on purpose."""
        names = [target['name'] for target in rdc_driver.TARGETS]
        self.assertIn('replay library', names)
        library = [t for t in rdc_driver.TARGETS if t['name'] == 'replay library'][0]
        self.assertEqual(library['artefact'], rdc_driver.REPLAY_LIB_PATH)
        self.assertEqual(library['source_dir'], rdc_driver.SOURCE_DIR)

    def fake_checkout(self, dll_time: float, source_time: float) -> str:
        """A tree with the three things the comparison reads: the DLL, a source, and the build file."""
        for relative, when, text in ((rdc_driver.REPLAY_LIB_PATH, dll_time, 'MZ'),
                                     (os.path.join(rdc_driver.SOURCE_DIR, 'one.cpp'), source_time, '//'),
                                     ('CMakeLists.txt', source_time - 1000, '#')):
            path = os.path.join(self.tmp, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(text)
            os.utime(path, (when, when))
        return os.path.join(self.tmp, rdc_driver.REPLAY_LIB_PATH)

    def test_a_library_older_than_its_sources_says_so_and_names_the_build(self) -> None:
        """The one case the driver's own warning cannot see: `WarnIfDriverIsStale` asks about the *host*
        process, which under a library session is `python.exe` -- no `src\\cpp` beside it, so it is quiet
        exactly when the answers are from the previous build (REFERENCE §9, §4.16).

        The sentence is the driver's own, so a reader who has seen its warning recognises this one.
        """
        dll = self.fake_checkout(dll_time=1000.0, source_time=2000.0)
        note = rdc_replay.stale_note(dll, root=self.tmp)
        self.assertIn('older than its sources', note)
        self.assertIn(os.path.join('src', 'cpp', 'one.cpp'), note)
        self.assertIn('1000 s later', note)
        self.assertIn('cmake --build build --config Release', note)

    def test_a_current_library_says_nothing(self) -> None:
        """A warning that fired on every normal run is a warning nobody reads."""
        dll = self.fake_checkout(dll_time=3000.0, source_time=2000.0)
        self.assertEqual(rdc_replay.stale_note(dll, root=self.tmp), '')

    def test_a_missing_library_is_a_missing_build_and_not_a_stale_one(self) -> None:
        """No file is not an old file: the caller reports "no library at", which is a different sentence
        and a different fix (`build`), and a staleness warning would blur the two."""
        dll = self.fake_checkout(dll_time=1000.0, source_time=2000.0)
        os.remove(dll)
        self.assertEqual(rdc_replay.stale_note(dll, root=self.tmp), '')

class TestTheAbi(TempDirCase):
    """The ABI as a caller meets it: the version handshake, then what each kind of failure looks like."""

    def setUp(self) -> None:
        super().setUp()
        if not LIBRARY:
            self.skipTest('no %s -- build it with `cmake --build build --config Release`'
                          % rdc_replay.dll_path())

    def test_the_library_reports_the_abi_this_module_speaks(self) -> None:
        self.assertEqual(rdc_replay.abi(), rdc_replay.ABI_VERSION)
        with rdc_replay.Session() as session:
            self.assertEqual(session.abi_version, rdc_replay.ABI_VERSION)

    def test_a_session_without_a_capture_answers_the_schemas(self) -> None:
        """The one document that needs no capture, and the reason `availablity` is not just "is it built".

        The text is compared with the executable's own, because that is the library's whole promise: one
        command language, one dispatcher, one answer. `schema` is the command both halves can run with no
        capture and no device, so it is the one this test can hold to byte for byte.
        """
        with rdc_replay.Session() as session:
            code, text = session.command('schema probe')
        self.assertEqual(code, 0)
        self.assertIn('"title": "probe"', text)
        if not HAVE_EXE:
            return
        import subprocess
        exe = subprocess.run([EXE, 'schema', 'probe'], cwd=ROOT, capture_output=True)
        self.assertEqual(exe.returncode, 0)
        self.assertEqual(exe.stdout.decode('utf-8', 'replace'), text)

    def test_a_command_that_needs_a_capture_is_refused_without_one(self) -> None:
        with rdc_replay.Session() as session:
            with self.assertRaises(rdc_replay.ReplayError) as caught:
                session.command('draws')
        self.assertIn("'draws' needs a capture", str(caught.exception))

    def test_an_empty_command_is_refused(self) -> None:
        with rdc_replay.Session() as session:
            with self.assertRaises(rdc_replay.ReplayError) as caught:
                session.command('   ')
        self.assertIn('empty command', str(caught.exception))

    def test_a_closed_session_refuses_further_commands(self) -> None:
        session = rdc_replay.Session()
        session.close()
        session.close()      # closing twice is legal and does nothing
        with self.assertRaises(rdc_replay.ReplayError) as caught:
            session.command('schema')
        self.assertIn('closed', str(caught.exception))

    def test_a_capture_that_is_not_there_is_reported_with_the_engines_own_reason(self) -> None:
        """No device is created for this: the file is opened before the replay is."""
        missing = os.path.join(self.tmp, 'not-here.rdc')
        with self.assertRaises(rdc_replay.ReplayError) as caught:
            rdc_replay.Session(missing)
        self.assertIn('cannot open', str(caught.exception))

    def test_a_capture_from_a_newer_engine_is_refused_before_a_device_is_created(self) -> None:
        """The version guard through the ABI -- the same refusal the command line makes, same sentence.

        The fixture is a 32-byte header and nothing else: the guard reads the container's own header
        rather than asking the engine, which is what lets it refuse before a device exists (REFERENCE §9).
        """
        future = os.path.join(self.tmp, 'from-the-future.rdc')
        header = bytearray(32)
        header[0:4] = b'RDOC'
        header[8:10] = struct.pack('<H', 258)          # logfile version, as every capture has it
        header[16:16 + 11] = b'9.99 future'
        with open(future, 'wb') as handle:
            handle.write(bytes(header))
        with self.assertRaises(rdc_replay.ReplayError) as caught:
            rdc_replay.Session(future)
        message = str(caught.exception)
        self.assertIn('recorded by RenderDoc 9.99', message)
        self.assertIn("must be at least the capture's version", message)
        self.assertIn('this replay engine is 1.46', message)    # the engine that is installed here

    def test_several_lines_run_in_one_session_and_report_their_own_codes(self) -> None:
        """The point of the library: N questions, one standup, and each answer's own exit code.

        `schema` with no name lists the 24 document kinds, which is a second shape of the same command:
        the code is the command's own, so a caller can tell "answered" from "refused" per line.
        """
        with rdc_replay.Session() as session:
            first, listed = session.command('schema probe')
            second, summary = session.command('schema')
        self.assertEqual([first, second], [0, 0])
        self.assertIn('"title": "probe"', listed)
        self.assertIn('document kind(s)', summary)

__all__ = [
    'TestTheAbi',
    'TestTheModuleItself',
]
