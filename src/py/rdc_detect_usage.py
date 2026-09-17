"""The usage-chain detectors: what the engine's per-resource history says about a read that nothing wrote, a write nothing read, a target nothing cleared, and a compute pass whose output dies."""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403
from rdc_detect_common import *  # noqa: F401,F403
from rdc_passes import *  # noqa: F401,F403

from typing import Dict, List, Tuple

def detect_read_before_write(bundle: BundleData) -> List[RedFlag]:
    """A resource read with nothing in the frame writing it first (`question`).

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
    """A resource written with nothing afterwards reading it (`question`).

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
    """A render target first used with nothing clearing, discarding or writing it (the usage chain).

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

def detect_dead_compute(bundle: BundleData) -> List[RedFlag]:
    """A compute pass that binds UAVs nothing afterwards reads (`question`).

    The unit is the *compute pass* the report already derives -- a run of dispatches agreeing on pipeline and
    shaders -- because that is as fine as a bundle can attribute work: the usage chain records *which event*
    a resource was used at, but not which dispatch within a pass did it, and a pass is one dispatch's worth
    of state. The write is evidence rather than inference: a `CS_RWResource` row inside the pass's eid range
    is the engine recording that the resource was bound as a compute UAV there.

    It needs **no descriptor resolution**, and that is a measurement, not a shortcut: on the Android capture
    the bundle carries state documents for 30 of 723 events (the driver writes them at state changes), while
    the *heap contents* a table points at can change between them -- so attributing a write to a register
    through a resolved slot would apply a snapshot to events it was never taken at. The two shipped binding
    rules are safe for the same reason this one is not: they compare things that are constant across a state
    (the signature's range against the heap), while a UAV *target* is not.

    What this adds over `write-never-read` is the work: that detector groups by the kind of last write and
    asks what consumes the result; this one names the pass that did the writing, which is the thing that
    costs time. `question` for the reasons a usage list always has: a later frame reading the result, and a
    CPU readback after the capture, look exactly like nothing ever reading it -- and a UAV bound is not
    necessarily written, so the finding is about a *binding* nothing read afterwards.
    """
    flags: List[RedFlag] = []
    for entry in reconstruct_passes(bundle['events'], bundle['resources']):
        if str(entry.get('kind')) != 'compute':
            continue
        first, last = int(entry['firstEid']), int(entry['lastEid'])
        lines: List[str] = []
        for resource in bundle['resources']:
            chain = _usage_judged(resource)
            if chain is None:
                continue
            bound = [eid for eid, names in chain if 'CS_RWResource' in names and first <= eid <= last]
            if not bound:
                continue
            if any(names & USAGE_READS for eid, names in chain if eid > last):
                continue                                # something after the pass might have read it
            last_use = max(eid for eid, _names in chain)
            at_last = next(names for eid, names in chain if eid == last_use)
            lines.append('%s: bound as a UAV at eid %s, last use eid %d (%s)'
                         % (_resource_label(resource), '/'.join(str(one) for one in bound), last_use,
                            '/'.join(sorted(at_last))))
        if not lines:
            continue
        flags.append(_usage_flag(
            'dead-compute',
            'the compute pass at eid %d..%d binds %d resource(s) as UAVs that nothing after eid %d reads -- '
            'dispatches whose result is never used. A question, not a verdict: a later frame and a CPU '
            'readback after the capture look exactly like nothing ever reading it, and a UAV bound is not '
            'necessarily written' % (first, last, len(lines), last),
            lines))
    return flags

# ---------------------------------------------------------------------------
# The pipeline state a bundle records per state *change*: viewport and scissor,
# depth, stencil, blend. A state document is written when the state changes, so one document describes a
# *range* of events -- measured on the Android capture, 30 documents for 723 events -- and every rule below
# is stated per range for that reason: nothing in the bundle distinguishes two events inside one.
#
# A dispatch is skipped by all of them. The engine reports at a compute event whatever output-merger state
# the last draw left bound, which is the fact the pass reconstruction is built around; judging a dispatch on
# it would report the previous pass's state as if the dispatch had asked for it.
__all__ = [
    'detect_dead_compute',
    'detect_load_instead_of_clear',
    'detect_read_before_write',
    'detect_write_never_read',
]
