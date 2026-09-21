"""The corpus and its goldens: what each capture is, what the tools answer about it, and what must fire.

This is the regression net for the **analyser**, the way the payload tests are the net for the parsers.
Two halves, both checked in:

  * `goldens/<capture>/<command>.txt` -- a *transcript*: the tool's own stdout for one command over one
    capture, with the command line, the exit code and the stderr that came with it. Running the same
    command again must reproduce it byte for byte, so a decoder or writer change shows up as a reviewable
    diff instead of silent drift (`goldens --check`);
  * `goldens/<capture>.expect.json` -- the *labels*: what a person checked it is right for the tool to
    say. The file-side detectors that must fire (`marker-imbalance`, `unattributed-draws`, `zero-work`),
    the report counts for that capture's bundle, and the numbers the bundle must compare to against
    *itself*. Only the labels can say a detector *fired*; a transcript alone says "it still says what it
    said", which is not the same claim.

The captures are **not** in the repository, and neither are their paths: a corpus is committed and read by
everyone, while where a dump sits is this machine's state and its file name may name the frame it came from.
So a capture here is named by a *key* (`desktop-1`, `mobile-1`: platform, then size ascending), identified by
its SHA-256, and `captures.local.json` beside the corpus -- absent from git, and optional -- says which file
on this machine that key is. A bundle is engine output for one machine's GPU, and is one command away too.
What is checked in is what must hold *about* them. So a capture or a bundle that is not on this machine is
reported as **not compared**, and `--check` exits 2 when nothing at all could be compared -- like
`build --check`, and for the same reason: whether a capture or a bundle has been produced is the state of a
working tree, not a property of the tool. That is also why none of this is in `tests/`: the suite is hermetic
and takes seconds, this reads hundreds of megabytes and takes minutes.

Nothing a run records carries a path either: a transcript's command line and the `capture` member of an A/B
document are written through `redact`, which puts the capture's key where the path was. That is what keeps a
committed golden generic *and* independent of where the file lives -- the same capture in another folder
still reproduces it, which a golden carrying the path would not.

The command list per capture is part of the corpus rather than of this module, and every command is run as
a **subprocess of the CLI as a user runs it** (`rdc_analysis.py <cmd> <path> ...`), not as a function call:
what a transcript pins is the command's output contract, exit code included.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, TypedDict, cast

import rdc_cache
import rdc_driver
import rdc_profile
import rdc_schemas

#: The folder the corpus lives in, relative to the repository root: `captures.json`, one
#: `<name>.expect.json` per capture, and a `<name>/` folder of transcripts per capture.
GOLDENS_DIR = 'goldens'

#: The corpus index, the expectations' suffix, and the token a command writes where it wants the capture
#: path a *second* time (`passdiff` compares a capture with itself). The token is also what a transcript and
#: an A/B document record *instead of* the path (`redact`), so its two uses are one idea: "the capture this
#: is about".
CORPUS_NAME = 'captures.json'
#: Where this machine's copies of the corpus captures live (`capturePaths`: key -> path). Not in git: the
#: committed corpus names its captures by key and digest, and a path is local state.
LOCAL_NAME = 'captures.local.json'
EXPECT_SUFFIX = '.expect.json'
CAPTURE_TOKEN = '<capture>'

#: The corpus format's own version, so a corpus from a newer tool is refused rather than half-read.
CORPUS_VERSION = 1

#: Where the harness writes what it runs (`report`, `replaydiff`): `build/` is gitignored, so a check
#: leaves nothing to commit behind.
WORK_DIR = os.path.join('build', 'goldens')

#: How many lines of a mismatch a diff prints before it stops. A transcript that changed usually changed in
#: one place; a hundred lines of it is a reader's problem, and the file itself is one `--write` away.
DIFF_LINES = 12


class GoldenCapture(TypedDict, total=False):
    """One capture in the corpus: which file it is, what it is, and what is expected of it."""
    #: The key this capture is named by everywhere: `desktop-1`, `desktop-2`, `mobile-1` -- platform, then
    #: size ascending. Generic by design, because the corpus is committed and a capture's own file name
    #: belongs to whoever dumped it. It names the folder under `goldens/` and the bundle under
    #: `build/goldens/` as well, so the key, the transcripts and the labels stay in step.
    name: str
    #: Where this machine's copy of it is, filled in from `captures.local.json` beside the corpus (see
    #: `_load_local_paths`) rather than written here. Empty means this machine does not have the capture,
    #: which is reported as *not compared* -- never as a failure, and never as a path in a committed file.
    path: str
    #: The container's SHA-256. Checked when the capture is present, because a golden is for *this* file:
    #: a re-capture under the same name would otherwise be compared against numbers from another frame.
    sha256: str
    what: str
    api: str
    bytes: int
    known: List[str]
    #: The commands whose transcript is pinned, each as its own argument list (`[["draws", "25"], ...]`):
    #: the capture path is inserted after the command name, and `<capture>` where it is wanted again.
    commands: List[List[str]]
    #: Where this capture's bundle is looked for, relative to the root. Absent when it has none.
    bundle: str


class GoldenCorpus(TypedDict):
    """The index: what this project's regression net is run over."""
    schemaVersion: int
    note: str
    captures: List[GoldenCapture]
    #: The A/B pair whose numbers `replaydiff` must reproduce when both bundles are present.
    pair: Dict[str, Any]


class GoldenExpect(TypedDict, total=False):
    """The labels for one capture: what must fire, and what the numbers must be."""
    name: str
    #: `detector -> the number of findings it must produce`, for the detectors that read the `.rdc` itself.
    detectors: Dict[str, int]
    #: What the report over this capture's bundle must count.
    report: Dict[str, int]
    #: `detector -> the findings it must produce` over that bundle: the histogram, so a detector that stops
    #: firing is a mismatch rather than a smaller total that still looks like a total.
    findings: Dict[str, int]
    #: What `replaydiff` must say about this capture's bundle against itself.
    selfAb: Dict[str, int]
    #: A whole `replaydiff.json`, checked in beside the transcripts and compared byte for byte: the counts
    #: above can stay the same while the document moves (a pass renamed, a row reordered), and this is the
    #: artefact a reader can diff (§4.16) -- "the same capture through two builds of the tools changed
    #: nothing" as a file rather than as a reading.
    document: str
    #: Why each label is what it is, one line per label, so a reader can disagree with the number.
    why: List[str]


def _relative(root: str, path: str) -> str:
    """`path` relative to the root with forward slashes, for output that does not name a machine."""
    try:
        return os.path.relpath(path, root).replace(os.sep, '/')
    except ValueError:
        return path.replace(os.sep, '/')


def _print(text: str) -> None:
    """One line of harness output, surviving a console that cannot encode it.

    A diff line is a capture's own text -- an engine name, a decode of arbitrary payload bytes -- and a
    Windows console runs on a code page that cannot represent all of it: printing such a line raised
    `UnicodeEncodeError` and killed the check that was reporting the problem. The line is printed with the
    characters this console cannot show replaced, which is a smaller lie than no output at all.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, 'encoding', None) or 'ascii'
        print(text.encode(encoding, 'replace').decode(encoding, 'replace'))


def load_corpus(path: str) -> GoldenCorpus:
    """Read `goldens/captures.json`, refusing a version this tool does not know.

    Read through `rdc_schemas.load_document`, so a corpus with a repeated key is a failure rather than a
    silently-won argument between two values of the same member.
    """
    try:
        document = rdc_schemas.load_document(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise rdc_schemas.SchemaError('cannot read the corpus %s: %s' % (path, exc)) from exc
    if not isinstance(document, dict):
        raise rdc_schemas.SchemaError('%s is not a JSON object' % path)
    if document.get('schemaVersion') != CORPUS_VERSION:
        raise rdc_schemas.SchemaError('corpus version %s, this tool reads %d'
                                      % (document.get('schemaVersion'), CORPUS_VERSION))
    if not isinstance(document.get('captures'), list):
        raise rdc_schemas.SchemaError('%s has no `captures` list' % path)
    raw: Any = document.get('captures')
    local = _load_local_paths(path)
    captures: List[GoldenCapture] = []
    names = set()
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get('name'):
            raise rdc_schemas.SchemaError('%s: every capture needs a `name`' % path)
        # A cast rather than a re-typed copy: the member that matters was checked above, and the rest of
        # the entry is read with `.get()` by every caller -- nothing indexes it into existence.
        capture = cast(GoldenCapture, entry)
        key = str(entry.get('name', ''))
        names.add(key)
        # A `path` written in the corpus is honoured (a corpus of one's own may carry one -- it is not
        # committed anywhere), and the local file fills in the rest.
        if not capture.get('path'):
            capture['path'] = local.get(key, '')
        captures.append(capture)
    # A local file that names a capture the corpus does not have is a typo that would otherwise present
    # itself as "not on this machine" for the capture that *is* there: say so instead.
    unknown = sorted(set(local) - names)
    if unknown:
        raise rdc_schemas.SchemaError('%s names %s, which %s does not have'
                                      % (LOCAL_NAME, ', '.join(unknown), os.path.basename(path)))
    return GoldenCorpus(schemaVersion=CORPUS_VERSION, note=str(document.get('note', '')),
                        captures=captures, pair=document.get('pair') or {})


def _load_local_paths(corpus_path: str) -> Dict[str, str]:
    """`key -> this machine's copy of that capture`, out of `captures.local.json` beside the corpus.

    The one member of the corpus that is deliberately *not* in the repository: a corpus is read by everyone,
    while where a dump sits is this machine's state and its file name may name the frame it came from. So
    the committed index carries a key and a digest and this file -- absent on a fresh clone, absent in CI --
    says which file on this machine that key is.

    An absent file is not an error: it is exactly the "no capture here" case the check already reports as
    *not compared*. A present file that cannot be read is an error, because a silent empty map would look
    like a machine with no captures.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(corpus_path)), LOCAL_NAME)
    if not os.path.isfile(path):
        return {}
    try:
        document = rdc_schemas.load_document(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise rdc_schemas.SchemaError('cannot read %s: %s' % (path, exc)) from exc
    if not isinstance(document, dict) or not isinstance(document.get('capturePaths'), dict):
        raise rdc_schemas.SchemaError('%s has no `capturePaths` object' % path)
    raw: Any = document['capturePaths']
    return {str(key): str(value) for key, value in raw.items()}


def redact(text: str, path: str) -> str:
    """A capture's own path written as `<capture>`: what a transcript and an A/B document record.

    Two reasons, one of them a requirement and one a bug fix. The requirement: everything this corpus
    commits is generic -- it names captures by key and digest -- and a recorded command line would otherwise
    put the file name of somebody's dump, and the folder it sits in, into the repository for good. The bug
    fix: a golden that carried the path would fail the moment the same capture moved, while the path is not
    part of any answer the tools give -- the digest is what identifies the bytes. So the recorded text says
    `<capture>`, the token a corpus command already uses for the same idea, and a transcript reads the same
    on every machine that has the file.

    Longest form first: the full path contains the file's own name, so replacing the short form first would
    leave `<capture>.rdc` behind.

    The file's *stem* -- `My Frame` for `My Frame.rdc` -- is deliberately not a form. A command would
    have to print the name without its extension for it to matter, and that string cannot be told from the
    tool's or the frame's own words (`capture`, `frame`, a marker named after the file), so replacing it
    could silently rewrite an answer rather than redact a path. Measured instead: no command in this corpus
    prints a stem, and the property is pinned by a test over the checked-in goldens.
    """
    if not path:
        return text
    base = os.path.basename(path)
    forms = {path, path.replace('\\', '/'), path.replace('/', '\\'), base}
    # An A/B document is JSON *text*, where a backslash is written twice: without the escaped spelling a
    # `renderdoc-src\\<capture>` -- the folder kept and only the file's name replaced -- is what survives,
    # which is a leak that still looks redacted.
    forms |= {json.dumps(form)[1:-1] for form in forms}
    for form in sorted(forms, key=len, reverse=True):
        if form:
            text = text.replace(form, CAPTURE_TOKEN)
    return text


def load_expect(path: str) -> GoldenExpect:
    """Read one `<name>.expect.json`, or an empty label set when there is no such file."""
    if not os.path.isfile(path):
        # Every member present and empty, so a caller can read the label set without checking for the keys
        # first: "nothing is labelled" and "this key was never written" are the same answer here.
        return GoldenExpect(name='', detectors={}, report={}, findings={}, selfAb={}, document='',
                            why=['(no expect file: only the transcripts are compared)'])
    try:
        document = rdc_schemas.load_document(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise rdc_schemas.SchemaError('cannot read %s: %s' % (path, exc)) from exc
    if not isinstance(document, dict):
        raise rdc_schemas.SchemaError('%s is not a JSON object' % path)
    return GoldenExpect(
        name=str(document.get('name', '')),
        detectors=document.get('detectors') or {},
        report=document.get('report') or {},
        findings=document.get('findings') or {},
        selfAb=document.get('selfAb') or {},
        document=str(document.get('document', '')),
        why=[str(line) for line in (document.get('why') or [])])


def sha256_file(path: str) -> str:
    """SHA-256 of a file, in 4 MB blocks: a capture is hundreds of megabytes and never held twice."""
    import hashlib
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            block = handle.read(4 << 20)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def tool_path() -> str:
    """The CLI this harness runs: `rdc_analysis.py`, next to this module."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rdc_analysis.py')


def command_line(capture: GoldenCapture, argv: Sequence[str]) -> List[str]:
    """One corpus command as the full command line: `<cmd> <path> <args...>`, `<capture>` repeated.

    The path is inserted rather than written in the corpus, so a corpus command is a *question* ("what
    does `draws 25` say about this capture") and not a file name, and the pair's self-comparison
    (`["passdiff", "<capture>"]`) does not have to spell the path twice.
    """
    parts = [str(capture.get('path', '')) if part == CAPTURE_TOKEN else part for part in argv]
    return [parts[0], str(capture.get('path', ''))] + parts[1:]


def _run(program: str, argv: Sequence[str], cwd: str) -> Tuple[int, str, str]:
    """Run one program and return `(exit code, stdout, stderr)`, with the line endings normalised.

    The child's environment is fixed rather than inherited: `$RDC_PROFILE`/`$RDC_PROGRESS` are removed (a
    phase table in a transcript would be a golden of this machine's speed) and `$RDC_NO_CACHE` is *set*,
    because the stream cache is otherwise invisible except in one place -- the `, cached` marker in a
    method label (`dxbc`, `verify`) -- and a transcript that depends on what this machine has already
    decoded is a transcript that fails on a fresh checkout. Decoding instead of reading the cache costs
    the capture's decode time per command, which is the price of the comparison meaning the same thing
    everywhere.
    """
    env = dict(os.environ)
    for name in (rdc_profile.PROFILE_ENV, rdc_profile.PROGRESS_ENV):
        env.pop(name, None)
    env[rdc_cache.NO_CACHE_ENV] = '1'
    try:
        finished = subprocess.run([program] + list(argv), cwd=cwd, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=1800)
    except OSError as exc:
        return 2, '', 'could not run %s: %s' % (program, exc)
    out = finished.stdout.decode('utf-8', 'replace').replace('\r\n', '\n')
    err = finished.stderr.decode('utf-8', 'replace').replace('\r\n', '\n')
    return finished.returncode, out, err


def run_command(argv: Sequence[str], cwd: str) -> Tuple[int, str, str]:
    """Run one offline-tool command as a subprocess: `(exit code, stdout, stderr)`.

    A subprocess rather than a call to the `cmd_*` function, because what a transcript pins is the command
    as a user runs it -- argument parsing, usage line, exit code -- and because a command that kills its
    process must be a failure of that command, not of the harness.

    `sys.executable` rather than `python`: the goldens must be produced by the interpreter that runs them,
    which on this machine is not the one on `PATH` either. A time-dependent phase table never reaches a
    transcript because `$RDC_PROFILE`/`$RDC_PROGRESS` are removed from the child's environment.
    """
    return _run(sys.executable, [tool_path()] + list(argv), cwd)


def run_driver(argv: Sequence[str], cwd: str) -> Tuple[int, str, str]:
    """Run one **driver** command (`bin/replay_dump.exe`), for the checks that need no device or capture.

    A separate entry point rather than a flag, because the two programs take different command sets and
    the same `argv` means different things to them -- asking the *offline tool* for `schema --check` answers
    with its usage and exit 0, which reads exactly like a passing check. That mistake was made here once,
    which is why the caller also counts a missing verdict line as a failure.
    """
    return _run(os.path.join(rdc_driver.repo_root(), rdc_driver.EXE_PATH), list(argv), cwd)


def transcript(argv: Sequence[str], code: int, out: str, err: str) -> str:
    """One command's transcript: what was run, what it exited with, and both streams.

    A header rather than a bare stdout dump, because a golden is read by a human when it changes: the
    command line says what produced it, the exit code catches a command that started failing while still
    printing most of its output, and the stderr section is where the tool's warnings live -- a command's
    stderr is empty otherwise, so a line arriving there is a finding rather than noise.
    """
    # Line endings normalised to '\n' on the way in as well as on the way out: a child's stdout arrives
    # with the platform's endings, the checked-in file may be re-written to them by `core.autocrlf`, and a
    # golden that changes when git touches a checkout is a golden nobody can keep.
    return ('# %s\n# exit: %d\n--- stdout ---\n%s--- stderr ---\n%s'
            % (' '.join(argv), code, out.replace('\r\n', '\n'), err.replace('\r\n', '\n')))


def transcript_name(argv: Sequence[str]) -> str:
    """The file name a command's transcript gets: the command and its arguments, joined by dashes.

    `<capture>` becomes `self` and everything a file name cannot hold (`< > : " / \\ | ? *`) becomes a
    dash, so `["passdiff", "<capture>"]` is `passdiff-self.txt` rather than a name Windows refuses.
    """
    parts = []
    for part in argv:
        cleaned = 'self' if part == CAPTURE_TOKEN else part
        for bad in '<>:"/\\|?*':
            cleaned = cleaned.replace(bad, '-')
        parts.append(cleaned.strip())
    return '-'.join(parts) + '.txt'


def diff_lines(expected: str, actual: str) -> List[str]:
    """The first few lines that differ, as `- expected` / `+ actual`, for a mismatch a reader can act on."""
    left, right = expected.splitlines(), actual.splitlines()
    lines: List[str] = []
    for index in range(max(len(left), len(right))):
        want = left[index] if index < len(left) else None
        have = right[index] if index < len(right) else None
        if want == have:
            continue
        if want is not None:
            lines.append('- %s' % want[:200])
        if have is not None:
            lines.append('+ %s' % have[:200])
        if len(lines) >= DIFF_LINES:
            lines.append('  ... (%d line(s) not shared)' % differing_lines(left, right))
            break
    return lines


def differing_lines(left: Sequence[str], right: Sequence[str]) -> int:
    """How many line positions the two files do not share (a count for a message, not a diff)."""
    return max(len(left), len(right)) - sum(1 for index in range(min(len(left), len(right)))
                                            if left[index] == right[index])


def check_transcripts(root: str, capture: GoldenCapture, write: bool,
                      verbose: bool) -> Tuple[int, int, List[str]]:
    """Compare (or write) one capture's transcripts. Returns `(compared, mismatched, problems)`."""
    name = str(capture.get('name', ''))
    directory = os.path.join(root, GOLDENS_DIR, name)
    compared = mismatched = 0
    problems: List[str] = []
    for argv in capture.get('commands', []):
        full = command_line(capture, argv)
        code, out, err = run_command(full, root)
        text = redact(transcript(full, code, out, err), str(capture.get('path', '')))
        path = os.path.join(directory, transcript_name(argv))
        if write:
            os.makedirs(directory, exist_ok=True)
            with open(path, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(text)
            _print('  %-20s written (%d line(s), exit %d)' % (transcript_name(argv),
                                                             len(out.splitlines()), code))
            continue
        compared += 1
        if not os.path.isfile(path):
            mismatched += 1
            problems.append('%s: no transcript (write it with `goldens --write`)' % transcript_name(argv))
            continue
        with open(path, encoding='utf-8') as handle:
            expected = handle.read()
        if expected == text:
            if verbose:
                _print('  %-20s ok (%d line(s))' % (transcript_name(argv), len(out.splitlines())))
            continue
        mismatched += 1
        problems.append('%s: %d line(s) of the transcript differ'
                        % (transcript_name(argv), differing_lines(expected.splitlines(),
                                                                  text.splitlines())))
        problems.extend('    %s' % line for line in diff_lines(expected, text))
    return compared, mismatched, problems


def check_labels(root: str, capture: GoldenCapture, expect: GoldenExpect) -> Tuple[int, int, List[str]]:
    """Run the file-side detectors the label file names, and compare their findings' counts.

    The detectors here read the `.rdc` itself (the report's other detectors read a bundle), which is what
    makes them checkable without one. Each is *skipped with the reason* when the chunk-name map is absent,
    exactly as the report treats it -- a label that could not be checked is not a passing label.
    """
    import rdc_detect_stream
    wanted = expect.get('detectors') or {}
    checked = wrong = 0
    problems: List[str] = []
    # Through the owner of the path: the detectors read the file *here*, not in a child process, so a
    # corpus-relative path has to be resolved against the corpus's root the way the transcripts' is.
    path = os.path.join(root, str(capture.get('path', '')))
    for detector, function in (('marker-imbalance', rdc_detect_stream.detect_marker_balance),
                               ('unattributed-draws', rdc_detect_stream.detect_unattributed_draws),
                               ('zero-work', rdc_detect_stream.detect_zero_work)):
        if detector not in wanted:
            continue
        try:
            found = function(path)
        except Exception as exc:      # a detector reports a file it cannot read; the harness must not die
            problems.append('%s: could not be checked (%s)' % (detector, exc))
            wrong += 1
            continue
        if found is None:
            problems.append('%s: not checked (no chunk-name map)' % detector)
            wrong += 1
            continue
        checked += 1
        if len(found) != wanted[detector]:
            wrong += 1
            problems.append('%s: %d finding(s), the label says %d' % (detector, len(found),
                                                                      wanted[detector]))
    return checked, wrong, problems


def check_bundle(root: str, capture: GoldenCapture, expect: GoldenExpect, verbose: bool,
                 schema_dir: Optional[str] = None,
                 write: bool = False) -> Tuple[bool, int, List[str]]:
    """Validate a capture's bundle, check the report's counts, and A/B the bundle against itself.

    Three questions, all about the *engine's* half of the tool and all answerable from files: are the
    driver's documents still documents (`validate` against the checked-in `schema/`, a repeated key
    included), does the report still count what it counted, and does the analyser agree with itself --
    `replaydiff` of a bundle against the *same* bundle must find no difference at all, which is the net for
    the analyser where the transcripts are the net for the parsers.

    Returns `(checked, failed, notes)`: `failed` counts real mismatches, `notes` what was not compared.
    """
    bundle = str(capture.get('bundle', ''))
    if not bundle:
        return False, 0, []
    if not os.path.isdir(os.path.join(root, bundle)):
        return False, 0, ['%s: not here (dump it: replay_dump dump "%s" %s)'
                          % (bundle, capture.get('path', ''), bundle)]
    checked = False
    failed = 0
    notes: List[str] = []

    if write:
        # A `--write` run produces the A/B document and nothing else from this half: `validate`, the
        # report's counts and the self-A/B's *numbers* are checks, and a check that writes its own
        # expectation is not one.
        return _write_document(root, capture, expect)

    # The checked-in `schema/` belongs to the repository and not to the corpus's root, so it is found from
    # the module rather than from the caller's directory (the tests hand in their own).
    schemas = schema_dir or os.path.join(rdc_driver.repo_root(), 'schema')
    code, out, err = run_command(['validate', bundle, schemas], root)
    if code != 0:
        failed += 1
        notes.append('validate %s: exit %d' % (bundle, code))
        notes.extend('    %s' % line for line in (out + err).splitlines() if line.startswith('FAIL'))
    else:
        checked = True
        if verbose:
            tail = [line for line in out.splitlines() if 'document(s) checked' in line]
            _print('  %-20s ok (%s)' % ('validate', tail[0] if tail else 'no summary line'))

    wanted_report = expect.get('report') or {}
    wanted_findings = expect.get('findings') or {}
    if wanted_report or wanted_findings:
        out_dir = os.path.join(WORK_DIR, 'report')
        code, out, err = run_command(['report', str(capture.get('path', '')), bundle, out_dir], root)
        if code != 0:
            failed += 1
            notes.append('report %s: exit %d' % (bundle, code))
        else:
            checked = True
            document = os.path.join(root, out_dir, 'report.json')
            found = _report_counts(document)
            for key in sorted(wanted_report):
                if found.get(key) != wanted_report[key]:
                    failed += 1
                    notes.append('report %s: %s is %s, the label says %s'
                                 % (bundle, key, found.get(key), wanted_report[key]))
            histogram = findings_by_detector(document)
            for detector in sorted(wanted_findings):
                if histogram.get(detector, 0) != wanted_findings[detector]:
                    failed += 1
                    notes.append('report %s: %s fired %d time(s), the label says %d'
                                 % (bundle, detector, histogram.get(detector, 0),
                                    wanted_findings[detector]))
            for detector in sorted(set(histogram) - set(wanted_findings)):
                failed += 1
                notes.append('report %s: %s fired %d time(s) and the label does not mention it'
                             % (bundle, detector, histogram[detector]))
            if verbose:
                _print('  %-20s ok (%s)' % ('report', ', '.join('%s %s' % (key, found.get(key))
                                                               for key in sorted(found))))

    wanted_self = expect.get('selfAb') or {}
    document = str(expect.get('document', ''))
    if wanted_self or document:
        out_dir = os.path.join(WORK_DIR, 'selfab')
        code, _out, _err = run_command(['replaydiff', bundle, bundle, '--out', out_dir], root)
        if code != 0:
            failed += 1
            notes.append('replaydiff %s against itself: exit %d' % (bundle, code))
        else:
            checked = True
            produced = os.path.join(root, out_dir, 'replaydiff.json')
            found = _summary_counts(produced)
            for key in sorted(wanted_self):
                if found.get(key) != wanted_self[key]:
                    failed += 1
                    notes.append('self-A/B %s: %s is %s, the label says %s'
                                 % (bundle, key, found.get(key), wanted_self[key]))
            if document:
                _kept, wrong = compare_document(root, capture, document, produced)
                failed += wrong
                if wrong:
                    notes.append('%s is not the document this run wrote (refresh it with `goldens --write`)'
                                 % document)
            if verbose:
                _print('  %-20s ok (same %s, constant blocks %s, shaders %s)'
                       % ('self-A/B', found.get('same'), found.get('constantsChanged'),
                          found.get('shadersDifferent')))
    return checked, failed, notes


def _write_document(root: str, capture: GoldenCapture,
                    expect: GoldenExpect) -> Tuple[bool, int, List[str]]:
    """Produce this capture's A/B document and keep it in the corpus (`goldens --write`)."""
    bundle = str(capture.get('bundle', ''))
    document = str(expect.get('document', ''))
    if not bundle or not document:
        return False, 0, []
    if not os.path.isdir(os.path.join(root, bundle)):
        return False, 0, ['%s: not here, so nothing was written' % bundle]
    out_dir = os.path.join(WORK_DIR, 'selfab')
    code, _out, _err = run_command(['replaydiff', bundle, bundle, '--out', out_dir], root)
    if code != 0:
        return False, 1, ['replaydiff %s against itself: exit %d' % (bundle, code)]
    compare_document(root, capture, document, os.path.join(root, out_dir, 'replaydiff.json'), write=True)
    return True, 0, []


def compare_document(root: str, capture: GoldenCapture, name: str, produced: str,
                     write: bool = False) -> Tuple[bool, int]:
    """Keep one produced document in the corpus, or compare it with the one already there.

    A document is an *output* rather than a label, so `--write` refreshes it the way it refreshes a
    transcript: byte for byte, because the document is deterministic by contract (`replaydiff` sorts, keeps
    frame order and timestamps nothing) and a comparison that parsed it first would forgive exactly the
    drift this exists for. Returns `(compared, failed)`; a document that is not in the corpus at all is a
    failure, because a label that names one and has none is not a check.
    """
    kept = os.path.join(root, GOLDENS_DIR, str(capture.get('name', '')), name)
    # The document's `capture` member is the path the tool was handed, and a document is committed: it is
    # redacted on the way in and on the way out, so the comparison is between two texts that carry the
    # capture's key rather than where this machine keeps it (see `redact`).
    path = str(capture.get('path', ''))
    if write:
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        with open(produced, encoding='utf-8') as source:
            text = redact(source.read(), path)
        with open(kept, 'w', encoding='utf-8', newline='\n') as target:
            target.write(text)
        _print('  %-20s written (%d KB)' % (name, len(text) // 1024))
        return True, 0
    if not os.path.isfile(kept):
        return False, 1
    with open(kept, encoding='utf-8') as handle:
        expected = redact(handle.read(), path)
    with open(produced, encoding='utf-8') as handle:
        actual = redact(handle.read(), path)
    return True, 0 if expected == actual else 1


def _report_counts(path: str) -> Dict[str, int]:
    """The report's own counts, read out of `report.json` (an absent file gives no counts, not zeros)."""
    try:
        document = rdc_schemas.load_document(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    frame = document.get('frame')
    flags = document.get('flags')
    detectors = document.get('detectors')
    passes = document.get('passes')
    if not isinstance(frame, dict) or not isinstance(flags, list) or not isinstance(detectors, list):
        return {}
    return {'events': int(frame.get('events', 0)),
            'passes': len(passes) if isinstance(passes, list) else 0,
            'resources': int(frame.get('resources', 0)),
            'findings': len(flags),
            'detectors': sum(1 for run in detectors if isinstance(run, dict) and run.get('ran'))}


def findings_by_detector(path: str) -> Dict[str, int]:
    """A `report.json`'s findings counted per detector: what *fired*, not only how much fired.

    The count alone cannot tell "the same 63 findings" from "21 fewer of one kind and 21 more of another",
    and a detector that stops firing is the failure this whole file exists to catch -- so the label is the
    histogram, and it is compared key by key (an absent detector is a mismatch of its own, not a zero).
    """
    try:
        document = rdc_schemas.load_document(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    flags = document.get('flags') if isinstance(document, dict) else None
    if not isinstance(flags, list):
        return {}
    counted: Dict[str, int] = {}
    for flag in flags:
        if isinstance(flag, dict):
            key = str(flag.get('detector', '?'))
            counted[key] = counted.get(key, 0) + 1
    return counted


def _summary_counts(path: str) -> Dict[str, int]:
    """A `replaydiff.json`'s summary, read out of the document."""
    try:
        document = rdc_schemas.load_document(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    summary = document.get('summary') if isinstance(document, dict) else None
    if not isinstance(summary, dict):
        return {}
    return {str(key): int(value) for key, value in summary.items()}


def check_pair(root: str, corpus: GoldenCorpus) -> Tuple[bool, int, List[str]]:
    """`replaydiff` the corpus's A/B pair, and compare its numbers with the pair's expectations.

    The one check here that is about *two* captures at once, and the reason the corpus has a pair: the
    mobile-vs-PC answer is the project's original question, so the numbers that answer it are worth pinning.
    Both bundles must be present; the expectations live in the pair's own member of the corpus, because they
    belong to the pair rather than to either capture.
    """
    pair = corpus.get('pair') or {}
    wanted = pair.get('expect') or {}
    left, right = str(pair.get('a', '')), str(pair.get('b', ''))
    if not left or not right or not wanted:
        return False, 0, []
    bundles = {str(entry.get('name', '')): str(entry.get('bundle', '')) for entry in corpus['captures']}
    missing = [name for name in (left, right)
               if not bundles.get(name) or not os.path.isdir(os.path.join(root, bundles[name]))]
    if missing:
        return False, 0, ['the pair %s/%s: %s not here (dump it)' % (left, right, ', '.join(missing))]
    out_dir = os.path.join(WORK_DIR, 'pair')
    # `--with-images`: the pair's labels include the image counts, so the bundles must have been written
    # with `dump --with-images` too -- stated as the corpus's precondition rather than assumed silently.
    code, _out, _err = run_command(['replaydiff', bundles[left], bundles[right], '--out', out_dir,
                                    '--with-images'], root)
    if code != 0:
        return False, 1, ['the pair %s/%s: replaydiff exit %d' % (left, right, code)]
    found = _summary_counts(os.path.join(root, out_dir, 'replaydiff.json'))
    notes = ['the pair %s/%s: %s is %s, the label says %s' % (left, right, key, found.get(key), value)
             for key, value in sorted(wanted.items()) if found.get(key) != value]
    return True, len(notes), notes


def cmd_goldens(argv: Sequence[str] = ()) -> int:
    """`goldens [--check|--write] [--capture <name>] [--corpus <file>] [--verbose]`.

    `--check` (the default) compares every transcript and every label it can reach; `--write` refreshes the
    transcripts -- review the diff before keeping it, that is the whole point. `--capture <name>` limits the
    run to one capture. A capture or a bundle that is not on this machine is reported as *not compared*, and
    the exit code says which of the three answers this run is: **0** everything compared matched, **1** a
    transcript, a label or a document differs, **2** nothing could be compared.

    The driver's own device-free check (`schema --check schema`) runs here too when `bin/replay_dump.exe`
    exists: it needs no capture and no GPU, so it belongs in the same pass. Everything else the driver does
    needs a GPU and stays a manual gate (REFERENCE §9).
    """
    repo = rdc_driver.repo_root()
    write = '--write' in argv
    verbose = '--verbose' in argv
    only = ''
    corpus_path = os.path.join(repo, GOLDENS_DIR, CORPUS_NAME)
    index = 0
    while index < len(argv):
        option = argv[index]
        if option in ('--check', '--write', '--verbose'):
            pass
        elif option == '--capture' and index + 1 < len(argv):
            index += 1
            only = argv[index]
        elif option == '--corpus' and index + 1 < len(argv):
            index += 1
            corpus_path = argv[index]
        else:
            _print('usage: rdc_analysis.py goldens [--check|--write] [--capture <name>] [--corpus <file>] '
                  '[--verbose]')
            return 2
        index += 1

    try:
        corpus = load_corpus(corpus_path)
    except rdc_schemas.SchemaError as exc:
        _print('error: %s' % exc)
        return 2

    # The corpus says where its captures are *relative to the root it sits under* (`<root>/goldens/…`),
    # so a corpus copied somewhere else -- a scratch directory in the tests, a checkout with its own
    # captures -- is read the same way. The driver and the checked-in schemas belong to the repository
    # rather than to the corpus, so those are found from the module.
    root = os.path.dirname(os.path.dirname(os.path.abspath(corpus_path)))
    _print('corpus  : %s (%d capture(s))' % (_relative(root, corpus_path), len(corpus['captures'])))
    schema_failed = 0
    if write:
        _print('schema  : not checked (a --write run only writes transcripts)')
    elif os.path.isfile(os.path.join(repo, rdc_driver.EXE_PATH)):
        code, out, err = run_driver(['schema', '--check', os.path.join(repo, 'schema')], root)
        verdict = [line for line in (out + err).splitlines() if 'schema file(s)' in line]
        if code != 0 or not verdict:
            schema_failed = 1
            _print('schema  : FAILED (exit %d, %s)' % (code, (out + err).strip().splitlines()[-1:]
                                                      or 'no output'))
        else:
            _print('schema  : %s' % verdict[0])
    else:
        _print('schema  : not checked (%s is not built: `build`)' % rdc_driver.EXE_PATH)
    if write:
        _print('labels  : not touched (a label file is hand-written, so `--write` never replaces one)')

    compared = mismatched = 0
    notes: List[str] = []
    for capture in corpus['captures']:
        name = str(capture.get('name', ''))
        if only and name != only:
            continue
        raw = str(capture.get('path', ''))
        path = os.path.join(root, raw) if raw else ''
        if not path or not os.path.isfile(path):
            # No path is not a failure: the committed corpus carries none, and the local file (this
            # machine's half, not in git) says which file each key is. This line prints the key and the
            # reason rather than a path, because this output is what CI keeps.
            reason = 'no path in %s' % LOCAL_NAME if not raw else 'the file it names is not here'
            _print('%-16s: not compared (%s)' % (name, reason))
            continue
        size = os.path.getsize(path)
        _print('%-16s: %.1f MB, %s' % (name, size / 1048576.0, capture.get('api', 'unknown API')))
        compared += 1
        wanted_bytes = int(capture.get('bytes', 0) or 0)
        if wanted_bytes and wanted_bytes != size:
            mismatched += 1
            notes.append('%s: the file is %d byte(s), the corpus says %d' % (name, size, wanted_bytes))
        wanted_sha = str(capture.get('sha256', ''))
        if wanted_sha:
            found_sha = sha256_file(path)
            if found_sha != wanted_sha:
                mismatched += 1
                notes.append('%s: sha256 %s, the corpus says %s (a re-capture under the same name?)'
                             % (name, found_sha[:16], wanted_sha[:16]))
        _count, wrong, problems = check_transcripts(root, capture, write, verbose)
        mismatched += wrong
        notes.extend('%s: %s' % (name, problem) for problem in problems)
        expect = load_expect(os.path.join(root, GOLDENS_DIR, name + EXPECT_SUFFIX))
        if write:
            # The A/B document is the other thing a `--write` refreshes (the labels stay hand-written).
            _kept, wrong, problems = check_bundle(root, capture, expect, verbose, write=True)
            mismatched += wrong
            notes.extend('%s: %s' % (name, problem) for problem in problems)
            continue
        checked, wrong, problems = check_labels(root, capture, expect)
        if compared and checked and verbose:
            _print('  %-20s ok (%d detector(s) checked)' % ('labels', checked))
        mismatched += wrong
        notes.extend('%s: %s' % (name, problem) for problem in problems)
        _bundle_checked, failed, problems = check_bundle(root, capture, expect, verbose)
        mismatched += failed
        notes.extend('%s: %s' % (name, problem) for problem in problems)

    pair_checked, failed, problems = (False, 0, []) if write else check_pair(root, corpus)
    mismatched += failed + schema_failed
    notes.extend(problems)
    if schema_failed:
        notes.append("the driver's schema table does not match the checked-in schema/ folder: "
                     'rebuild and run `replay_dump schema --out schema` (REFERENCE §4.12)')

    for note in notes:
        _print('  %s' % note)
    if write:
        _print('goldens : transcripts written for %d capture(s) -- review the diff before keeping it'
              % compared)
        return 0
    _print('goldens : %d capture(s) compared, %d mismatch(es), the pair is %s'
          % (compared, mismatched, 'compared' if pair_checked else 'not compared'))
    if not compared and not pair_checked:
        _print('          nothing to compare: no capture of this corpus is on this machine (exit 2)')
        _print('          %s, beside the corpus, says where this machine keeps them' % LOCAL_NAME)
        return 2
    return 1 if mismatched else 0


__all__ = [
    'CAPTURE_TOKEN',
    'CORPUS_NAME',
    'CORPUS_VERSION',
    'DIFF_LINES',
    'EXPECT_SUFFIX',
    'GOLDENS_DIR',
    'GoldenCapture',
    'GoldenCorpus',
    'GoldenExpect',
    'WORK_DIR',
    'check_bundle',
    'check_labels',
    'check_pair',
    'check_transcripts',
    'cmd_goldens',
    'command_line',
    'compare_document',
    'diff_lines',
    'differing_lines',
    'findings_by_detector',
    'load_corpus',
    'load_expect',
    'redact',
    'run_command',
    'run_driver',
    'sha256_file',
    'tool_path',
    'transcript',
    'transcript_name',
]
