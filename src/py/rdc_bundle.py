"""The report's input: the bundle of engine answers, its types, and the helpers every later module shares (ids, markdown escaping, target text)."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, TypedDict, Union

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
#: the engine held, not what the frame intended. `unproven` is the ROADMAP §1.4 gate: a detector that has
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
    messages: List[Union[BundleMessage, str]] = (_bundle_file(bundle_dir, 'messages.json', False) or {}).get(
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

    # Built as a `BundleData` rather than as a dict literal: the seven members have different types, and a
    # literal widens to a union of them, which is no longer the type this function promises to return.
    return BundleData(manifest=manifest, capture=capture, events=events, resources=resources,
                      messages=messages, states=states, cbuffers=cbuffers)

__all__ = [
    'BUNDLE_VERSION',
    'BundleData',
    'BundleError',
    'BundleEvent',
    'BundleMessage',
    'BundleResource',
    'BundleUsage',
    'DetectorRun',
    'REPORT_VERSION',
    'RedFlag',
    'ReportDocument',
    'ReportPass',
    '_bundle_file',
    '_is_resource',
    '_md',
    '_res_id',
    '_targets_text',
    'load_bundle',
]
