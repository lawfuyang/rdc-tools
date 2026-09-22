"""`vram`: what the frame's memory amounts to by *role*, which pass holds the most of it, and what else.

`memory` (rdc_uses) answers the placement questions -- committed/placed/reserved, lifetimes, aliasing,
what nothing reads. This is the budget question on top of the same ledger: how much of the frame's memory
is render targets, how much is textures, how much is buffers, which pass has the largest working set, and
what would change if a texture were half the size or an MRT were dropped. It adds no extraction of its
own: every figure comes from the use ledger and the resource table, which is why it needs no device
(REFERENCE §4.15).

Three conventions, all of them the report's own, so a number here can be read beside a number there:

* a texture is counted at **4 bytes a pixel**, because a capture records no texture byte count, and every
  figure that contains one is marked `~` (`rdc_uses.resource_bytes`);
* a figure is over the resources the frame **references**, not over everything the capture creates: an
  unused allocation is `memory`'s business, and adding it to a budget would charge the frame for it;
* "the widest pass" is the pass with the largest **peak live bytes** inside it -- the resources whose use
  windows overlap that pass, summed at the moment the most of them are live -- and the pass ranges come
  from the file's own markers (`rdc_passdiff.marker_passes`), in chunk indices, not the engine's event
  ids.
"""
from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
from rdc_uses import (UseTally as UseTally, _deps_ledger as _deps_ledger,
                      _group_bytes as _group_bytes, _peak_live as _peak_live,
                      _size_text as _size_text, resource_bytes as resource_bytes, tally as tally)
from rdc_passdiff import MarkerPass as MarkerPass, marker_passes as marker_passes
from rdc_stream import FrameError as FrameError
import rdc_passdiff  # noqa: F401  (used qualified: the pass list is read inside the command)
import rdc_profile
import rdc_table  # noqa: F401  (the csv/markdown shapes)

from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

#: The order roles are reported in: the two the pixels are made of first, then the data, then whatever
#: else has a size. A role with no resources is left out rather than printed as a zero row.
ROLE_ORDER: Tuple[str, ...] = ('render target', 'texture', 'buffer', 'acceleration structure', 'other')

#: What a resource is *for*, from the uses the ledger recorded: a render target or depth target is what
#: the frame renders into, everything else is classified by kind. A resource that is both (a texture
#: written as a target and read as an SRV) is a render target: that is the role it plays in the frame's
#: memory, and `deps` is where the double life is visible.
TARGET_USES = ('rtv', 'dsv')


class Role(NamedTuple):
    """One row of the budget: what a resource is for, how many, and how many bytes."""

    role: str
    resources: int
    bytes: int
    estimated: bool


class PassPeak(NamedTuple):
    """One pass's working set: what it touches, and the most of it that is live at one moment."""

    path: str
    first_chunk: int
    last_chunk: int
    resources: int
    touched: int
    touched_estimated: bool
    peak: int
    peak_at: int
    peak_estimated: bool


def roles(ledger: UseLedger, resources: Dict[int, ResourceInfo],
          rows: Dict[int, UseTally]) -> List[Role]:
    """The referenced resources by role, biggest role first, in `ROLE_ORDER` within a size.

    A resource with no use has no role: it is not part of the frame's working set, and the two things
    that *are* worth saying about it (never read, never touched) are `memory`'s.
    """
    grouped: Dict[str, List[int]] = {}
    for rid in rows:
        if not rows[rid]['first']:
            continue
        info = resources.get(rid)
        kind = info['kind'] if info is not None else 'unknown'
        if any(use['how'] in TARGET_USES for use in ledger['resources'][rid]['uses']):
            role = 'render target'
        elif kind.startswith('texture'):
            role = 'texture'
        elif kind == 'buffer':
            role = 'buffer'
        elif kind in ('blas', 'tlas'):
            role = 'acceleration structure'
        elif kind == 'unknown':
            continue                    # no descriptor: nothing is known about its size or its role
        else:
            role = 'other'
        grouped.setdefault(role, []).append(rid)
    out: List[Role] = []
    for role in ROLE_ORDER:
        group = grouped.get(role)
        if not group:
            continue
        total, estimated = _group_bytes(group, resources)
        out.append(Role(role=role, resources=len(group), bytes=total, estimated=estimated))
    out.sort(key=lambda entry: -entry.bytes)
    return out


def pass_peaks(ledger: UseLedger, resources: Dict[int, ResourceInfo], rows: Dict[int, UseTally],
               passes: Sequence[MarkerPass], limit: int = 0) -> List[PassPeak]:
    """Every pass that touches something, widest first: the peak live bytes inside its own range.

    A pass's *working set* is the resources whose use window overlaps it and the *peak* is the most of
    those bytes alive at one moment in it (`_peak_live`, the same sweep `memory` uses for a heap, with
    the windows clipped to the pass so a resource that outlives the pass is not charged to it twice).
    Only resources with a use are counted: a static texture the frame samples inside the pass is in it,
    and an allocation nothing touches is not part of any working set.
    """
    out: List[PassPeak] = []
    for entry in passes:
        windows: List[Tuple[int, int, int]] = []
        touched: List[int] = []
        for rid, row in rows.items():
            if not row['first'] or row['last'] < entry.first_chunk or row['first'] > entry.last_chunk:
                continue
            size = resource_bytes(resources.get(rid))[0]
            if not size:
                continue
            touched.append(rid)
            windows.append((max(row['first'], entry.first_chunk),
                            min(row['last'], entry.last_chunk), size))
        if not windows:
            continue
        peak, at = _peak_live(windows, entry.last_chunk)
        total, estimated = _group_bytes(touched, resources)
        out.append(PassPeak(path=entry.path, first_chunk=entry.first_chunk,
                            last_chunk=entry.last_chunk, resources=len(touched), touched=total,
                            touched_estimated=estimated, peak=peak, peak_at=at,
                            peak_estimated=estimated))
    out.sort(key=lambda item: (-item.peak, item.path))
    return out[:limit] if limit else out


def what_if(rows_of_roles: Sequence[Role], resources: Dict[int, ResourceInfo],
            drops: Sequence[str]) -> List[Tuple[str, int, bool, str]]:
    """`(scenario, bytes, estimated, note)` for the budget now, halved, and with names dropped.

    The two arithmetic cases the budget exists for: a render target or a texture at half resolution is a
    quarter of its bytes (both are counted by area), a buffer is not; and dropping the resources whose
    *name* matches a filter -- the MRT a pass could stop writing, a debug buffer -- is a subtraction.
    Nothing here decides whether either is *legal*: that is the frame's business, and the numbers are
    what the decision is made with.
    """
    total = sum(entry.bytes for entry in rows_of_roles)
    estimated = any(entry.estimated for entry in rows_of_roles)
    halved = sum(entry.bytes if entry.role in ('buffer', 'acceleration structure') else entry.bytes // 4
                 for entry in rows_of_roles)
    out: List[Tuple[str, int, bool, str]] = [
        ('now', total, estimated, '%d role(s)' % len(rows_of_roles)),
        ('at half resolution', halved, estimated,
         'render targets and textures scale by area (a quarter of their bytes); buffers and '
         'acceleration structures do not'),
    ]
    for pattern in drops:
        matched = [rid for rid, info in resources.items()
                   if info['kind'] != 'unknown' and pattern.lower() in info['name'].lower()]
        saved, estimated_here = _group_bytes(matched, resources)
        out.append(('with "%s" dropped' % pattern, total - saved, estimated or estimated_here,
                    '%d resource(s) match, %s' % (len(matched), _size_text(saved, estimated_here))))
    return out


@rdc_profile.timed('vram')
def cmd_vram(path: str, limit: int = 8, drops: Sequence[str] = (), fmt: str = 'table') -> None:
    """`vram <rdc> [maxPasses] [--drop <nameFilter>]` -- the budget, the widest pass, and the what-if.

    Reads the use ledger and the resource table, groups what the frame references into roles, finds the
    pass with the largest peak inside it, and does the two arithmetic cases the roadmap asks for: this
    frame at half resolution, and this frame with the resources a name filter matches taken out. What
    it cannot say is whether either change is *legal* (a target's format, a pass's dependencies), and
    which of the bytes nothing reads -- `deps` and `memory` answer those, and the last section says so.
    """
    ledger, resources, _how = _deps_ledger(path)
    rows = {rid: tally(rid, record) for rid, record in ledger['resources'].items()}
    grouping = roles(ledger, resources, rows)
    total = sum(entry.bytes for entry in grouping)
    notes: List[str] = ['budget: %d resource(s) referenced, %d created by this capture, %d chunk(s) '
                        'walked' % (len(ledger['resources']),
                                    sum(1 for record in ledger['resources'].values() if record['created']),
                                    sum(ledger['seen'].values()))]
    row_items: List[Tuple[object, ...]] = [
        ('role', entry.role, '%d resource(s), %s, %d%%'
         % (entry.resources, _size_text(entry.bytes, entry.estimated),
            round(100.0 * entry.bytes / total) if total else 0)) for entry in grouping]

    heap_total = sum(ledger['heaps'].values())
    if ledger['heaps']:
        notes.append('heaps: %d created by the capture, %s in total -- placed resources live inside '
                     'them and are counted again in the roles above, because a budget wants what the '
                     'frame uses and `memory` is where the placement is unpacked'
                     % (len(ledger['heaps']), _size_text(heap_total, False)))

    passes: Optional[List[MarkerPass]] = None
    try:
        passes = rdc_passdiff.marker_passes(path)
    except FrameError:
        passes = None
    peaks: List[PassPeak] = []
    if passes is None:
        notes.append('passes: not read (no chunk-name map, README §1.1), so the widest pass is not '
                     'known')
    else:
        peaks = pass_peaks(ledger, resources, rows, passes)
        for entry in peaks[:limit]:
            row_items.append(('pass', '%s (#%d..#%d)' % (entry.path, entry.first_chunk,
                                                         entry.last_chunk),
                              '%d resource(s) touched, %s; peak %s at #%d'
                              % (entry.resources, _size_text(entry.touched, entry.touched_estimated),
                                 _size_text(entry.peak, entry.peak_estimated), entry.peak_at)))
        if not peaks:
            notes.append('passes: none of the %d marker pass(es) in this capture touches a sized '
                         'resource' % len(passes))

    for scenario, size, estimated, note in what_if(grouping, resources, drops):
        row_items.append(('what-if', scenario, '%s -- %s' % (_size_text(size, estimated), note)))

    notes.append('a `~` marks a figure containing a texture counted at 4 bytes a pixel (a capture '
                 'records no texture byte count); the base level only, so a mip chain would add about a '
                 'third')
    notes.append('what this cannot say: whether a change is legal (a target\'s format, a pass\'s '
                 'dependencies), and which bytes nothing reads -- `deps` and `memory` answer those '
                 '(REFERENCE §4.15)')

    if fmt != 'table':
        rdc_table.emit(fmt, ('section', 'name', 'value'), row_items, notes)
        return
    print(notes[0])
    print('by role')
    for entry in grouping:
        share = round(100.0 * entry.bytes / total) if total else 0
        print('  %-22s %4d  %-11s %3d%%' % (entry.role, entry.resources,
                                            _size_text(entry.bytes, entry.estimated), share))
    if not grouping:
        print('  (nothing the frame references has a known size)')
    if ledger['heaps']:
        print(notes[1])
    print('widest pass')
    if passes is None:
        print('  not read: no chunk-name map (README §1.1)')
    elif not peaks:
        print('  none of the %d marker pass(es) in this capture touches a sized resource' % len(passes))
    else:
        for entry in peaks[:limit]:
            print('  %-44s %s' % (_clip(entry.path, 44),
                                  '%d resource(s), %s touched, peak %s at #%d'
                                  % (entry.resources, _size_text(entry.touched, entry.touched_estimated),
                                     _size_text(entry.peak, entry.peak_estimated), entry.peak_at)))
        if len(peaks) > limit:
            print('  ... %d more pass(es) with a smaller peak (maxPasses=0 shows all)' % (len(peaks) - limit))
    print('what-if')
    for scenario, size, estimated, note in what_if(grouping, resources, drops):
        print('  %-24s %-11s %s' % (scenario, _size_text(size, estimated), note))
    print('what this cannot say')
    print('  whether a change is legal: a target\'s format, a pass\'s dependencies and the aliasing')
    print('    requirements a capture does not record are not in this arithmetic')
    print('  which bytes nothing reads -- `deps` (write-never-read) and `memory` (never read) answer')
    print('    that; this counts what the frame uses, not what it needs')


def _clip(text: str, width: int) -> str:
    """`text` at most `width` characters, with `...` when it was cut (a marker path can be long)."""
    return text if len(text) <= width else text[:width - 3] + '...'


__all__ = [
    'ROLE_ORDER',
    'TARGET_USES',
    'PassPeak',
    'Role',
    '_clip',
    'cmd_vram',
    'pass_peaks',
    'roles',
    'what_if',
]
