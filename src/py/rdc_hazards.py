"""Offline: the conflicts a frame commits against its own resource states and bindings (`hazards`).

`deps` reads the barrier stream to say *who* touches a resource; this module asks the next question of the
same evidence -- whether the touch was permitted. Two classes, both arithmetic over what the stream already
decodes and both device-free:

* **a use its resource's state forbids** -- a read while the resource is left in a write state
  (`RenderTarget`, `UnorderedAccess`, `CopyDest`), a write while it is left in a read one, a depth target
  written while the resource is in `DepthRead`. The state comes from the frame's own transitions; the
  permission table is the one thing here that is not derived from the capture, and it is the Direct3D 12
  rules written out in REFERENCE 4.24.
* **one event using a resource two ways at once** -- bound as a render or depth target while also reachable
  as a shader resource or a UAV through the same root signature's tables, which is the read-write loop; and
  a resource bound as an SRV and a UAV at the same event, which is the same loop through two views.

Both are questions the engine answers only when the *application* ran with the D3D12 debug layer at capture
time, and this corpus is the measured proof that it usually did not: `debug --group` reports 0 messages on
`desktop-1`, and the `d3d12sdklayers` section a capture carries is a copy of the SDK's own DLL -- stored so
*replay* can load a layer (`d3d12_device.cpp`) -- not a log. What the file still has is the barriers
themselves, which are the frame's own statement of what it believes each resource is, and that is what this
reads.

**Both barrier forms are read.** A legacy `List_ResourceBarrier` names the state before and after, and an
enhanced `List_Barrier` names a layout and access bits instead -- a different enum for the same idea, which
`ACCESS_STATES`/`LAYOUT_STATES` translate into the state words the rest of this module reasons in. That
translation is this tool's, not D3D12's, and it is published in REFERENCE 4.24; it is also *why* both forms
can be checked by one permit table instead of two checks that would drift apart. Counting matters: one corpus
capture transitions 91 times through the enhanced form and 12 through the legacy one, so a check that read
only one of them would be answering about a tenth of the frame.

**What it deliberately does not claim.** The table is the lenient reading of the rules, because a finding has
to be a use no permitted promotion explains: `Common` (0) promotes to whatever a use needs, and a read-only
state serves any read (D3D12 promotes read states to each other), so neither is ever reported. A resource the
frame never states a state for -- created in one and used with no barrier at all, which is most of a UE
frame's resources -- is **not** reported either: the use is counted in `unknown` instead, the rule `deps`
follows for a slot the capture never wrote. A resource whose per-subresource transitions disagree with its
whole-resource state is skipped the same way and counted in `ambiguous`: the views this tool knows are
whole-resource, so a claim about one mip of a texture would be a claim the stream does not make.
"""
from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_cache import *  # noqa: F401,F403
from rdc_payloads import *  # noqa: F401,F403
from rdc_resources import *  # noqa: F401,F403
import rdc_cache  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_profile
import rdc_table
import rdc_uses
from rdc_sigcheck import _table_slots
from rdc_uses import STATE_WRITES

from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict

# ---------------------------------------------------------------------------
# The permission table.
#
# Bits are looked up by *name* in `rdc_payloads.RESOURCE_STATES` rather than written as literals, so the one
# table that prints a state is also the one that decides about it: a state renamed there cannot go on being
# checked here under its old number.
# ---------------------------------------------------------------------------
def _bits(*names: str) -> int:
    """The state word made of the named `RESOURCE_STATES` bits."""
    wanted = set(names)
    total = 0
    for bit, label in RESOURCE_STATES:
        if label in wanted:
            total |= bit
    return total

#: The state each use needs. `srv` accepts either shader-resource bit (a read in any stage), `clear` accepts
#: either target state and is narrowed by the call's own chunk name, and anything not listed -- a discard, a
#: barrier -- is not a use this module has a rule for.
STATE_FOR_USE: Dict[str, int] = {
    'vb': _bits('VertexAndConstantBuffer'),
    'cbv': _bits('VertexAndConstantBuffer'),
    'ib': _bits('IndexBuffer'),
    'srv': _bits('NonPixelShaderResource', 'PixelShaderResource'),
    'uav': _bits('UnorderedAccess'),
    'rtv': _bits('RenderTarget'),
    # A depth view is *not* always a write: `D3D12_DSV_FLAG_READ_ONLY_DEPTH` binds the depth buffer for
    # testing without writing it, which is the normal way a frame samples the depth it is also testing
    # against -- and the flag lives in the view's descriptor, which this tool does not decode (only the
    # resource a binding names). So a depth view is permitted by either depth state and reported only when
    # the resource carries neither, which is the case no depth view can use.
    'dsv': _bits('DepthWrite', 'DepthRead'),
    'copy-dst': _bits('CopyDest'),
    'copy-src': _bits('CopySource'),
    'resolve-dst': _bits('ResolveDest'),
    'resolve-src': _bits('ResolveSource'),
    'clear': _bits('RenderTarget', 'DepthWrite'),
}

#: The `how` values that only *read*: a read-only state serves them by promotion. Every other use in
#: `STATE_FOR_USE` writes, and a write needs its own state exactly.
READ_USES = frozenset({'vb', 'cbv', 'ib', 'srv', 'copy-src', 'resolve-src'})

#: A clear's four forms, so the finding can name the state the call itself asked for. A use's `call` is the
#: chunk name with the `List_` prefix already stripped by `_needed_for`, because that is the prefix every
#: chunk has and none of these names carries.
CLEAR_STATES: Dict[str, int] = {
    'ClearRenderTargetView': _bits('RenderTarget'),
    'ClearDepthStencilView': _bits('DepthWrite'),
    'ClearUnorderedAccessViewUint': _bits('UnorderedAccess'),
    'ClearUnorderedAccessViewFloat': _bits('UnorderedAccess'),
}

#: `D3D12_BARRIER_ACCESS` bit -> the state bits that mean the same permission, so an enhanced barrier's
#: access word can be checked by the same table a legacy barrier's `after` word is checked by. The access
#: values are the source tree's own (`dx/official/d3d12.h`); the mapping between the two enums is this
#: tool's and is documented in REFERENCE 4.24. `RAYTRACING_ACCELERATION_STRUCTURE_WRITE` maps to nothing,
#: because no `D3D12_RESOURCE_STATES` bit names an acceleration-structure build -- the same gap
#: `rdc_uses.UNATTRIBUTED_CHUNKS` records for the build chunks themselves.
ACCESS_STATES: Dict[int, int] = {
    0x1: _bits('VertexAndConstantBuffer'),                                  # VERTEX_BUFFER
    0x2: _bits('VertexAndConstantBuffer'),                                  # CONSTANT_BUFFER
    0x4: _bits('IndexBuffer'),                                              # INDEX_BUFFER
    0x8: _bits('RenderTarget'),                                             # RENDER_TARGET
    0x10: _bits('UnorderedAccess'),                                         # UNORDERED_ACCESS
    0x20: _bits('DepthWrite'),                                              # DEPTH_STENCIL_WRITE
    0x40: _bits('DepthRead'),                                               # DEPTH_STENCIL_READ
    0x80: _bits('NonPixelShaderResource', 'PixelShaderResource'),           # SHADER_RESOURCE
    0x100: _bits('StreamOut'),                                              # STREAM_OUTPUT
    0x200: _bits('IndirectArgument'),                                       # INDIRECT_ARGUMENT / PREDICATION
    0x400: _bits('CopyDest'),                                               # COPY_DEST
    0x800: _bits('CopySource'),                                             # COPY_SOURCE
    0x1000: _bits('ResolveDest'),                                           # RESOLVE_DEST
    0x2000: _bits('ResolveSource'),                                         # RESOLVE_SOURCE
    0x4000: _bits('RaytracingAccelerationStructure'),                       # AS_READ
    0x8000: 0,                                                             # AS_WRITE: no state names it
    0x10000: _bits('ShadingRateSource'),                                    # SHADING_RATE_SOURCE
    0x20000: _bits('VideoDecodeRead'),
    0x40000: _bits('VideoDecodeWrite'),
    0x80000: _bits('VideoProcessRead'),
    0x100000: _bits('VideoProcessWrite'),
    0x200000: _bits('VideoEncodeRead'),
    0x400000: _bits('VideoEncodeWrite'),
}

#: `D3D12_BARRIER_LAYOUT` -> the same state bits, for a transition that names no access word at all. Layout
#: 0 is `COMMON`/`PRESENT`, which is the "no claim" this module starts every resource from; `GENERIC_READ` is
#: every read a resource can be given; the rest are one state each.
LAYOUT_STATES: Dict[int, int] = {
    0: 0,
    1: _bits('VertexAndConstantBuffer', 'IndexBuffer', 'NonPixelShaderResource', 'PixelShaderResource',
             'IndirectArgument', 'CopySource', 'ResolveSource', 'DepthRead'),
    2: _bits('RenderTarget'),
    3: _bits('UnorderedAccess'),
    4: _bits('DepthWrite'),
    5: _bits('DepthRead'),
    6: _bits('NonPixelShaderResource', 'PixelShaderResource'),
    7: _bits('CopySource'),
    8: _bits('CopyDest'),
    9: _bits('ResolveSource'),
    10: _bits('ResolveDest'),
    11: _bits('ShadingRateSource'),
}

#: The two kinds of finding, for the table's first column and for the report's detector.
KIND_STATE = 'state'
KIND_LOOP = 'loop'

class HazardFinding(TypedDict):
    """One conflict: what was used, where, and the two sides of the disagreement.

    `state` is what the frame left the resource in and `needed` the state the use required, both as text so
    a finding prints without re-reading the stream. `certainty` is `certain` for a state conflict (the
    arithmetic is exact) and `likely` for a resource bound as an SRV and a UAV at one event, because the two
    views could address different subresources and the stream does not say which.
    """
    kind: str
    certainty: str
    rid: int
    name: str
    eid: int
    call: str
    how: str
    detail: str
    state: str
    needed: str

class HazardScan(TypedDict):
    """What one capture's hazards add up to, and what could not be claimed.

    `unknown` counts the uses whose resource the frame never states a state for and `ambiguous` the
    resources whose per-subresource transitions disagree with their whole-resource state -- both are uses
    deliberately *not* reported, and both are printed, because "no hazard" and "no evidence" are two answers.
    """
    findings: List[HazardFinding]
    uses: int
    unknown: int
    ambiguous: int
    transitions: int
    events: int

def _needed_for(how: str, call: str) -> int:
    """The state bits a use requires, with a clear narrowed by its own chunk name.

    A use's `call` is the chunk name as the stream spells it (`List_ClearRenderTargetView`), so the prefix
    every command-list chunk carries is dropped before the lookup: a table keyed by the bare name is what a
    reader of the documentation would write, and the transform belongs here rather than in the table.
    """
    if how == 'clear':
        return CLEAR_STATES.get(call.replace('List_', ''), STATE_FOR_USE['clear'])
    return STATE_FOR_USE.get(how, 0)

def check_state(how: str, call: str, state: int) -> Optional[Tuple[str, int]]:
    """`(certainty, needed state)` when a use of a resource in this state is one no promotion explains.

    None means permitted: the state is `Common` (which promotes to whatever a use needs), it already carries
    the bit the use requires, or the use only reads while the state only reads -- the lenient rule, because
    D3D12 promotes a read state to another read state, and a finding has to be something no permitted
    promotion explains.
    """
    if not how or state == 0:
        return None
    needed = _needed_for(how, call)
    if needed == 0 or state & needed:
        return None
    if how in READ_USES and not state & STATE_WRITES:
        return None
    return 'certain', needed

#: A transition that names no subresource: `D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES`, and 0 for a buffer,
#: which has exactly one.
WHOLE_RESOURCE = (0, 0xffffffff)

#: The chunks that name a command list, i.e. the ones a per-list state walk has to key: every call a list
#: records. `rdc_uses` reads the same set for the same reason, and a chunk outside it neither binds nor
#: transitions anything.
LIST_CHUNKS = (DRAW_CHUNKS + CLEAR_CHUNKS + COPY_CHUNKS + RESOLVE_CHUNKS + DISCARD_CHUNKS
               + BARRIER_CHUNKS)

def _access_state(access: int, layout: int) -> int:
    """The state an enhanced barrier's access word (or its layout) stands for.

    An access word is a bitmask, so its states are the union of what each bit means; a transition with no
    access bits at all falls back to its layout, which is the coarser statement of the same thing.
    """
    state = 0
    if access:
        for bit, mapped in ACCESS_STATES.items():
            if access & bit:
                state |= mapped
        if state:
            return state
    return LAYOUT_STATES.get(layout, 0)

def _state_track(stream: Buffer, names: Dict[int, str]
                 ) -> Tuple[Dict[Tuple[int, int], List[Tuple[int, int, int]]], Dict[int, int], Set[int],
                            int]:
    """One walk: states per (command list, resource), which list each use belongs to, and a transition count.

    The track is **per command list**, and that is not a detail. A barrier is recorded into the list it is
    recorded in, so a transition in one list says nothing about a resource's state as another list or a later
    submission sees it -- and UE records dozens of command lists from worker threads, so the stream's order is
    the *record* order, not the order the GPU runs them in. Tracking one global state makes that assumption
    silently and reports hundreds of findings that say so (measured: 797 false "vertex buffer in CopyDest"
    rows on `desktop-1` alone, on four long-lived buffers whose transitions are recorded in other lists). A
    use whose list recorded no transition for its resource is counted in `unknown` instead.

    Both barrier forms feed one track, and a `before` word is never used as a seed: a D3D12 resource is
    created *in* a state with no barrier at all, so treating a creation-time state as a transitioned one
    would report a frame against a transition it never made.
    """
    track: Dict[Tuple[int, int], List[Tuple[int, int, int]]] = {}
    lists: Dict[int, int] = {}
    ambiguous: Set[int] = set()
    transitions = 0

    def claim(rid: int, list_id: int, sub: int, after: int, eid: int) -> None:
        """Record one transition's result, and notice when two claims about one resource disagree."""
        rows = track.setdefault((list_id, rid), [])
        if sub in WHOLE_RESOURCE:
            rows.append((eid, 0xffffffff, after))
            if any(other != after for _e, other_sub, other in rows if other_sub != 0xffffffff):
                ambiguous.add(rid)
        else:
            rows.append((eid, sub, after))
            if any(other != after for _e, other_sub, other in rows if other_sub == 0xffffffff):
                ambiguous.add(rid)

    for eid, ch in enumerate(iter_chunks(stream), 1):
        name = names.get(ch['id'], '')
        # every chunk that names a command list opens with its handle (`d3d12_serialise.cpp` writes the list
        # first for a barrier and for every command-list call), which is the key the state walk uses
        if name not in LIST_CHUNKS:
            continue
        blob = chunk_payload(stream, ch)
        if len(blob) < 8:
            continue
        lists[eid] = u64(blob, 0)
        if name not in BARRIER_CHUNKS:
            continue
        list_id = lists.get(eid, 0)
        entries = parse_barriers(blob)
        if entries is not None:
            for entry in entries:
                if entry['kind'] != 'transition' or not entry['resource']:
                    continue
                transitions += 1
                claim(entry['resource'], list_id, int(entry['subresource'] or 0), entry['after'], eid)
            continue
        groups = parse_barrier_groups(blob)
        if groups is None:
            continue
        for group in groups:
            rid = group.get('resource', 0) or 0
            if not rid:
                continue
            transitions += 1
            claim(rid, list_id, group.get('subresource', 0xffffffff),
                  _access_state(group.get('access', 0) or 0, group.get('layout', 0) or 0), eid)
    return track, lists, ambiguous, transitions

def _state_at(track: Dict[Tuple[int, int], List[Tuple[int, int, int]]], rid: int, list_id: int,
              eid: int) -> Optional[int]:
    """The state `rid` was left in *on this command list* before `eid`, or None when that list never says."""
    state: Optional[int] = None
    for row_eid, sub, word in track.get((list_id, rid), ()):
        if row_eid >= eid:
            break
        if sub == 0xffffffff:
            state = word
    return state

def _views_of_event(state: DrawState, compute: bool, sigs: Dict[int, RootSignature],
                    heaps: Dict[int, Dict[int, DescriptorInfo]]) -> List[Tuple[int, str, str]]:
    """`(rid, kind, where)` for every CBV/SRV/UAV the event's own bindings reach.

    Root descriptors name a resource directly; a table is resolved through the heaps *as they are at this
    point in the frame*, which is why the caller applies descriptor writes in stream order rather than
    reading the frame's final heap state -- UE re-uses descriptor memory, and a slot can be an SRV early and
    a UAV later.
    """
    out: List[Tuple[int, str, str]] = []
    roots = ((state['compCbv'], state['compSrv'], state['compUav']) if compute
             else (state['gfxCbv'], state['gfxSrv'], state['gfxUav']))
    for kind, items in zip(('cbv', 'srv', 'uav'), roots):
        for rp, (res, _off) in sorted(items.items()):
            if res:
                out.append((res, kind, 'rp%d' % rp))
    sig_id = state['compSig'] if compute else state['gfxSig']
    sig = sigs.get(sig_id) if sig_id else None
    if sig is not None:
        for rp, (heap, base) in sorted((state['compTable'] if compute
                                        else state['gfxTable']).items()):
            if rp >= len(sig['params']) or sig['params'][rp]['kind'] != 'table':
                continue
            for fact in _table_slots(sig['params'][rp], heap, base, heaps):
                if fact is None or not fact.resource or fact.kind not in ('srv', 'uav'):
                    continue
                out.append((fact.resource, fact.kind, 'rp%d (table %s)' % (rp, fact.kind)))
    return out

def _scan_loops(stream: Buffer, names: Dict[int, str],
                resources: Dict[int, ResourceInfo]) -> List[HazardFinding]:
    """Events that use one resource two ways at once: a target also read, or an SRV also a UAV.

    The target half is a *graphics* question: D3D12 binds render and depth targets through the output-merger
    stage, and a compute dispatch neither sets nor clears them -- a `DrawState` still holds whatever the last
    graphics call bound, so reading it at a dispatch would report a target the dispatch never had.
    """
    sigs = parse_root_signatures(stream, names)
    heaps: Dict[int, Dict[int, DescriptorInfo]] = {}
    states: Dict[int, DrawState] = {}
    out: List[HazardFinding] = []
    for eid, ch in enumerate(iter_chunks(stream), 1):
        name = names.get(ch['id'], '')
        readable = (name in DRAW_CHUNKS or name in STATE_CHUNKS or name in DESCRIPTOR_KINDS
                    or name in DESCRIPTOR_COPY_CHUNKS)
        blob = chunk_payload(stream, ch) if readable else b''
        if name in DESCRIPTOR_KINDS or name in DESCRIPTOR_COPY_CHUNKS:
            apply_descriptor_chunk(name, blob, heaps)
        elif name in DRAW_CHUNKS:
            state = states.get(u64(blob, 0) if len(blob) >= 8 else 0)
            if state is None:
                continue
            compute = name in COMPUTE_CHUNKS
            views = _views_of_event(state, compute, sigs, heaps)
            if not views:
                continue
            reach: Dict[int, List[str]] = {}
            for res, kind, where in views:
                reach.setdefault(res, []).append('%s %s' % (kind, where))
            targets: Set[int] = set()
            if not compute:
                targets = set(state['rtv'])
                if state['dsv']:
                    targets.add(state['dsv'])
            call = name.replace('List_', '')
            for res, wheres in sorted(reach.items()):
                found = resources.get(res, {})
                name_of = found.get('name', '') if found else ''
                if res in targets:
                    out.append(HazardFinding(kind=KIND_LOOP, certainty='certain', rid=res,
                                             name=name_of, eid=eid, call=call,
                                             how='target+%s' % wheres[0].split()[0],
                                             detail=wheres[0],
                                             state='bound as a render or depth target here', needed=''))
                elif any(w.startswith('srv') for w in wheres) and any(w.startswith('uav')
                                                                      for w in wheres):
                    out.append(HazardFinding(kind=KIND_LOOP, certainty='likely', rid=res,
                                             name=name_of, eid=eid, call=call, how='srv+uav',
                                             detail=', '.join(wheres), state='',
                                             needed='both an SRV and a UAV at one event'))
        elif _apply_state_chunk(name, blob, states):
            pass
    return out

@rdc_profile.timed('hazards')
def scan_hazards(path: str) -> Optional[HazardScan]:
    """Every conflict one capture commits, or None when no chunk can be named at all.

    None rather than an empty answer when the chunk-name map is missing (`README.md` §1.1): "no hazards"
    would then be a statement about the tool rather than about the frame, the rule `rdc_sigcheck` and the
    report's file-side detectors already follow.
    """
    names = rdc_chunkmap.load_chunk_names()
    if not names:
        return None
    _info, stream, _how = rdc_cache.load_stream(path)
    resources = parse_resource_table(stream, names)
    track, lists, ambiguous, transitions = _state_track(stream, names)
    ledger = rdc_uses.walk_uses(stream, names, resources)
    findings: List[HazardFinding] = []
    uses = unknown = 0
    for rid, record in sorted(ledger['resources'].items()):
        if rid in ambiguous:
            continue
        found = resources.get(rid, {})
        name_of = found.get('name', '') if found else ''
        for use in record['uses']:
            if use['how'] in ('barrier', 'discard'):
                continue
            uses += 1
            list_id = lists.get(use['eid'], 0)
            state = _state_at(track, rid, list_id, use['eid']) if list_id else None
            if state is None:
                unknown += 1
                continue
            verdict = check_state(use['how'], use['call'], state)
            if verdict is None:
                continue
            findings.append(HazardFinding(kind=KIND_STATE, certainty=verdict[0], rid=rid,
                                          name=name_of, eid=use['eid'],
                                          call=use['call'].replace('List_', ''), how=use['how'],
                                          detail=use['detail'], state=state_text(state),
                                          needed=state_text(verdict[1])))
    findings.extend(_scan_loops(stream, names, resources))
    findings.sort(key=lambda f: (f['rid'], f['eid'], f['kind']))
    return HazardScan(findings=findings, uses=uses, unknown=unknown, ambiguous=len(ambiguous),
                      transitions=transitions, events=ledger['events'])

def _summary(scan: HazardScan) -> str:
    """The one-line summary every output starts with."""
    counts: Dict[str, int] = {}
    for finding in scan['findings']:
        counts[finding['kind']] = counts.get(finding['kind'], 0) + 1
    parts = ', '.join('%d %s' % (counts[kind], kind) for kind in sorted(counts))
    return ('hazards: %d finding(s)%s in %d use(s) of %d event(s); %d transition(s) read, %d use(s) with '
            'no state evidence, %d resource(s) left unclaimed (per-subresource states disagree)'
            % (len(scan['findings']), ' (%s)' % parts if parts else '', scan['uses'], scan['events'],
               scan['transitions'], scan['unknown'], scan['ambiguous']))

def group_hazards(scan: HazardScan) -> List[Dict[str, Any]]:
    """Fold the findings into one row per distinct conflict, keeping its count and its eid range.

    A frame that fights a barrier repeats it every draw -- measured, `desktop-1` reports the same
    vertex-buffer-in-`CopyDest` conflict 167 times across four long-lived buffers -- and a table of 167
    identical rows hides the three that matter. This is the fold `debug --group` applies to validation
    messages, for the same reason. What is folded is the *conflict*: the resource, the kind, the two states
    and the use; the count, the first and the last eid, and the first call stay, so nothing a reader would
    have seen in the unfolded rows is lost except the repetition.
    """
    rows: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for f in scan['findings']:
        key = (f['kind'], f['rid'], f['how'], f['state'], f['needed'])
        row = rows.get(key)
        if row is None:
            rows[key] = {'kind': f['kind'], 'certainty': f['certainty'], 'rid': f['rid'],
                         'name': f['name'], 'how': f['how'], 'state': f['state'],
                         'needed': f['needed'], 'detail': f['detail'], 'call': f['call'],
                         'eid': f['eid'], 'lastEid': f['eid'], 'count': 1}
        else:
            row['count'] += 1
            row['lastEid'] = f['eid']
    return sorted(rows.values(), key=lambda r: (-r['count'], r['rid']))

def cmd_hazards(path: str, limit: int = 40, fmt: str = 'table') -> int:
    """Print the conflicts a frame commits against its own states and bindings, one row per conflict.

    Exit code 1 when a `certain` finding exists and 0 otherwise -- the gate `debug --fail-on` gives a replay
    session, so a sweep can rank frames by it without a reader parsing the table. 2 means the question could
    not be asked at all (no chunk names).
    """
    scan = scan_hazards(path)
    if scan is None:
        print('hazards: no chunk names -- the RenderDoc source tree is missing (README.md 1.1)')
        return 2
    grouped = group_hazards(scan)
    rows: List[Tuple[str, ...]] = []
    for f in grouped[:limit]:
        where = 'res%d%s' % (f['rid'], '[%s]' % f['name'] if f['name'] else '')
        what = ('read as %s while in %s' % (f['how'], f['state'])) if f['kind'] == KIND_STATE \
            else ('%s' % f['needed'] if f['needed'] else f['state'])
        span = '#%d %s' % (f['eid'], f['call'])
        if f['count'] > 1:
            span += ' (x%d to #%d)' % (f['count'], f['lastEid'])
        rows.append((f['kind'], f['certainty'], where, span, what, f['detail']))
    notes = [_summary(scan), '%d distinct conflict(s)' % len(grouped)]
    if len(grouped) > limit:
        notes.append('... %d more' % (len(grouped) - limit))
    if fmt != 'table':
        rdc_table.emit(fmt, ('kind', 'certainty', 'resource', 'where', 'what', 'detail'), rows, notes)
    else:
        for note in notes:
            print(note)
        for row in rows:
            print('%-6s %-8s %-24s %-30s %-40s %s'
                  % (row[0], row[1], row[2][:24], row[3][:30], row[4][:40], row[5]))
    return 1 if any(f['certainty'] == 'certain' for f in scan['findings']) else 0

__all__ = [
    'ACCESS_STATES',
    'CLEAR_STATES',
    'HazardFinding',
    'HazardScan',
    'KIND_LOOP',
    'KIND_STATE',
    'LAYOUT_STATES',
    'READ_USES',
    'STATE_FOR_USE',
    'check_state',
    'cmd_hazards',
    'scan_hazards',
]
