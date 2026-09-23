"""The pipeline-state detectors: depth, scissor, stencil, blend homogeneity, format range -- and the one rule that needs no state, multisampled targets nothing resolves."""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403
from rdc_detect_common import *  # noqa: F401,F403
from rdc_detect_binding import *  # noqa: F401,F403

import re

from typing import Any, Dict, List, Tuple

def _state_ranges(bundle: BundleData) -> List[Tuple[int, int, Dict[str, Any]]]:
    """`(first, last, state)` per state document: the events that document's state is bound for."""
    eids = sorted(int(one) for one in bundle['states']
                  if isinstance((bundle['states'][one] or {}).get('state'), dict))
    last_event = max((int(event.get('eid', 0) or 0) for event in bundle['events']), default=0)
    ranges: List[Tuple[int, int, Dict[str, Any]]] = []
    for index, eid in enumerate(eids):
        end = (eids[index + 1] - 1) if index + 1 < len(eids) else last_event
        ranges.append((eid, max(end, eid), bundle['states'][str(eid)]['state']))
    return ranges

def _range_label(first: int, last: int) -> str:
    return 'eid %d..%d' % (first, last)

def _graphics_state_ranges(bundle: BundleData) -> List[Tuple[int, int, Dict[str, Any]]]:
    """The state ranges whose first event is a *draw*: a dispatch inherits the last draw's output-merger
    state, so a rule about depth, stencil, blend, viewport or scissor must not read it there."""
    by_eid = {int(event.get('eid', 0) or 0): event for event in bundle['events']}
    return [one for one in _state_ranges(bundle)
            if str(by_eid.get(one[0], {}).get('psoKind', 'graphics')) != 'compute']

def _pipeline_state_reason(bundle: BundleData) -> str:
    """`''` when the bundle carries the pipeline-state blocks, else the reason its absence is a *skip*.

    A bundle from a driver that predates them has no `outputMerger` anywhere, and "the state is fine" and "I
    could not look at the state" are different answers -- the run list is where that difference lives.
    """
    for documents in bundle['states'].values():
        state = (documents or {}).get('state')
        if isinstance(state, dict) and isinstance(state.get('outputMerger'), dict):
            return ''
    return 'no pipeline state in this bundle (written by an older driver)'

#: How many lines a state finding lists before it counts the rest, for the same reason as USAGE_LIST_LIMIT:
#: the finding is the group, not a wall of ranges.
STATE_LIST_LIMIT = 8

def _state_flag(detector: str, what: str, lines: List[str], certainty: str) -> RedFlag:
    """One finding from a group of state lines, capped and rolled up like the usage rules'."""
    return {
        'detector': detector,
        'what': what,
        'evidence': lines[:STATE_LIST_LIMIT] + (
            ['%d more, not listed' % (len(lines) - STATE_LIST_LIMIT)] if len(lines) > STATE_LIST_LIMIT else []),
        'certainty': certainty,
        'unproven': True,
    }

def detect_depth_logic(bundle: BundleData) -> List[RedFlag]:
    """Depth writes with depth testing off, or a depth test with nothing bound (certain).

    The first is the expensive one: with `depthWrites` on and `depthEnable` off, nothing rejects a fragment,
    so every draw writes its depth over whatever was there and later passes occlude against the last writer
    instead of the nearest surface. The second is milder but just as literal: the state asks for a depth test
    while no depth target is bound, and a test with nothing to test against is skipped. Both are read from
    the state the driver now records, and both are about draws -- a dispatch reports what the last draw left.
    """
    writes: List[str] = []
    unbound: List[str] = []
    for first, last, state in _graphics_state_ranges(bundle):
        merger = state.get('outputMerger')
        if not isinstance(merger, dict):
            continue
        bound = _res_id(str(state.get('depthTarget', '0')))
        if bool(merger.get('depthWrites')) and not bool(merger.get('depthEnable')):
            writes.append('%s: depthWrites on with depthEnable off (depthFunction %s)%s'
                          % (_range_label(first, last), merger.get('depthFunction', '?'),
                             '' if bound == '0' else ', on res%s' % bound))
        elif bool(merger.get('depthEnable')) and bound == '0':
            unbound.append('%s: depthEnable on (depthFunction %s) with no depth target bound'
                           % (_range_label(first, last), merger.get('depthFunction', '?')))

    flags: List[RedFlag] = []
    if writes:
        flags.append(_state_flag(
            'depth-logic',
            'depth writes are on while depth testing is off in %d state range(s): nothing rejects a fragment, '
            'so each draw writes its depth over the last one' % len(writes), writes, 'certain'))
    if unbound:
        flags.append(_state_flag(
            'depth-logic',
            'depth testing is on with no depth target bound in %d state range(s): the test has nothing to '
            'read and is skipped' % len(unbound), unbound, 'certain'))
    return flags

def detect_empty_scissor(bundle: BundleData) -> List[RedFlag]:
    """A bound scissor or viewport that can only produce nothing (certain).

    A rectangle whose `width` or `height` is zero or less, on an *enabled* rectangle: the draw is issued, the
    shaders run for no pixels, and the target keeps what it had. A *disabled* rectangle is the opposite --
    the engine is told not to use it -- and says nothing about the draw, so only enabled ones are read.
    """
    degenerate: List[str] = []
    for first, last, state in _graphics_state_ranges(bundle):
        for viewport in state.get('viewports', []) or []:
            if not isinstance(viewport, dict) or not viewport.get('enabled'):
                continue
            width, height = float(viewport.get('width', 0) or 0), float(viewport.get('height', 0) or 0)
            if width <= 0 or height <= 0:
                degenerate.append('%s: viewport x %g y %g width %g height %g'
                                  % (_range_label(first, last), float(viewport.get('x', 0) or 0),
                                     float(viewport.get('y', 0) or 0), width, height))
        for scissor in state.get('scissors', []) or []:
            if not isinstance(scissor, dict) or not scissor.get('enabled'):
                continue
            width, height = int(scissor.get('width', 0) or 0), int(scissor.get('height', 0) or 0)
            if width <= 0 or height <= 0:
                degenerate.append('%s: scissor x %d y %d width %d height %d'
                                  % (_range_label(first, last), int(scissor.get('x', 0) or 0),
                                     int(scissor.get('y', 0) or 0), width, height))
    if not degenerate:
        return []
    return [_state_flag(
        'empty-scissor',
        '%d enabled viewport/scissor rectangle(s) have a zero or negative extent: every draw in those state '
        'ranges is issued and shades nothing' % len(degenerate), degenerate, 'certain')]

def _stencil_writes(state: Dict[str, Any]) -> bool:
    """Whether a state *writes* stencil: the test is on, the target is not read-only, and a face has an
    operation other than `Keep` with a mask that lets it through."""
    merger = state.get('outputMerger')
    if not isinstance(merger, dict) or not merger.get('stencilEnable') or merger.get('stencilReadOnly'):
        return False
    for key in ('frontFace', 'backFace'):
        face = merger.get(key)
        if not isinstance(face, dict) or not int(face.get('writeMask', 0) or 0):
            continue
        if any(str(face.get(op, 'Keep')) != 'Keep' for op in ('fail', 'depthFail', 'pass')):
            return True
    return False

def detect_stencil_without_writer(bundle: BundleData) -> List[RedFlag]:
    """Stencil testing where nothing earlier in the frame wrote stencil (question).

    For a state range that enables stencil *and* binds a depth-stencil target, the question is what put
    anything in that target's stencil: an earlier range that used the same target and wrote stencil (an
    operation other than `Keep`, with a write mask), or this range's own write. A `Clear` or `Discard` usage
    row is deliberately *not* counted as a writer, because it does not say which of depth and stencil was
    cleared -- which is exactly why this is a question and not a verdict. A stencil buffer nothing wrote
    holds what it was allocated with, or what the previous frame left there, and a test against that usually
    discards every fragment -- which is the shape of the bug this looks for.
    """
    wrote: Dict[str, List[str]] = {}
    uninitialised: Dict[str, List[str]] = {}
    for first, last, state in _graphics_state_ranges(bundle):
        merger = state.get('outputMerger')
        bound = _res_id(str(state.get('depthTarget', '0')))
        if isinstance(merger, dict) and merger.get('stencilEnable') and bound != '0' \
                and not wrote.get(bound) and not _stencil_writes(state):
            uninitialised.setdefault(bound, []).append(
                '%s: stencil tests res%s (read-only %s) and nothing earlier wrote its stencil'
                % (_range_label(first, last), bound,
                   'yes' if merger.get('stencilReadOnly') else 'no'))
        if bound != '0' and _stencil_writes(state):
            wrote.setdefault(bound, []).append(_range_label(first, last))

    lines = [line for target in sorted(uninitialised) for line in uninitialised[target]]
    if not lines:
        return []
    return [_state_flag(
        'stencil-without-writer',
        'stencil testing is enabled in %d state range(s) on a depth-stencil target nothing earlier in the '
        'frame wrote: the test reads whatever the buffer was allocated with, or whatever the previous frame '
        'left in it. A clear is not counted as a writer, because a `Clear` row does not say which of depth '
        'and stencil it cleared' % len(lines), lines, 'question')]

def detect_mismatched_msaa(bundle: BundleData) -> List[RedFlag]:
    """A multisampled colour target nothing ever resolves (question).

    `samples` is in the resource table and a resolve is a usage row, so the decidable half of the row needs
    no chunk payload: a texture with more than one sample that was used as a colour target and that no
    resolve usage ever touched. The other half -- *which* subresource a resolve copied -- is read from the
    file rather than from a bundle: the `ResolveSubresource` payload is decoded offline (2026-09-22), so
    `deps` prints every resolve as a read->write pair naming both subresources. What no rule claims is
    whether a resolve was the *right* one: nothing in a capture says which slice was supposed to be
    resolved, and a rule that guessed would be worse than one that admits it. A depth target is not judged:
    an MSAA depth buffer nobody resolves is the ordinary case, since the depth test reads it directly rather
    than sampling it.
    """
    lines: List[str] = []
    for resource in sorted(bundle['resources'], key=lambda r: int(r.get('resource', '0') or 0)):
        if str(resource.get('kind')) != 'texture' or int(resource.get('samples', 1) or 1) <= 1:
            continue
        chain = _usage_judged(resource)
        if chain is None:
            continue
        if not any('ColorTarget' in names for _eid, names in chain):
            continue
        if any('Resolve' in name for _eid, names in chain for name in names):
            continue
        sampled = sum(1 for _eid, names in chain if names & USAGE_READS)
        lines.append('%s: %d samples, used as a colour target, %s'
                     % (_resource_label(resource), int(resource.get('samples', 1) or 1),
                        'nothing reads it either' if not sampled else
                        'read by %d event(s) -- each of which sees one sample unless it was resolved outside '
                        'the capture' % sampled))
    if not lines:
        return []
    return [_state_flag(
        'mismatched-msaa',
        '%d multisampled colour target(s) that no resolve usage ever touched: a read of the multisampled '
        'texture returns one sample of it, and what a later pass expects is the resolved image' % len(lines),
        lines, 'question')]

#: A render-target row as `state` writes it: `slot 0  res2269`.
RENDER_TARGET_ROW = re.compile(r'^\s*slot\s+(?P<slot>\d+)\s+res(?P<resource>\d+)\s*$')

#: Target names that say "this pass writes surface data for lighting to read" -- a GBuffer or a base pass.
#: Blending there is the signal *blend in an opaque pass* describes; the list is short on
#: purpose, because a name that merely *might* be opaque would make the heuristic noise.
OPAQUE_TARGET_NAMES = ('gbuffer', 'g_buffer', 'basepass', 'base_pass', 'scenedepth')

def detect_blend_in_opaque_pass(bundle: BundleData) -> List[RedFlag]:
    """Blending enabled on a target whose name says the pass is opaque (`[heuristic]`).

    The state says blending is on for slot N and the resource bound there has a name like `GBufferA` or
    `BasePass`, which is surface data for a lighting pass to read: blending in one usually means a stale
    blend state from a UI or translucent pass rather than intent. It is a *name* heuristic and it says so --
    a bundle carries no *marker* names, because the driver does not write them, so "the pass name says
    base/GBuffer" is read from the resource the pass writes instead of from the pass. A weaker fact, stated
    plainly, is worth more than a stronger one that is not in the file.

    The engine does have the marker names (`ActionDescription::customName`, through `GetRootActions()`, which
    is also where the bundle's `psoKind` comes from) -- so when the driver starts writing them per event,
    this rule should prefer the pass's own name and say which it used.
    """
    # The resources by id, once. The loop below asks "which resource is bound at this slot" per slot per
    # state range, and answering that by scanning `bundle['resources']` each time is O(ranges x slots x R).
    by_id = {_res_id(str(one.get('resource', ''))): one for one in bundle['resources']}
    lines: List[str] = []
    for first, last, state in _graphics_state_ranges(bundle):
        merger = state.get('outputMerger')
        if not isinstance(merger, dict):
            continue
        blends = merger.get('blends') or []
        bound: Dict[int, str] = {}
        for row in state.get('renderTargets', []) or []:
            match = RENDER_TARGET_ROW.match(str(row))
            if match is not None:
                bound[int(match.group('slot'))] = match.group('resource')
        for slot, resource in sorted(bound.items()):
            # D3D12's own rule: without independent blending, `blends[0]` is what every target uses, so an
            # array with one entry is not a missing entry for slot 2.
            index = slot if merger.get('independentBlend') else 0
            blend = blends[index] if index < len(blends) and isinstance(blends[index], dict) else None
            if blend is None or not blend.get('enabled'):
                continue
            target = by_id.get(resource)
            name = ' '.join(str(target.get('name', '')).split()) if target else ''
            lowered = name.lower()
            if not any(pattern in lowered for pattern in OPAQUE_TARGET_NAMES):
                continue
            lines.append('%s: slot %d is res%s "%s" with blending on (src %s, dst %s, op %s, writeMask %s)'
                         % (_range_label(first, last), slot, resource, name,
                            blend.get('srcColor', '?'), blend.get('dstColor', '?'),
                            blend.get('colorOperation', '?'), blend.get('writeMask', '?')))
    if not lines:
        return []
    return [_state_flag(
        'blend-in-opaque-pass',
        'blending is enabled while writing %d target(s) whose name says the pass is opaque, like a GBuffer: a '
        '[heuristic], because a name is the application\'s convention rather than the frame\'s statement -- '
        'it is worth a look, not a verdict' % len(lines), lines, 'heuristic')]

#: A channel count inside a format name: `R8G8B8A8_UNORM` has 8s, `R10G10B10A2_UNORM` a 10 -- which is why
#: the maximum, not the first, is what `_unorm_bits` reads.
FORMAT_CHANNELS = re.compile(r'(\d+)')

def _unorm_bits(format_name: str) -> int:
    """The channel width of a plain UNORM/SNORM format, or 0 for anything else.

    Only plain integer formats count: `R8G8B8A8_UNORM` is 8, while a float format, a packed format
    (`B10G11R11_UFloatPack32`) and the sRGB variant of a UNORM one are all 0 -- an sRGB target carries its
    transfer function, which is the opposite of the suspicion here.
    """
    name = format_name.upper()
    if not name.endswith(('_UNORM', '_SNORM')):
        return 0
    digits = [int(one) for one in FORMAT_CHANNELS.findall(name.split('_')[0])]
    if not digits:
        return 0
    return max(digits) if max(digits) <= 8 else 0

def detect_format_units_suspicion(bundle: BundleData) -> List[RedFlag]:
    """A float shader output written to a narrow linear target (`[heuristic]`).

    The pixel shader's output signature says the component *type* (`float`, since the driver now records the
    engine's `VarType`) and the render target's format says how many bits carry it, so one question is
    decidable: does the pass hand the target more range than the target can keep? A `float4` into an
    `R8G8B8A8_UNORM` target is 256 steps per channel; for an LDR pass that is the design, and for a pass
    computing in float -- HDR values, a tonemap that did not happen -- it is where banding and clipped
    highlights come from. That is why this is a `[heuristic]`: the *type* says float and the target says 8
    bits, and what the values actually were is not in the bundle.

    The row's other half -- an sRGB/linear mismatch between the write and a later read -- is a *separate*
    rule, and one that reads the file rather than the bundle: `detect_srgb_view_mismatch` compares each
    texture's declared format against the formats the views over it declare (a resource declared `..._UNORM`
    read through an `..._UNORM_SRGB` view). A bundle carries neither the view's format nor the resource's,
    which is why that half is not claimed *here*.
    """
    # One id -> resource map for the whole rule, as in `detect_blend_in_opaque_pass`: the alternative is a
    # scan of every resource per slot per state range.
    by_id = {_res_id(str(one.get('resource', ''))): one for one in bundle['resources']}
    lines: List[str] = []
    for first, last, state in _graphics_state_ranges(bundle):
        documents = bundle['states'].get(str(first)) or {}
        shaders = documents.get('shaders')
        if not isinstance(shaders, dict):
            continue
        pixel = next((stage for stage in shaders.get('stages', [])
                      if str(stage.get('stage')) == 'ps'), None)
        if pixel is None:
            continue
        outputs: Dict[int, Tuple[str, str]] = {}
        for row in pixel.get('outputSignature', []) or []:
            semantic = _semantic(row)
            if semantic is not None and semantic[0].startswith('SV_TARGET') and semantic[3]:
                outputs[semantic[1]] = (semantic[3], str(row))
        if not outputs:
            continue
        bound: Dict[int, str] = {}
        for row in state.get('renderTargets', []) or []:
            match = RENDER_TARGET_ROW.match(str(row))
            if match is not None:
                bound[int(match.group('slot'))] = match.group('resource')
        for slot in sorted(set(outputs) & set(bound)):
            kind, raw = outputs[slot]
            if kind not in ('float', 'half', 'double'):
                continue
            target = by_id.get(bound[slot])
            if target is None or str(target.get('kind')) != 'texture':
                continue
            bits = _unorm_bits(str(target.get('format', '')))
            if not bits:
                continue
            lines.append('%s: slot %d is res%s "%s" (%s, %d bits) and the pixel shader writes %s to it'
                         % (_range_label(first, last), slot, bound[slot],
                            ' '.join(str(target.get('name', '')).split()),
                            str(target.get('format', '?')), bits, raw))
    if not lines:
        return []
    return [_state_flag(
        'format-units-suspicion',
        '%d render target write(s) feed a float pixel-shader output into an 8-bit-or-narrower linear target: '
        'an LDR pass does this by design, and a pass that computes in float will band or clip here. A '
        '[heuristic]: the types are facts, the values are not in the bundle' % len(lines), lines, 'heuristic')]

#: Marker chunk names, by the job they do. The end of a queue-level marker is `Queue_EndEvent`; the offline
#: tool's own `MARKER_CHUNKS` lists the *beginnings* only, because that is all a marker *tree* needs.
__all__ = [
    'FORMAT_CHANNELS',
    'OPAQUE_TARGET_NAMES',
    'RENDER_TARGET_ROW',
    'STATE_LIST_LIMIT',
    '_graphics_state_ranges',
    '_pipeline_state_reason',
    '_range_label',
    '_state_flag',
    '_state_ranges',
    '_stencil_writes',
    '_unorm_bits',
    'detect_blend_in_opaque_pass',
    'detect_depth_logic',
    'detect_empty_scissor',
    'detect_format_units_suspicion',
    'detect_mismatched_msaa',
    'detect_stencil_without_writer',
]
