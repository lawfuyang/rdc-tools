"""The detectors that read the capture's chunk stream: marker balance, draws outside any marker, and calls that can only produce nothing."""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_cache  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_stream  # noqa: F401  (used qualified: the loader is called from inside functions)

from typing import Dict, List, Optional, Sequence, Tuple

PUSH_MARKER_CHUNKS = ('PushMarker', 'Queue_BeginEvent')
POP_MARKER_CHUNKS = ('PopMarker', 'Queue_EndEvent')

#: How many unattributed draws are named before the rest are counted.
UNATTRIBUTED_LIMIT = 10

def _named_chunks(path: str, wanted: Sequence[str]) -> Optional[List[Tuple[int, str, bytes]]]:
    """`(chunk index, name, payload)` for the chunks whose name is in `wanted`, or None if this tool cannot
    name chunks at all.

    The import is *inside* the function on purpose: `rdc_analysis` imports this module (the CLI dispatches
    `cmd_report` through it and the tests call `R.cmd_report`), so a module-level import back would be a
    cycle. These three detectors are the only thing here that needs the chunk stream, so the cycle is broken
    once, in the one place they share.

    The names come from the RenderDoc source tree. Without it every chunk is a number and a detector cannot
    tell a marker from a draw -- None says that, and the caller reports the detector as *skipped* rather than
    letting a report say "nothing found" about something it could not look at.
    """

    _info, stream, _how = rdc_cache.load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    if not names:
        return None

    found: List[Tuple[int, str, bytes]] = []
    for index, chunk in enumerate(rdc_stream.iter_chunks(stream), 1):
        name = names.get(chunk['id'], '')
        if name in wanted:
            found.append((index, name, rdc_stream.chunk_payload(stream, chunk)))
    return found

def detect_marker_balance(path: str) -> Optional[List[RedFlag]]:
    """Markers that do not balance (certain).

    A `PopMarker` with nothing pushed, or pushes still open at the end of the stream. Evidence is the chunk
    index, never an event id: the two are different spaces and the stream is all this detector can see
    (REFERENCE §9). Drawing a pass boundary from an unbalanced tree is how a report attributes work to the
    wrong pass, so this is reported before anything tries to.
    """
    chunks = _named_chunks(path, PUSH_MARKER_CHUNKS + POP_MARKER_CHUNKS)
    if chunks is None:
        return None

    flags: List[RedFlag] = []
    depth = 0
    extra_pops: List[int] = []
    open_at: List[int] = []
    for index, name, _payload in chunks:
        if name in PUSH_MARKER_CHUNKS:
            depth += 1
            open_at.append(index)
        elif depth > 0:
            depth -= 1
            open_at.pop()
        else:
            extra_pops.append(index)

    if extra_pops:
        flags.append({
            'detector': 'marker-imbalance',
            'what': '%d marker pop(s) with nothing pushed' % len(extra_pops),
            'evidence': ['chunk %s' % ', '.join(str(index) for index in extra_pops[:UNATTRIBUTED_LIMIT])],
            'certainty': 'certain',
            'unproven': True,
        })
    if open_at:
        flags.append({
            'detector': 'marker-imbalance',
            'what': '%d marker(s) never popped: the tree is still open where the stream ends' % len(open_at),
            'evidence': ['chunk %s' % ', '.join(str(index) for index in open_at[:UNATTRIBUTED_LIMIT])],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags

def detect_unattributed_draws(path: str) -> Optional[List[RedFlag]]:
    """Draws and dispatches outside any marker (certain).

    A hygiene note, not a bug: plenty of engines draw outside markers. It matters here because the report
    attributes work per pass, and a draw with no marker has nothing to be attributed *to*.
    """

    chunks = _named_chunks(path, PUSH_MARKER_CHUNKS + POP_MARKER_CHUNKS + tuple(rdc_chunkmap.DRAW_CHUNKS))
    if chunks is None:
        return None

    depth = 0
    unattributed: List[Tuple[int, str]] = []
    for index, name, _payload in chunks:
        if name in PUSH_MARKER_CHUNKS:
            depth += 1
        elif name in POP_MARKER_CHUNKS:
            depth = max(0, depth - 1)
        elif depth == 0:
            unattributed.append((index, name))

    if not unattributed:
        return []
    counted: Dict[str, int] = {}
    for _index, name in unattributed:
        counted[name] = counted.get(name, 0) + 1
    return [{
        'detector': 'unattributed-draws',
        'what': '%d draw(s) or dispatch(es) outside any marker -- a note, not a bug: there is no marker path '
                'to attribute them to' % len(unattributed),
        'evidence': ['%s: chunk %s' % (name, ', '.join(str(index) for index, other in unattributed
                                                       if other == name)) [:160]
                     for name in sorted(counted)],
        'certainty': 'certain',
        'unproven': True,
    }]

def detect_zero_work(path: str) -> Optional[List[RedFlag]]:
    """Draws and dispatches that can only produce nothing (certain).

    Zero indices, zero vertices, zero instances or a zero dispatch dimension: the call is in the stream and
    the GPU does nothing. The counts come from the same payload decoder `chunks`/`draws` use, so the fields
    are read in one place only.
    """

    wanted = ('List_DrawIndexedInstanced', 'List_DrawInstanced', 'List_Dispatch')
    chunks = _named_chunks(path, wanted)
    if chunks is None:
        return None

    flags: List[RedFlag] = []
    for index, name, payload in chunks:
        if len(payload) < 8:
            continue
        if name == 'List_DrawIndexedInstanced' and len(payload) >= 28:
            indices, instances = rdc_stream.u32(payload, 8), rdc_stream.u32(payload, 12)
            described = '%d indices, %d instance(s)' % (indices, instances)
            empty = indices == 0 or instances == 0
        elif name == 'List_DrawInstanced' and len(payload) >= 24:
            vertices, instances = rdc_stream.u32(payload, 8), rdc_stream.u32(payload, 12)
            described = '%d vertices, %d instance(s)' % (vertices, instances)
            empty = vertices == 0 or instances == 0
        elif name == 'List_Dispatch' and len(payload) >= 20:
            groups = (rdc_stream.u32(payload, 8), rdc_stream.u32(payload, 12), rdc_stream.u32(payload, 16))
            described = 'dispatch %dx%dx%d' % groups
            empty = 0 in groups
        else:
            continue
        if empty:
            flags.append({
                'detector': 'zero-work',
                'what': 'this call draws nothing: %s' % described,
                'evidence': ['%s at chunk %d' % (name, index)],
                'certainty': 'certain',
                'unproven': True,
            })
    return flags

#: `b0 s0` in a reflection row: the register and space a constant block is expected at. The `cbuffer[0]`
#: form is what `shaders <eid>` writes and it is the only reflection row whose format is pinned down here
#: by real output; the read-only and write-only resource rows are not parsed until a capture shows them.
__all__ = [
    'POP_MARKER_CHUNKS',
    'PUSH_MARKER_CHUNKS',
    'UNATTRIBUTED_LIMIT',
    '_named_chunks',
    'detect_marker_balance',
    'detect_unattributed_draws',
    'detect_zero_work',
]
