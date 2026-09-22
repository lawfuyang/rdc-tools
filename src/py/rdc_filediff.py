"""`diff`: two captures compared by what their *streams* recorded -- the file's own view of a frame.

`passdiff` (same folder) compares the two marker trees, and `replaydiff` (`rdc_ab`) compares two bundles
of engine answers. This is the third thing, and the one neither of those can do: the same call, as the
file itself recorded it -- the marker path it sits under, its own arguments, the state chunks that
changed just before it, and every binding in force at it. "rp2 was a compute CBV at b0, now a vertex CBV
at b1; `SkyViewLut` left the table" is a sentence about the stream, and this is where it is read.

**What is comparable across two captures is not their ids.** Resource ids, heap ids and PSO ids are each
one recording session's numbering, so this compares *descriptions*: a named resource by the name the
application gave it, an unnamed one by its kind and shape, a binding by what the root signature says the
slot is (`rp2(table t0 n5 s0)`, `rdc_resources._root_param_label`). That is the one thing the two
captures of a pair share, and it is all this command claims: two unnamed resources of the same shape
compare *equal* here, which the output says out loud rather than leaving to be discovered.

**Alignment follows `passdiff`**, because the same problem appears one level down. Paths pair by full
text first and then by their innermost name -- dynamic marker text is real (`CullLights 22x14x8` against
`CullLights 32x20x8`, the same pass with a different light grid) -- and within a paired path, calls pair
by name in occurrence order, so the second `DrawIndexed` under a pass pairs with the second. What is
left over is `only A` or `only B`, which for two frames of the same scene is a finding in itself.

Exit code: 0 when the comparison ran. A capture whose chunks cannot be named at all -- no source tree and
no bundled table, README §1.1 -- is reported as *not looked at* rather than as having no calls: 1, the
same answer `passdiff` gives.
"""
from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_cache import *  # noqa: F401,F403
from rdc_payloads import *  # noqa: F401,F403
from rdc_resources import *  # noqa: F401,F403
from rdc_passdiff import MARKER_MINLEN, UNNAMED, _pair_by
import rdc_cache  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_profile
import rdc_resources  # noqa: F401  (used qualified: the label helper is called from inside functions)
import rdc_stream  # noqa: F401  (used qualified: the walkers are called from inside functions)
import rdc_table  # noqa: F401  (the csv/markdown shapes)

from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

#: How many rows each section prints before the rest are counted. A real pair of frames of one scene can
#: differ in hundreds of calls; `--all` is what prints every row, and the counts are always complete.
ROW_LIMIT = 40

#: The marker path of a call that sits under no marker at all. Not `UNNAMED`, which is a marker whose
#: name could not be read: "this call is outside every pass" is a different statement from "this pass has
#: no name", and the second one has a name of its own (`rdc_passdiff.UNNAMED`).
NO_MARKER = '(no marker)'


class CallRecord(NamedTuple):
    """One draw or dispatch, as the stream recorded it -- the unit this command compares.

    `path` is the full marker chain the call sits under (`Scene > BasePass`) or `NO_MARKER`; `call` is
    the chunk name without its `List_` prefix, which is the same word `draws` prints; `ordinal` is which
    occurrence of that call under that path it is, 1-based, so an `only A` row can be pointed at.

    `args` is the call's own arguments (`idx=1200 inst=1`) and `setters` are the state chunks applied to
    *this* command list since its previous call -- the "what changed just before the call" half, where
    `bindings` is the "what was in force at it" half. Both are comparable across captures: setters are
    chunk names, and every binding is a description rather than an id (`_describe_resource`).
    """

    path: str
    call: str
    ordinal: int
    args: str
    setters: Tuple[str, ...]
    bindings: Dict[str, str]


class ChangeRow(NamedTuple):
    """One line of the comparison, in the shape both the terminal and `--format` print.

    `status` is `changed`, `only A`, `only B` or `path` (a marker path the two captures spell
    differently but that paired by its innermost name). For a `changed` row, `field` names the slot that
    differs -- `args`, `setters`, or a binding's own key (`RTV`, `rp2(table t0 n5 s0)`) -- and for a
    one-sided row it is the whole call, with `a`/`b` holding its summary on the side that has it.
    """

    status: str
    path: str
    call: str
    field: str
    a: str
    b: str


def _call_args(name: str, blob: Buffer) -> str:
    """The call's own arguments, in the same words `draws` prints them."""
    if name == 'List_DrawIndexedInstanced' and len(blob) >= 28:
        return 'idx=%d inst=%d' % (u32(blob, 8), u32(blob, 12))
    if name == 'List_DrawInstanced' and len(blob) >= 24:
        return 'verts=%d inst=%d' % (u32(blob, 8), u32(blob, 12))
    if len(blob) >= 20:
        return 'x=%d y=%d z=%d' % (u32(blob, 8), u32(blob, 12), u32(blob, 16))
    return ''


def _describe_resource(resources: Dict[int, ResourceInfo], rid: int) -> str:
    """`SkyViewLut`, or the kind and shape of a resource the capture never named.

    The name is the only identity that survives a re-capture: an id is that session's numbering, so a
    resource with no name is described by what it *is* (`texture2d 1024x1024x1`, `buffer 65536 B`) and
    two unnamed resources of the same shape therefore compare equal here -- the caveat the command
    prints, not a bug it hides.
    """
    info = resources.get(rid)
    if info is None:
        return 'not in the resource table'
    if info['name']:
        return info['name']
    if info['kind'] in ('buffer', 'blas', 'tlas'):
        return '%s %d B' % (info['kind'], info['size'])
    if info['kind'] == 'unknown':
        return 'unnamed (no descriptor)'
    return '%s %dx%dx%d' % (info['kind'], info['width'], info['height'], info['depth'])


def call_bindings(state: Optional[DrawState], compute: bool,
                  resources: Dict[int, ResourceInfo],
                  heaps: Dict[int, Dict[int, DescriptorInfo]],
                  sigs: Dict[int, RootSignature],
                  binds: Dict[str, Dict[Tuple[str, int, int], str]]) -> Dict[str, str]:
    """Every binding in force at one call as `key -> description`, comparable across captures.

    Keys are what the signature says the slot *is* (`CBV rp0(cbv b1 s0)`, `Table rp2(table t0 n5 s0)`),
    plus the fixed slots a graphics call has: `RTV`, `DSV`, `VB0`, `IB`, `PSO`, and `rootsig` for the
    signature itself. An empty value is information -- `RTV: ''` is "nothing was bound", which is why
    those keys are always present for a draw and `PSO` is absent until one is set.

    Returned rather than printed so the terminal and `--format csv` cannot disagree about what changed,
    the same way `draw_state_lines` is the one source for `draws`.
    """
    out: Dict[str, str] = {}
    if state is None:
        return out
    sig_id = state['compSig'] if compute else state['gfxSig']
    sig = sigs.get(sig_id) if sig_id is not None else None

    def key(rp: int) -> str:
        """The slot by what it *is*, not by its index -- the index is the session's own numbering.

        `rdc_resources._root_param_what` is the shared half of `_root_param_label`: the same words
        `draws` prints inside the parentheses, plus the stage when the parameter is not visible
        everywhere and the `RDEF` name when the capture still has reflection. Two builds of one scene
        that bind the same slot through different `rpN` indices therefore compare as the *same* binding,
        which a key of `rp10` against `rp5` could never show.
        """
        if sig is None or not (0 <= rp < len(sig['params'])):
            return 'rp%d (no signature decoded)' % rp
        param = sig['params'][rp]
        vis = '' if param['visibility'] == 'all' else param['visibility'] + ' '
        name = rdc_resources._bind_name(binds, param)
        return '%s%s%s' % (vis, rdc_resources._root_param_what(param), ' [%s]' % name if name else '')

    if sig is not None:
        # The signature's identity is its shape, not its id: a pair of captures that bind
        # differently-shaped signatures here is a difference worth a row of its own.
        out['rootsig'] = '%s, %d dwords, %d param(s)' % (sig['version'], sig['dwords'],
                                                         len(sig['params']))
    for _tag, roots in (('CBV', state['compCbv'] if compute else state['gfxCbv']),
                        ('SRV', state['compSrv'] if compute else state['gfxSrv']),
                        ('UAV', state['compUav'] if compute else state['gfxUav'])):
        # The byte offset is deliberately not part of the value: for a resource sub-allocated out of a
        # UE page it is where the sub-allocation sits in that page, which is the recording's own
        # layout rather than an identity (REFERENCE §4.9), and comparing it would make every page
        # binding in a pair differ for no reason a reader could act on.
        for rp, (res, _off) in sorted(roots.items()):
            out[key(rp)] = _describe_resource(resources, res)
    for rp, (heap, index) in sorted((state['compTable'] if compute else state['gfxTable']).items()):
        info = heaps.get(heap, {}).get(index)
        if info is None:
            out[key(rp)] = 'unwritten slot'
        elif info['kind'] == 'sampler':
            out[key(rp)] = 'sampler'
        else:
            out[key(rp)] = '%s %s' % (info['kind'], _describe_resource(resources, info['resource']))
    if compute:
        return out
    out['RTV'] = ', '.join(_describe_resource(resources, res) for res in state['rtv'])
    out['DSV'] = _describe_resource(resources, state['dsv']) if state['dsv'] else ''
    for slot, view in sorted(state['vbs'].items()):
        out['VB%d' % slot] = '%s (size %d, stride %d)' % (_describe_resource(resources, view[0]),
                                                         view[2], view[3])
    if state['ib'] is not None:
        out['IB'] = _describe_resource(resources, state['ib'][0])
    if state['pso']:
        # The PSO's id is this session's numbering; that one was *set* is the comparable half, and it
        # is the one that matters here -- `passdiff` is where two frames' pipeline shapes are compared.
        out['PSO'] = 'bound'
    return out


@rdc_profile.timed('call records')
def call_records(path: str) -> Optional[List[CallRecord]]:
    """Every draw and dispatch of `path`'s frame stream, in stream order, with its bindings.

    None when no chunk can be named at all: then "no call has this binding" would be a statement about
    the tool rather than about the capture, which is the answer `rdc_passdiff.marker_passes` gives for
    the same reason. The walk is the one `draws` does -- the same state chunks, the same marker stack,
    the same payload slicing (only the chunks this loop reads) -- plus the per-command-list record of
    which setters have run since that list's previous call.
    """
    info, stream, _how = rdc_cache.load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    if not names:
        return None
    resources = parse_resource_table(stream, names)
    heaps = parse_descriptor_heaps(stream, names)
    sigs = parse_root_signatures(stream, names)
    binds = shader_bind_names(stream, rdc_cache.stream_source(path, info))

    records: List[CallRecord] = []
    stack: List[str] = []
    states: Dict[int, DrawState] = {}
    pending: Dict[int, List[str]] = {}
    seen: Dict[Tuple[str, str], int] = {}
    for chunk in iter_chunks(stream):
        name = names.get(chunk['id'], '')
        blob = chunk_payload(stream, chunk) if (name in DRAW_CHUNKS or name in STATE_CHUNKS) else b''
        if name in PUSH_MARKER_CHUNKS:
            strings = chunk_strings(stream, chunk, MARKER_MINLEN, 1)
            stack.append(strings[0] if strings else UNNAMED)
        elif name in POP_MARKER_CHUNKS:
            if stack:
                stack.pop()
        elif name in DRAW_CHUNKS:
            list_id = u64(blob, 0) if len(blob) >= 8 else 0
            state = states.get(list_id)
            path_text = ' > '.join(stack) if stack else NO_MARKER
            call = name.replace('List_', '')
            key = (path_text, call)
            seen[key] = seen.get(key, 0) + 1
            records.append(CallRecord(path=path_text, call=call, ordinal=seen[key],
                                      args=_call_args(name, blob),
                                      setters=tuple(pending.pop(list_id, ())),
                                      bindings=call_bindings(state, name in COMPUTE_CHUNKS,
                                                             resources, heaps, sigs, binds)))
        elif _apply_state_chunk(name, blob, states):
            # `List_Reset` carries its command list's id at +40, every other setter at +0 (`rdc_payloads`).
            list_id = (u64(blob, 40) if name == 'List_Reset' and len(blob) >= 48
                       else (u64(blob, 0) if len(blob) >= 8 else 0))
            pending.setdefault(list_id, []).append(name.replace('List_', ''))
    return records


def align_paths(paths_a: Sequence[str], paths_b: Sequence[str]) -> Tuple[Dict[int, int],
                                                                       List[Tuple[str, str]]]:
    """Pair the two sides' marker paths: full text first, then innermost name.

    The two rules are `rdc_passdiff`'s, in its order, because the problem is the same one (a path
    carries dynamic text no table could list) and a pair of frames should not be aligned one way by
    `passdiff` and another by `diff`. Returns the pairing (`index in A -> index in B`) and the paths
    that only paired by their innermost name, which is the evidence a reader needs to judge the pairing.
    """
    paired: Dict[int, int] = {}
    # `_pair_by` records the note it paired a row by; the section header already says which rule matched
    # them (and it is the second one in every case that lands here), so the notes are not printed.
    notes: Dict[int, str] = {}
    used_b: List[bool] = [False] * len(paths_b)
    _pair_by(list(paths_a), list(paths_b), paired, notes, used_b,
             {index: text for index, text in enumerate(paths_a)},
             {index: text for index, text in enumerate(paths_b)}, '')
    _pair_by(list(paths_a), list(paths_b), paired, notes, used_b,
             {index: text.rsplit(' > ', 1)[-1] for index, text in enumerate(paths_a)},
             {index: text.rsplit(' > ', 1)[-1] for index, text in enumerate(paths_b)},
             'the innermost marker name is the same (`%s`)')
    renamed = [(paths_a[index], paths_b[other]) for index, other in sorted(paired.items())
               if paths_a[index] != paths_b[other]]
    return paired, renamed


def _by_call(records: Sequence[CallRecord]) -> Dict[str, List[CallRecord]]:
    """The records of one path, grouped by call name in the order they occur."""
    out: Dict[str, List[CallRecord]] = {}
    for record in records:
        out.setdefault(record.call, []).append(record)
    return out


def _call_summary(record: Optional[CallRecord]) -> str:
    """A one-line description of a call, for a row that has it on one side only."""
    if record is None:
        return ''
    setters = ', '.join(record.setters) if record.setters else 'none'
    return 'args %s; setters since the previous call: %s' % (record.args or '(none)', setters)


def _differences(one: CallRecord, two: CallRecord) -> List[Tuple[str, str, str]]:
    """`(field, A, B)` for every slot the two records disagree on, in a fixed order."""
    out: List[Tuple[str, str, str]] = []
    if one.args != two.args:
        out.append(('args', one.args, two.args))
    if one.setters != two.setters:
        out.append(('setters', ' + '.join(one.setters) or '(none)',
                    ' + '.join(two.setters) or '(none)'))
    for key in sorted(set(one.bindings) | set(two.bindings)):
        left = one.bindings.get(key, '')
        right = two.bindings.get(key, '')
        if left != right:
            out.append((key, left or '(not bound)', right or '(not bound)'))
    return out


def align_calls(a: Sequence[CallRecord], b: Sequence[CallRecord]) -> Tuple[List[ChangeRow],
                                                                          Dict[str, int]]:
    """Pair two captures' calls and report every difference, plus the counts behind the summary.

    Paths pair as `align_paths` says; inside a paired path, calls pair by name in occurrence order. A
    call whose *path* exists on one side only is a one-sided row of its own -- its marker never ran in
    the other frame -- and so is the nth `DrawIndexed` of a pass that ran it once.
    """
    paths_a = list(dict.fromkeys(record.path for record in a))
    paths_b = list(dict.fromkeys(record.path for record in b))
    paired, renamed = align_paths(paths_a, paths_b)
    index_a = {text: index for index, text in enumerate(paths_a)}
    index_b = {text: index for index, text in enumerate(paths_b)}

    rows: List[ChangeRow] = [ChangeRow('path', one, '', 'paired by its innermost name', one, two)
                             for one, two in renamed]
    counts: Dict[str, int] = {'same': 0, 'changed': 0, 'only A': 0, 'only B': 0}
    calls_a: Dict[int, List[CallRecord]] = {}
    calls_b: Dict[int, List[CallRecord]] = {}
    for record in a:
        calls_a.setdefault(index_a[record.path], []).append(record)
    for record in b:
        calls_b.setdefault(index_b[record.path], []).append(record)

    for i, where in enumerate(paths_a):
        other = paired.get(i)
        if other is None:
            for record in calls_a.get(i, []):
                rows.append(ChangeRow('only A', where, record.call, '(the whole call)',
                                      _call_summary(record), ''))
                counts['only A'] += 1
            continue
        groups_a = _by_call(calls_a.get(i, []))
        groups_b = _by_call(calls_b.get(other, []))
        for call in sorted(set(groups_a) | set(groups_b)):
            left, right = groups_a.get(call, []), groups_b.get(call, [])
            for k in range(max(len(left), len(right))):
                one = left[k] if k < len(left) else None
                two = right[k] if k < len(right) else None
                path_text = one.path if one is not None else (two.path if two is not None else where)
                if one is None or two is None:
                    side = 'only B' if one is None else 'only A'
                    rows.append(ChangeRow(side, path_text, call, '(the whole call)',
                                          _call_summary(one), _call_summary(two)))
                    counts[side] += 1
                    continue
                found = _differences(one, two)
                if not found:
                    counts['same'] += 1
                    continue
                counts['changed'] += 1
                for field, before, after in found:
                    rows.append(ChangeRow('changed', path_text, call, field, before, after))
    # One path can appear in B only: there is no A-side loop iteration to find it in, and a pass that
    # only the second frame ran is as much an answer as a call that only it ran.
    claimed = set(paired.values())
    for j, where in enumerate(paths_b):
        if j in claimed:
            continue
        for record in calls_b.get(j, []):
            rows.append(ChangeRow('only B', where, record.call, '(the whole call)', '',
                                  _call_summary(record)))
            counts['only B'] += 1
    return rows, counts


def _show(rows: Sequence[ChangeRow], status: str, show_all: bool) -> List[ChangeRow]:
    """The rows of one section, capped unless `--all`."""
    group = [row for row in rows if row.status == status]
    return group if show_all else group[:ROW_LIMIT]


def _clip(text: str, width: int) -> str:
    """`text` at most `width` characters, with `...` when it was cut.

    The terminal is a five-column table and a resource name can be longer than its column; the CSV and
    Markdown forms carry the whole value, and a clipped cell says so rather than looking complete. The
    path is the one column where the *tail* is kept instead (`_tail`), because the innermost marker is
    the part that names the pass.
    """
    return text if len(text) <= width else text[:width - 3] + '...'


def _tail(text: str, width: int) -> str:
    """The last `width` characters of `text`: a marker path read from its innermost name outwards."""
    return text if len(text) <= width else '...' + text[-(width - 3):]


def cmd_filediff(path_a: str, path_b: str, show_all: bool = False, fmt: str = 'table') -> int:
    """`diff <a.rdc> <b.rdc> [--all]` -- the two streams' own calls, compared.

    Exactly what the file can say: which calls are in both (by marker path, call name and occurrence),
    which are in only one, and for the ones in both, which slot holds a different value -- the call's
    arguments, the state chunks that changed before it, or any binding in force at it. What it cannot
    say is whether a difference is *wrong*: that needs the engine's answer (`replaydiff`), the frame's
    intent (a reader), or both.

    `--format csv|markdown` prints one row per difference, with the sections' rows in the order the
    terminal prints them, so a spreadsheet sees exactly what the terminal showed.
    """
    lists: List[Optional[List[CallRecord]]] = []
    for path in (path_a, path_b):
        try:
            lists.append(call_records(path))
        except rdc_stream.FrameError as exc:
            print('error: %s' % exc)
            return 1
    if lists[0] is None or lists[1] is None:
        print('error: no chunk-name map: the RenderDoc source tree was not found (README §1.1), so no '
              'chunk can be named and two streams cannot be compared')
        return 1

    a, b = lists[0], lists[1]
    rows, counts = align_calls(a, b)
    paths_a = len(set(record.path for record in a))
    paths_b = len(set(record.path for record in b))
    rei = sum(1 for row in rows if row.status == 'path')
    # The three counts the terminal prints as its header, and the three things a reader must be told
    # before believing any row. Both go to stderr under `--format`, so stdout stays a table.
    summary = [
        'calls     : %d in A, %d in B (a call is a draw or a dispatch in the stream)' % (len(a), len(b)),
        'paths     : %d in A, %d in B, %d paired by their innermost name' % (paths_a, paths_b, rei),
        'verdicts  : %d in both and identical, %d changed, %d only in A, %d only in B'
        % (counts['same'], counts['changed'], counts['only A'], counts['only B']),
    ]
    notes = summary + [
        "note      : chunk indices are the file's numbering, not the engine's event ids (REFERENCE §9)",
        'note      : ids are capture-local: a binding is compared by the resource it names, so two '
        'unnamed resources of the same kind and shape compare equal here',
        'note      : the byte offset of a binding is not compared: a sub-allocation out of a UE page '
        'is named after the page and sits wherever that recording put it (REFERENCE §4.9)',
        'note      : a difference is not a defect -- this says what changed, not what should not have',
    ]
    if fmt != 'table':
        rdc_table.emit(fmt, ('status', 'path', 'call', 'field', 'a', 'b'), rows, notes)
        return 0

    print('A         : %s' % path_a)
    print('B         : %s' % path_b)
    for line in summary:
        print(line)
    for status, label in (('changed', 'changed'), ('only A', 'only in A'), ('only B', 'only in B'),
                          ('path', 'paired by their innermost name')):
        group = [row for row in rows if row.status == status]
        if not group:
            continue
        print('--- %s (%d) ---' % (label, len(group)))
        if status != 'path':
            print('%-32s %-18s %-30s %-32s %s' % ('path', 'call', 'field', 'A', 'B'))
        shown = _show(rows, status, show_all)
        for row in shown:
            if status == 'path':
                print('  %-52s ==  %s' % (_clip(row.a, 52), row.b))
            else:
                print('%-32s %-18s %-30s %-32s %s'
                      % (_tail(row.path, 32), _clip(row.call, 18), _clip(row.field, 30),
                         _clip(row.a, 32), row.b))
        if len(group) > len(shown):
            print('  ... %d more row(s) (--all prints every row)' % (len(group) - len(shown)))
    for line in notes[3:]:
        print(line)
    return 0


__all__ = [
    'ChangeRow',
    'CallRecord',
    'NO_MARKER',
    'ROW_LIMIT',
    '_call_args',
    '_call_summary',
    '_clip',
    '_describe_resource',
    '_differences',
    '_tail',
    'align_calls',
    'align_paths',
    'call_bindings',
    'call_records',
    'cmd_filediff',
]
