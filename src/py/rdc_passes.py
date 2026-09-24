"""Pass reconstruction and the roll-ups that describe a frame: what a pass is, why it starts where it does, and the frame at a glance."""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403
from rdc_detect_common import *  # noqa: F401,F403

from typing import Any, Dict, List, Sequence, Tuple

def reconstruct_passes(events: Sequence[BundleEvent],
                       resources: Sequence[BundleResource]) -> List[ReportPass]:
    """Group the bundle's events into passes, and say why each one starts.

    A pass here is a run of consecutive events that agree on the call kind, the render targets and the depth
    target, and the *reason* is recorded per pass so a reader can disagree with the rule rather than the
    result. The engine's marker path of the pass's *first* event is carried along as a label -- the marker the
    pass starts under -- which is what lets a report name a pass in the engine's own words; the pass
    boundaries themselves are still drawn from state, not from the markers.
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
                           'marker': str(event.get('marker', '')),
                           'events': 0, 'graphics': 0, 'compute': 0, 'targets': targets,
                           'depth': depth, 'structure': _pass_structure(kind, targets, depth),
                           'shaders': [], 'otherShaders': [], 'blocks': [], 'firstTouched': [],
                           'vertices': 0, 'instances': 0, 'triangles': 0, 'threads': 0, 'volumeCalls': 0,
                           'cost': 0.0, 'share': 0.0, 'costRows': 0, 'dearestEid': 0, 'writes': []})

        current = passes[-1]
        current['lastEid'] = int(event['eid'])
        current['events'] += 1
        current['graphics' if kind != 'compute' else 'compute'] += 1

        # What this call asked for, when the bundle carries it (`BundleEvent.volume`): the sum over the pass is
        # the pass's own work, and `volumeCalls` counts the events that contributed -- so a bundle written
        # before the driver wrote volumes is reported as "not measured" rather than as a pass that asked for
        # nothing.
        volume = event.get('volume') or {}
        if volume:
            current['volumeCalls'] += 1
            for key in ('vertices', 'instances', 'triangles', 'threads'):
                current[key] += int(volume.get(key, 0) or 0)

        last_kind, last_targets, last_depth, last_dispatch = kind, targets, depth, dispatch

    for entry in passes:
        first, last = entry['firstEid'], entry['lastEid']
        entry['firstTouched'] = _describe(
            entry, [r for r in resources if first <= int(r.get('firstEvent', 0) or 0) <= last])
    return passes

def _pass_structure(kind: str, targets: Sequence[str], depth: str) -> str:
    """What the pass *is*, from state alone: kind, targets, depth.

    Not what it is *for*: that is claimed in the report's engine-vocabulary section, where the capture's own
    names are read against the tables in `engine-schemas/` (REFERENCE §4.11), and this string can be one of the
    names a concept matches on (`depth only (no colour target)` for a shadow pass).
    """
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

#: The engine's `GraphicsAPI` values in declaration order, which is what `capture.json`'s `pipelineType` is
#: (`renderdoc-src/renderdoc/api/replay/replay_enums.h`). A value outside the table is named by its number
#: rather than guessed at, the same way an unknown format is.
GRAPHICS_APIS = ('D3D11', 'D3D12', 'OpenGL', 'Vulkan')

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
    # What the pass writes, where its outputs are not render targets: a dispatch's UAVs. The state document
    # is written at every state change, so the pass's *first* event carries the set the pass starts with --
    # the same reason the shaders and blocks above come from there. A bundle from before the driver wrote
    # this array has none, which the renderer says as `n/a` (the targets column) rather than as a dispatch
    # that writes nothing.
    if isinstance(state, dict):
        entry['writes'] = [' '.join(str(row).split()) for row in state.get('uavs', []) if str(row).strip()]

def cost_rows(bundle: BundleData) -> List[BundleCounter]:
    """The rows of the counter this bundle costs by: `costCounter`'s, or every row when it names none.

    One counter, because a bundle holds a row per event *per counter* and adding a byte count to a duration
    would be a number with no meaning. Two callers ask this -- the pass table's roll-up and `replaydiff`'s
    comparison -- so it is written once.
    """
    want = int(bundle.get('costCounter', 0) or 0)
    return [row for row in bundle['counters'] if not want or int(row['counter']) == want]

def pass_cost(bundle: BundleData, first: int, last: int) -> Tuple[float, int, int]:
    """One range of events costed by a bundle's counters: `(sum, how many rows, the dearest eid)`.

    `0` rows is "not measured", which is a different answer from a range that cost nothing, and `dearest` is 0
    when there was no row to be dearest.
    """
    inside = [row for row in cost_rows(bundle) if first <= int(row['eid']) <= last]
    cost = sum(float(row['value']) for row in inside)
    dearest = int(max(inside, key=lambda row: float(row['value']))['eid']) if inside else 0
    return cost, len(inside), dearest

def _cost_rollup(bundle: BundleData, passes: Sequence[ReportPass]) -> None:
    """Fill each pass's cost, its share of the frame and its dearest event, from the bundle's counters.

    One counter is summed -- the one the bundle names as the cost (`costCounter`, which is the engine's own
    choice: `EventGPUDuration` when the replay produced one) -- because a bundle holds a row per event *per
    counter*, and adding a byte count to a duration would be a number with no meaning. The pass ranges are
    this module's own, from the state documents, and not the engine's fold over its own passes: the two
    groupings divide a frame differently, so what is comparable is each pass's *events* summed here, which is
    what this does. `share` is against the sum over every event that has a value, so a bundle whose counter
    measured only part of the frame says so (the shares do not add to 1) rather than scaling the missing half
    to zero. Every field stays 0 in a bundle with no counters, and `costRows` is what tells "no counter" from
    "a pass that cost nothing".
    """
    total = sum(float(row['value']) for row in cost_rows(bundle))
    for entry in passes:
        cost, measured, dearest = pass_cost(bundle, int(entry['firstEid']), int(entry['lastEid']))
        entry['cost'] = cost
        entry['costRows'] = measured
        entry['share'] = (cost / total) if total else 0.0
        entry['dearestEid'] = dearest

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
    pipeline = int(capture.get('pipelineType', 0) or 0)
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
        'pipelineType': pipeline,
        'api': GRAPHICS_APIS[pipeline] if 0 <= pipeline < len(GRAPHICS_APIS) else 'api(%d)' % pipeline,
        'localRenderer': int(capture.get('localRenderer', 0) or 0),
        'vendor': int(capture.get('vendor', 0) or 0),
        'shaderDebugging': int(capture.get('shaderDebugging', 0) or 0),
        'pixelHistory': int(capture.get('pixelHistory', 0) or 0),
        # The counter the pass table costs by, and whether the bundle had one to cost with: the engine's own
        # choice and its own name for it (`counters.json`), so a reader knows what the pass costs *are*
        # -- "12.4 ms of EventGPUDuration" -- without opening another document. `costMeasured` is how many
        # events the counter gave a value for, which is the number that says how much of the frame the shares
        # cover (a driver that measured a third of it is a different claim from one that measured all of it).
        'costCounter': str(bundle['costCounterName']),
        'costUnit': str(bundle['counterUnit']),
        'costMeasured': sum(1 for row in bundle['counters']
                            if not bundle['costCounter'] or int(row['counter']) == bundle['costCounter']),
    }

# ---------------------------------------------------------------------------
# Detectors: the red flags.
#
# A detector is a pure function over the bundle that returns findings -- no printing, no presentation order,
# no state. What it may say is bounded by what a bundle *proves*, and that is why the three kept here are the
# ones that need nothing but the frame's own tables: the rules that need the descriptor writes (unbound
# descriptors, read-before-write), the pipeline state (depth logic, scissor, MSAA) or the chunk stream (zero
# work) live in the modules beside this one. A detector that would have to guess is not written: it would
# produce exactly the kind of confident wrong answer this tool exists to avoid.
__all__ = [
    'COMPUTE_STAGES',
    'GRAPHICS_APIS',
    '_cost_rollup',
    '_describe',
    '_pass_structure',
    '_row_severity',
    '_state_rollup',
    'cost_rows',
    'frame_facts',
    'pass_cost',
    'reconstruct_passes',
]
