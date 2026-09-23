"""Offline: who writes what and who reads it (`deps`), and what the frame's memory amounts to (`memory`).

`rdc_detect_usage` asks the same questions of a replay driver's *bundle* -- the engine's own record of
which events touched each resource. This module is the file-side twin: one walk of the chunk stream,
no device and no GPU, with the same two names for the same two findings (`read-before-write`,
`write-never-read`) so a result from either side can be read beside the other.

The evidence a stream has that a bundle does not is the **barriers**. A `List_ResourceBarrier`
transition names the use a resource is being declared for (`RenderTarget`, `UnorderedAccess`,
`CopyDest`), which is how a resource no draw in the frame binds -- a copy destination, a clear-only
target, an upload buffer -- gets a producer at all; a `List_Barrier` texture barrier names an access
bitmask and can carry the discard flag; and an aliasing barrier is the frame saying two resources
share one piece of memory, which is what `memory` reads its aliasing section from.

What the stream does *not* have is said everywhere it matters: nothing records a release (D3D12
writes no destruction chunk, so a lifetime here ends at the frame's last use), a texture's byte count
is not in the file (the report's pixels x 4 estimate is used and every figure that contains one is
marked `~`), a resource sub-allocated out of a UE page is named after the page rather than after
itself (REFERENCE 4.9), and a handful of chunks that reference resources are not decoded as uses at
all (`rdc_chunkmap.UNATTRIBUTED_CHUNKS`) -- both commands print the ones their capture actually
contains, because "nothing read it" and "nothing this tool can see read it" are different sentences.
"""
from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_cache import *  # noqa: F401,F403
from rdc_payloads import *  # noqa: F401,F403
from rdc_resources import *  # noqa: F401,F403
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_resources  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_profile

from typing import Dict, List, Optional, Sequence, Tuple, TypedDict

# ---------------------------------------------------------------------------
# What a binding, a barrier or a copy means for the resource it names.
#
# The two bit tables are the *classification* half of the enums `rdc_payloads` prints: the same
# `D3D12_RESOURCE_STATES` / `D3D12_BARRIER_ACCESS` words, split into the bits that mean the resource
# is written and the bits that mean it is read. `UNORDERED_ACCESS` is in both -- a UAV may read and
# write, which is the reading the report gives a bundle's `CS_RWResource` row -- and `Common` (0) is
# in neither, so a transition to or from it records no use at all.
# ---------------------------------------------------------------------------
#: `D3D12_RESOURCE_STATES` bits that write: RenderTarget, UnorderedAccess, DepthWrite, StreamOut,
#: CopyDest, ResolveDest and the video writes.
STATE_WRITES = 0x4 | 0x8 | 0x10 | 0x100 | 0x400 | 0x1000 | 0x20000 | 0x80000 | 0x800000

#: `D3D12_RESOURCE_STATES` bits that read: vertex/constant and index buffers, `UnorderedAccess` (a
#: UAV may be read as well as written, which is why it is in both tables), DepthRead, the shader
#: resource and indirect/predication states, CopySource, ResolveSource, the video reads, a shading
#: rate source, and `RAYTRACING_ACCELERATION_STRUCTURE` (an acceleration structure is read by the
#: dispatch that traces it; the *build* that writes it is not decoded -- see `UNATTRIBUTED_CHUNKS`).
STATE_READS = (0x1 | 0x2 | 0x8 | 0x20 | 0x40 | 0x80 | 0x200 | 0x800 | 0x2000 | 0x10000 | 0x40000
               | 0x200000 | 0x400000 | 0x1000000)

#: `D3D12_BARRIER_ACCESS` bits that write (the 1.7-era form, same reading of `UnorderedAccess`).
ACCESS_WRITES = 0x8 | 0x10 | 0x20 | 0x100 | 0x400 | 0x1000 | 0x8000 | 0x40000 | 0x100000 | 0x400000

#: `D3D12_BARRIER_ACCESS` bits that read, `UnorderedAccess` included. `NO_ACCESS` (0x80000000) is in
#: neither, exactly like `Common` (0) in the state tables.
ACCESS_READS = (0x1 | 0x2 | 0x4 | 0x10 | 0x40 | 0x80 | 0x200 | 0x800 | 0x2000 | 0x4000 | 0x10000
                | 0x20000 | 0x80000 | 0x200000)

#: `D3D12_BARRIER_LAYOUT` values that write, used only when a texture barrier names no access bits at
#: all -- the layout is the coarser statement of the same thing (RenderTarget, UnorderedAccess,
#: DepthStencilWrite, CopyDest, ResolveDest).
LAYOUT_WRITES = frozenset({2, 3, 4, 8, 10})

#: `D3D12_BARRIER_LAYOUT` values that read (GenericRead, DepthStencilRead, ShaderResource, CopySource,
#: ResolveSource, ShadingRateSource).
LAYOUT_READS = frozenset({1, 5, 6, 7, 9, 11})

#: What a descriptor's kind means for the resource it names. A render target or depth view is written
#: by the draw that binds it -- `OMSetRenderTargets` is where D3D12 binds one, and RenderDoc's own
#: frame marking calls it a partial write (`d3d12_command_list_wrap.cpp`). A sampler names nothing.
ACCESS_BY_KIND: Dict[str, str] = {'cbv': 'read', 'srv': 'read', 'uav': 'read+write', 'rtv': 'write',
                                  'dsv': 'write'}

def _access_of(write_bits: int, read_bits: int, word: int) -> str:
    """`read` / `write` / `read+write` for one state or access word, or '' when it names neither."""
    writing = bool(word & write_bits)
    reading = bool(word & read_bits)
    if writing and reading:
        return 'read+write'
    if writing:
        return 'write'
    if reading:
        return 'read'
    return ''

def _use(ledger: UseLedger, rid: int, eid: int, access: str, how: str, call: str,
         detail: str = '') -> None:
    """Record one use of `rid`; a use of resource 0 (a NULL binding) records nothing."""
    if not rid or not access:
        return
    record = ledger['resources'].get(rid)
    if record is None:
        record = ResourceUse(created=0, placement='external', heap=0, offset=0, uses=[])
        ledger['resources'][rid] = record
    record['uses'].append(UseInfo(eid=eid, access=access, how=how, call=call, detail=detail))

def _va_resource(resources: Dict[int, ResourceInfo], va: int) -> int:
    """The buffer a GPU virtual address falls in, or 0 when it falls in none.

    A CBV *descriptor* does not carry a resource id: its payload holds a `D3D12_GPU_VIRTUAL_ADDRESS`
    (`D3D12Descriptor::Init(const D3D12_CONSTANT_BUFFER_VIEW_DESC*)` in d3d12_manager.cpp stores no
    resource at all), so a descriptor-table slot holding a CBV can only be resolved through the
    resource table's `gpuAddress` -- the base VA every buffer creation payload ends with. A VA that
    falls in no known buffer resolves to 0 rather than to a guess.
    """
    for rid, info in resources.items():
        base = info['gpuAddress']
        if base and info['size'] and base <= va < base + info['size']:
            return rid
    return 0

def _bind_uses(ledger: UseLedger, eid: int, call: str, state: DrawState, compute: bool,
               heaps: Dict[int, Dict[int, DescriptorInfo]],
               resources: Dict[int, ResourceInfo]) -> None:
    """Record the uses one draw's or dispatch's bindings are.

    The classification is by what is bound, not by the call: a root or table CBV/SRV is a read, a
    UAV -- root or through a table -- is a read *and* a write, a vertex or index buffer is a read, and
    a bound render target is a write. A table slot the capture never wrote resolves to nothing and is
    counted in `ledger['unresolved']`, which is the same rule `draws` follows: a slot the frame never
    wrote cannot be named (`parse_descriptor_heaps` records only written ones).
    """
    for kind, roots in (('cbv', state['compCbv'] if compute else state['gfxCbv']),
                        ('srv', state['compSrv'] if compute else state['gfxSrv']),
                        ('uav', state['compUav'] if compute else state['gfxUav'])):
        for rp, (res, _off) in sorted(roots.items()):
            _use(ledger, res, eid, ACCESS_BY_KIND[kind], kind, call, 'rp%d' % rp)
    for rp, (heap, index) in sorted((state['compTable'] if compute else state['gfxTable']).items()):
        info = heaps.get(heap, {}).get(index)
        if info is None:
            ledger['unresolved'] += 1
            continue
        if info['kind'] == 'sampler':
            continue
        res = info['resource']
        if info['kind'] == 'cbv':
            res = _va_resource(resources, res)      # a CBV slot holds a VA, not an id
        _use(ledger, res, eid, ACCESS_BY_KIND[info['kind']], info['kind'], call,
             'rp%d (table %s)' % (rp, info['kind']))
    if compute:
        return
    for _slot, view in sorted(state['vbs'].items()):
        _use(ledger, view[0], eid, 'read', 'vb', call, 'slot %d' % _slot)
    if state['ib'] is not None:
        _use(ledger, state['ib'][0], eid, 'read', 'ib', call)
    for res in state['rtv']:
        _use(ledger, res, eid, 'write', 'rtv', call)
    _use(ledger, state['dsv'], eid, 'write', 'dsv', call)

def _barrier_uses(ledger: UseLedger, eid: int, name: str, blob: Buffer) -> bool:
    """Record the uses one barrier payload is about; False when it did not parse cleanly."""
    if name == 'List_ResourceBarrier':
        entries = parse_barriers(blob)
        if entries is None:
            return False
        for e in entries:
            if e['kind'] == 'aliasing':
                ledger['aliases'].append((eid, e['resource'], e['resource2']))
                continue
            if e['kind'] == 'uav':
                # a UAV barrier says the resource's writes are finished and readable; it is not a use
                # of either kind, so nothing is recorded for it beyond the payload having parsed
                continue
            access = _access_of(STATE_WRITES, STATE_READS, e['after'])
            _use(ledger, e['resource'], eid, access, 'barrier', name,
                 '%s -> %s' % (state_text(e['before']), state_text(e['after'])))
        return True
    groups = parse_barrier_groups(blob)
    if groups is None:
        return False
    for g in groups:
        access = _access_of(ACCESS_WRITES, ACCESS_READS, g['access'])
        if not access and g['kind'] == 'texture':
            # no access bits at all: the layout is the coarser statement of the same thing
            if g['layout'] in LAYOUT_WRITES:
                access = 'write'
            elif g['layout'] in LAYOUT_READS:
                access = 'read'
        detail = access_text(g['access'])
        if g['kind'] == 'texture':
            detail += ' (layout %s)' % BARRIER_LAYOUTS.get(g['layout'], '0x%x' % g['layout'])
        _use(ledger, g['resource'], eid, access, 'barrier', name, detail)
        if g['kind'] == 'texture' and g['flags'] & TEXTURE_BARRIER_DISCARD:
            _use(ledger, g['resource'], eid, 'discard', 'discard', name, 'barrier discard')
    return True

@rdc_profile.timed('use ledger')
def walk_uses(stream: Buffer, names: Optional[Dict[int, str]] = None,
              resources: Optional[Dict[int, ResourceInfo]] = None,
              heaps: Optional[Dict[int, Dict[int, DescriptorInfo]]] = None) -> UseLedger:
    """Walk the chunk stream once and record every resource use it shows.

    What counts as a use: the bindings in force at each draw or dispatch (root CBV/SRV/UAV, root
    descriptor tables resolved through the heaps, vertex and index buffers, the render targets
    `OMSetRenderTargets` bound), a clear, a discard, a copy's two sides, and the barriers -- a
    transition records what the resource is declared to be used for next, which is the only evidence
    the stream has for a resource no draw binds. What does not: creating a resource, writing a
    descriptor into a heap (`Device_Create*View` names a resource but does not use it), naming one,
    or setting state that binds nothing.

    `List_ExecuteIndirect` can be a draw or a dispatch and the stream does not say which, so it
    contributes *both* namespaces' bindings -- the same choice `draws` makes when it prints them
    both. A payload that does not parse cleanly is counted in `failed` and contributes nothing: a
    half-read barrier would become a claim.
    """
    if names is None:
        names = rdc_chunkmap.load_chunk_names()
    if resources is None:
        resources = rdc_resources.parse_resource_table(stream, names)
    if heaps is None:
        heaps = rdc_resources.parse_descriptor_heaps(stream, names)
    ledger: UseLedger = UseLedger(resources={}, aliases=[], heaps={}, seen={}, events=0, failed={},
                                  unresolved=0)
    states: Dict[int, DrawState] = {}
    readable = (DRAW_CHUNKS + TARGET_CHUNKS + STATE_CHUNKS + CLEAR_CHUNKS + DISCARD_CHUNKS
                + COPY_CHUNKS + RESOLVE_CHUNKS + BARRIER_CHUNKS + HEAP_CHUNKS + ('CreateAS',)
                + tuple(RESOURCE_CHUNKS))
    for eid, ch in enumerate(iter_chunks(stream), 1):
        name = names.get(ch['id'], '')
        ledger['seen'][name] = ledger['seen'].get(name, 0) + 1
        blob: Buffer = chunk_payload(stream, ch) if name in readable else b''
        if name in RESOURCE_CHUNKS:
            parsed = _parse_resource(blob, RESOURCE_CHUNKS[name])
            if parsed is not None:
                rid = u64(blob, len(blob) - 16)
                record = ledger['resources'].setdefault(
                    rid, ResourceUse(created=eid, placement='', heap=0, offset=0, uses=[]))
                record['created'] = eid
                record['placement'] = ('placed' if name.startswith('Device_CreatePlaced')
                                       else 'reserved' if name.startswith('Device_CreateReserved')
                                       else 'committed')
                if record['placement'] == 'placed' and len(blob) >= 16:
                    record['heap'], record['offset'] = u64(blob, 0), u64(blob, 8)
        elif name == 'CreateAS':
            parsed = _parse_acceleration_structure(blob)
            if parsed is not None:
                ledger['resources'].setdefault(
                    parsed[0], ResourceUse(created=eid, placement='external', heap=0, offset=0,
                                           uses=[]))
        elif name in HEAP_CHUNKS and len(blob) >= 16:
            ledger['heaps'][u64(blob, len(blob) - 8)] = u64(blob, 0)
        elif name in DRAW_CHUNKS:
            ledger['events'] += 1
            st = states.get(u64(blob, 0)) if len(blob) >= 8 else None
            if st is not None:
                _bind_uses(ledger, eid, name, st, name in COMPUTE_CHUNKS, heaps, resources)
                if name == 'List_ExecuteIndirect':
                    _bind_uses(ledger, eid, name, st, True, heaps, resources)
        elif name in CLEAR_CHUNKS:
            _use(ledger, clear_target(blob), eid, 'write', 'clear', name)
        elif name in DISCARD_CHUNKS:
            _use(ledger, discard_target(blob), eid, 'discard', 'discard', name)
        elif name in COPY_CHUNKS:
            pair = copy_pair(name, blob)
            if pair is None:
                ledger['failed'][name] = ledger['failed'].get(name, 0) + 1
            else:
                size = ' bytes=%d' % u64(blob, 40) if name == 'List_CopyBufferRegion' else ''
                _use(ledger, pair[0], eid, 'write', 'copy-dst', name, 'from res%d%s' % (pair[1], size))
                _use(ledger, pair[1], eid, 'read', 'copy-src', name, 'to res%d%s' % (pair[0], size))
        elif name in RESOLVE_CHUNKS:
            resolved = parse_resolve(name, blob)
            if resolved is None:
                ledger['failed'][name] = ledger['failed'].get(name, 0) + 1
            else:
                # Both rows name the *other* side's subresource, because that is the question a resolve
                # raises: which slice of the multisampled texture was resolved into which of the
                # single-sample one. Reading a resolve as a write-only would lose half of that, and
                # reading it as nothing at all -- what happened before this decoder existed -- made a
                # resource only ever resolved look unused in `deps`.
                _use(ledger, resolved['destination'], eid, 'write', 'resolve-dst', name,
                     'from res%d subresource %d' % (resolved['source'],
                                                    resolved['sourceSubresource']))
                _use(ledger, resolved['source'], eid, 'read', 'resolve-src', name,
                     'to res%d subresource %d' % (resolved['destination'],
                                                  resolved['destinationSubresource']))
        elif name in BARRIER_CHUNKS:
            if not _barrier_uses(ledger, eid, name, blob):
                ledger['failed'][name] = ledger['failed'].get(name, 0) + 1
        elif _apply_state_chunk(name, blob, states):
            pass                        # a tracked setter: it binds, and the draw that uses it records
    for record in ledger['resources'].values():
        record['uses'].sort(key=lambda u: u['eid'])
    return ledger

# ---------------------------------------------------------------------------
# Reading a ledger: the two questions both commands ask of it.
# ---------------------------------------------------------------------------
class UseTally(TypedDict):
    """What one resource's use list adds up to (`tally`).

    `writes`/`reads`/`discards` are the *uses*, not counts, so a caller can print the evidence for a
    finding; a `read+write` use is in both lists, which is what a UAV is. `first`/`last` are its
    first and last use of any kind, or 0 when it has none.
    """
    rid: int
    writes: List[UseInfo]
    reads: List[UseInfo]
    discards: List[UseInfo]
    first: int
    last: int

def tally(rid: int, record: ResourceUse) -> UseTally:
    """Add one resource's use list up."""
    uses = record['uses']
    return UseTally(rid=rid,
                    writes=[u for u in uses if u['access'] in ('write', 'read+write')],
                    reads=[u for u in uses if u['access'] in ('read', 'read+write')],
                    discards=[u for u in uses if u['access'] == 'discard'],
                    first=uses[0]['eid'] if uses else 0,
                    last=uses[-1]['eid'] if uses else 0)

def read_before_write(count: UseTally) -> Optional[Tuple[UseInfo, int]]:
    """`(first read, first write or 0)` when a resource's first use is a read nothing writes first.

    "First" is a comparison of event ids and a write at the *same* event does not clear the read --
    one call can write and read (a UAV is in both lists for exactly that reason), so only a strictly
    earlier write clears it. This is the offline form of the bundle detector of the same name, and it
    is a question for the same reason: a static resource, a call outside the capture and a producer
    before the capture's first event all read the same way.
    """
    if not count['reads']:
        return None
    first_read = count['reads'][0]['eid']
    if any(w['eid'] <= first_read for w in count['writes']):
        return None
    return count['reads'][0], count['writes'][0]['eid'] if count['writes'] else 0

def write_never_read(count: UseTally) -> Optional[Tuple[UseInfo, List[UseInfo]]]:
    """`(last write, discards after it)` when nothing after that write reads the resource.

    "Afterwards" is strict: only a read at a strictly later event counts, or a copy's own source row
    would read as a consumer of what its destination just wrote. The discards come back with the
    finding because they *explain* it -- a target the frame throws away after writing it is a
    deliberate "these contents are no longer needed", which is not the same claim as a write nothing
    consumed -- so the table prints that case in its own words.
    """
    if not count['writes']:
        return None
    last_write = count['writes'][-1]['eid']
    if any(r['eid'] > last_write for r in count['reads']):
        return None
    return count['writes'][-1], [d for d in count['discards'] if d['eid'] > last_write]

def dep_flags(count: UseTally) -> List[str]:
    """The words the `deps` table prints for one resource."""
    flags: List[str] = []
    if read_before_write(count) is not None:
        flags.append('read-before-write')
    dead = write_never_read(count)
    if dead is not None:
        flags.append('discarded' if dead[1] else 'write-never-read')
    return flags

def resource_bytes(info: Optional[ResourceInfo]) -> Tuple[int, bool]:
    """`(bytes, estimated)` for a resource: a buffer's own size, a texture's pixels x 4.

    A capture records no texture byte count, so the report's convention applies here too: a texture
    is counted at 4 bytes a pixel and any figure containing one is marked `~`. The count is the base
    level only (the mip chain would add about a third more); an id with no descriptor -- a heap, a
    queue, a PSO -- is 0 bytes rather than a guess.
    """
    if info is None or info['kind'] == 'unknown':
        return 0, False
    if info['kind'] in ('buffer', 'blas', 'tlas'):
        return info['size'], False
    pixels = max(info['width'], 1) * max(info['height'], 1) * max(info['depth'], 1)
    return pixels * 4, True

def _label(resources: Dict[int, ResourceInfo], rid: int) -> str:
    """`res664 [SceneColor]` -- the id everything prints, plus the capture's name when it has one."""
    return 'res%d%s' % (rid, rdc_resources._name_suffix(resources, rid, 40))

def _size_text(total: int, estimated: bool, formats: Optional[Dict[int, str]] = None) -> str:
    """`940.6 MB`, with a `~` when the figure contains an estimated texture size."""
    mb = total / 1048576.0
    if estimated:
        return '~%.1f MB' % mb
    return '%.1f MB' % mb

# ---------------------------------------------------------------------------
# `deps`: the table, and the two graphs.
# ---------------------------------------------------------------------------
def _escape(text: str) -> str:
    """A label safe inside the quotes of a DOT or Mermaid node: no quotes, no backslashes."""
    return text.replace('\\', '/').replace('"', "'")

def _rows(ledger: UseLedger) -> List[UseTally]:
    """Every resource the walk saw a use for, busiest first (writes + reads, then id)."""
    rows = [tally(rid, record) for rid, record in ledger['resources'].items() if record['uses']]
    rows.sort(key=lambda t: (-(len(t['writes']) + len(t['reads'])), t['rid']))
    return rows

def _unattributed(ledger: UseLedger) -> str:
    """`List_ResolveQueryData x42, ...` -- the resource-referencing chunks this walk does not decode."""
    parts = ['%s x%d' % (name, ledger['seen'][name]) for name in sorted(ledger['seen'])
             if name in UNATTRIBUTED_CHUNKS]
    return ', '.join(parts)

def cmd_deps(path: str, limit: int = 40, fmt: str = 'table') -> None:
    """Who writes what and who reads it, from the capture's own stream.

    `deps <rdc> [maxResources] [table|dot|mermaid]`: one row per resource the stream shows a use for
    -- how many writes and reads, the first and last event, and the flags -- followed by the evidence
    for every flagged resource. The graph forms print the same thing as a bipartite graph: an event
    that wrote is a node with a `w` id, an event that read is one with an `r` id, a resource is a box,
    and the edge is the binding. A resource that is only read has no producer to connect from and is
    left out of the graph (the table still counts it); a UAV is both a producer and a consumer and is
    drawn as both, because it is.

    Event ids are chunk indices -- the numbering `draws` prints, not the engine's event ids, which a
    file read cannot know. The two findings are named after the bundle detectors that ask the same
    questions (`read-before-write`, `write-never-read`); what this command adds is the barriers, and
    what it cannot see is printed under them.
    """
    ledger, resources, _how = _deps_ledger(path)
    rows = _rows(ledger)
    if fmt in ('dot', 'mermaid'):
        for line in _graph(rows, resources, fmt, limit):
            print(line)
        return
    print('resources with a use the stream shows: %d  (the capture names %d ids; %d draws/dispatches '
          'over %d events)' % (len(rows), len(resources), ledger['events'], sum(ledger['seen'].values())))
    print('%-9s %-9s %-26s %5s %5s %7s %7s  %s'
          % ('res', 'kind', 'name', 'write', 'read', 'first', 'last', 'flags'))
    shown = rows if not limit else rows[:limit]
    for row in shown:
        info = resources.get(row['rid'])
        print('%-9s %-9s %-26s %5d %5d %7s %7s  %s'
              % ('res%d' % row['rid'], info['kind'] if info else '?',
                 (info['name'][:26] if info else '') or '-', len(row['writes']), len(row['reads']),
                 '#%d' % row['first'] if row['first'] else '-',
                 '#%d' % row['last'] if row['last'] else '-', ', '.join(dep_flags(row))))
    if len(rows) > len(shown):
        print('... %d more resource(s) with uses (maxResources=0 shows all)' % (len(rows) - len(shown)))
    early = [(r, read_before_write(r)) for r in rows]
    early = [(r, one) for r, one in early if one is not None]
    dead = [(r, write_never_read(r)) for r in rows]
    dead = [(r, one) for r, one in dead if one is not None]
    if early:
        print('read-before-write: %d resource(s) whose first use is a read nothing writes first '
              '(a question: a static resource, a call outside the capture and a producer before it '
              'all look the same)' % len(early))
        for row, one in early[:limit or len(early)]:
            first_read, first_write = one
            print('  %s: read #%d (%s), %s' % (_label(resources, row['rid']), first_read['eid'],
                                               first_read['how'],
                                               'first write #%d' % first_write if first_write
                                               else 'no write in this capture'))
    if dead:
        dropped = [one for _row, one in dead if one[1]]
        blind = (', and %d table binding(s) in this frame resolve to nothing at all, so a resource '
                 'read only through one of those reads as unread here' % ledger['unresolved']
                 if ledger['unresolved'] else '')
        print('write-never-read: %d resource(s) written with nothing afterwards reading them '
              '(%d of them discarded by the frame, which explains the write)%s'
              % (len(dead), len(dropped), blind))
        for row, one in dead[:limit or len(dead)]:
            last_write, discards = one
            note = (', discarded at #%d' % discards[0]['eid']) if discards else ''
            print('  %s: last write #%d (%s)%s' % (_label(resources, row['rid']), last_write['eid'],
                                                   last_write['how'], note))
    unattributed = _unattributed(ledger)
    if unattributed:
        print('not decoded as uses: %s -- a resource only these touch reads as unused here '
              '(REFERENCE 4.15)' % unattributed)
    if ledger['unresolved']:
        print('unresolved: %d descriptor-table binding(s) point at a heap slot the capture never '
              'wrote' % ledger['unresolved'])
    if ledger['failed']:
        print('unreadable: %s -- payloads that did not parse cleanly are never guessed at'
              % ', '.join('%s x%d' % (k, v) for k, v in sorted(ledger['failed'].items())))

def _graph(rows: List[UseTally], resources: Dict[int, ResourceInfo], fmt: str,
           limit: int) -> List[str]:
    """The DOT or Mermaid lines for the resources with a producer, capped at `limit` of them.

    Nodes: `w<eid>` for an event that wrote (or read *and* wrote), `r<eid>` for a reader, and
    `res<id>` for the resource. Each edge's label is the binding kind, which is the only thing that
    says *how* the resource was touched. A resource nothing wrote is not a dependency and is left out
    of the graph -- the table still counts it -- and the first line says how many were left out.
    """
    producers = [row for row in rows if row['writes']]
    shown = producers[:limit] if limit else producers
    head = ('// deps: %d resource(s) with a producer, %d shown (maxResources=0 shows all; a resource '
            'that is only read has no producer and is not drawn)' % (len(producers), len(shown)))
    # Two bindings of the same resource in one event are one edge: a draw whose two vertex streams
    # come out of the same buffer reads it once, and the graph says so once (the table counts both).
    nodes: Dict[str, str] = {}
    kinds: Dict[str, str] = {}
    edges: List[Tuple[str, str, str]] = []
    seen = set()
    for row in shown:
        rid = 'res%d' % row['rid']
        info = resources.get(row['rid'])
        nodes[rid] = 'res%d%s' % (row['rid'], rdc_resources._name_suffix(resources, row['rid'], 40))
        kinds[rid] = info['kind'] if info else '?'
        for use in row['writes']:
            node = 'w%d' % use['eid']
            nodes[node] = '#%d %s' % (use['eid'], use['call'])
            if (node, rid, use['how']) not in seen:
                seen.add((node, rid, use['how']))
                edges.append((node, rid, use['how']))
        for use in row['reads']:
            # a UAV is in both lists and is drawn as both, because it is both
            node = 'r%d' % use['eid']
            nodes[node] = '#%d %s' % (use['eid'], use['call'])
            if (rid, node, use['how']) not in seen:
                seen.add((rid, node, use['how']))
                edges.append((rid, node, use['how']))
    if fmt != 'dot':
        out = [head.replace('//', '%%'), 'flowchart LR']
        for node, text in nodes.items():
            out.append('  %s["%s"]' % (node, _escape(text)))
        for src, dst, label in edges:
            out.append('  %s -->|%s| %s' % (src, label, dst))
        return out
    out = [head, 'digraph deps {', '  rankdir=LR;', '  node [shape=box, fontsize=10];']
    for node, text in nodes.items():
        if node.startswith('res'):
            out.append('  "%s" [label="%s\\n%s"];' % (node, _escape(text), kinds[node]))
    out.append('  node [shape=ellipse, fontsize=9];')
    for node, text in nodes.items():
        if not node.startswith('res'):
            out.append('  "%s" [label="%s"];' % (node, _escape(text)))
    for src, dst, label in edges:
        out.append('  "%s" -> "%s" [label="%s"];' % (src, dst, label))
    out.append('}')
    return out

# ---------------------------------------------------------------------------
# `memory`: placement, lifetimes, aliasing, and what nothing reads.
# ---------------------------------------------------------------------------
def _peak_live(windows: Sequence[Tuple[int, int, int]], end: int) -> Tuple[int, int]:
    """`(peak bytes, the event it is reached at)` for `(first use, last use, bytes)` windows.

    A sweep of the ends: a resource's bytes count from its first use to its last, inclusive. A
    resource the frame never touches has no window of its own -- it is known to exist and nothing is
    known about when it stopped being used -- so it counts from the start of the capture to the end,
    which is the conservative reading.
    """
    events: List[Tuple[int, int]] = []
    for first, last, size in windows:
        if not first:
            first, last = 1, max(end, 1)
        events.append((first, size))
        events.append((last + 1, -size))
    live = peak = 0
    at = 0
    for eid, delta in sorted(events, key=lambda item: (item[0], item[1])):
        live += delta
        if live > peak:
            peak, at = live, eid
    return peak, at

def cmd_memory(path: str, limit: int = 20) -> None:
    """The frame's memory: placement, capture-relative lifetimes, aliasing and what nothing reads.

    `memory <rdc> [maxRows]`: what the capture creates by placement and kind, then the two questions
    worth asking of it -- which bytes nothing reads, and which of them could have shared memory. The
    aliasing half reads the frame's own aliasing barriers first (two resources handed the same
    memory, in order) and then looks for candidates itself: placed resources in one heap whose use
    windows do not overlap. A heap's `total - peak` is what perfect packing could save *if* every
    pair were legal, and D3D12's aliasing requirements are not in the file, so it is stated as an
    upper bound rather than as a plan.

    Lifetimes are capture-relative throughout: `first use .. last use` inside the frame, because
    D3D12 writes no destruction to the stream. A resource the capture does not create at all (UE
    allocates its heaps and static textures at startup) has no creation event here and is reported as
    external rather than given one.
    """
    ledger, resources, _how = _deps_ledger(path)
    rows = {rid: tally(rid, record) for rid, record in ledger['resources'].items()}
    created = {rid: record for rid, record in ledger['resources'].items() if record['created']}
    end = sum(ledger['seen'].values())
    print('memory: %d resource(s) referenced, %d created by this capture, %d chunk(s) walked'
          % (len(ledger['resources']), len(created), end))
    # --- placement -------------------------------------------------------------------------------
    print('by placement')
    for where in ('committed', 'placed', 'reserved'):
        group = [rid for rid, record in created.items() if record['placement'] == where]
        total, estimated = _group_bytes(group, resources)
        extra = ''
        if where == 'placed':
            heaps = {created[rid]['heap'] for rid in group}
            heap_bytes = sum(ledger['heaps'].get(h, 0) for h in heaps)
            extra = ' in %d heap(s) the capture creates (%s)' % (
                len(heaps), _size_text(heap_bytes, False) if heap_bytes else 'no heap sizes recorded')
        print('  %-10s %4d  %-11s %s' % (where, len(group), _size_text(total, estimated), extra))
    external = len(ledger['resources']) - len(created)
    print('  %-10s %4d  (referenced, not created here: a capture records the frame, and UE '
          'allocates at startup)' % ('external', external))
    print('  (a `~` figure counts a texture at 4 bytes a pixel -- a capture records no texture bytes '
          '-- so it need not agree with the heap sizes beside it)')
    # --- by kind ---------------------------------------------------------------------------------
    print('by kind')
    kinds: Dict[str, List[int]] = {}
    for rid, info in resources.items():
        if rid in ledger['resources']:
            kinds.setdefault(info['kind'], []).append(rid)
    for kind in sorted(kinds, key=lambda k: -sum(resource_bytes(resources[r])[0] for r in kinds[k])):
        total, estimated = _group_bytes(kinds[kind], resources)
        note = '  (~ = pixels x 4: a capture records no texture bytes)' if estimated else ''
        print('  %-10s %4d  %-11s%s' % (kind, len(kinds[kind]), _size_text(total, estimated), note))
    # --- what nothing reads ----------------------------------------------------------------------
    print('never read')
    touched = [rid for rid, row in rows.items() if row['first']]
    unused = [rid for rid in created if rid not in rows or not rows[rid]['first']]
    dead = [(rid, write_never_read(rows[rid])) for rid in touched]
    dead = [(rid, one) for rid, one in dead if one is not None]
    total, estimated = _group_bytes([rid for rid, _one in dead], resources)
    blind = (', and %d table binding(s) here resolve to nothing (the capture never wrote that heap '
             'slot): a resource read only through one of those looks unread'
             % ledger['unresolved'] if ledger['unresolved'] else '')
    print('  %d resource(s), %s: written with nothing afterwards reading them%s'
          % (len(dead), _size_text(total, estimated), blind))
    for rid, one in sorted(dead, key=lambda item: -resource_bytes(resources.get(item[0]))[0])[:limit]:
        last_write, discards = one
        print('    %s: last write #%d (%s)%s'
              % (_label(resources, rid), last_write['eid'], last_write['how'],
                 (', discarded at #%d' % discards[0]['eid']) if discards else ''))
    total, estimated = _group_bytes(unused, resources)
    print('  %d resource(s), %s: created here and touched by no decoded chunk'
          % (len(unused), _size_text(total, estimated)))
    for rid in sorted(unused, key=lambda r: -resource_bytes(resources.get(r))[0])[:limit]:
        record = ledger['resources'][rid]
        print('    %s: created #%d, %s' % (_label(resources, rid), record['created'],
                                            record['placement']))
    # --- aliasing --------------------------------------------------------------------------------
    print('aliasing')
    if ledger['aliases']:
        print('  %d aliasing barrier(s) hand one piece of memory over, in order:' % len(ledger['aliases']))
        for eid, before, after in ledger['aliases'][:limit]:
            print('    #%d %s -> %s' % (eid, _label(resources, before), _label(resources, after)))
    else:
        print('  no aliasing barrier in this capture')
    placed = [rid for rid, record in created.items() if record['placement'] == 'placed']
    lonely: List[int] = []
    scored: List[Tuple[int, int, List[int], int, bool, int, int, List[Tuple[int, int, int]]]] = []
    for heap in sorted({created[rid]['heap'] for rid in placed}):
        in_heap = [rid for rid in placed if created[rid]['heap'] == heap]
        if len(in_heap) < 2:
            lonely.extend(in_heap)          # one resource in a heap cannot share it with anything
            continue
        total, estimated = _group_bytes(in_heap, resources)
        windows = [(rows[rid]['first'] if rid in rows else 0,
                    rows[rid]['last'] if rid in rows else 0,
                    resource_bytes(resources.get(rid))[0]) for rid in in_heap]
        peak, at = _peak_live(windows, end)
        scored.append((total - peak, heap, in_heap, total, estimated, peak, at,
                       _alias_candidates(in_heap, rows, resources, limit)))
    # the heaps worth looking at first are the ones whose total is furthest above their peak: that
    # gap is what sharing could save, and a heap where nothing overlaps has none
    scored.sort(key=lambda item: -item[0])
    for saving, heap, in_heap, total, estimated, peak, at, pairs in (scored[:limit] if limit else scored):
        print('  heap%d: %d resource(s), %s in it, %s live at once (peak at #%d), so up to %s of it '
              'could be shared' % (heap, len(in_heap), _size_text(total, estimated),
                                   _size_text(peak, estimated), at, _size_text(saving, estimated)))
        if pairs:
            print('    disjoint use windows -- candidates, not a plan (D3D12 aliasing has '
                  'requirements a capture does not record):')
            for a, b, shared in pairs:
                print('      %s #%d..#%d and %s #%d..#%d: %s'
                      % (_label(resources, a), rows[a]['first'], rows[a]['last'],
                         _label(resources, b), rows[b]['first'], rows[b]['last'],
                         _size_text(shared, resource_bytes(resources.get(a))[1]
                                    or resource_bytes(resources.get(b))[1])))
    if len(scored) > limit:
        print('  ... %d more heap(s) with a smaller gap between their total and their peak '
              '(maxRows=0 shows all)' % (len(scored) - limit))
    if lonely:
        total, estimated = _group_bytes(lonely, resources)
        print('  %d heap(s) hold one resource each (%s): nothing in them to share'
              % (len(lonely), _size_text(total, estimated)))
    # --- what the report cannot prove ------------------------------------------------------------
    print('what this cannot prove')
    print('  destruction: D3D12 writes no release to the stream, so every lifetime ends at the')
    print('    frame\'s last use, and a resource created before the capture starts has no creation')
    print('  a texture\'s bytes are not recorded: `~` marks every figure counted at 4 bytes a pixel')
    print('  a buffer sub-allocated out of a UE page is named after the page (REFERENCE 4.9)')
    unattributed = _unattributed(ledger)
    if unattributed:
        print('  reads and writes this does not attribute: %s' % unattributed)
    if ledger['failed']:
        print('  payloads that did not parse cleanly: %s'
              % ', '.join('%s x%d' % (k, v) for k, v in sorted(ledger['failed'].items())))

def _group_bytes(group: Sequence[int], resources: Dict[int, ResourceInfo]) -> Tuple[int, bool]:
    """`(total bytes, any of them estimated)` over a set of resource ids."""
    total = 0
    estimated = False
    for rid in group:
        size, guess = resource_bytes(resources.get(rid))
        total += size
        estimated = estimated or guess
    return total, estimated

def _alias_candidates(in_heap: Sequence[int], rows: Dict[int, UseTally],
                      resources: Dict[int, ResourceInfo], limit: int
                      ) -> List[Tuple[int, int, int]]:
    """Pairs of resources in one heap whose use windows do not overlap, biggest first.

    The pair is a candidate and nothing more: D3D12 will only alias resources whose descriptions and
    heap properties the runtime agrees are compatible, and a capture records neither the runtime's
    answer nor the reason. `shared` is the smaller side, which is what aliasing the pair could save.
    """
    out: List[Tuple[int, int, int]] = []
    for i, a in enumerate(in_heap):
        for b in in_heap[i + 1:]:
            one, two = rows.get(a), rows.get(b)
            if one is None or two is None or not one['first'] or not two['first']:
                continue
            if one['first'] <= two['last'] and two['first'] <= one['last']:
                continue                    # their use windows overlap: they could not share
            size_a = resource_bytes(resources.get(a))[0]
            size_b = resource_bytes(resources.get(b))[0]
            out.append((a, b, min(size_a, size_b)))
    out.sort(key=lambda item: -item[2])
    return out[:limit]

def _deps_ledger(path: str) -> Tuple[UseLedger, Dict[int, ResourceInfo], str]:
    """The use ledger and the resource table for a capture, from one decompressed stream."""
    _info, stream, how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    resources = rdc_resources.parse_resource_table(stream, names)
    heaps = rdc_resources.parse_descriptor_heaps(stream, names)
    return walk_uses(stream, names, resources, heaps), resources, how

__all__ = [
    'ACCESS_BY_KIND',
    'ACCESS_READS',
    'ACCESS_WRITES',
    'LAYOUT_READS',
    'LAYOUT_WRITES',
    'STATE_READS',
    'STATE_WRITES',
    'UseTally',
    '_alias_candidates',
    '_bind_uses',
    '_deps_ledger',
    '_group_bytes',
    '_label',
    '_peak_live',
    '_rows',
    '_size_text',
    '_unattributed',
    '_use',
    '_va_resource',
    'cmd_deps',
    'cmd_memory',
    'dep_flags',
    'read_before_write',
    'resource_bytes',
    'tally',
    'walk_uses',
    'write_never_read',
]
