"""The frame report: a bundle in, deterministic Markdown and JSON out (`report`).

Everything here reads files and returns text, which is why it is testable from fixture bundles
(`tests/test_rdc_report.py`) with no capture, no GPU and no driver. Split out of `rdc_analysis.py`
when that file passed 3000 lines; `rdc_analysis.py report` is still the entry point, and it
re-exports everything here.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, TypedDict, Union

# ---------------------------------------------------------------------------
# The frame report (`report`).
#
# ROADMAP section 1, skeleton: a bundle written by `replay_dump dump` (REFERENCE §9) in, a deterministic
# Markdown report out. What is here is deliberately structural -- it says what the engine reported,
# every claim carries the event id it came from, and the report states in its own text what it cannot
# say. The detectors, the notable lists and the engine-name interpretation are the later items in that
# section, and the report names them as absent rather than guessing at them.
#
# Byte-stable for a fixed bundle: no timestamps, no absolute paths, every table sorted, and nothing
# iterated out of a set. That is what lets two runs be diffed against each other and the output be
# pinned by a test (tests/test_rdc_report.py).
BUNDLE_VERSION = 1
REPORT_VERSION = 1


class BundleError(Exception):
    """A directory that cannot be read as a bundle: a missing file, a wrong version, invalid JSON."""


class BundleEvent(TypedDict):
    eid: int
    pso: str
    psoKind: str
    shaders: str
    targets: List[str]
    depth: str
    rootParameters: int
    state: str


class BundleUsage(TypedDict):
    eid: int
    usage: int


class BundleResource(TypedDict, total=False):
    resource: str
    name: str
    kind: str
    format: str
    dimension: int
    width: int
    height: int
    depth: int
    mips: int
    arraySize: int
    samples: int
    bytes: int
    usage: List[BundleUsage]
    usageCount: int
    firstEvent: int
    lastEvent: int


class BundleMessage(TypedDict, total=False):
    eid: int
    severity: int
    severityText: str
    text: str


class BundleData(TypedDict):
    manifest: Dict[str, Any]
    capture: Dict[str, Any]
    events: List[BundleEvent]
    resources: List[BundleResource]
    #: The driver writes messages as *rows* (`eid 12  error  <text>`), which is what the schema describes; an
    #: object with the same facts is accepted too, because a consumer should not have to know the spelling.
    messages: List[Union[BundleMessage, str]]
    states: Dict[str, Dict[str, Any]]
    cbuffers: Dict[str, Dict[str, Any]]


class ReportPass(TypedDict):
    index: int
    firstEid: int
    lastEid: int
    kind: str
    reason: str
    events: int
    graphics: int
    compute: int
    targets: List[str]
    depth: str
    structure: str
    shaders: List[str]
    otherShaders: List[str]
    blocks: List[str]
    firstTouched: List[str]


#: One red flag. `what` is the *observation*; what it means is the reader's, because a bundle can prove what
#: the engine held, not what the frame intended. `unproven` is the ROADMAP §1.5 gate: a detector that has
#: never been checked against a capture whose bugs are known has not earned a verdict.
class RedFlag(TypedDict):
    detector: str
    what: str
    evidence: List[str]
    certainty: str
    unproven: bool


#: What a detector did. `why` is empty for one that ran and the reason for one that could not -- a report
#: that lists only findings cannot tell "clean" from "not looked at", and that difference matters.
class DetectorRun(TypedDict):
    detector: str
    ran: bool
    why: str


class ReportDocument(TypedDict):
    reportVersion: int
    capture: str
    captureSha256: str
    bundleDir: str
    bundle: Dict[str, Any]
    frame: Dict[str, Any]
    passes: List[ReportPass]
    flags: List[RedFlag]
    detectors: List[DetectorRun]
    caveats: List[str]
    appendix: List[str]


def _res_id(text: str) -> str:
    """The id inside a resource reference as the driver prints it.

    The engine's own documents are not uniform about this: `events.json` writes bare numbers
    (`"2207"`), while `state`/`shaders` write `res2207`. Both are read here, so the report parses what
    the driver actually writes rather than what one of its commands happens to look like.
    """
    token = text.strip()
    return token[3:] if token.startswith('res') else token


def _is_resource(idtext: str) -> bool:
    """Whether an id names something: `0` is the engine's null resource, not a resource called zero."""
    return _res_id(idtext) not in ('', '0')


def _targets_text(entry: ReportPass, short: bool = False) -> str:
    """The targets a pass writes, or why that question does not apply to it.

    At a compute event the engine still reports the output-merge state, but a dispatch did not set it:
    printing it as this pass's targets would be a claim about the frame that the event does not
    support, so it says so instead. (The reported values stay in the JSON document.)
    """
    if entry['kind'] == 'compute':
        # The map column is narrow and the reason is the same for every dispatch: `n/a` there, the
        # sentence in the pass section and in the caveats.
        return 'n/a' if short else 'n/a — a dispatch does not set the output merger'
    if short:
        return ', '.join('res%s' % _res_id(t.split()[0]) for t in entry['targets']) or 'none'
    return ', '.join(entry['targets']) or 'none'


def _md(text: str) -> str:
    """One line of Markdown-safe text: a table cell or a heading fragment cannot contain a pipe or a
    newline, and a resource name comes from the application."""
    return ' '.join(str(text).split()).replace('|', '\\|')


def _bundle_file(bundle_dir: str, name: str, required: bool = True) -> Any:
    path = os.path.join(bundle_dir, name)
    try:
        with open(path, encoding='utf-8-sig') as fh:
            return json.load(fh)
    except FileNotFoundError:
        if required:
            raise BundleError(
                '%s is missing: write the bundle with `replay_dump dump <rdc> %s`' % (name, bundle_dir))
        return None
    except json.JSONDecodeError as exc:
        raise BundleError('%s is not valid JSON (%s): the bundle is damaged, write it again'
                          % (name, exc)) from exc


def load_bundle(bundle_dir: str) -> BundleData:
    """Read a bundle: the files `replay_dump dump` writes (REFERENCE §9).

    `manifest.json`, `capture.json`, `events.json` and `resources.json` are required, because a report
    without them would have to invent something; `messages.json` and the per-event `states/` documents
    are optional and are reported as absent. A bundle written by a newer driver is refused by version
    rather than half-read.
    """
    if not os.path.isdir(bundle_dir):
        raise BundleError('no such bundle directory: %s' % bundle_dir)

    manifest: Dict[str, Any] = _bundle_file(bundle_dir, 'manifest.json')
    version = manifest.get('bundleVersion')
    if version != BUNDLE_VERSION:
        raise BundleError('bundle version %s, this tool reads %d (write the bundle again with the '
                          'current replay_dump)' % (version, BUNDLE_VERSION))

    capture: Dict[str, Any] = _bundle_file(bundle_dir, 'capture.json')
    events: List[BundleEvent] = _bundle_file(bundle_dir, 'events.json').get('events', [])
    resources: List[BundleResource] = _bundle_file(bundle_dir, 'resources.json').get('resources', [])
    messages: List[BundleMessage] = (_bundle_file(bundle_dir, 'messages.json', False) or {}).get(
        'messages', [])

    # The per-event documents that exist, keyed by eid: the pass roll-up names the shaders and the
    # constant blocks of the pass's first event, which is exactly the event the bundle writes a state
    # file for (its state hash includes the render targets, so a pass boundary is always a change).
    states: Dict[str, Dict[str, Any]] = {}
    states_dir = os.path.join(bundle_dir, 'states')
    if os.path.isdir(states_dir):
        for event in events:
            eid = int(event['eid'])
            state = _bundle_file(bundle_dir, os.path.join('states', '%d.state.json' % eid), False)
            shaders = _bundle_file(bundle_dir, os.path.join('states', '%d.shaders.json' % eid), False)
            states[str(eid)] = {'state': state, 'shaders': shaders}

    # The constant-block dumps, keyed by their file name. They are core, not optional: a bundle without
    # `--with-images` still has them, and the all-zero detector is the one that needs them.
    cbuffers: Dict[str, Dict[str, Any]] = {}
    cbuffer_dir = os.path.join(bundle_dir, 'cbuffers')
    if os.path.isdir(cbuffer_dir):
        for name in sorted(os.listdir(cbuffer_dir)):
            if name.endswith('.json'):
                document = _bundle_file(bundle_dir, os.path.join('cbuffers', name), False)
                if isinstance(document, dict):
                    cbuffers[name] = document

    return {'manifest': manifest, 'capture': capture, 'events': events, 'resources': resources,
            'messages': messages, 'states': states, 'cbuffers': cbuffers}


def reconstruct_passes(events: Sequence[BundleEvent],
                       resources: Sequence[BundleResource]) -> List[ReportPass]:
    """Group the bundle's events into passes, and say why each one starts.

    The engine exposes no action list, so a bundle has no marker names and no call kinds: a pass here
    is a run of consecutive events that agree on the call kind, the render targets and the depth
    target, and the *reason* is recorded per pass so a reader can disagree with the rule rather than
    the result. (Deriving real passes needs the action list -- ROADMAP §2.)
    """
    passes: List[ReportPass] = []
    last_kind = ''
    last_targets: List[str] = []
    last_depth = ''
    last_dispatch = ''
    for event in events:
        kind = str(event.get('psoKind', 'graphics'))
        targets = [str(t) for t in event.get('targets', [])]
        depth = str(event.get('depth', '0'))
        # A dispatch does not set the output-merge state, so the targets and depth the engine reports
        # at a compute event are whatever was bound before it -- grouping on those invented passes out
        # of stale state (measured: a compute-only capture looked like five passes that way). A
        # dispatch is grouped by what it *does* set: its pipeline and its shaders, so a run of
        # identical dispatches is one pass.
        dispatch = '%s %s' % (event.get('pso', ''), _md(str(event.get('shaders', ''))))

        new_pass = False
        reason = ''
        if not passes:
            new_pass, reason = True, 'the first event with bound state in the bundle'
        elif last_kind != kind:
            new_pass, reason = True, 'the call kind changed: %s -> %s' % (last_kind, kind)
        elif kind == 'compute':
            if dispatch != last_dispatch:
                new_pass = True
                reason = 'the dispatch changed (a different pipeline or shaders)'
        elif targets != last_targets:
            new_pass = True
            reason = 'the render targets changed: %s -> %s' % (
                ', '.join(last_targets) or 'none', ', '.join(targets) or 'none')
        elif depth != last_depth:
            new_pass = True
            reason = 'the depth target changed: %s -> %s' % (last_depth, depth)

        if new_pass:
            passes.append({'index': len(passes) + 1, 'firstEid': int(event['eid']),
                           'lastEid': int(event['eid']), 'kind': kind, 'reason': reason,
                           'events': 0, 'graphics': 0, 'compute': 0, 'targets': targets,
                           'depth': depth, 'structure': _pass_structure(kind, targets, depth),
                           'shaders': [], 'otherShaders': [], 'blocks': [], 'firstTouched': []})

        current = passes[-1]
        current['lastEid'] = int(event['eid'])
        current['events'] += 1
        current['graphics' if kind != 'compute' else 'compute'] += 1
        last_kind, last_targets, last_depth, last_dispatch = kind, targets, depth, dispatch

    for entry in passes:
        first, last = entry['firstEid'], entry['lastEid']
        entry['firstTouched'] = _describe(
            entry, [r for r in resources if first <= int(r.get('firstEvent', 0) or 0) <= last])
    return passes


def _pass_structure(kind: str, targets: Sequence[str], depth: str) -> str:
    """What the pass *is*, from state alone: kind, targets, depth. Not what it is *for* -- naming that
    (shadow map, G-buffer, UI) needs the engine schema table, ROADMAP §1.4."""
    if kind == 'compute':
        return 'compute'
    has_depth = _is_resource(depth)
    if not targets and has_depth:
        return 'depth only (no colour target)'
    if targets and not has_depth:
        return 'colour only (no depth target)'
    if len(targets) >= 2:
        return 'multi-target'
    if targets:
        return 'single colour target'
    return 'no targets bound'


def _describe(pass_entry: ReportPass, resources: Sequence[BundleResource]) -> List[str]:
    """One line per resource, sorted by id: the pass roll-up's "what appears here"."""
    out: List[str] = []
    for resource in sorted(resources, key=lambda r: int(r.get('resource', '0') or 0)):
        idtext = 'res%s' % _res_id(str(resource.get('resource', '')))
        # A resource name comes from the application, so it can hold anything: whitespace (including
        # newlines) is collapsed to keep one resource on one line. A `|` is *left alone* here, because
        # this is a list item and not a table cell -- escaping it would render as `\|` to a reader for
        # no reason.
        name = ' '.join(str(resource.get('name', '')).split())
        named = ' "%s"' % name if name else ''
        kind = str(resource.get('kind', 'other'))
        if kind == 'texture':
            detail = '%dx%dx%d %s' % (int(resource.get('width', 0)), int(resource.get('height', 0)),
                                      int(resource.get('depth', 0)), str(resource.get('format', '?')))
        elif kind == 'buffer':
            detail = '%.2f MB' % (int(resource.get('bytes', 0)) / 1048576.0)
        else:
            detail = kind
        out.append('%s%s (%s, %s)' % (idtext, named, kind, detail))
    return out


#: The stages each kind of pass is made of: a dispatch uses the compute-ish ones, a draw the graphics
#: ones. Anything else the engine reports at that event is a leftover binding from an earlier call, and
#: saying so is the difference between "this pass uses these shaders" and "these were also bound".
COMPUTE_STAGES = ('cs', 'as', 'ms')


def _state_rollup(bundle: BundleData, entry: ReportPass) -> None:
    """Fill a pass's shaders and constant blocks from the state documents of its first event.

    The state document lists *every* bound stage, which at a dispatch includes the vertex and pixel
    shaders left over from an earlier draw. Those are recorded separately (`otherShaders`) rather than
    as the pass's shaders, because a reader would otherwise take them for part of the pass.
    """
    documents = bundle['states'].get(str(entry['firstEid']), {})
    state = documents.get('state')
    shaders = documents.get('shaders')

    if isinstance(state, dict):
        for row in state.get('shaders', []):
            parts = str(row).split()
            if len(parts) < 2:
                continue
            label = '%s %s' % (parts[0], _res_id(parts[1]))
            used = (parts[0] in COMPUTE_STAGES) == (entry['kind'] == 'compute')
            entry['shaders' if used else 'otherShaders'].append(label)
    if isinstance(shaders, dict):
        for stage in shaders.get('stages', []):
            label = '%s %s' % (str(stage.get('stage', '?')), _res_id(str(stage.get('resource', ''))))
            for block in stage.get('constantBlocks', []):
                entry['blocks'].append('%s: %s' % (label, _md(str(block))))


def _row_severity(text: str) -> str:
    """The severity out of a `messages.json` row: `eid %-6u %-8s %s` is the driver's format."""
    parts = text.split()
    return parts[2] if len(parts) >= 3 and parts[0] == 'eid' else 'unparsed'


def frame_facts(bundle: BundleData) -> Dict[str, Any]:
    #: `stateDocuments` is the count of per-event state *pairs* the bundle carries, which is a fact
    #: about the bundle rather than about the frame (the driver writes them per state change).
    """The frame at a glance: everything counted from the bundle, nothing inferred."""
    events = bundle['events']
    resources = bundle['resources']
    by_kind: Dict[str, int] = {'texture': 0, 'buffer': 0, 'other': 0}
    bytes_by_kind: Dict[str, int] = {'texture': 0, 'buffer': 0}
    for resource in resources:
        kind = str(resource.get('kind', 'other'))
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if kind in bytes_by_kind:
            bytes_by_kind[kind] += int(resource.get('bytes', 0))

    targets: List[str] = []
    formats: List[str] = []
    for event in events:
        for target in event.get('targets', []):
            parts = str(target).split()
            idtext = 'res%s' % _res_id(parts[0])
            if idtext not in targets:
                targets.append(idtext)
            if len(parts) >= 3 and parts[2] not in formats:
                formats.append(parts[2])

    severities: Dict[str, int] = {}
    for message in bundle['messages']:
        # The driver writes messages as rows (`eid 12  error  <text>`) -- the same text the terminal prints
        # -- so a reader that expected an object per message would fall over on the first capture that has
        # one. Both forms are read: the row form is what the schema describes, the object form what an
        # older or external writer might produce.
        if isinstance(message, dict):
            label = str(message.get('severityText', message.get('severity', '?')))
        else:
            label = _row_severity(str(message))
        severities[label] = severities.get(label, 0) + 1

    capture = bundle['capture']
    return {
        'events': len(events),
        'graphicsEvents': sum(1 for e in events if str(e.get('psoKind', 'graphics')) != 'compute'),
        'computeEvents': sum(1 for e in events if str(e.get('psoKind', 'graphics')) == 'compute'),
        'resources': len(resources),
        'resourcesByKind': {k: by_kind[k] for k in sorted(by_kind)},
        'textureBytes': bytes_by_kind['texture'],
        'bufferBytes': bytes_by_kind['buffer'],
        'targetsSeen': sorted(targets),
        'formatsSeen': sorted(formats),
        'messages': len(bundle['messages']),
        'messagesBySeverity': {k: severities[k] for k in sorted(severities)},
        'chunks': int(capture.get('chunks', 0) or 0),
        'stateDocuments': len(bundle['states']),
        'pipelineType': int(capture.get('pipelineType', 0) or 0),
        'localRenderer': int(capture.get('localRenderer', 0) or 0),
        'vendor': int(capture.get('vendor', 0) or 0),
    }


# ---------------------------------------------------------------------------
# Detectors (ROADMAP §1.1): the red flags.
#
# A detector is a pure function over the bundle that returns findings -- no printing, no presentation order,
# no state. What it may say is bounded by what a bundle *proves*, and that is why there are three of them:
# the rest of §1.1's list needs call arguments (zero work), the descriptor writes (unbound descriptors,
# read-before-write), the action list (marker imbalance, unattributed draws) or the pipeline state (depth
# logic, scissor, MSAA), and a bundle holds none of those. A detector that would have to guess is not written:
# it would produce exactly the kind of confident wrong answer this tool exists to avoid.
def detect_messages(bundle: BundleData) -> List[RedFlag]:
    """The API's own complaints (`debug`), grouped by severity and text (ROADMAP §1.1, certain).

    Rows are what the driver writes (`eid <n>  <severity>  <text>`), and the grouping key is the text with
    the eid taken out -- the same complaint at forty events is one finding with an eid range, not forty.
    """
    groups: Dict[Tuple[str, str], List[int]] = {}
    for message in bundle['messages']:
        if isinstance(message, dict):
            severity = str(message.get('severityText', message.get('severity', '?')))
            text = str(message.get('text', ''))
            eid = int(message.get('eid', 0) or 0)
        else:
            parts = str(message).split(None, 3)
            severity = parts[2] if len(parts) >= 3 else '?'
            text = parts[3] if len(parts) >= 4 else str(message)
            eid = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        groups.setdefault((severity, text), []).append(eid)

    flags: List[RedFlag] = []
    for key in sorted(groups):
        eids = sorted(groups[key])
        flags.append({
            'detector': 'debug-message',
            'what': '%s: %s' % (key[0], _md(key[1])),
            'evidence': ['%d message(s), eid %d..%d' % (len(eids), eids[0], eids[-1])],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags


def detect_zero_constant_blocks(bundle: BundleData) -> List[RedFlag]:
    """A constant block whose every numeric value is zero (ROADMAP §1.1, certain).

    The *fact* is certain: the block the engine read holds zeros. What it means is not -- a feature switched
    off looks exactly the same as a buffer that was never filled -- so the wording is the observation and the
    meaning is left open. Occurrences of one block are grouped by (stage, slot, buffer) and the evidence says
    how many of the events it was dumped at were all-zero, because "zero at 120" and "zero everywhere" are
    different findings. Rows with no number in them (a struct's opening brace, a string) are not evidence
    either way.
    """
    number = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
    blocks: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for name in sorted(bundle['cbuffers']):
        document = bundle['cbuffers'][name]
        values: List[float] = []
        for row in document.get('variables', []):
            text = str(row)
            if '=' in text:                          # the value side only: a name can hold a digit
                values.extend(float(token) for token in number.findall(text.split('=', 1)[1]))
        key = (str(document.get('stage', '?')), str(document.get('slot', '?')),
               str(document.get('buffer', '?')))
        entry = blocks.setdefault(key, {'zero': [], 'all': [], 'values': 0})
        entry['all'].append(int(document.get('eid', 0) or 0))
        if values and all(value == 0.0 for value in values):
            entry['zero'].append(int(document.get('eid', 0) or 0))
            entry['values'] = max(entry['values'], len(values))

    flags: List[RedFlag] = []
    for key in sorted(blocks, key=lambda k: (k[0], str(k[1]), k[2])):
        entry = blocks[key]
        if not entry['zero']:
            continue
        zero, every = sorted(entry['zero']), sorted(entry['all'])
        # The driver says so itself when nothing is bound (`buffer` is then a note, not a resource), and that
        # is a different and stronger finding than "a buffer happens to read as zeros": the shader reads a
        # block that no descriptor reaches. Measured on the Android capture, where most hits are this one.
        unbound = 'none bound' in key[2]
        what = ('no root descriptor is bound for this block, so its %d value(s) read as zero -- a shader '
                'reading a block nothing reaches' % entry['values'] if unbound else
                'every value in this block is zero: %d value(s), zero at %d of the %d event(s) it was dumped '
                'at -- never filled in reads the same way as switched off'
                % (entry['values'], len(zero), len(every)))
        flags.append({
            'detector': 'all-zero-constant-block',
            'what': what,
            'evidence': ['%s stage, slot %s, %s%s' % (
                key[0], key[1],
                'no root descriptor bound' if unbound else 'buffer res%s' % _res_id(key[2]),
                ', eid %d..%d' % (zero[0], zero[-1]) if len(zero) > 1 else ', eid %d' % zero[0])],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags


#: How many unused resources a report names before it stops and counts the rest. The point of the finding is
#: usually the bytes, so the biggest come first; a capture with two hundred dead allocations should not bury
#: every other finding in the report.
DEAD_ALLOCATION_LIMIT = 20


def detect_dead_allocations(bundle: BundleData) -> List[RedFlag]:
    """A texture or buffer no call in the frame uses (ROADMAP §1.1, certain).

    "Uses" is the engine's own usage list: a resource whose every record is usage 0 was created and never
    reached a call. Resources of kind `other` -- fences, queues, the descriptor heaps -- are not allocations
    the application made, and flagging them would bury the real ones. Needs the usage lists, so a bundle
    written with `--no-usage` skips this detector rather than reporting it clean.
    """
    dead: List[BundleResource] = []
    for resource in bundle['resources']:
        if str(resource.get('kind')) not in ('texture', 'buffer'):
            continue
        if any(int(record.get('usage', 0) or 0) != 0 for record in resource.get('usage', [])):
            continue
        dead.append(resource)

    flags: List[RedFlag] = []
    for resource in sorted(dead, key=lambda r: (-int(r.get('bytes', 0) or 0),
                                                int(r.get('resource', '0') or 0)))[:DEAD_ALLOCATION_LIMIT]:
        size = int(resource.get('bytes', 0) or 0)
        name = ' '.join(str(resource.get('name', '')).split())
        if str(resource.get('kind')) == 'texture':
            detail = '%dx%dx%d %s' % (int(resource.get('width', 0) or 0), int(resource.get('height', 0) or 0),
                                      int(resource.get('depth', 0) or 0), str(resource.get('format', '?')))
        else:
            detail = 'buffer'
        flags.append({
            'detector': 'dead-allocation',
            'what': 'created and never used by any call: %.2f MB' % (size / 1048576.0),
            'evidence': ['res%s%s (%s, %s)' % (_res_id(str(resource.get('resource', ''))),
                                               ' "%s"' % name if name else '', resource.get('kind'), detail)],
            'certainty': 'certain',
            'unproven': True,
        })
    if len(dead) > DEAD_ALLOCATION_LIMIT:
        flags.append({
            'detector': 'dead-allocation',
            'what': '%d smaller unused resource(s) are not listed' % (len(dead) - DEAD_ALLOCATION_LIMIT),
            'evidence': ['%d unused in total' % len(dead)],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags


# ---------------------------------------------------------------------------
# The usage chain (ROADMAP §1.1, route B): read-before-write, write-never-read, load-instead-of-clear.
#
# `GetUsage` answers, per resource, the events it was used at and *how*. The decode below is measured rather
# than inferred, and two of its facts are the kind that would be wrong if assumed: the value is *one usage,
# not a bitmask* (the API's own example says one entry per usage, and the real bundle shows one buffer with
# eight `VertexBuffer` rows at one eid -- one per binding slot), and the numbering is the enum's declaration
# order in `renderdoc-src/renderdoc/api/replay/replay_enums.h`. All 17 distinct values in the real bundle
# land where their semantics say they must: 32/33 on the two target kinds `events.json` also names (the depth
# texture's own chain proves it), 35/36 on the Clear/Discard rows before them, 42/43 on the copy halves.
#
# A resource whose only row is `(eid 0, Unused)` was not tracked by the engine -- the API documents exactly
# that marker -- and is never judged. What the chain cannot show is named in the findings themselves: the
# list stops at the capture, so a read by the next frame, or by the CPU after it, is indistinguishable from
# nothing ever reading the resource.
USAGE_NAMES: Tuple[str, ...] = (
    'Unused', 'VertexBuffer', 'IndexBuffer',
    'VS_Constants', 'HS_Constants', 'DS_Constants', 'GS_Constants', 'PS_Constants', 'CS_Constants',
    'TS_Constants', 'MS_Constants', 'All_Constants',
    'StreamOut',
    'VS_Resource', 'HS_Resource', 'DS_Resource', 'GS_Resource', 'PS_Resource', 'CS_Resource',
    'TS_Resource', 'MS_Resource', 'All_Resource',
    'VS_RWResource', 'HS_RWResource', 'DS_RWResource', 'GS_RWResource', 'PS_RWResource', 'CS_RWResource',
    'TS_RWResource', 'MS_RWResource', 'All_RWResource',
    'InputTarget', 'ColorTarget', 'DepthStencilTarget',
    'Indirect',
    'Clear', 'Discard', 'GenMips', 'Resolve', 'ResolveSrc', 'ResolveDst', 'Copy', 'CopySrc', 'CopyDst',
    'Barrier', 'CPUWrite')

_USAGE_STAGES = ('VS', 'HS', 'DS', 'GS', 'PS', 'CS', 'TS', 'MS')

#: Which usages *read* the resource's contents and which *write* them. `Resolve` and `Copy` are the
#: source-and-destination forms and are in both; `Barrier` and `Unused` are in neither. The RW family is in
#: both on purpose: a UAV may be read and written by the same dispatch and the row does not say which
#: happened, so a rule that needs "definitely nothing writes here" must not see a read in an RW row.
USAGE_READS = frozenset(
    {'VertexBuffer', 'IndexBuffer', 'InputTarget', 'Indirect', 'ResolveSrc', 'CopySrc', 'Resolve', 'Copy',
     'All_Constants', 'All_Resource'}
    | {'%s_Constants' % stage for stage in _USAGE_STAGES}
    | {'%s_Resource' % stage for stage in _USAGE_STAGES})
USAGE_WRITES = frozenset(
    {'ColorTarget', 'DepthStencilTarget', 'Clear', 'Discard', 'GenMips', 'StreamOut', 'ResolveDst',
     'CopyDst', 'CPUWrite', 'Resolve', 'Copy', 'All_RWResource'}
    | {'%s_RWResource' % stage for stage in _USAGE_STAGES})

#: The usages that make a resource a render target (the load-instead-of-clear rule keys off these).
USAGE_TARGETS = frozenset({'ColorTarget', 'DepthStencilTarget'})

#: How many resources a usage finding names before it counts the rest, for the same reason as
#: DEAD_ALLOCATION_LIMIT: the finding is the group, not a wall of resource ids.
USAGE_LIST_LIMIT = 8


def usage_name(value: int) -> str:
    """The engine's name for a usage value, or `usage(N)` -- the same fallback the driver prints, for a value
    this table does not know (a newer engine's usage, and naming it would be a guess)."""
    return USAGE_NAMES[value] if 0 <= value < len(USAGE_NAMES) else 'usage(%d)' % value


def _usage_chain(resource: BundleResource) -> List[Tuple[int, FrozenSet[str]]]:
    """A resource's usage as `(eid, {names})` per event, ascending.

    Rows are one usage each, so an eid can carry several and the chain is a *set* per event: deduplicating
    here is what keeps "the same buffer bound to eight slots" from reading as eight different uses. `[]`
    means the list is empty (a resource no call touched) and `[(0, {'Unused'})]` means the engine did not
    track it -- two different facts, and every caller below checks both.
    """
    per_event: Dict[int, set] = {}
    for record in resource.get('usage', []):
        # A bundle written with `--no-usage` puts a *string* here (`"(not collected: --no-usage)"`), which is
        # why the callers are gated on the manifest and this skips it rather than trusting the shape.
        if not isinstance(record, dict):
            continue
        per_event.setdefault(int(record.get('eid', 0) or 0), set()).add(
            usage_name(int(record.get('usage', 0) or 0)))
    return [(eid, frozenset(names)) for eid, names in sorted(per_event.items())]


def _usage_judged(resource: BundleResource) -> Optional[List[Tuple[int, FrozenSet[str]]]]:
    """The chain of a resource this family may judge, or None: textures and buffers only (a heap or a queue
    is not an allocation the application reads and writes), and never a resource the engine did not track."""
    if str(resource.get('kind')) not in ('texture', 'buffer'):
        return None
    chain = _usage_chain(resource)
    if not chain or (len(chain) == 1 and chain[0][1] == frozenset(['Unused'])):
        return None
    return chain


def _resource_label(resource: BundleResource) -> str:
    """`res2207 "BufferedRT" (texture, 3104x3296x1 B10G11R11_UFloatPack32)`: enough to find it in the capture."""
    name = ' '.join(str(resource.get('name', '')).split())
    if str(resource.get('kind')) == 'texture':
        detail = '%dx%dx%d %s' % (int(resource.get('width', 0) or 0), int(resource.get('height', 0) or 0),
                                  int(resource.get('depth', 0) or 0), str(resource.get('format', '?')))
    else:
        detail = '%.2f MB' % (int(resource.get('bytes', 0) or 0) / 1048576.0)
    return 'res%s%s (%s, %s)' % (_res_id(str(resource.get('resource', ''))),
                                 ' "%s"' % name if name else '', str(resource.get('kind')), detail)


def _usage_flag(detector: str, what: str, lines: List[str]) -> RedFlag:
    """One finding from a group of resource lines, capped and rolled up like the dead-allocation list."""
    return {
        'detector': detector,
        'what': what,
        'evidence': lines[:USAGE_LIST_LIMIT] + (
            ['%d more, not listed' % (len(lines) - USAGE_LIST_LIMIT)] if len(lines) > USAGE_LIST_LIMIT else []),
        'certainty': 'question',
        'unproven': True,
    }


def detect_read_before_write(bundle: BundleData) -> List[RedFlag]:
    """A resource read with nothing in the frame writing it first (ROADMAP §1.1, route B; `question`).

    "First" is a comparison of eids, and a write at the *same* eid does not count: one call can write and
    read (the RW family is in both sets for exactly that reason), so only a strictly earlier write clears a
    read. The finding is a question because the legitimate shapes are the common ones -- a static resource
    (written by a previous frame, or by a call outside the capture), a CPU upload, a producer before the
    capture's first event -- and the chain cannot tell them from a genuine ordering bug. Grouped by first-read
    usage, because that is what the group is: "five textures are read by a pixel shader and never written" is
    one thing to look at, not five. Measured on the Android capture: 6 resources, every one read and *never*
    written in the frame -- two asset textures, an LUT, the font atlas, a sky cube and one allocator buffer,
    which is what the legitimate case looks like.
    """
    groups: Dict[Tuple[str, str], List[str]] = {}
    for resource in sorted(bundle['resources'], key=lambda r: int(r.get('resource', '0') or 0)):
        chain = _usage_judged(resource)
        if chain is None:
            continue
        first_read = next(((eid, names) for eid, names in chain if names & USAGE_READS), None)
        if first_read is None or any(names & USAGE_WRITES for eid, names in chain if eid <= first_read[0]):
            continue
        first_write = next((eid for eid, names in chain if names & USAGE_WRITES), 0)
        groups.setdefault((str(resource.get('kind')), ', '.join(sorted(first_read[1] & USAGE_READS))),
                          []).append('%s: read at eid %d, first write %s'
                                     % (_resource_label(resource), first_read[0],
                                        'eid %d' % first_write if first_write else 'never in the frame'))

    flags: List[RedFlag] = []
    for key in sorted(groups):
        flags.append(_usage_flag(
            'read-before-write',
            '%d %s(s) whose first use is a read (%s) that nothing in the frame writes first: a question, not '
            'a verdict -- a static resource, a call outside the capture and a producer before its first event '
            'all read the same way' % (len(groups[key]), key[0], key[1]),
            groups[key]))
    return flags


def detect_write_never_read(bundle: BundleData) -> List[RedFlag]:
    """A resource written with nothing afterwards reading it (ROADMAP §1.1, route B; `question`).

    "Afterwards" is strict: only a read at a strictly later eid counts, or a copy's own source row would read
    as a reader of what its destination just wrote. Grouped by the *kind* of last write, because that is what
    each group means -- a `ResolveDst` nothing reads is a readback that never happened, a `CS_RWResource`
    nothing reads is a dispatch whose output dies, a `CopyDst` nothing reads is a fill for nobody. The
    question is what consumes the result, and the chain cannot see past the capture: a later frame and a CPU
    readback after the frame look exactly like nothing. A colour or depth target carries one more reading --
    a swapchain image that exists only to be presented looks the same, and a present is not a usage row --
    which is why the target groups say so in their own words rather than calling anything dead.
    """
    groups: Dict[str, List[str]] = {}
    for resource in sorted(bundle['resources'], key=lambda r: int(r.get('resource', '0') or 0)):
        chain = _usage_judged(resource)
        if chain is None:
            continue
        last_write = max((eid for eid, names in chain if names & USAGE_WRITES), default=0)
        if not last_write or any(names & USAGE_READS for eid, names in chain if eid > last_write):
            continue
        written = ', '.join(sorted(next(names for eid, names in chain
                                        if eid == last_write) & USAGE_WRITES))
        groups.setdefault(written, []).append(
            '%s: last write %s at eid %d, nothing after it reads the resource'
            % (_resource_label(resource), written, last_write))

    flags: List[RedFlag] = []
    for key in sorted(groups):
        presented = (' A swapchain image that exists only to be presented looks the same, and a present is '
                     'not a usage row.' if key in ('ColorTarget', 'DepthStencilTarget') else '')
        flags.append(_usage_flag(
            'write-never-read',
            '%d resource(s) whose last write is %s and which nothing afterwards reads.%s A question, not a '
            'verdict: what consumes the result would be a later frame or a CPU readback after the capture, '
            'and a usage list stops at the capture' % (len(groups[key]), key, presented),
            groups[key]))
    return flags


def detect_load_instead_of_clear(bundle: BundleData) -> List[RedFlag]:
    """A render target first used with nothing clearing, discarding or writing it (ROADMAP §1.1, route B).

    A target's *contents* are not in the usage list, but its history is: when nothing cleared, discarded or
    wrote the resource before the first event that binds it as a target, whatever that pass loads is what the
    allocation happened to hold (or an earlier frame left there). Whether it matters is not decidable from a
    bundle -- a pass that overwrites every pixel with blending off is fine, and blend and load state are not
    in a bundle -- so the finding states the observation and names the missing state. Measured on the Android
    capture: exactly one of four targets fires, and the other three each have a `Clear` or a `Discard` before
    their first target event, which is what a correctly initialised target looks like.
    """
    flags: List[RedFlag] = []
    for resource in sorted(bundle['resources'], key=lambda r: int(r.get('resource', '0') or 0)):
        chain = _usage_judged(resource)
        if chain is None:
            continue
        first_target = next(((eid, names) for eid, names in chain if names & USAGE_TARGETS), None)
        if first_target is None:
            continue
        before = [(eid, names) for eid, names in chain if eid < first_target[0]]
        if any(names & USAGE_WRITES for _eid, names in before):
            continue
        flags.append({
            'detector': 'load-instead-of-clear',
            'what': 'a render target is first used as %s at eid %d with nothing clearing, discarding or '
                    'writing it before: whatever the pass loads is what the allocation held -- whether that '
                    'matters needs blend and load state, which a bundle does not carry'
                    % (', '.join(sorted(first_target[1] & USAGE_TARGETS)), first_target[0]),
            'evidence': [_resource_label(resource),
                         'before that: %s' % (' '.join('%d:%s' % (eid, '/'.join(sorted(names)))
                                                       for eid, names in before) or 'nothing at all')],
            'certainty': 'question',
            'unproven': True,
        })
    return flags


#: Marker chunk names, by the job they do. The end of a queue-level marker is `Queue_EndEvent`; the offline
#: tool's own `MARKER_CHUNKS` lists the *beginnings* only, because that is all a marker *tree* needs.
PUSH_MARKER_CHUNKS = ('PushMarker', 'Queue_BeginEvent')
POP_MARKER_CHUNKS = ('PopMarker', 'Queue_EndEvent')

#: How many unattributed draws are named before the rest are counted.
UNATTRIBUTED_LIMIT = 10


def _named_chunks(path: str, wanted: Sequence[str]) -> Optional[List[Tuple[int, str, bytes]]]:
    """`(chunk index, name, payload)` for the chunks whose name is in `wanted`, or None if this tool cannot
    name chunks at all.

    The import is *inside* the function on purpose: `rdc_analysis` imports this module (the CLI dispatches
    `cmd_report` through it and the tests call `R.cmd_report`), so a module-level import back would be a
    cycle. These three detectors are the only thing here that needs the chunk stream, so the cycle is broken
    once, in the one place they share.

    The names come from the RenderDoc source tree. Without it every chunk is a number and a detector cannot
    tell a marker from a draw -- None says that, and the caller reports the detector as *skipped* rather than
    letting a report say "nothing found" about something it could not look at.
    """
    import rdc_analysis as analysis

    _info, stream, _how = analysis.load_stream(path)
    names = analysis.load_chunk_names()
    if not names:
        return None

    found: List[Tuple[int, str, bytes]] = []
    for index, chunk in enumerate(analysis.iter_chunks(stream), 1):
        name = names.get(chunk['id'], '')
        if name in wanted:
            found.append((index, name, analysis.chunk_payload(stream, chunk)))
    return found


def detect_marker_balance(path: str) -> Optional[List[RedFlag]]:
    """Markers that do not balance (ROADMAP §1.1, certain).

    A `PopMarker` with nothing pushed, or pushes still open at the end of the stream. Evidence is the chunk
    index, never an event id: the two are different spaces and the stream is all this detector can see
    (REFERENCE §9). Drawing a pass boundary from an unbalanced tree is how a report attributes work to the
    wrong pass, so this is reported before anything tries to.
    """
    chunks = _named_chunks(path, PUSH_MARKER_CHUNKS + POP_MARKER_CHUNKS)
    if chunks is None:
        return None

    flags: List[RedFlag] = []
    depth = 0
    extra_pops: List[int] = []
    open_at: List[int] = []
    for index, name, _payload in chunks:
        if name in PUSH_MARKER_CHUNKS:
            depth += 1
            open_at.append(index)
        elif depth > 0:
            depth -= 1
            open_at.pop()
        else:
            extra_pops.append(index)

    if extra_pops:
        flags.append({
            'detector': 'marker-imbalance',
            'what': '%d marker pop(s) with nothing pushed' % len(extra_pops),
            'evidence': ['chunk %s' % ', '.join(str(index) for index in extra_pops[:UNATTRIBUTED_LIMIT])],
            'certainty': 'certain',
            'unproven': True,
        })
    if open_at:
        flags.append({
            'detector': 'marker-imbalance',
            'what': '%d marker(s) never popped: the tree is still open where the stream ends' % len(open_at),
            'evidence': ['chunk %s' % ', '.join(str(index) for index in open_at[:UNATTRIBUTED_LIMIT])],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags


def detect_unattributed_draws(path: str) -> Optional[List[RedFlag]]:
    """Draws and dispatches outside any marker (ROADMAP §1.1, certain).

    A hygiene note, not a bug: plenty of engines draw outside markers. It matters here because the report
    attributes work per pass, and a draw with no marker has nothing to be attributed *to*.
    """
    import rdc_analysis as analysis

    chunks = _named_chunks(path, PUSH_MARKER_CHUNKS + POP_MARKER_CHUNKS + tuple(analysis.DRAW_CHUNKS))
    if chunks is None:
        return None

    depth = 0
    unattributed: List[Tuple[int, str]] = []
    for index, name, _payload in chunks:
        if name in PUSH_MARKER_CHUNKS:
            depth += 1
        elif name in POP_MARKER_CHUNKS:
            depth = max(0, depth - 1)
        elif depth == 0:
            unattributed.append((index, name))

    if not unattributed:
        return []
    counted: Dict[str, int] = {}
    for _index, name in unattributed:
        counted[name] = counted.get(name, 0) + 1
    return [{
        'detector': 'unattributed-draws',
        'what': '%d draw(s) or dispatch(es) outside any marker -- a note, not a bug: there is no marker path '
                'to attribute them to' % len(unattributed),
        'evidence': ['%s: chunk %s' % (name, ', '.join(str(index) for index, other in unattributed
                                                       if other == name)) [:160]
                     for name in sorted(counted)],
        'certainty': 'certain',
        'unproven': True,
    }]


def detect_zero_work(path: str) -> Optional[List[RedFlag]]:
    """Draws and dispatches that can only produce nothing (ROADMAP §1.1, certain).

    Zero indices, zero vertices, zero instances or a zero dispatch dimension: the call is in the stream and
    the GPU does nothing. The counts come from the same payload decoder `chunks`/`draws` use, so the fields
    are read in one place only.
    """
    import rdc_analysis as analysis

    wanted = ('List_DrawIndexedInstanced', 'List_DrawInstanced', 'List_Dispatch')
    chunks = _named_chunks(path, wanted)
    if chunks is None:
        return None

    flags: List[RedFlag] = []
    for index, name, payload in chunks:
        if len(payload) < 8:
            continue
        if name == 'List_DrawIndexedInstanced' and len(payload) >= 28:
            indices, instances = analysis.u32(payload, 8), analysis.u32(payload, 12)
            described = '%d indices, %d instance(s)' % (indices, instances)
            empty = indices == 0 or instances == 0
        elif name == 'List_DrawInstanced' and len(payload) >= 24:
            vertices, instances = analysis.u32(payload, 8), analysis.u32(payload, 12)
            described = '%d vertices, %d instance(s)' % (vertices, instances)
            empty = vertices == 0 or instances == 0
        elif name == 'List_Dispatch' and len(payload) >= 20:
            groups = (analysis.u32(payload, 8), analysis.u32(payload, 12), analysis.u32(payload, 16))
            described = 'dispatch %dx%dx%d' % groups
            empty = 0 in groups
        else:
            continue
        if empty:
            flags.append({
                'detector': 'zero-work',
                'what': 'this call draws nothing: %s' % described,
                'evidence': ['%s at chunk %d' % (name, index)],
                'certainty': 'certain',
                'unproven': True,
            })
    return flags


#: `b0 s0` in a reflection row: the register and space a constant block is expected at. The `cbuffer[0]`
#: form is what `shaders <eid>` writes and it is the only reflection row whose format is pinned down here
#: by real output; the read-only and write-only resource rows are not parsed until a capture shows them.
CONSTANT_BLOCK_ROW = re.compile(r'\bb(?P<reg>\d+)\s+s(?P<space>\d+)\b')

#: A root parameter as `state` writes it: `rp2   reg=0 space=0 res1907`, `rp0   reg=0 space=0 heap298+0x21cda`
#: for a table, or -- for one that is *not set to anything* -- `rp1   reg=0 space=0` and nothing more.
ROOT_PARAMETER_ROW = re.compile(r'reg=(?P<reg>\d+)\s+space=(?P<space>\d+)(?:\s+(?P<target>\S+))?')


def detect_unbound_root_parameters(bundle: BundleData) -> List[RedFlag]:
    """A constant block the shader reads, at a root parameter that is not set to anything (ROADMAP §1.1).

    The two halves come from one event's own documents: `shaders` says the stage reads a block at `bR sS`, and
    `state` says what the root parameter at `reg=R space=S` holds -- nothing, when the row ends at the
    register. Measured on the Android capture, where the driver's own note ("none bound as a root descriptor")
    says the same thing from the other side, so this is a detector that agrees with the engine rather than
    guessing at it.

    `question`, not `certain`, for one reason: a shader register can also be served by *root constants*, which
    are bound by value and print with no resource either. The root signature would say which it is, and a
    bundle does not carry the signature's contents -- so the finding is the observation, and the other
    possibility is named in it. A register with *no* root parameter row at all is not reported: a descriptor
    table whose range covers that register looks exactly the same from here.
    """
    flags: List[RedFlag] = []
    groups: Dict[Tuple[str, int, int], List[int]] = {}
    for key in sorted(bundle['states']):
        documents = bundle['states'][key]
        state, shaders = documents.get('state'), documents.get('shaders')
        if not isinstance(state, dict) or not isinstance(shaders, dict):
            continue

        unset: Dict[Tuple[int, int], bool] = {}
        for row in state.get('rootParameters', []):
            match = ROOT_PARAMETER_ROW.search(str(row))
            if match:
                unset[(int(match.group('reg')), int(match.group('space')))] = match.group('target') is None

        for stage in shaders.get('stages', []):
            for row in stage.get('constantBlocks', []):
                block = CONSTANT_BLOCK_ROW.search(str(row))
                if not block:
                    continue
                where = (int(block.group('reg')), int(block.group('space')))
                if unset.get(where) is True:
                    eid = int(state.get('eid', 0) or 0)
                    groups.setdefault((str(stage.get('stage', '?')), where[0], where[1]), []).append(eid)

    for key in sorted(groups):
        eids = sorted(groups[key])
        flags.append({
            'detector': 'unbound-root-parameter',
            'what': 'the shader reads a constant block at b%d s%d, and the root parameter there is not set to '
                    'a resource -- root constants are the other way that register can be served, and the '
                    'bundle does not say which' % (key[1], key[2]),
            'evidence': ['%s stage, eid %d..%d' % (key[0], eids[0], eids[-1])],
            'certainty': 'question',
            'unproven': True,
        })
    return flags


#: A signature row as the reflection writes it: `<SEMANTIC><index> reg<N> [c<M>]` — `SV_Position0 reg4`,
#: `TEXCOORD9 reg3 c3`, `TEXCOORD10_centroid0 reg0 c4`. The `cN` is the component count the engine reports
#: (`SigParameter::compCount`), and it is *optional*: a bundle written by an older driver has no `cN`, and
#: such a row is compared for name and index only rather than guessed at. The semantic carries its index,
#: and HLSL's interpolation modifier (`_centroid`, `_linear`, `_nointerpolation`) can be on either side or
#: on both, which is why the comparison below tries the name with and without it rather than keeping a list
#: of modifiers to strip.
SIGNATURE_ROW = re.compile(r'^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*?)(?P<index>\d+)\s+reg\d+'
                           r'(?:\s+c(?P<count>\d+))?\s*$')


def _semantic(row: Any) -> Optional[Tuple[str, int, Optional[int]]]:
    """`(name, index, components)` for a signature row, or None for a row this rule does not read. The
    count is None when the row does not carry one, which is not the same as a count of zero."""
    match = SIGNATURE_ROW.match(str(row))
    if match is None:
        return None
    count = match.group('count')
    return match.group('name'), int(match.group('index')), int(count) if count is not None else None


def _pair(semantic: Tuple[str, int, Optional[int]]) -> Tuple[str, int]:
    """Name and index, which is what "the same semantic" means here. The width is a separate question and
    is compared separately, because reading *fewer* components than the producer writes is legal."""
    return semantic[0], semantic[1]


def _semantic_matches(produced: Tuple[str, int], consumed: Tuple[str, int]) -> bool:
    """Whether one side's semantic serves the other's, ignoring an interpolation suffix on either.

    `TEXCOORD10_centroid0` and `TEXCOORD10` are the same semantic -- measured on `PC Renderer.rdc` at eid 700,
    where the vertex shader emits `TEXCOORD10_centroid0` and the pixel shader reads exactly that, so the
    suffix is not what distinguishes them. The suffix is only removed when the base name matches the other
    side, so this can never invent a match that is not there.
    """
    if produced == consumed:
        return True
    for side, other in ((produced, consumed), (consumed, produced)):
        base, _, _suffix = side[0].rpartition('_')
        if base and (base, side[1]) == other:
            return True
    return False


def detect_shader_io_mismatch(bundle: BundleData) -> List[RedFlag]:
    """A pixel shader input the vertex shader does not provide (ROADMAP §1.1, certain).

    Both reflections are in the same document, so this needs no capture, and it answers both halves of the
    row *VS out is not PS in*:

    * **A semantic the vertex shader does not emit at all.** D3D12 requires every PS input to be produced by
      the preceding stage, and the only exceptions are the `SV_` system values, which the rasteriser supplies
      (`SV_IsFrontFace` in the measured case -- the one input of six that the vertex shader does not emit, and
      the reason the rule ignores `SV_` on both sides). A vertex shader emitting *more* than the pixel shader
      reads is legal and never reported.
    * **The same semantic at a greater width.** The driver's rows carry the engine's component count
      (`c4`, `c3`, `c1`; measured on `PC Renderer.rdc`, where `TEXCOORD9` is c3 and `SV_Position0` is c4), and
      an input that reads *more* components of a semantic than its producer writes cannot be satisfied at
      pipeline creation. Reading *fewer* is a legal prefix subset and stays silent. The component *type*
      (float against uint) is not in the row, so a type-level mismatch is not reported: the row says what it
      says, and the row is the evidence.

    A geometry, hull or domain shader between them can change the signature, so an event that has one is
    skipped rather than reported: the rule compares adjacent stages, and those are not adjacent.
    """
    flags: List[RedFlag] = []
    missing: Dict[Tuple[str, str, str], List[int]] = {}
    narrow: Dict[Tuple[str, str, str, int, int], Dict[str, Any]] = {}
    for key in sorted(bundle['states']):
        shaders = bundle['states'][key].get('shaders')
        if not isinstance(shaders, dict):
            continue
        stages = {str(stage.get('stage')): stage for stage in shaders.get('stages', [])}
        if 'vs' not in stages or 'ps' not in stages:
            continue
        if any(other in stages for other in ('gs', 'hs', 'ds')):
            continue
        eid = int(shaders.get('eid', 0) or 0)
        vs_entry = str(stages['vs'].get('entry', '?'))
        ps_entry = str(stages['ps'].get('entry', '?'))

        produced = [(row, raw) for row, raw in
                    ((_semantic(r), str(r)) for r in stages['vs'].get('outputSignature', []))
                    if row is not None and not row[0].startswith('SV_')]
        for row in stages['ps'].get('inputSignature', []):
            consumed = _semantic(row)
            if consumed is None or consumed[0].startswith('SV_'):
                continue
            # An exact name match wins over a suffix match: if the vertex shader writes both `TEXCOORD9`
            # and `TEXCOORD9_centroid`, the plain one is what a plain `TEXCOORD9` input is compared with.
            matches = [one for one in produced if _semantic_matches(_pair(one[0]), _pair(consumed))]
            exact = [one for one in matches if one[0][0] == consumed[0]]
            match = exact[0] if exact else (matches[0] if matches else None)
            if match is None:
                missing.setdefault((vs_entry, ps_entry, '%s%d' % (consumed[0], consumed[1])),
                                   []).append(eid)
                continue
            if match[0][2] is not None and consumed[2] is not None and consumed[2] > match[0][2]:
                key2 = (vs_entry, ps_entry, '%s%d' % (consumed[0], consumed[1]),
                        match[0][2], consumed[2])
                entry = narrow.setdefault(key2, {'eids': [], 'vs': match[1], 'ps': str(row)})
                entry['eids'].append(eid)

    for key in sorted(missing):
        eids = sorted(missing[key])
        flags.append({
            'detector': 'shader-io-mismatch',
            'what': 'the pixel shader reads %s and the vertex shader does not emit it -- D3D12 has no other '
                    'source for it than the preceding stage' % key[2],
            'evidence': ['vs %s -> ps %s, eid %d..%d' % (key[0], key[1], eids[0], eids[-1])],
            'certainty': 'certain',
            'unproven': True,
        })
    for key in sorted(narrow):
        entry = narrow[key]
        eids = sorted(entry['eids'])
        flags.append({
            'detector': 'shader-io-mismatch',
            'what': 'the pixel shader reads %s at width c%d and the vertex shader writes it at c%d: an input '
                    'may use fewer components than its producer writes, never more' % (key[2], key[4], key[3]),
            'evidence': ['vs %s -> ps %s, eid %d..%d' % (key[0], key[1], eids[0], eids[-1]),
                         'vs writes: %s' % entry['vs'], 'ps reads: %s' % entry['ps']],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags


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
    runs.append({'detector': 'shader-io-mismatch', 'ran': True, 'why': ''})
    flags.extend(detect_shader_io_mismatch(bundle))

    # Four detectors read the usage lists, so they share one gate: a bundle written with --no-usage carries
    # `resourceUsage: (not collected...)` and each of them is *skipped with the reason* rather than reported
    # clean. The three chain rules stay separate detectors (and separate rows of ROADMAP §1.1) rather than one
    # pass, because each has its own certainty and each can be checked on its own.
    collected = str(bundle['manifest'].get('resourceUsage', '')) == 'collected'
    reason = '' if collected else 'no usage lists in this bundle (written with --no-usage)'
    for detector, function in (('dead-allocation', detect_dead_allocations),
                               ('read-before-write', detect_read_before_write),
                               ('write-never-read', detect_write_never_read),
                               ('load-instead-of-clear', detect_load_instead_of_clear)):
        runs.append({'detector': detector, 'ran': collected, 'why': reason})
        if collected:
            flags.extend(function(bundle))

    # The .rdc-side three: they need the chunk stream, so they need the capture path, and they need the
    # RenderDoc source tree to name what they are looking at. Either being absent is a *skip* with the
    # reason, never a clean report, because "no marker is unbalanced" and "I could not tell markers apart"
    # are different answers and only one of them is worth anything.
    for detector, function in (('marker-imbalance', detect_marker_balance),
                               ('unattributed-draws', detect_unattributed_draws),
                               ('zero-work', detect_zero_work)):
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

    # The findings keep the order their detector gave them -- "biggest first" is information, and a final
    # sort by evidence would throw it away (each detector sorts its own findings, so this is deterministic).
    return flags, runs


def report_caveats() -> List[str]:
    """What this report cannot say, in its own words. Fixed text, in a fixed order: it is part of the
    output a reader compares between runs, and a report that quietly stops mentioning a gap is worse
    than one that never mentioned it."""
    return [
        'A pass here is a run of consecutive events with the same call kind, render targets and depth '
        'target -- not a named pass. Call kinds (draw/copy/clear/marker), per-event triangle and thread '
        'counts, and marker names are not in a bundle at all: the replay API exposes no action list '
        '(ROADMAP §2).',
        'A compute pass is a run of dispatches with the same pipeline and shaders, and its targets and '
        'depth are given as not applicable: a dispatch does not set the output-merge state, so what the '
        'engine reports there is leftover from an earlier call. What a dispatch *does* write (its UAVs) '
        'is not in a bundle at all.',
        'What a pass is *for* (shadow map, depth prepass, G-buffer, base pass, post-process, UI) is not '
        'inferred: the structure given is only the targets, the depth target and the call kind. Naming '
        'a purpose needs the engine schema table (ROADMAP §1.4).',
        'Eleven detectors run -- five over the bundle, three over the usage chain and three over the '
        'capture\'s chunk stream -- and every finding is unproven: none of them has been checked against a '
        'capture whose bug list is known (ROADMAP §1.5). What is not checked at all (ROADMAP §1.1): '
        '"nothing bound through a descriptor table" and "binding kind mismatch" need the descriptor writes '
        'followed through the stream (which in turn needs the event-id-to-chunk calibration, ROADMAP §2); '
        '"dead compute" needs those plus the dispatch\'s UAV bindings; the depth-test, scissor and stencil rows '
        'need pipeline state a bundle does not carry (a driver change); MSAA needs the ResolveSubresource '
        'payload read, and none of the captures here carries a multisampled resource or a resolve at all; the '
        'component *type* of a signature element (float against uint) is not in the reflection rows, so a '
        'type-level mismatch is not checked even though the width is; and the two heuristics wait on those. '
        'Ranked notables and recommendations are not implemented yet either (ROADMAP §1.2, §1.3).',
        'The usage chain is the engine\'s record, not the frame\'s intention: one row is one usage (a buffer '
        'bound to eight slots has eight rows at one eid), and the list stops at the capture -- a read by the '
        'next frame or by the CPU afterwards looks exactly like nothing ever reading the resource. A resource '
        'whose only row is `eid 0, Unused` was not tracked by the engine and is never judged (101 of the 133 '
        'resources in the measured capture).',
        'Counters are not folded per pass yet (ROADMAP §4). A bundle written with --with-counters '
        'carries the per-event results in counters.json.',
        'Blend, depth-test, stencil and raster state are not in the bundle, so the state given per pass '
        'is the bound shaders and their constant blocks. The events listed as having a state document '
        'carry the full D3D12 state instead.',
        'Nothing here samples or decodes a texture or a render target: formats are named, pixels are '
        'not read.',
        'A bundle is a cache of what the engine said in one session. If the capture changed since it '
        'was written, the manifest still carries the hash of the capture it was written from (the '
        'provenance table above), and the report is about that capture.',
    ]


def render_report_markdown(doc: ReportDocument, rdc: str) -> str:
    """The report as Markdown. Deterministic: same bundle in, same bytes out."""
    manifest = doc['bundle']
    frame = doc['frame']
    capture_name = doc['capture'] or '(unknown)'
    lines: List[str] = []

    lines.append('# Frame report — %s' % _md(capture_name))
    lines.append('')
    # No directory paths here: the prose is compared byte-for-byte between runs and machines, so it
    # names the bundle by what it holds (the manifest's own counts) rather than by where it sits. The
    # JSON twin carries `bundleDir` for a tool that needs to know.
    lines.append('Written from a bundle of %s file(s) (manifest v%s, driver %s, RenderDoc %s): %d '
                 'event(s) with bound state, %d state-derived pass(es), %d resource(s), %d debug '
                 'message(s).' % (manifest.get('fileCount'), manifest.get('bundleVersion'),
                                  _md(str(manifest.get('driver'))), _md(str(manifest.get('renderdoc'))),
                                  frame['events'], len(doc['passes']), frame['resources'],
                                  frame['messages']))
    lines.append('')
    lines.append('## Provenance')
    lines.append('')
    lines.append('| field | value |')
    lines.append('|---|---|')
    lines.append('| capture | %s |' % _md(capture_name))
    lines.append('| capture sha256 | %s |' % _md(doc['captureSha256'] or '(not in the manifest)'))
    lines.append('| renderdoc | %s |' % _md(str(manifest.get('renderdoc'))))
    lines.append('| driver | %s |' % _md(str(manifest.get('driver'))))
    lines.append('| bundle | version %s, ids %s..%s, --with-images %s, --with-counters %s, resource '
                 'usage %s |' % (manifest.get('bundleVersion'), manifest.get('since'),
                                 manifest.get('until'), manifest.get('withImages'),
                                 manifest.get('withCounters'), _md(str(manifest.get('resourceUsage')))))
    lines.append('| files read | capture.json, events.json, resources.json, messages.json, '
                 'manifest.json, %d state document pair(s) |' % frame['stateDocuments'])
    lines.append('')
    lines.append('## Frame at a glance')
    lines.append('')
    lines.append('| what | value |')
    lines.append('|---|---|')
    lines.append('| events with bound state | %d |' % frame['events'])
    lines.append('| passes (state-derived) | %d |' % len(doc['passes']))
    lines.append('| graphics / compute events | %d / %d |' % (frame['graphicsEvents'],
                                                              frame['computeEvents']))
    lines.append('| resources | %d (%s) |' % (frame['resources'], ', '.join(
        '%d %s' % (count, kind) for kind, count in sorted(frame['resourcesByKind'].items()))))
    lines.append('| bytes in resources | %.2f MB of textures, %.2f MB of buffers |'
                 % (frame['textureBytes'] / 1048576.0, frame['bufferBytes'] / 1048576.0))
    lines.append('| render targets seen | %s |' % (', '.join(frame['targetsSeen']) or 'none'))
    lines.append('| formats seen | %s |' % (', '.join(frame['formatsSeen']) or 'none'))
    lines.append('| debug messages | %d%s |' % (frame['messages'], (
        ' (' + ', '.join('%s %d' % (k, v) for k, v in frame['messagesBySeverity'].items()) + ')')
        if frame['messages'] else ''))
    lines.append('| chunks in the capture | %d |' % frame['chunks'])
    lines.append('')
    lines.append('## Pipeline map')
    lines.append('')
    lines.append('| # | eids | kind | events | targets | depth | structure |')
    lines.append('|---|---|---|---|---|---|---|')
    for entry in doc['passes']:
        # The depth column says the same thing the pass section does for a dispatch: not applicable,
        # for the same reason the targets do.
        depth = ('n/a' if entry['kind'] == 'compute' else
                 'res%s' % _res_id(entry['depth']) if _is_resource(entry['depth']) else 'none')
        lines.append('| %d | %d–%d | %s | %d | %s | %s | %s |'
                     % (entry['index'], entry['firstEid'], entry['lastEid'], entry['kind'],
                        entry['events'], _targets_text(entry, True), depth, entry['structure']))
    lines.append('')
    lines.append('Passes and the targets they write:')
    lines.append('')
    lines.append('```mermaid')
    lines.append('graph LR')
    for entry in doc['passes']:
        node = 'P%d["pass %d (eid %d)"]' % (entry['index'], entry['index'], entry['firstEid'])
        if entry['kind'] == 'compute' or not entry['targets']:
            lines.append('  %s' % node)
            continue
        for target in entry['targets']:
            idtext = _res_id(target.split()[0])
            lines.append('  %s --> T%s["res%s"]' % (node, idtext, idtext))
    lines.append('```')
    lines.append('')
    lines.append('## Red flags')
    lines.append('')
    ran = [run['detector'] for run in doc['detectors'] if run['ran']]
    skipped = ['%s (%s)' % (run['detector'], run['why']) for run in doc['detectors'] if not run['ran']]
    lines.append('%d finding(s). Detectors that ran: %s.%s'
                 % (len(doc['flags']), ', '.join(ran) or 'none',
                    ' Skipped: %s.' % ', '.join(skipped) if skipped else ''))
    lines.append('')
    lines.append('`certain` means the bundle proves the observation; `question` would mean the observation is '
                 'real but its meaning depends on what the frame was for. Every finding is **unproven**: none '
                 'of these detectors has been checked against a capture whose bugs are known (ROADMAP §1.5), '
                 'so they are leads, not verdicts.')
    lines.append('')
    if doc['flags']:
        lines.append('| detector | finding | evidence | certainty |')
        lines.append('|---|---|---|---|')
        for flag in doc['flags']:
            lines.append('| %s | %s | %s | %s%s |'
                         % (flag['detector'], flag['what'], '; '.join(flag['evidence']), flag['certainty'],
                            ', unproven' if flag['unproven'] else ''))
    else:
        lines.append('Nothing fired -- which is a statement about these detectors, not about the frame: the '
                     'caveats below say what was not checked at all.')
    lines.append('')
    lines.append('## Pass by pass')
    for entry in doc['passes']:
        lines.append('')
        lines.append('### Pass %d — eid %d–%d (%s)'
                     % (entry['index'], entry['firstEid'], entry['lastEid'], entry['kind']))
        lines.append('')
        lines.append('- starts here because %s' % entry['reason'])
        lines.append('- work: %d event(s) (%d graphics, %d compute) — events, not vertices: the bundle '
                     'carries no counts' % (entry['events'], entry['graphics'], entry['compute']))
        lines.append('- targets: %s' % _targets_text(entry))
        if entry['kind'] != 'compute':
            lines.append('- depth: %s' % (entry['depth'] if _is_resource(entry['depth']) else 'none'))
        lines.append('- structure: %s' % entry['structure'])
        if entry['shaders']:
            lines.append('- shaders (from states/%d.state.json): %s'
                         % (entry['firstEid'], ', '.join(entry['shaders'])))
        if entry['otherShaders']:
            lines.append('- also bound at that event, and not used by a %s: %s'
                         % ('dispatch' if entry['kind'] == 'compute' else 'draw',
                            ', '.join(entry['otherShaders'])))
        if entry['blocks']:
            lines.append('- constant blocks (from states/%d.shaders.json):' % entry['firstEid'])
            for block in entry['blocks']:
                lines.append('  - %s' % block)
        if entry['firstTouched']:
            lines.append('- resources first used here (%d):' % len(entry['firstTouched']))
            for described in entry['firstTouched']:
                lines.append('  - %s' % described)
        else:
            lines.append('- no resource is used here for the first time')
    lines.append('')
    lines.append('## What this report cannot tell you')
    lines.append('')
    for caveat in doc['caveats']:
        lines.append('- %s' % caveat)
    lines.append('')
    lines.append('## Appendix — reproduce any claim')
    lines.append('')
    lines.append('| pass | commands |')
    lines.append('|---|---|')
    for entry in doc['passes']:
        first = entry['firstEid']
        lines.append("| %d | `replay_dump state '%s' %d` · `replay_dump shaders '%s' %d` |"
                     % (entry['index'], _md(rdc), first, _md(rdc), first))
    lines.append('')
    lines.append('A usage finding is checked the same way: `replay_dump usage \'%s\' <resId>` prints the same '
                 'list the detectors read (the engine\'s own `GetUsage`).' % _md(rdc))
    lines.append('')
    lines.append("Regenerate the bundle itself with `replay_dump dump '%s' <dir>`; run one replay at a "
                 'time (REFERENCE §9).' % _md(rdc))
    lines.append('')
    return '\n'.join(lines)


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

    flags, detectors = detect_all(bundle, path)

    doc: ReportDocument = {
        'reportVersion': REPORT_VERSION,
        'capture': recorded or path,
        'captureSha256': str(bundle['manifest'].get('captureSha256', '')),
        'bundleDir': bundle_dir,
        'bundle': bundle['manifest'],
        'frame': frame_facts(bundle),
        'passes': passes,
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
    print('written  : %s' % markdown_path)
    print('written  : %s' % json_path)
    print('flags    : %d finding(s) from %d detector(s), all unproven (ROADMAP §1.5)'
          % (len(flags), sum(1 for run in detectors if run['ran'])))
    return 0

