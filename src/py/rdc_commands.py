"""The commands that answer questions about a capture -- each one prints, and the entry point dispatches to them by name."""

from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_cache import *  # noqa: F401,F403
from rdc_dxbc import *  # noqa: F401,F403
from rdc_resources import *  # noqa: F401,F403
from rdc_payloads import *  # noqa: F401,F403
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_cache  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_resources  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_scan  # noqa: F401  (the whole-stream scans, which it may run in slices)
import rdc_table  # noqa: F401  (the csv/markdown shapes of the row commands)

import os
import re
import time

from typing import Dict, List, Optional, Sequence, Tuple

def cmd_sections(path: str) -> None:
    """Print the container header, metadata and every section, then the frame-capture stats."""
    info = parse_container(path)
    print('file        :', path)
    print('size        : %d (%.2f MB)' % (info['size'], info['size'] / 1048576.0))
    print('rdc version : 0x%x   progVersion %s' % (info['version'], info['progVersion']))
    print('thumbnail   : %dx%d %d bytes' % info['thumbnail'])
    print('driver      : %s (id=%d)' % (info['meta']['driverName'], info['meta']['driverID']))
    for s in info['sections']:
        print('  type=%-3d flags=0x%x ver=%-3d comp=%-10d uncomp=%-10d %s'
              % (s['type'], s['flags'], s['version'], s['compLen'], s['uncompLen'], s['name']))
    stream_len, how = stream_stats(path, info)
    print('framecapture stream: %d bytes  (expected %d)  [%s]'
          % (stream_len, info['sections'][0]['uncompLen'], how))

def cmd_blocks(path: str) -> None:
    """Print per section: name, flags and the first 16 bytes hex (enough to spot compression)."""
    info = parse_container(path)
    for s in info['sections']:
        blob = info['_data'][s['dataOffset']:s['dataOffset'] + s['compLen']]
        print('%-40s flags=0x%x first16=%s' % (s['name'], s['flags'], blob[:16].hex()))

def cmd_cache(args: Optional[Sequence[str]] = None) -> int:
    """Inspect or clear the decompressed-stream cache: `cache [list|dir|clear]`.

    Returns a process exit code: 0 normally, 2 for an unknown sub-command. This is the only
    command that needs no capture file.
    """
    argv = list(args or [])
    what = argv[0] if argv else 'list'
    if what == 'dir':
        print(rdc_cache.cache_dir())
        return 0
    if what == 'clear':
        count, freed = cache_clear()
        print('removed %d cache files (%.1f MB) from %s'
              % (count, freed / 1048576.0, rdc_cache.cache_dir()))
        return 0
    if what != 'list':
        print('usage: rdc_analysis.py cache [list|dir|clear]')
        return 2
    entries = cache_entries()
    print('cache dir : %s' % rdc_cache.cache_dir())
    print('entries   : %d, %.1f MB of streams'
          % (len(entries), sum(e['streamLen'] for e in entries) / 1048576.0))
    for e in entries:
        try:
            built = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(e['file'])))
        except OSError:
            built = '?'
        print('  %-12d %-18s %s  %s'
              % (e['streamLen'], _method_label(e['method'], e['blocks']), built, e['srcPath']))
    unusable = len(_cache_names()) - len(entries)
    if unusable:
        print('unusable  : %d (left over from another version or an interrupted write;'
              ' `cache clear` removes them)' % unusable)
    if not entries:
        print('  (nothing cached yet -- any command that needs the stream fills it)')
    return 0

def cmd_descriptors(path: str, limit: int = 200, heap_filter: Optional[str] = None,
                    fmt: str = 'table') -> None:
    """List the written slots of every descriptor heap: heap, slot, kind and resource.

    This is what makes a `draws` line like `rp0=heap298[138458]` readable: the binding names a slot
    in a heap, and this says what the capture wrote into it. Only written slots are listed (a heap
    can have a million), and the filter matches either the heap id or its name.

    `--format csv|markdown` prints one row per written slot, with the heap's id and name repeated on
    each -- the terminal form's two levels are one table's rows (rdc_table).
    """
    _info, stream, _how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    resources = parse_resource_table(stream, names)
    heaps = parse_descriptor_heaps(stream, names)
    written = sum(len(slots) for slots in heaps.values())
    rows: List[Tuple[int, str, int, str, str]] = []    # heap, heap name, slot, kind, target
    shown = 0
    for heap in sorted(heaps):
        label = resources.get(heap, {}).get('name') or ''
        if heap_filter and heap_filter != str(heap) and heap_filter.lower() not in label.lower():
            continue
        if limit and shown >= limit:
            break
        for index in sorted(heaps[heap]):
            if limit and shown >= limit:
                break
            info = heaps[heap][index]
            target = 'sampler' if info['kind'] == 'sampler' else (
                'res%d%s' % (info['resource'], rdc_resources._name_suffix(resources, info['resource'])))
            rows.append((heap, label, index, info['kind'], target))
            shown += 1
    notes = ['descriptor heaps: %d, %d written slots' % (len(heaps), written),
             'total slots: %d (shown %d)' % (written, shown)]
    if fmt != 'table':
        return rdc_table.emit(fmt, ('heap', 'heapName', 'slot', 'kind', 'target'), rows, notes)
    print(notes[0])
    heap_printed: Optional[int] = None
    for heap, label, index, kind, target in rows:
        if heap != heap_printed:
            print('heap%d %s' % (heap, label or '-'))
            heap_printed = heap
        print('  [%-9d] %-8s %s' % (index, kind, target))
    print(notes[1])

def cmd_resources(path: str, limit: int = 200, name_filter: Optional[str] = None,
                  fmt: str = 'table') -> None:
    """List the resource table: id, kind, size or dimensions, and the capture's own name.

    The names are the application's (UE names its buffers, e.g. `SkyAtmosphere.SkyViewLut`), which
    is what makes `res342` in `draws` readable. Ids that are only named -- heaps, queues, fences --
    have no descriptor and show `-`. `limit` counts rows and 0 means no limit, so
    `resources <rdc> 0 lut` answers "which buffers mention a LUT".

    `--format csv|markdown` prints the same rows, and the counts around them go to stderr: a
    spreadsheet wants columns, not a header line (rdc_table).
    """
    _info, stream, _how = load_stream(path)
    table = parse_resource_table(stream, rdc_chunkmap.load_chunk_names())
    formats = rdc_resources.load_format_names()
    described = sum(1 for r in table.values() if r['kind'] != 'unknown')
    named = sum(1 for r in table.values() if r['name'])
    rows: List[Tuple[int, str, str, str]] = []    # id, kind, size, name
    shown = 0
    for rid in sorted(table):
        info = table[rid]
        if name_filter and name_filter.lower() not in info['name'].lower():
            continue
        if not limit or shown < limit:
            rows.append((rid, info['kind'], rdc_resources._resource_size(info, formats),
                         info['name'] or ''))
            shown += 1
    notes = ['resources: %d ids (%d with a descriptor, %d named)' % (len(table), described, named)]
    if not formats:
        notes.append('note: no format names at all (no source tree, no bundled table); showing numbers')
    notes.append('total resources: %d (shown %d)' % (len(table), shown))
    if fmt != 'table':
        return rdc_table.emit(fmt, ('id', 'kind', 'size', 'name'), rows, notes)
    print(notes[0])
    if len(notes) == 3:
        print(notes[1])
    for rid, kind, size, name in rows:
        print('res%-8d %-9s %-34s %s' % (rid, kind, size, name or '-'))
    print(notes[-1])

def cmd_strings(path: str, minlen: int = 6, maxlines: int = 200) -> None:
    """Print the unique ASCII strings >= `minlen`, ranked by occurrence then first offset.

    The scan itself is `rdc_scan.scan_runs`, which splits it across processes when it is worth it (see
    REFERENCE 4.13); the ranking below is unchanged, and so is its output.
    """
    info, stream, how = load_stream(path)
    print('stream %d bytes [%s]' % (len(stream), how))
    counts, order = rdc_scan.scan_runs(stream, minlen, rdc_cache.stream_source(path, info))
    print('unique ascii strings >= %d: %d' % (minlen, len(counts)))
    # Ranked by count, ties by first offset -- and the tie-break needs no key of its own: a dict keeps
    # its insertion order, `counts` is filled in ascending first-offset order (the scan walks the stream
    # forward, and the parallel merge concatenates slices in offset order), and a stable sort keeps it.
    # Measured: the tuple key was 1.46 s of this command's 6.2 s over 1.16 M strings.
    ranked = sorted(counts, key=counts.__getitem__, reverse=True)
    for s in ranked[:maxlines]:
        print('%6d  @0x%-9x %s' % (counts[s], order[s], s[:150]))

#: What makes a string look like an object, a shader or a pass name. Compiled once: this is applied to
#: every unique string the scan found (427,823 of them on `desktop-2`).
_NAME_LIKE = re.compile(
    r'(Shader|shader|BasePass|Lightmap|LightMap|Volumetric|IndirectLighting|HISM|Instanced|'
    r'StaticMesh|Sphere|Mobile|CachedPoint|NoLightMap|Policy|Permutation|FScreenPass|SceneColor|'
    r'Primitive|View|FShader|VertexFactory)')

def cmd_names(path: str, minlen: int = 10) -> None:
    """Strings that look like UE/RenderDoc object or shader names."""
    info, stream, how = load_stream(path)
    print('stream %d bytes [%s]' % (len(stream), how))
    _counts, seen = rdc_scan.scan_runs(stream, minlen, rdc_cache.stream_source(path, info),
                                       counts=False)
    interesting = [s for s in seen if _NAME_LIKE.search(s)]
    print('interesting name-like strings: %d' % len(interesting))
    for s in sorted(interesting, key=lambda x: seen[x])[:400]:
        print('  @0x%-9x %s' % (seen[s], s[:160]))

def cmd_grep(path: str, pattern: str, context: int = 200, cap: int = 30) -> None:
    """Print every byte-occurrence of the ASCII `pattern`, each with +/-`context` bytes of text."""
    _info, stream, how = load_stream(path)
    needle = pattern.encode()
    print('stream %d bytes [%s]; grep %r' % (len(stream), how, pattern))
    pos = 0
    hits = 0
    while hits < cap:
        i = stream.find(needle, pos)
        if i < 0:
            break
        hits += 1
        lo, hi = max(0, i - context), min(len(stream), i + context)
        text = ''.join(chr(c) if 32 <= c < 127 else '.' for c in stream[lo:hi])
        print('--- hit %d @0x%x ---\n%s' % (hits, i, text))
        pos = i + 1
    if not hits:
        print('NOT FOUND')
    else:
        print('(%d hits shown, cap %d)' % (hits, cap))

def cmd_dump(path: str, start: int, length: int, minlen: int = 4) -> None:
    """Print every ASCII string (>= minlen) inside [start, start+length)."""
    _info, stream, how = load_stream(path)
    start, length = int(start), int(length)
    end = min(len(stream), start + length)
    print('stream %d bytes [%s]; window 0x%x..0x%x' % (len(stream), how, start, end))
    n = 0
    for off, s in string_runs(stream, minlen, start, end):
        print('  @0x%-9x (%3d) %s' % (off, len(s), s[:180]))
        n += 1
        if n > 500:
            print('  ... truncated at 500 strings')
            break
    if n == 0:
        print('  (no strings)')

def cmd_rootsig(path: str, limit: int = 40, fmt: str = 'table') -> None:
    """Print every root signature the capture creates: flags, cost, parameters and ranges.

    `--format csv|markdown` flattens the two levels into one row per parameter, with the signature's
    own fields repeated on each: a signature with no parameters is one row with an empty one
    (rdc_table).
    """
    info, stream, _how = load_stream(path)
    sigs = parse_root_signatures(stream)
    binds = shader_bind_names(stream, rdc_cache.stream_source(path, info))
    rows: List[Tuple[int, str, int, int, str, str]] = []    # id, version, dwords, samplers, flags, parameter
    for rid, sig in sorted(sigs.items())[:limit]:
        flags = ' '.join(nm for bit, nm in ROOT_FLAGS if sig['flags'] & bit) or 'none'
        labels = [_root_param_label(sig, binds, i) for i in range(len(sig['params']))] or ['']
        for label in labels:
            rows.append((rid, sig['version'], sig['dwords'], sig['samplers'],
                         '0x%x [%s]' % (sig['flags'], flags), label))
    notes = ['root signatures: %d%s' % (len(sigs), '' if binds else
                                        '  (no RDEF reflection in this capture: parameters are typed,'
                                        ' not named)')]
    if len(sigs) > limit:
        notes.append('... %d more' % (len(sigs) - limit))
    if fmt != 'table':
        return rdc_table.emit(fmt, ('id', 'version', 'dwords', 'samplers', 'flags', 'parameter'),
                              rows, notes)
    print(notes[0])
    printed: Optional[int] = None
    for rid, version, dwords, samplers, flags, label in rows:
        if rid != printed:
            print('res%-7d ver=%s dwords=%d samplers=%d flags=%s'
                  % (rid, version, dwords, samplers, flags))
            printed = rid
        if label:
            print('  %s' % label)
    if len(notes) > 1:
        print(notes[1])

def cmd_chunk_detail(path: str, index: int, hexlen: int = 160) -> None:
    """Full inspector for one chunk: header, decoded fields, hex dump and payload strings."""
    _info, stream, _how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    for i, ch in enumerate(iter_chunks(stream), 1):
        if i != index:
            continue
        nm = names.get(ch['id'], 'Chunk%d' % ch['id'])
        print('chunk #%d  @0x%x  id=%d (%s)  flags=0x%x  length=%d'
              % (i, ch['off'], ch['id'], nm, ch['flags'], ch['length']))
        print('payload @0x%x (header+metadata = %d bytes)'
              % (ch['payload_offset'], ch['payload_offset'] - ch['off']))
        blob = chunk_payload(stream, ch)
        for line in decode_chunk(nm, blob):
            print('  ' + line)
        for o in range(0, min(len(blob), hexlen), 16):
            row = blob[o:o + 16]
            print('  %04x  %-47s  %s' % (o, ' '.join('%02x' % c for c in row),
                                         ''.join(chr(c) if 32 <= c < 127 else '.' for c in row)))
        strs = [s for _, s in string_runs(blob, 4)]
        if strs:
            print('  strings:')
            for s in strs[:40]:
                print('    %s' % s[:160])
        return
    print('chunk #%d not found' % index)

def cmd_chunks(path: str, limit: int = 200, name_filter: Optional[str] = None) -> None:
    """List chunks: index, offset, name, payload length and a preview of the payload strings."""
    _info, stream, how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    print('stream %d bytes [%s]; known chunk names: %d' % (len(stream), how, len(names)))
    if not names:
        # Only when there is nothing to name them with at all: no source tree *and* an empty bundled table.
        # A tree that is merely absent is covered by `rdc_chunknames`, which warns about its own version on
        # stderr (`load_chunk_names`), so saying it again here would be the same fact twice.
        print('WARNING: no chunk names at all (no source tree, no bundled table) -> numeric chunk ids')
    total = shown = 0
    for ch in iter_chunks(stream):
        total += 1
        nm = names.get(ch['id'], 'Chunk%d' % ch['id'])
        if name_filter and name_filter.lower() not in nm.lower():
            continue
        if not limit or shown < limit:         # 0 = no limit, like `resources`
            strs = chunk_strings(stream, ch)
            print('#%-6d @0x%-10x %-40s len=%-8d%s'
                  % (total, ch['off'], nm, ch['length'], (' ' + ' | '.join(strs)) if strs else ''))
            shown += 1
    print('total chunks: %d (shown %d)' % (total, shown))

def cmd_verify(path: str) -> int:
    """Check the chunk framing, the alignment padding and the payload lengths of a capture.

    Returns 0 when nothing is wrong, 1 when a frame or a payload length does not check out, so it
    can gate a script. These are the checks that would have caught past decoding mistakes -- the
    index-buffer payload length being the most recent one.
    """
    _info, stream, how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    problems, notes = check_stream(stream, names)
    print('stream %d bytes [%s]' % (len(stream), how))
    print('problems: %d' % len(problems))
    for problem in problems:
        print('  ' + problem)
    if notes:
        print('notes: %d' % len(notes))
        for note in notes:
            print('  ' + note)
    return 1 if problems else 0

def cmd_draws(path: str, max_draws: int = 80, fmt: str = 'table') -> None:
    """Per-draw table: marker path, PSO, constant buffers (resourceId+offset), vertex streams, args.

    The state printed for a draw is the state of its *command list* at that point -- everything that
    is still bound, not only what changed since the previous draw (see `DrawState`). Dispatches
    report the compute root bindings, draws the graphics ones plus the vertex streams and the index
    buffer. A root descriptor table is reported as `heap<id>[index]` plus what the capture wrote
    into that slot (`-> srv res2233[SkyViewLut]`, see `parse_descriptor_heaps`); the descriptors
    after the first are the root signature's business (`rootsig` prints the ranges that say how many each
    covers). Bound resources are annotated
    with the name the capture gave them, when it has one (`[SceneUniformBuffer]`, see
    `parse_resource_table`), and every `rpN` with what its root parameter *is*
    (`rp2(cbv b1 s0)`, see `_root_param_label`).

    `--format csv|markdown` is one row per draw, with the indented state lines folded into one cell
    separated by `; ` -- a spreadsheet cannot hold a second level either (rdc_table).
    """
    info, stream, _how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    resources = parse_resource_table(stream, names)
    heaps = parse_descriptor_heaps(stream, names)
    sigs = parse_root_signatures(stream, names)
    binds = shader_bind_names(stream, rdc_cache.stream_source(path, info))
    stack: List[str] = []
    states: Dict[int, DrawState] = {}
    n_draw = 0
    rows: List[Tuple[int, str, Optional[int], str, str, List[str]]] = []    # chunk, path, pso, args, call, state
    for idx, ch in enumerate(iter_chunks(stream), 1):
        nm = names.get(ch['id'], '')
        # The payload is sliced only for the chunks this loop reads (`STATE_CHUNKS` is the setters plus
        # `List_Reset`). Slicing every one of `desktop-2`'s 29,212 payloads copies 1.47 GB to throw
        # it away and measured 1.08 s of this command's 2.8 s; `summary` and `markers` never paid it
        # because they hand `chunk_strings` the chunk and it slices for itself.
        blob = chunk_payload(stream, ch) if (nm in DRAW_CHUNKS or nm in STATE_CHUNKS) else b''
        if nm == 'PushMarker':
            s = chunk_strings(stream, ch, 3, 1)
            stack.append(s[0] if s else '?')
        elif nm == 'PopMarker':
            if stack:
                stack.pop()
        elif nm in DRAW_CHUNKS:
            n_draw += 1
            if n_draw <= max_draws:
                if nm == 'List_DrawIndexedInstanced' and len(blob) >= 28:
                    args = 'idx=%d inst=%d' % (u32(blob, 8), u32(blob, 12))
                elif nm == 'List_DrawInstanced' and len(blob) >= 24:
                    args = 'verts=%d inst=%d' % (u32(blob, 8), u32(blob, 12))
                else:
                    args = 'x=%d y=%d z=%d' % (u32(blob, 8), u32(blob, 12), u32(blob, 16))
                st = states.get(u64(blob, 0)) if len(blob) >= 8 else None
                lines: List[str] = []
                if nm == 'List_ExecuteIndirect':
                    # it can be a graphics or a compute call, so both namespaces are reported
                    lines += draw_state_lines(st, compute=True, resources=resources, heaps=heaps,
                                              sigs=sigs, binds=binds)
                    lines += draw_state_lines(st, compute=False, resources=resources, heaps=heaps,
                                              sigs=sigs, binds=binds)
                else:
                    lines += draw_state_lines(st, compute=nm in COMPUTE_CHUNKS, resources=resources,
                                              heaps=heaps, sigs=sigs, binds=binds)
                rows.append((idx, ' / '.join(stack)[-24:], st['pso'] if st else None, args,
                             nm.replace('List_', ''), lines))
        elif _apply_state_chunk(nm, blob, states):
            pass                       # a tracked setter: it changes the state, it prints nothing
    notes = ['total draws/dispatches: %d' % n_draw]
    if fmt != 'table':
        return rdc_table.emit(fmt, ('chunk', 'path', 'pso', 'args', 'call', 'state'),
                              ((idx, where, pso, args, call, '; '.join(lines))
                               for idx, where, pso, args, call, lines in rows), notes)
    print('%-7s %-24s %-8s %-9s %s' % ('chunk', 'pass / primitive', 'pso', 'args', 'state'))
    for idx, where, pso, args, call, lines in rows:
        print('#%-6d %-24s %-8s %-9s %s' % (idx, where, pso, args, call))
        for line in lines:
            print('        %s' % line)
    print(notes[0])

def cmd_summary(path: str, fmt: str = 'table') -> None:
    """Chunk/draw/marker counts, the chunk-type histogram (top 40) and all markers in order.

    `--format csv|markdown` prints the long form -- `section,name,value` rows -- because the three
    parts are three different shapes and a table with one column per part would be empty wherever the
    others are (rdc_table). The caps are the terminal form's: the top 40 chunk types, the first 120
    markers, with the rest counted in a note.
    """
    _info, stream, _how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    hist: Dict[str, int] = {}
    markers: List[Tuple[int, str, str]] = []
    draws: List[Tuple[int, str]] = []
    total = 0
    for ch in iter_chunks(stream):
        total += 1
        nm = names.get(ch['id'], 'Chunk%d' % ch['id'])
        hist[nm] = hist.get(nm, 0) + 1
        if nm in MARKER_CHUNKS:
            s = chunk_strings(stream, ch, 3, 1)
            markers.append((total, nm, s[0] if s else ''))
        if nm in DRAW_CHUNKS:
            draws.append((total, nm))
    top = sorted(hist.items(), key=lambda kv: -kv[1])[:40]
    if fmt != 'table':
        rows: List[Tuple[object, ...]] = [
            ('frame', 'chunks', total), ('frame', 'draws', len(draws)), ('frame', 'markers', len(markers))]
        rows += [('chunk-type', nm, c) for nm, c in top]
        rows += [('marker', '#%d' % i, '%s %s' % (nm, s[:110])) for i, nm, s in markers[:120]]
        notes = ['  ... %d more' % (len(markers) - 120)] if len(markers) > 120 else []
        return rdc_table.emit(fmt, ('section', 'name', 'value'), rows, notes)
    print('chunk count        : %d' % total)
    print('draw/dispatch calls: %d' % len(draws))
    print('markers            : %d' % len(markers))
    print('--- chunk type histogram ---')
    for nm, c in top:
        print('  %-44s %d' % (nm, c))
    print('--- markers in order ---')
    for i, nm, s in markers[:120]:
        print('  #%-6d %-16s %s' % (i, nm, s[:110]))
    if len(markers) > 120:
        print('  ... %d more' % (len(markers) - 120))

def cmd_markers(path: str) -> None:
    """Print every marker chunk: index, kind and up to 3 payload strings."""
    _info, stream, _how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    n = 0
    for ch in iter_chunks(stream):
        n += 1
        nm = names.get(ch['id'], '')
        if nm in MARKER_CHUNKS:
            print('#%-6d %-16s %s' % (n, nm, ' | '.join(chunk_strings(stream, ch, 3, 3))))

def cmd_dump_chunk(path: str, index: int, outfile: str) -> None:
    """Write one chunk's payload verbatim to `outfile`."""
    _info, stream, _how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    for i, ch in enumerate(iter_chunks(stream), 1):
        if i == index:
            blob = chunk_payload(stream, ch)
            with open(outfile, 'wb') as f:
                f.write(blob)
            print('chunk #%d %s (id=%d, flags=0x%x) -> %s (%d bytes)'
                  % (i, names.get(ch['id'], '?'), ch['id'], ch['flags'], outfile, len(blob)))
            return
    print('chunk #%d not found (stream has fewer chunks)' % index)

def cmd_dump_shaders(path: str, outdir: str) -> None:
    """Write every DXBC/DXIL container to `outdir` as a `.dxil` file plus an index in `shaders.txt`.

    This is the offline route to the D3D12 harness's input (ROADMAP §4.5) and a way to hand a shader to
    `dxc` or `dxil-spirv` yourself. What is *in* the shader is not summarised here: that is the
    reflection's job, and the reflection is the replay driver's (REFERENCE §9).
    """
    info, stream, _how = load_stream(path)
    os.makedirs(outdir, exist_ok=True)
    lines: List[str] = []
    n = 0
    for off, size, h, parts in parse_dxil_containers(stream, rdc_cache.stream_source(path, info)):
        names = [p[0] for p in parts]
        if 'RTS0' in names:
            continue
        n += 1
        blob = stream[off:off + size]
        fn = os.path.join(outdir, 'shader_%02d_%s.dxil' % (n, h[:12]))
        with open(fn, 'wb') as f:
            f.write(blob)
        lines.append('shader_%02d  hash=%s  size=%d  parts=%s' % (n, h, size, ','.join(names)))
        lines.append('    file    : %s' % fn)
    with open(os.path.join(outdir, 'shaders.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('wrote %d shader blobs + shaders.txt to %s' % (n, outdir))

__all__ = [
    'cmd_blocks',
    'cmd_cache',
    'cmd_chunk_detail',
    'cmd_chunks',
    'cmd_descriptors',
    'cmd_draws',
    'cmd_dump',
    'cmd_dump_chunk',
    'cmd_dump_shaders',
    'cmd_grep',
    'cmd_markers',
    'cmd_names',
    'cmd_resources',
    'cmd_rootsig',
    'cmd_sections',
    'cmd_strings',
    'cmd_summary',
    'cmd_verify',
]
