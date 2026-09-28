"""The pipeline-state detectors: depth, scissor, stencil, blend homogeneity, format range -- and the one rule that needs no state, multisampled targets nothing resolves."""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403
from rdc_detect_common import *  # noqa: F401,F403
from rdc_detect_binding import *  # noqa: F401,F403

import re

from typing import Any, Dict, List, Optional, Sequence, Tuple

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

def detect_mismatched_msaa(bundle: BundleData, chains: Optional[UsageChains] = None) -> List[RedFlag]:
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
        chain = _usage_judged(resource, chains)
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

# ---------------------------------------------------------------------------
# The geometry rules: where a draw's own positions land, against the target it writes.
#
# `dump --bounds` folds each draw's post-VS positions (`mesh --bounds` prints the same numbers per
# instance): the clip-space box, the NDC box over the vertices in front of the eye, and the three
# counts that say what the boxes cover. Everything below is arithmetic on that fold, which is why the
# rules can be *certain* where the fold is complete -- and why every one of them checks first.

#: The D3D clip volume, as the six half-spaces a primitive is clipped against, named by what the box
#: test looks like rather than by which end of the projection it is: `z = 0` is the "near" plane in a
#: standard projection and the "far" one in a reversed-Z projection, and a bundle does not say which.
#: Naming the half-space keeps the verdict true either way.
def _outside_plane(clip_min: Sequence[float], clip_max: Sequence[float]) -> Optional[str]:
    """The one clip plane every vertex of the box is outside, or None.

    A primitive wholly outside one plane cannot be rasterised -- that is what clipping means -- so this
    is the strongest verdict the fold supports, and the one that does not need the target at all. The
    tests are on `x + w` and `x - w` rather than on `x`: a box's extremes *bound* those sums
    (`max(x + w) <= clipMax.x + clipMax.w`), so a plane can be named from two corners even though the
    bundle holds no vertex. They are deliberately conservative -- a box that straddles a plane is not
    claimed about, because a primitive that clips against it may still cover part of the screen.
    """
    x_min, y_min, z_min, _w_min = clip_min
    x_max, y_max, z_max, w_max = clip_max
    if x_max + w_max < 0.0:
        return 'left of the clip plane x = -w'
    if x_min - w_max > 0.0:
        return 'right of the clip plane x = w'
    if y_max + w_max < 0.0:
        return 'below the clip plane y = -w'
    if y_min - w_max > 0.0:
        return 'above the clip plane y = w'
    if z_max < 0.0:
        return 'beyond the clip plane z = 0'
    if z_min - w_max > 0.0:
        return 'beyond the clip plane z = w'
    return None

def _bounds_reason(bundle: BundleData) -> str:
    """`''` when the bundle carries post-VS geometry bounds, else the reason its absence is a *skip*.

    The members come from `dump --bounds`, which is opt-in because folding a draw's geometry is the one
    thing a dump asks the engine to *run* rather than to describe (a stream-out pass and a readback per
    draw). The manifest says whether the flag was given, which is what keeps "nobody asked" apart from
    "asked, and the engine gave no post-projection position for any draw" -- a fact about the capture
    rather than about the bundle, and one a reader of a *clean* report would otherwise not see.
    """
    for event in bundle['events']:
        if isinstance(event.get('bounds'), dict):
            return ''
    if int(bundle['manifest'].get('withBounds', 0) or 0) == 0:
        return ('no post-VS geometry bounds in this bundle: `dump --bounds` writes them, and this '
                'bundle was written without that flag')
    return ('no draw in this bundle carries post-VS geometry bounds although it was dumped with '
            '--bounds: the engine gave no post-projection position for any of its draws')

def _bounds_counts(bounds: Dict[str, Any]) -> Optional[Tuple[int, int, int]]:
    """`(vertices, finite, projected)` from a `bounds` member, or None when it does not carry them.

    All three are required: a bundle whose member named only the boxes could not be judged soundly, and
    reading the missing ones as zero would turn "I cannot tell" into "nothing projects".
    """
    for key in ('vertices', 'finite', 'projected'):
        if key not in bounds:
            return None
    try:
        return (int(bounds['vertices']), int(bounds['finite']), int(bounds['projected']))
    except (TypeError, ValueError):
        return None

def _bounds_box(bounds: Dict[str, Any], key: str, count: int) -> Optional[List[float]]:
    """One box out of a `bounds` member: `count` floats from the driver's space-separated string."""
    text = bounds.get(key)
    if not isinstance(text, str):
        return None
    parts = text.split()
    if len(parts) != count:
        return None
    try:
        return [float(part) for part in parts]
    except ValueError:
        return None

def _target_rect(bundle: BundleData, event: BundleEvent) -> Optional[Tuple[str, int, int]]:
    """The rectangle the draw's own targets cover: `(what, width, height)`.

    The *union* of every bound target and the depth target, because the answer needed is "is this
    inside anything the call writes" and a mixed-size MRT is legal. `what` names what it came from for
    the evidence line. There are three ways to have no answer -- a target row the parse cannot read, a
    depth target with no resource table entry, no output at all -- and each returns None, which the
    caller reports as *not judged* rather than as clean.
    """
    width, height = 0, 0
    name = ''
    for row in event.get('targets', []) or []:
        parts = str(row).split()
        if len(parts) < 2:
            continue
        size = parts[1].split('x')
        if len(size) != 3:
            continue
        try:
            w, h = int(size[0]), int(size[1])
        except ValueError:
            continue
        width, height = max(width, w), max(height, h)
        name = 'res%s' % _res_id(parts[0]) if not name else name
    if width <= 0 or height <= 0:
        depth = _res_id(str(event.get('depth', '0') or '0'))
        resource = next((r for r in bundle['resources'] if _res_id(str(r.get('resource', ''))) == depth),
                        None)
        if resource is not None and str(resource.get('kind')) == 'texture':
            width = int(resource.get('width', 0) or 0)
            height = int(resource.get('height', 0) or 0)
            name = 'res%s' % depth
    if width <= 0 or height <= 0:
        return None
    return (name or 'the target', width, height)

def _pixel_box(ndc_min: Sequence[float], ndc_max: Sequence[float],
               viewport: Optional[Tuple[float, float, float, float]],
               target: Tuple[int, int]) -> Tuple[float, float, float, float]:
    """Where the NDC box lands, in pixels: `(x0, y0, x1, y1)`, y down.

    The divide by `w` is the driver's (one per vertex, which is the only place it can be done exactly),
    so this is the viewport transform alone. The viewport is used when the state in force has one --
    NDC is mapped onto *it*, not onto the target -- and the target's own rectangle otherwise, which is
    the same thing when the pass renders the whole of it.
    """
    x, y, width, height = viewport if viewport is not None else (0.0, 0.0, float(target[0]),
                                                                float(target[1]))
    left = x + (ndc_min[0] * 0.5 + 0.5) * width
    right = x + (ndc_max[0] * 0.5 + 0.5) * width
    top = y + (0.5 - ndc_max[1] * 0.5) * height
    bottom = y + (0.5 - ndc_min[1] * 0.5) * height
    return (min(left, right), min(top, bottom), max(left, right), max(top, bottom))

def _rect_in_force(bundle: BundleData, eid: int,
                   key: str) -> Optional[Tuple[float, float, float, float]]:
    """The first enabled rectangle of the state document in force at `eid`: `(x, y, width, height)`.

    `key` is `viewports` or `scissors` -- the state document's own spelling of the two rectangles a
    draw is clipped by. The state documents are written when the *state hash* changes, and the hash
    carries neither: a draw can sit under a document written for an earlier one in the same range. That
    is why a verdict built on one of these is a question rather than a certainty, while the target's own
    rectangle (`targets` *is* in the hash) is not.
    """
    best: Optional[Dict[str, Any]] = None
    for first, _last, state in _state_ranges(bundle):
        if first > eid:
            break
        best = state
    if not isinstance(best, dict):
        return None
    for rect in best.get(key, []) or []:
        if not isinstance(rect, dict) or not rect.get('enabled'):
            continue
        try:
            width, height = float(rect.get('width', 0) or 0), float(rect.get('height', 0) or 0)
        except (TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            return (float(rect.get('x', 0) or 0), float(rect.get('y', 0) or 0), width, height)
    return None

def _outside_rect(box: Sequence[float], x: float, y: float, width: float, height: float) -> bool:
    """Whether the pixel box `(x0, y0, x1, y1)` and the rectangle share no pixel."""
    return box[2] <= x or box[0] >= x + width or box[3] <= y or box[1] >= y + height

def detect_geometry_offscreen(bundle: BundleData) -> List[RedFlag]:
    """Draws whose own geometry cannot have written their target (certain, or `question`).

    Four verdicts, each from the geometry's own numbers and each one *checked for coverage first*: a
    box is only a statement about a draw when the fold that produced it held every vertex (`finite ==
    vertices`) and no vertex was behind the eye (`projected == finite`) -- a vertex whose position is
    not a number is left out of the box, and one behind the eye can clip its primitive back into view,
    so a box drawn around the others is not something a "this draw wrote nothing" claim may rest on.

    * every vertex behind the eye: `projected == 0`, and the whole primitive is behind the camera;
    * every vertex outside one clip plane: the geometry is culled before rasterisation, and the plane is
      named as the half-space (`left of x = -w`, `beyond z = w`) rather than as near or far, which
      depends on a projection convention a bundle does not carry;
    * the pixels the geometry covers lie wholly outside the target's rectangle (certain). Its reachable
      case is a viewport that is not the target's own rectangle -- inside the clip volume the two agree,
      which is why the NDC box is compared with the *pixels* rather than with NDC;
    * ... or wholly outside the viewport or scissor in force (a question: neither is part of the state
      hash, so the document may have been written for an earlier draw in the same range).

    What this cannot say is why: the application's own frustum decision is not in the capture
    (`cullFlags` is nowhere in the public replay API of 1.46), so a draw the CPU culled did not reach
    the file at all, and a draw that did was left to the GPU to reject.
    """
    behind: List[str] = []
    planes: List[str] = []
    rectangles: List[str] = []
    clipped: List[str] = []
    for event in bundle['events']:
        if str(event.get('psoKind', 'graphics')) == 'compute':
            continue
        bounds = event.get('bounds')
        if not isinstance(bounds, dict):
            continue
        counts = _bounds_counts(bounds)
        if counts is None:
            continue
        vertices, finite, projected = counts
        if vertices <= 0 or finite != vertices:
            continue
        eid = int(event.get('eid', 0) or 0)
        label = 'eid %d (%s)' % (eid, ' '.join(str(event.get('marker', '')).split()) or 'no marker')
        if projected == 0:
            behind.append('%s: %d vertex/vertices, every one behind the eye (w <= 0)'
                          % (label, vertices))
            continue

        clip_min = _bounds_box(bounds, 'clipMin', 4)
        clip_max = _bounds_box(bounds, 'clipMax', 4)
        if clip_min is not None and clip_max is not None:
            plane = _outside_plane(clip_min, clip_max)
            if plane is not None:
                planes.append('%s: %d vertex/vertices, every one %s (clip x %g..%g y %g..%g z %g..%g '
                              'w %g..%g)' % (label, vertices, plane, clip_min[0], clip_max[0],
                                             clip_min[1], clip_max[1], clip_min[2], clip_max[2],
                                             clip_min[3], clip_max[3]))
                continue

        if projected != finite:
            continue
        ndc_min = _bounds_box(bounds, 'ndcMin', 3)
        ndc_max = _bounds_box(bounds, 'ndcMax', 3)
        target = _target_rect(bundle, event)
        if ndc_min is None or ndc_max is None or target is None:
            continue
        viewport = _rect_in_force(bundle, eid, 'viewports')
        box = _pixel_box(ndc_min, ndc_max, viewport, (target[1], target[2]))
        what, width, height = target
        text = 'x %g..%g y %g..%g of %s %dx%d' % (box[0], box[2], box[1], box[3], what, width, height)
        if _outside_rect(box, 0.0, 0.0, float(width), float(height)):
            rectangles.append('%s: %d vertex/vertices project wholly outside the target: %s'
                              % (label, vertices, text))
            continue
        for name, rect in (('the viewport in force', viewport),
                           ('the scissor in force', _rect_in_force(bundle, eid, 'scissors'))):
            if rect is None or not _outside_rect(box, rect[0], rect[1], rect[2], rect[3]):
                continue
            clipped.append('%s: %d vertex/vertices project wholly outside %s (x %g y %g %gx%g): %s'
                           % (label, vertices, name, rect[0], rect[1], rect[2], rect[3], text))
            break

    flags: List[RedFlag] = []
    if behind:
        flags.append(_state_flag(
            'geometry-offscreen',
            '%d draw(s) whose every vertex is behind the eye: the geometry is behind the camera, so '
            'the call is issued and cannot write a pixel' % len(behind), behind, 'certain'))
    if planes:
        flags.append(_state_flag(
            'geometry-offscreen',
            '%d draw(s) wholly outside one clip plane: a primitive entirely outside a plane is culled '
            'before rasterisation, so none of them can have written its target' % len(planes), planes,
            'certain'))
    if rectangles:
        flags.append(_state_flag(
            'geometry-offscreen',
            '%d draw(s) whose geometry projects wholly outside the rectangle of the target it writes: '
            'the call is issued and its fragments fall outside the pixels that exist' % len(rectangles),
            rectangles, 'certain'))
    if clipped:
        flags.append(_state_flag(
            'geometry-offscreen',
            '%d draw(s) whose geometry projects wholly outside the rectangle the state in force clips '
            'it to: a question, because a viewport and a scissor are not part of the state hash and the '
            'document may have been written for an earlier draw in the same range' % len(clipped),
            clipped, 'question'))
    return flags

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
    '_bounds_box',
    '_bounds_counts',
    '_bounds_reason',
    '_graphics_state_ranges',
    '_outside_plane',
    '_outside_rect',
    '_pixel_box',
    '_pipeline_state_reason',
    '_range_label',
    '_rect_in_force',
    '_state_flag',
    '_state_ranges',
    '_stencil_writes',
    '_target_rect',
    '_unorm_bits',
    'detect_blend_in_opaque_pass',
    'detect_depth_logic',
    'detect_empty_scissor',
    'detect_format_units_suspicion',
    'detect_geometry_offscreen',
    'detect_mismatched_msaa',
    'detect_stencil_without_writer',
]
