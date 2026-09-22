"""`formats <rdc>` — the offline half of the format coverage audit (the driver's `formats` is the other half).

Two questions, and they are the same question asked of two different things. The driver asks the **engine**
what it can *show*: for every format in the frame's texture list, what it is made of and whether a picture of
it is possible (REFERENCE §9). This asks the **file** what it *holds*: which formats the resource table names,
how many resources use each, what a reading of the DXGI name says about it, and — the part a summary must not
skip — what the payload walk actually decoded, what it deliberately did not, and why.

Why it is worth a command rather than a footnote in `resources`: a table that silently omits what it could not
read is worse than one that says *27 resources use a format no table here names*, because the omission looks
like an answer. That is the same rule the report generator follows with its "what this cannot say" section, and
the same one the driver's audit follows with its `needCast`/`noLayout` totals.

What it is built from is deliberately the structures the rest of the tool already keeps: `rdc_chunkmap`'s
`UNATTRIBUTED_CHUNKS` and `EXPECTED_LENGTHS`, and `WalkUses`' ledger (`failed`, `unresolved`, `seen`), which is
what `deps` and `memory` report from. A coverage number that came from its own private walk could disagree with
them, and the first reader to notice would be the one trusting this table.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import rdc_chunkmap
import rdc_resources
import rdc_table
import rdc_uses
from rdc_cache import load_stream

#: The classes a resource's format can fall into that need *saying* in the summary: the ones where a picture is
#: not simply a picture. A typeless format needs a cast before the display path can read it, a video layout has
#: no plain texel size, and a name this does not know is the case the audit exists to surface.
_NOT_PLAIN = ('typeless', 'yuv', 'other')

def cmd_formats(path: str, fmt: str = 'table') -> None:
    """`formats <rdc>`: which formats the file holds, and what the parse did with it.

    The table has one row per format, named where a name exists and numbered where one does not (an unnamed
    format is still a format a reader has to know about), with how many resources use it, what its *class* is,
    the components and their width, and the two flags that change what a picture of it means. `bytes` counts
    only what the file's own table gives a byte size for -- buffers and acceleration structures; a texture's
    bytes are not in that table, and a zero there would read as "this format costs nothing", so the column is
    named for what it is.

    Under the table, the payload coverage: how many chunks the walk named and how many it could not, how many
    payloads failed to parse, how many descriptor bindings pointed at a slot the capture never wrote, and which
    payload kinds are deliberately *not* attributed as uses (`UNATTRIBUTED_CHUNKS`, REFERENCE §4.15). Those are
    the "skipped, and why" numbers -- and they come from the ledger `deps` and `memory` print from, so the two
    cannot disagree.
    """
    _info, stream, _how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    resources = rdc_resources.parse_resource_table(stream, names)
    formats = rdc_resources.load_format_names()
    ledger = rdc_uses.walk_uses(stream, names, resources,
                                rdc_resources.parse_descriptor_heaps(stream, names))

    groups: Dict[int, Dict[str, Any]] = {}
    buffers = 0
    for _rid, info in resources.items():
        # Only a *texture* has a format in this table. A buffer has none, and an acceleration structure
        # (`blas`/`tlas`) has neither a format nor a size -- so both would arrive as one enormous row called
        # "UNKNOWN", which is a fact about resources rather than about formats and the row the table exists to
        # stay readable by. Counted, and said in the totals instead.
        if info['format'] == 0 and not info['kind'].startswith('texture'):
            buffers += 1
            continue
        entry = groups.get(info['format'])
        if entry is None:
            entry = {'resources': 0, 'buffers': 0, 'bytes': 0}
            groups[info['format']] = entry
        entry['resources'] += 1
        if info['kind'] == 'buffer':
            entry['buffers'] += 1
            entry['bytes'] += info['size']

    rows: List[Tuple[str, ...]] = []
    notices = 0
    for fmt_id, entry in sorted(groups.items(), key=lambda pair: -pair[1]['resources']):
        name = formats.get(fmt_id) or ('id %d' % fmt_id)
        shape = rdc_resources.classify_format(name)
        if shape['class'] in _NOT_PLAIN:
            notices += 1
        rows.append((name,
                     str(entry['resources']),
                     str(entry['bytes']),
                     shape['class'],
                     shape['layout'] or '-',
                     'srgb' if shape['srgb'] else ('block' if shape['blockCompressed'] else ''),
                     shape['note']))

    unattributed = [name for name in rdc_chunkmap.UNATTRIBUTED_CHUNKS if ledger['seen'].get(name)]
    failed = sum(ledger['failed'].values())
    notes = [
        'formats: %d format(s) over %d resource(s) -- %d named, %d not (%d buffer(s) have no format at all)' %
        (len(groups), len(resources), len([f for f in groups if formats.get(f)]),
         len([f for f in groups if not formats.get(f)]), buffers),
        'payloads: %d event(s) walked, %d distinct payload kind(s), %d payload(s) failed to parse' %
        (ledger['events'], len(ledger['seen']), failed),
        'not attributed as uses: %d kind(s) -- a resource only these touch reads as unused (REFERENCE §4.15)' %
        len(unattributed),
        'descriptor bindings to a slot the capture never wrote: %d' % ledger['unresolved'],
    ]
    if notices:
        notes.append('note: %d format(s) in the list above are not a plain picture: a cast, a video layout, or '
                     'a name no table here has' % notices)
    if fmt != 'table':
        return rdc_table.emit(fmt, ('format', 'resources', 'bytes', 'class', 'layout', 'flags', 'what it is'),
                              rows, notes)
    # The format line first, then the table, then everything the table is *not*: the payload coverage and the
    # counts a summary reads. Printing a sample of them (as an earlier version did) left the two numbers the
    # command exists for -- what was skipped, and why -- off the end of the output entirely.
    print(notes[0])
    for row in rows:
        print('%-28s %5s %12s  %-9s %-14s %-6s %s' % row)
    for line in notes[1:]:
        print(line)


__all__ = ['cmd_formats']
