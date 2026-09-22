"""`passdiff`: the marker trees of two captures side by side -- the offline half of the A/B workflow.

`replaydiff` (`rdc_ab`) compares what the *engine* said about two captures, which needs a bundle of each
and a device to write them. This is the half that needs neither: two `.rdc` files, the marker chunks
inside their frame streams, and nothing else. It answers the first question of the mobile-vs-PC pair --
"is the same pass even there?" -- before anything is replayed, which is what makes it the cheap half.

A pass here is a marker with at least one draw or dispatch inside it, nested markers included. The driver
counts a pass the same way (`sheet`'s `PassList`), so a path this prints can be pasted into `--at-marker`
and the two tools agree about what a pass is.

Chunk indices are the *file's* numbering (`REFERENCE.md` §9): the engine's own event ids come from the
action list, which only replay has, so "the range shifted" here means it shifted in the stream. `draws`
prints both numberings when that difference matters.
"""
from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Sequence

import rdc_cache  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_stream  # noqa: F401  (used qualified: the stream walkers are called from inside functions)

#: What stands in for a marker whose name the capture did not carry (an empty string, or a push this
#: reader could not decode). Never an invented name: a pass the file does not name is named as unnamed.
UNNAMED = '<unnamed>'

#: How many "in both" rows are printed before the rest are counted. The one-sided and rename sections are
#: never capped -- they are the answer, and they are short in every real pair.
IN_BOTH_LIMIT = 40

#: The shortest string a marker's name may be read from, and the reason it is not 1: a marker payload is
#: framed, and the frame's bytes decode as one- or two-character printable runs *before* the name does
#: (measured on both real captures: `chunk_strings(..., 1, 1)` returns `B`, `_` or `r` where the name is
#: `MobileSceneRender`, and at two characters it still returns runs that are not names). Three is the
#: floor `markers` and `summary` have always used, and it keeps every real name: the shortest here are
#: `Sky` and `Clear`.
MARKER_MINLEN = 3


class MarkerPass(NamedTuple):
    """One marker of the file's stream that contains at least one call.

    `path` is the full `A > B` chain and is what two captures are aligned on, because a pass *name*
    survives a re-capture where a chunk index does not. `calls` counts every draw or dispatch inside the
    scope, nested markers included -- the number `sheet` calls a pass's calls, so the same pass gets the
    same count from either tool.
    """

    path: str
    name: str
    depth: int
    first_chunk: int
    last_chunk: int
    calls: int


class PassRow(NamedTuple):
    """One line of the comparison: a status, the path(s) it applies to, and which side(s) have it.

    `a_index`/`b_index` are the pass's positions in its own list (-1 on the side the row does not have),
    kept because "the same position" is part of the evidence a rename suggestion rests on.

    `status` is `same`, `added`, `removed` or `renamed?`. The question mark is load-bearing: the file
    cannot say that a pass was *renamed* rather than replaced, so a rename is offered as a pair that
    matches in position and call count, with that evidence in `note`, and a reader who disagrees can see
    why it was suggested.
    """

    status: str
    path: str
    a: Optional[MarkerPass]
    b: Optional[MarkerPass]
    a_index: int
    b_index: int
    note: str


class _OpenMarker:
    """One marker still open while the stream is walked: the tree's own bookkeeping, not a pass.

    Mutable and private because it is a cursor rather than a record: `calls` and `last` change for every
    frame on the stack as the walk passes each call.
    """

    __slots__ = ('name', 'chunk', 'last', 'calls')

    def __init__(self, name: str, chunk: int) -> None:
        self.name = name
        self.chunk = chunk
        self.last = chunk
        self.calls = 0


def marker_passes(path: str) -> Optional[List[MarkerPass]]:
    """Every marker of `path`'s frame stream that has a call inside it, in stream order.

    None when no chunk can be named -- the RenderDoc source tree is absent (`README.md` §1.1) -- because
    then "no pass has this name" would be a statement about the tool rather than about the capture. The
    report's stream detectors take the same `None` for the same reason.

    The tree is built from `PushMarker`/`Queue_BeginEvent` through to their `PopMarker`/`Queue_EndEvent`;
    `SetMarker`/`Queue_SetMarker` are counted but not walked, because a `SetMarker` names the current
    marker without opening a scope and a tree built from it would nest every later marker inside one that
    never closes. A marker still open where the stream ends is still a pass: the frame was cut, and
    dropping it would hide every pass of that frame. (The report's marker-balance detector is what reports
    the imbalance itself.)
    """
    _info, stream, _how = rdc_cache.load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    if not names:
        return None

    passes: List[MarkerPass] = []
    stack: List[_OpenMarker] = []
    for index, chunk in enumerate(rdc_stream.iter_chunks(stream), 1):
        name = names.get(chunk['id'], '')
        if name in rdc_chunkmap.PUSH_MARKER_CHUNKS:
            strings = rdc_stream.chunk_strings(stream, chunk, MARKER_MINLEN, 1)
            stack.append(_OpenMarker(strings[0] if strings else UNNAMED, index))
        elif name in rdc_chunkmap.POP_MARKER_CHUNKS:
            if stack:
                _close_pass(passes, stack, stack.pop())
        elif name in rdc_chunkmap.DRAW_CHUNKS:
            for frame in stack:
                frame.calls += 1
                frame.last = index
    while stack:
        _close_pass(passes, stack, stack.pop())
    return passes


def _close_pass(passes: List[MarkerPass], stack: Sequence[_OpenMarker], frame: _OpenMarker) -> None:
    """Record one closed marker as a pass, unless nothing was drawn inside it.

    A marker that holds only other markers is a heading rather than a pass; the ones around it are the
    passes, which is why `calls` counts a scope's whole content instead of its own calls.
    """
    if not frame.calls:
        return
    path = ' > '.join(entry.name for entry in list(stack) + [frame])
    passes.append(MarkerPass(path=path, name=frame.name, depth=len(stack), first_chunk=frame.chunk,
                             last_chunk=frame.last, calls=frame.calls))


def align_passes(a: Sequence[MarkerPass], b: Sequence[MarkerPass]) -> List[PassRow]:
    """Line up two pass lists, and say what only one side has.

    Two rules, strongest first, each applied to what the previous one left unpaired:

     1. the **full path** (`Scene > BasePass`), which is what a re-capture preserves;
     2. the **innermost name**, because a path carries dynamic text no table could list -- one real pair of
        captures has `CullLights 22x14x8 NumLights 0` against `CullLights 32x20x8 NumLights 0`, the same
        pass with a different light grid -- and matching the name inside a path is the rule `--at-marker`
        resolves by too (`REFERENCE.md` §9).

    Both pair by *occurrence*: a name used twice in a frame pairs with its own occurrence on the other
    side, so the second `Scene > Shadow` pairs with the second, and the two ranges stay about one pass.

    What is left over is offered as `removed`/`added`, except that a removed pass and an added one in the
    same slot, with the same parent path and the same call count, are reported together as `renamed?`: the
    file cannot prove a rename, so it says what the pair has in common and leaves the verdict to a reader.
    """
    paired: Dict[int, int] = {}
    notes: Dict[int, str] = {}
    used_b: List[bool] = [False] * len(b)
    _pair_by(a, b, paired, notes, used_b,
             {index: entry.path for index, entry in enumerate(a)},
             {index: entry.path for index, entry in enumerate(b)}, '')
    _pair_by(a, b, paired, notes, used_b,
             {index: _leaf(entry.path) for index, entry in enumerate(a)},
             {index: _leaf(entry.path) for index, entry in enumerate(b)},
             'the full marker path differs (`%s` is the innermost name of both)')

    rows: List[PassRow] = []
    for index, entry in enumerate(a):
        other_index = paired.get(index)
        if other_index is None:
            rows.append(PassRow('removed', entry.path, entry, None, index, -1, ''))
            continue
        other = b[other_index]
        row_notes: List[str] = []
        if notes.get(index):
            row_notes.append(notes[index])
        if other_index != index:
            row_notes.append('moved: pass %d of A is pass %d of B' % (index + 1, other_index + 1))
        if other.depth != entry.depth:
            row_notes.append('depth %d -> %d' % (entry.depth, other.depth))
        if other.calls != entry.calls:
            row_notes.append('calls %d -> %d' % (entry.calls, other.calls))
        shift = other.first_chunk - entry.first_chunk
        if shift:
            row_notes.append('%+d chunk(s)' % shift)
        rows.append(PassRow('same', entry.path, entry, other, index, other_index, '; '.join(row_notes)))

    added = [index for index in range(len(b)) if not used_b[index]]
    consumed = _pair_renames(b, added, rows)
    for index in added:
        if index not in consumed:
            rows.append(PassRow('added', b[index].path, None, b[index], -1, index, ''))
    return rows


def _pair_by(a: Sequence[object], b: Sequence[object], paired: Dict[int, int],
             notes: Dict[int, str], used_b: List[bool], keys_a: Dict[int, str], keys_b: Dict[int, str],
             explain: str) -> None:
    """Pair what is still unpaired by equal keys, in occurrence order -- '' keys pair with nothing.

    `used_b` is mutated (an index is taken the moment it is paired) and so are `paired`/`notes`; `a` and
    `b` are only read, so a caller can run a second rule over what the first did not claim. Only their
    lengths are read, which is why the sequences are `object` rather than `MarkerPass`: `rdc_filediff`
    aligns two captures' *paths* with the same two rules, and the alignment is the thing that must not
    exist twice.
    """
    queues: Dict[str, List[int]] = {}
    for index in range(len(a)):
        key = keys_a.get(index, '')
        if index not in paired and key:
            queues.setdefault(key, []).append(index)
    for index in range(len(b)):
        key = keys_b.get(index, '')
        if used_b[index] or not key:
            continue
        queue = queues.get(key)
        if queue:
            other = queue.pop(0)
            paired[other] = index
            used_b[index] = True
            if explain:
                notes[other] = explain % key


def _leaf(path: str) -> str:
    """The innermost component of a path (`A > B` -> `B`): what survives dynamic text."""
    return path.rsplit(' > ', 1)[-1].strip() if path else ''


def _parent(path: str) -> str:
    """A path without its innermost component (`A > B` -> `A`), '' for a top-level marker."""
    head, sep, _tail = path.rpartition(' > ')
    return head.strip() if sep else ''


def _pair_renames(b: Sequence[MarkerPass], added: Sequence[int],
                  rows: List[PassRow]) -> Dict[int, bool]:
    """Turn a removed/added pair in the same slot into one `renamed?` row.

    The evidence is position, *parent path* and call count: the same slot in the frame, under the same
    parent marker, doing the same number of calls. All three, because any one of them alone is a
    coincidence -- on a real mobile-vs-PC pair, position and call count alone suggested nineteen renames
    between two frames that share almost no passes, while none of them shared a parent. A pass at the top
    of the frame has no parent to match, so it is never suggested: "the same parent" has to be something
    that was true, not something that was vacuous.

    Returns which additions were consumed, so the caller does not also list them as new. The pairing is
    1:1 and only between rows still unmatched, so one suggestion never consumes another's partner; where
    several candidates exist the earliest position wins, which is deterministic.
    """
    added_set = set(added)
    consumed: Dict[int, bool] = {}
    for row_index, row in enumerate(rows):
        if row.status != 'removed' or row.a is None:
            continue
        other_index = row.a_index
        if other_index < 0 or other_index not in added_set or other_index in consumed:
            continue
        other = b[other_index]
        parent = _parent(row.a.path)
        if not parent or other.calls != row.a.calls or _parent(other.path) != parent:
            continue      # no parent at all is not "the same parent": a vacuous match is not evidence
        consumed[other_index] = True
        rows[row_index] = PassRow(
            'renamed?', '%s  ->  %s' % (row.a.path, other.path), row.a, other, row.a_index,
            other_index,
            'same slot (pass %d), the same parent marker (`%s`) and the same %d call(s): the file cannot '
            'say whether this pass was renamed or replaced'
            % (other_index + 1, _parent(other.path) or '(none)', other.calls))
    return consumed


def cmd_passdiff(path_a: str, path_b: str, show_all: bool = False) -> int:
    """`passdiff <a.rdc> <b.rdc> [--all]` -- the two captures' marker trees, side by side.

    Exactly what the file can say, and no more: which pass paths exist in both (with the chunk range and
    call count of each), which exist in only one, and which pairs look like a rename. A capture whose
    chunks cannot be named (no `renderdoc-src`, `README.md` §1.1) is reported as *not looked at* rather
    than as having no passes.
    """
    lists: List[Optional[List[MarkerPass]]] = []
    for path in (path_a, path_b):
        try:
            lists.append(marker_passes(path))
        except rdc_stream.FrameError as exc:
            print('error: %s' % exc)
            return 1
    if lists[0] is None or lists[1] is None:
        print('error: no chunk-name map: the RenderDoc source tree was not found (README §1.1), so no '
              'chunk can be named and a pass list cannot be read')
        return 1

    a, b = lists[0], lists[1]
    rows = align_passes(a, b)

    print('A         : %s' % path_a)
    print('B         : %s' % path_b)
    print('passes    : %d in A, %d in B (a pass is a marker with a call inside it)' % (len(a), len(b)))
    counted: Dict[str, int] = {}
    for row in rows:
        counted[row.status] = counted.get(row.status, 0) + 1
    print('verdicts  : %s, %d path(s) in both'
          % (', '.join('%s %d' % (name, counted.get(name, 0))
                       for name in ('added', 'removed', 'renamed?')),
             counted.get('same', 0) + counted.get('renamed?', 0)))

    both = [row for row in rows if row.status == 'same']
    if both:
        print('--- in both, by pass path ---')
        print('%-5s %-16s %-16s %-8s %-8s %s'
              % ('#', 'chunks A', 'chunks B', 'calls A', 'calls B', 'pass'))
        shown = both if show_all else both[:IN_BOTH_LIMIT]
        for row in shown:
            assert row.a is not None and row.b is not None
            print('%-5d %-16s %-16s %-8d %-8d %s'
                  % (row.a_index + 1, '%d..%d' % (row.a.first_chunk, row.a.last_chunk),
                     '%d..%d' % (row.b.first_chunk, row.b.last_chunk), row.a.calls, row.b.calls,
                     row.path))
        if len(both) > len(shown):
            print('  ... %d more (--all prints every row)' % (len(both) - len(shown)))
        for row in both:
            if row.note:
                print('  note    %s: %s' % (row.path, row.note))

    for status, label in (('removed', 'only in A'), ('added', 'only in B'),
                          ('renamed?', 'possible rename')):
        group = [row for row in rows if row.status == status]
        if not group:
            continue
        print('--- %s (%d) ---' % (label, len(group)))
        for row in group:
            side = row.a if row.a is not None else row.b
            assert side is not None
            print('  %-52s calls %-5d chunks %d..%d%s'
                  % (row.path, side.calls, side.first_chunk, side.last_chunk,
                     '   (%s)' % row.note if row.note else ''))

    print("note      : chunk indices are the file's numbering, not the engine's event ids "
          '(REFERENCE §9): `replay_dump draws` prints the engine\'s own, and both take a marker path')
    return 0


__all__ = [
    'IN_BOTH_LIMIT',
    'MarkerPass',
    'PassRow',
    'UNNAMED',
    'align_passes',
    'cmd_passdiff',
    'marker_passes',
]
