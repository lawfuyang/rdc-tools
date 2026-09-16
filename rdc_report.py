"""The frame report: a bundle in, deterministic Markdown and JSON out (`report`).

Everything here reads files and returns text, which is why it is testable from fixture bundles
(`tests/test_rdc_report.py`) with no capture, no GPU and no driver. Split out of `rdc_analysis.py`
when that file passed 3000 lines; `rdc_analysis.py report` is still the entry point, and it
re-exports everything here.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence, TypedDict

# ---------------------------------------------------------------------------
# The frame report (`report`).
#
# ROADMAP section 1, skeleton: a bundle written by `replay_dump dump` (README §9) in, a deterministic
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
    messages: List[BundleMessage]
    states: Dict[str, Dict[str, Any]]


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


class ReportDocument(TypedDict):
    reportVersion: int
    capture: str
    captureSha256: str
    bundleDir: str
    bundle: Dict[str, Any]
    frame: Dict[str, Any]
    passes: List[ReportPass]
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
    """Read a bundle: the files `replay_dump dump` writes (README §9).

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

    return {'manifest': manifest, 'capture': capture, 'events': events, 'resources': resources,
            'messages': messages, 'states': states}


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
        'Detectors, red flags and recommendations are not implemented yet (ROADMAP §1.1), and neither '
        'are the notable-pass and notable-resource rankings (ROADMAP §1.2).',
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
    lines.append("Regenerate the bundle itself with `replay_dump dump '%s' <dir>`; run one replay at a "
                 'time (README §9).' % _md(rdc))
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

    doc: ReportDocument = {
        'reportVersion': REPORT_VERSION,
        'capture': recorded or path,
        'captureSha256': str(bundle['manifest'].get('captureSha256', '')),
        'bundleDir': bundle_dir,
        'bundle': bundle['manifest'],
        'frame': frame_facts(bundle),
        'passes': passes,
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
    return 0

