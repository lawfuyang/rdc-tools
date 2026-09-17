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

def cmd_descriptors(path: str, limit: int = 200, heap_filter: Optional[str] = None) -> None:
    """List the written slots of every descriptor heap: heap, slot, kind and resource.

    This is what makes a `draws` line like `rp0=heap298[138458]` readable: the binding names a slot
    in a heap, and this says what the capture wrote into it. Only written slots are listed (a heap
    can have a million), and the filter matches either the heap id or its name.
    """
    _info, stream, _how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    resources = parse_resource_table(stream, names)
    heaps = parse_descriptor_heaps(stream, names)
    written = sum(len(slots) for slots in heaps.values())
    print('descriptor heaps: %d, %d written slots' % (len(heaps), written))
    shown = 0
    for heap in sorted(heaps):
        label = resources.get(heap, {}).get('name') or ''
        if heap_filter and heap_filter != str(heap) and heap_filter.lower() not in label.lower():
            continue
        if limit and shown >= limit:
            break
        print('heap%d %s' % (heap, label or '-'))
        for index in sorted(heaps[heap]):
            if limit and shown >= limit:
                break
            info = heaps[heap][index]
            target = 'sampler' if info['kind'] == 'sampler' else (
                'res%d%s' % (info['resource'], rdc_resources._name_suffix(resources, info['resource'])))
            print('  [%-9d] %-8s %s' % (index, info['kind'], target))
            shown += 1
    print('total slots: %d (shown %d)' % (written, shown))

def cmd_resources(path: str, limit: int = 200, name_filter: Optional[str] = None) -> None:
    """List the resource table: id, kind, size or dimensions, and the capture's own name.

    The names are the application's (UE names its buffers, e.g. `SkyAtmosphere.SkyViewLut`), which
    is what makes `res342` in `draws` readable. Ids that are only named -- heaps, queues, fences --
    have no descriptor and show `-`. `limit` counts rows and 0 means no limit, so
    `resources <rdc> 0 lut` answers "which buffers mention a LUT".
    """
    _info, stream, _how = load_stream(path)
    table = parse_resource_table(stream, rdc_chunkmap.load_chunk_names())
    formats = rdc_resources.load_format_names()
    described = sum(1 for r in table.values() if r['kind'] != 'unknown')
    named = sum(1 for r in table.values() if r['name'])
    print('resources: %d ids (%d with a descriptor, %d named)'
          % (len(table), described, named))
    if not formats:
        print('note: DXGI format names need the RenderDoc source (README 1.1); showing numbers')
    shown = 0
    for rid in sorted(table):
        info = table[rid]
        if name_filter and name_filter.lower() not in info['name'].lower():
            continue
        if not limit or shown < limit:
            print('res%-8d %-9s %-34s %s'
                  % (rid, info['kind'], rdc_resources._resource_size(info, formats), info['name'] or '-'))
            shown += 1
    print('total resources: %d (shown %d)' % (len(table), shown))

def cmd_strings(path: str, minlen: int = 6, maxlines: int = 200) -> None:
    """Print the unique ASCII strings >= `minlen`, ranked by occurrence then first offset."""
    _info, stream, how = load_stream(path)
    print('stream %d bytes [%s]' % (len(stream), how))
    counts: Dict[str, int] = {}
    order: Dict[str, int] = {}
    for off, s in string_runs(stream, minlen):
        counts[s] = counts.get(s, 0) + 1
        order.setdefault(s, off)
    print('unique ascii strings >= %d: %d' % (minlen, len(counts)))
    ranked = sorted(counts, key=lambda s: (-counts[s], order[s]))
    for s in ranked[:maxlines]:
        print('%6d  @0x%-9x %s' % (counts[s], order[s], s[:150]))

def cmd_names(path: str, minlen: int = 10) -> None:
    """Strings that look like UE/RenderDoc object or shader names."""
    _info, stream, how = load_stream(path)
    print('stream %d bytes [%s]' % (len(stream), how))
    seen: Dict[str, int] = {}
    for off, s in string_runs(stream, minlen):
        seen.setdefault(s, off)
    interesting = [s for s in seen if re.search(
        r'(Shader|shader|BasePass|Lightmap|LightMap|Volumetric|IndirectLighting|HISM|Instanced|'
        r'StaticMesh|Sphere|Mobile|CachedPoint|NoLightMap|Policy|Permutation|FScreenPass|SceneColor|'
        r'Primitive|View|FShader|VertexFactory)', s)]
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

def cmd_rootsig(path: str, limit: int = 40) -> None:
    """Print every root signature the capture creates: flags, cost, parameters and ranges."""
    _info, stream, _how = load_stream(path)
    sigs = parse_root_signatures(stream)
    binds = shader_bind_names(stream)
    print('root signatures: %d%s' % (len(sigs), '' if binds else
                                     '  (no RDEF reflection in this capture: parameters are typed,'
                                     ' not named)'))
    for rid, sig in sorted(sigs.items())[:limit]:
        flags = ' '.join(nm for bit, nm in ROOT_FLAGS if sig['flags'] & bit) or 'none'
        print('res%-7d ver=%s dwords=%d samplers=%d flags=0x%x [%s]'
              % (rid, sig['version'], sig['dwords'], sig['samplers'], sig['flags'], flags))
        for i in range(len(sig['params'])):
            print('  %s' % _root_param_label(sig, binds, i))
    if len(sigs) > limit:
        print('... %d more' % (len(sigs) - limit))

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
        print('WARNING: RenderDoc source not found at %r -> numeric chunk ids only' % rdc_chunkmap.RENDERDOC_SRC)
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

def cmd_draws(path: str, max_draws: int = 80) -> None:
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
    """
    _info, stream, _how = load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    resources = parse_resource_table(stream, names)
    heaps = parse_descriptor_heaps(stream, names)
    sigs = parse_root_signatures(stream, names)
    binds = shader_bind_names(stream)
    stack: List[str] = []
    states: Dict[int, DrawState] = {}
    n_draw = 0
    print('%-7s %-24s %-8s %-9s %s' % ('chunk', 'pass / primitive', 'pso', 'args', 'state'))
    for idx, ch in enumerate(iter_chunks(stream), 1):
        nm = names.get(ch['id'], '')
        blob = chunk_payload(stream, ch)
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
                print('#%-6d %-24s %-8s %-9s %s'
                      % (idx, ' / '.join(stack)[-24:], st['pso'] if st else None, args,
                         nm.replace('List_', '')))
                if nm == 'List_ExecuteIndirect':
                    # it can be a graphics or a compute call, so both namespaces are reported
                    _print_draw_state(st, compute=True, resources=resources, heaps=heaps, sigs=sigs,
                                      binds=binds)
                    _print_draw_state(st, compute=False, resources=resources, heaps=heaps, sigs=sigs,
                                      binds=binds)
                else:
                    _print_draw_state(st, compute=nm in COMPUTE_CHUNKS, resources=resources,
                                      heaps=heaps, sigs=sigs, binds=binds)
        elif _apply_state_chunk(nm, blob, states):
            pass                       # a tracked setter: it changes the state, it prints nothing
    print('total draws/dispatches: %d' % n_draw)

def cmd_summary(path: str) -> None:
    """Chunk/draw/marker counts, the chunk-type histogram (top 40) and all markers in order."""
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
    print('chunk count        : %d' % total)
    print('draw/dispatch calls: %d' % len(draws))
    print('markers            : %d' % len(markers))
    print('--- chunk type histogram ---')
    for nm, c in sorted(hist.items(), key=lambda kv: -kv[1])[:40]:
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

    This is the offline route to the D3D12 harness's input (ROADMAP §7.5) and a way to hand a shader to
    `dxc` or `dxil-spirv` yourself. What is *in* the shader is not summarised here: that is the
    reflection's job, and the reflection is the replay driver's (REFERENCE §9).
    """
    _info, stream, _how = load_stream(path)
    os.makedirs(outdir, exist_ok=True)
    lines: List[str] = []
    n = 0
    for off, size, h, parts in parse_dxil_containers(stream):
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
