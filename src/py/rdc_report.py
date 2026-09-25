"""The frame report: a bundle in, deterministic Markdown and JSON out (`report`).

Everything here reads files and returns text, which is why it is testable from fixture bundles
(`tests/test_rdc_report.py`) with no capture, no GPU and no driver. Split out of `rdc_analysis.py`
when that file passed 3000 lines; `rdc_analysis.py report` is still the entry point, and it
re-exports everything here.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional, Sequence, Tuple, Union

from rdc_bundle import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_goldens import KnownCause as KnownCause, known_for_capture as known_for_capture
from rdc_passes import *  # noqa: F401,F403  (re-exported for the CLI and tests)
import rdc_profile
from rdc_notable import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_engine_schema import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_detect_common import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_recommend import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_detect_bundle import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_detect_usage import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_detect_state import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_detect_stream import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_detect_binding import *  # noqa: F401,F403  (re-exported for the CLI and tests)
from rdc_report_render import *  # noqa: F401,F403  (re-exported for the CLI and tests)

# ---------------------------------------------------------------------------
# The frame report (`report`).
#
# REFERENCE §4.11, the frame report: a bundle written by `replay_dump dump` (REFERENCE §9) in, a
# deterministic Markdown report out. What is here is deliberately structural -- it says what the engine
# reported, every claim carries the event id it came from, and the report states in its own text what it
# cannot say. The detectors, the notable lists and the engine-name interpretation landed on top of it,
# and the report says which detector ran and which did not rather than guessing at either.
#
# Byte-stable for a fixed bundle: no timestamps, no absolute paths, every table sorted, and nothing
# iterated out of a set. That is what lets two runs be diffed against each other and the output be

def _known_matches(flag: RedFlag, entry: KnownCause) -> bool:
    """Whether one corpus cause is about one finding.

    Three parts, and the third is what makes a cause specific: the detector must be the same, the cause's
    `what` must be a substring of the finding's (so `the shader reads a constant block at b0 s0` is
    matched by `the shader reads a constant block at`), and the cause's `evidence` -- when it is not
    empty -- must appear in one of the finding's evidence lines, which is how one finding out of twenty
    of the same detector is picked out (a name, a register, an eid range).
    """
    if flag['detector'] != entry['detector'] or entry['what'] not in flag['what']:
        return False
    return not entry['evidence'] or any(entry['evidence'] in line for line in flag['evidence'])

def apply_known(flags: List[RedFlag], known: Sequence[Union[str, KnownCause]]) -> List[str]:
    """Mark the findings a known cause explains, and return the causes that explained nothing.

    A match turns `unproven` off and fills `cause` and `verdict`, and this is the **only** path by which
    that gate opens (REFERENCE §4.17): a detector is not trusted because it exists, but a finding whose cause
    has been followed to the frame -- or to the engine's answer -- and written down in the corpus is not
    an observation any more, it is a verdict. A note (a string in `known`) is not a cause and matches
    nothing.

    What comes back is the **stale** list: an entry that no longer matches any finding is a corpus that
    lies about the frame it describes (a detector renamed, a finding gone), and the caller prints it
    rather than leaving it to be found one day. The flags are updated in place; the caller's list is its
    own, built by `detect_all` for this run.
    """
    stale: List[str] = []
    for entry in known:
        if isinstance(entry, str):
            continue
        if not any(_known_matches(flag, entry) for flag in flags):
            stale.append('%s: %s' % (entry['detector'], entry['what']))
            continue
        for flag in flags:
            if _known_matches(flag, entry):
                flag['unproven'] = False
                flag['cause'] = entry['cause']
                flag['verdict'] = entry['verdict']
    return stale

@rdc_profile.timed('report: detectors')
def detect_all(bundle: BundleData, rdc_path: Optional[str] = None) -> Tuple[List[RedFlag], List[DetectorRun]]:
    """Every finding, and what each detector did -- listed, so "clean" cannot be confused with "unchecked"."""
    flags: List[RedFlag] = []
    runs: List[DetectorRun] = []

    runs.append({'detector': 'debug-message', 'ran': True, 'why': ''})
    flags.extend(detect_messages(bundle))
    runs.append({'detector': 'all-zero-constant-block', 'ran': True, 'why': ''})
    flags.extend(detect_zero_constant_blocks(bundle))
    runs.append({'detector': 'unbound-root-parameter', 'ran': True, 'why': ''})
    flags.extend(detect_unbound_root_parameters(bundle))

    # The two rules that read *resolved* table slots share a gate: a bundle from a driver that did not resolve
    # tables has nothing for them to look at, and "no table binds anything wrongly" and "I could not look" are
    # different answers -- the run list is where that difference lives. The root-descriptor half above needs no
    # table rows at all, which is why it is not gated with them.
    resolved = any(TABLE_SLOT_ROW.match(str(row))
                   for documents in bundle['states'].values()
                   for row in (documents.get('state') or {}).get('rootParameters', []))
    why = '' if resolved else 'no resolved descriptor tables in this bundle (written by an older driver)'
    for detector, function in (('unbound-table-slot', detect_unbound_table_slots),
                               ('binding-kind-mismatch', detect_binding_kind_mismatch)):
        runs.append({'detector': detector, 'ran': resolved, 'why': why})
        if resolved:
            flags.extend(function(bundle))
    runs.append({'detector': 'shader-io-mismatch', 'ran': True, 'why': ''})
    flags.extend(detect_shader_io_mismatch(bundle))

    # Four detectors read the usage lists, so they share one gate: a bundle written with --no-usage carries
    # `resourceUsage: (not collected...)` and each of them is *skipped with the reason* rather than reported
    # clean. The three chain rules stay separate detectors (and separate rules) rather than one
    # pass, because each has its own certainty and each can be checked on its own.
    collected = str(bundle['manifest'].get('resourceUsage', '')) == 'collected'
    reason = '' if collected else 'no usage lists in this bundle (written with --no-usage)'
    for detector, function in (('dead-allocation', detect_dead_allocations),
                               ('read-before-write', detect_read_before_write),
                               ('write-never-read', detect_write_never_read),
                               ('load-instead-of-clear', detect_load_instead_of_clear),
                               ('dead-compute', detect_dead_compute)):
        runs.append({'detector': detector, 'ran': collected, 'why': reason})
        if collected:
            flags.extend(function(bundle))

    # The pipeline-state rules need the blocks the driver started recording with this bundle format
    # (viewports/scissors/outputMerger, and the component type on signature rows). A bundle from an older
    # driver carries none of them, and each rule reports itself as *not looked at* with that reason rather
    # than as clean -- the same contract the table rules follow.
    state_reason = _pipeline_state_reason(bundle)
    for detector, function in (('depth-logic', detect_depth_logic),
                               ('empty-scissor', detect_empty_scissor),
                               ('stencil-without-writer', detect_stencil_without_writer),
                               ('blend-in-opaque-pass', detect_blend_in_opaque_pass),
                               ('format-units-suspicion', detect_format_units_suspicion)):
        runs.append({'detector': detector, 'ran': not state_reason, 'why': state_reason})
        if not state_reason:
            flags.extend(function(bundle))
    # MSAA is the one of the group that needs no pipeline state: `samples` is in the resource table and a
    # resolve is a usage row, which every bundle has.
    runs.append({'detector': 'mismatched-msaa', 'ran': True, 'why': ''})
    flags.extend(detect_mismatched_msaa(bundle))

    # The .rdc-side rules: they need the chunk stream, so they need the capture path, and they need the
    # RenderDoc source tree to name what they are looking at. Either being absent is a *skip* with the
    # reason, never a clean report, because "no marker is unbalanced" and "I could not tell markers apart"
    # are different answers and only one of them is worth anything.
    for detector, function in (('marker-imbalance', detect_marker_balance),
                               ('unattributed-draws', detect_unattributed_draws),
                               ('zero-work', detect_zero_work),
                               ('srgb-view-mismatch', detect_srgb_view_mismatch),
                               ('aliased-write', detect_aliased_writes)):
        if not rdc_path:
            runs.append({'detector': detector, 'ran': False, 'why': 'no capture path given'})
            continue
        try:
            found = function(rdc_path)
        except Exception as exc:
            # A capture that moved, or that is not a capture: these detectors are the only part of a report
            # that touches the file, and a report about a bundle must not die because the .rdc is elsewhere.
            # The reason goes in the run list, which is where a reader looks for what was not checked.
            runs.append({'detector': detector, 'ran': False,
                         'why': 'the capture could not be read: %s' % exc})
            continue
        if found is None:
            runs.append({'detector': detector, 'ran': False,
                         'why': 'no chunk-name map: the RenderDoc source tree was not found '
                                '(README §1.1)'})
            continue
        runs.append({'detector': detector, 'ran': True, 'why': ''})
        flags.extend(found)

    # The provenance verdicts: every `read-before-write` finding gains the chunk stream's own answer to
    # "what was this supposed to hold" -- the writer chain behind the flagged read (REFERENCE §4.15),
    # which is what tells a static asset from an ordering bug. It reads the same file the detectors
    # above did and fails the same way: silently, with the run list already carrying the reason.
    add_provenance_verdicts(rdc_path, flags)

    # The findings keep the order their detector gave them -- "biggest first" is information, and a final
    # sort by evidence would throw it away (each detector sorts its own findings, so this is deterministic).
    return flags, runs

def cmd_report(path: str, bundle_dir: str, out_dir: Optional[str] = None) -> int:
    """Write `report.md` and `report.json` for a bundle: `report <rdc> <bundleDir> [outDir]`.

    The report is written next to the bundle by default, because that is where the evidence it cites
    lives. stdout gets a short summary and the paths; the documents are the output.
    """
    try:
        bundle = load_bundle(bundle_dir)
    except BundleError as exc:
        print('error: %s' % exc)
        return 1

    recorded = str(bundle['manifest'].get('capture', ''))
    if recorded and os.path.basename(recorded) != os.path.basename(path):
        print('warning: the bundle was written for %s, but this capture is %s: the report describes '
              'the bundle' % (recorded, path))

    passes = reconstruct_passes(bundle['events'], bundle['resources'])
    for entry in passes:
        _state_rollup(bundle, entry)
    # The frame's time, where the bundle can say it: one counter summed over each pass's events, its share
    # of the frame's total and the dearest event in it. Zero everywhere in a bundle written without
    # `--with-counters`, which `costRows` 0 says rather than a table of costs that are not measurements.
    _cost_rollup(bundle, passes)

    # The engine's own vocabulary: the names the capture wrote, read against the tables in `engine-schemas/`.
    # It never guesses an engine and never invents a concept -- a bundle whose names match no table comes back
    # with an empty interpretation and the reason, which is a statement the report prints.
    engine = interpret_frame(bundle, passes)

    flags, detectors = detect_all(bundle, path)

    # The causes the corpus knows for *this* capture, matched by the corpus's own identity for it (its
    # SHA-256, `rdc_goldens.known_for_capture`): a finding whose cause has been followed to the frame and
    # written down stops being an observation, and `apply_known` is the only thing that turns that off.
    known = known_for_capture(path)
    stale = apply_known(flags, known)

    # Which of them is worth looking at first (REFERENCE §4.11): a ranking whose inputs and rules are printed with
    # its result, plus everything the rules list whatever its rank. The two lists are computed together because
    # they share the rule table and the roll-up notes.
    notable_lists = notables(bundle, passes)

    # And what to *do* about it (REFERENCE §4.11): one lead per detector that fired, per oddity rule that matched
    # and per gap the report could not close -- each with the command that shows its evidence. It reads the
    # findings and the notable lists rather than the frame, so nothing is measured twice.
    todo = recommendations(path, bundle, flags, detectors,
                           notable_lists['passes'], notable_lists['resources'])

    doc: ReportDocument = {
        'schemaVersion': REPORT_SCHEMA_VERSION,
        'reportVersion': REPORT_VERSION,
        'capture': recorded or path,
        'captureSha256': str(bundle['manifest'].get('captureSha256', '')),
        'bundleDir': bundle_dir,
        'bundle': bundle['manifest'],
        'frame': frame_facts(bundle),
        'passes': passes,
        'engine': engine,
        'notables': notable_lists,
        'recommendations': todo,
        'severityTable': severity_table(),
        'flags': flags,
        'detectors': detectors,
        'caveats': report_caveats(),
        'appendix': [],
    }

    target_dir = out_dir or bundle_dir
    try:
        if target_dir and not os.path.isdir(target_dir):
            os.makedirs(target_dir)
        markdown_path = os.path.join(target_dir, 'report.md')
        json_path = os.path.join(target_dir, 'report.json')
        # `newline=''` with an explicit '\n': the report is compared byte-for-byte between runs and
        # between platforms, so the line ending is the tool's decision, not the platform's.
        with open(markdown_path, 'w', encoding='utf-8', newline='') as fh:
            fh.write(render_report_markdown(doc, path))
        with open(json_path, 'w', encoding='utf-8', newline='') as fh:
            json.dump(doc, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write('\n')
    except OSError as exc:
        print('error: cannot write the report into %s: %s' % (target_dir, exc))
        return 1

    print('capture  : %s' % (recorded or path))
    print('bundle   : %s' % bundle_dir)
    print('events   : %d with bound state, %d pass(es), %d resource(s)'
          % (doc['frame']['events'], len(passes), doc['frame']['resources']))
    print('engine   : %s' % (
        '%s (%d concept(s) by name, %d question(s))' % (engine['engine'], len(engine['concepts']),
                                                        len(engine['questions']))
        if engine['engine'] else 'not interpreted: %s' % (
            engine['notInterpreted'][0] if engine['notInterpreted'] else 'no engine table matched')))
    print('written  : %s' % markdown_path)
    print('written  : %s' % json_path)
    print('notable  : %d pass(es), %d resource(s)' % (len(notable_lists['passes']),
                                                      len(notable_lists['resources'])))
    print('look at  : %d recommendation(s), ranked' % len(todo['rows']))
    proven = sum(1 for flag in flags if not flag['unproven'])
    print('flags    : %d finding(s) from %d detector(s), %d proven by a known cause, %d unproven'
          % (len(flags), sum(1 for run in detectors if run['ran']), proven, len(flags) - proven))
    if known:
        print('known    : %d entr(y/ies) for this capture in the corpus, %d of them a cause that matched'
              % (len(known), sum(1 for entry in known if not isinstance(entry, str))))
    for entry in stale:
        print('stale    : the corpus knows a cause for %s that no finding here matches any more' % entry)
    return 0

