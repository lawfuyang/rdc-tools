"""Offline: every sampler a frame reads through, and the mip range each one can reach (`samplers`).

A sampler is the one piece of pipeline state whose *values* decide what a texture lookup can return, and
until now this tool printed only how many a signature carries (`rootsig`: `samplers=6`). This module decodes
them and does the arithmetic that catches the class of bug the Khronos Vulkan tutorial works through end to
end: a texture that samples black because the sampler asks for a mip level that does not exist.

Where the values come from:

* a **static sampler** -- the signature's own array, at the offset the `RTS0` header carries (REFERENCE 4.24
  has the layout and the field order, which is `d3d12.h`'s because RenderDoc casts the runtime's own struct);
* a **heap sampler** -- a `Device_CreateSampler` slot, `D3D12_SAMPLER_DESC2` after the 16-byte descriptor.

How a sampler is paired with a texture, and why the pairing is printed as a *basis* rather than assumed:

* `table rpN` -- one descriptor table binds an SRV range and a sampler range, which is D3D12's own way of
  saying which sampler goes with which view. This is the only pairing the frame states outright.
* `space N` -- a static sampler is bound by the signature, not by a table, so the texture it is used with is
  a matter of the shader's registers: the pairs printed are the SRVs whose range shares the sampler's
  register space. That is a convention rather than a fact in the file, so the column says so.

The arithmetic, in the two certainties the report already uses: `maxLod < minLod` (a range D3D12 forbids),
`minLod` at or past the texture's mip count (the levels it asks for do not exist, so every fetch is clamped
to the smallest one), and -- as observations -- mip mapping switched off for a texture that has a chain, and
a comparison filter reading a format that is not a depth format.

**Measured on this corpus**: `desktop-1` creates no heap sampler at all (`CreateSampler` appears zero times
in its stream and no heap holds a sampler slot), so what the corpus verifies here is the static half; the
heap half is covered by the fixtures in `tests/test_rdc_samplers.py`.
"""
from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_cache import *  # noqa: F401,F403
from rdc_payloads import *  # noqa: F401,F403
from rdc_resources import *  # noqa: F401,F403
import rdc_cache  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_chunkmap
import rdc_profile
import rdc_table
from rdc_sigcheck import _table_slots

from typing import Any, Dict, List, Mapping, Optional, Tuple, TypedDict

#: `FLT_MAX`, which is what a `maxLod` of "the whole chain" is written as. A row prints it as `max` rather
#: than as 3.4e38, because the number is a sentinel and not a level.
FLT_MAX = 3.4028234663852886e38

class SamplerRow(TypedDict):
    """One sampler, on its own or paired with a texture.

    `source` is where the sampler lives (`static res11365` or `heap 7 slot 12`), `register` where it is
    bound for the row's basis, and `lod` its mip range as text. `texture` is empty for an inventory row --
    a sampler the frame creates, listed before any event is walked -- and set for a pair, where `basis` says
    what established the pairing and `verdict`/`certainty` carry the arithmetic when something is wrong.
    """
    source: str
    register: str
    filterText: str
    address: str
    lod: str
    comparison: str
    texture: str
    basis: str
    verdict: str
    certainty: str

def _lod_text(sampler: Mapping[str, Any]) -> str:
    """The mip range and bias of a sampler as one cell: `0..max bias 0`."""
    top = sampler['maxLod']
    return '%g..%s bias %g' % (sampler['minLod'], 'max' if top >= FLT_MAX else '%g' % top,
                               sampler['mipLodBias'])

def _row(source: str, register: str, sampler: Mapping[str, Any]) -> SamplerRow:
    """The inventory half of a row: everything a sampler is, before any texture is involved."""
    return SamplerRow(source=source, register=register, filterText=sampler['filterText'],
                      address='%s/%s/%s' % (sampler['addressU'], sampler['addressV'],
                                            sampler['addressW']),
                      lod=_lod_text(sampler), comparison=sampler['comparison'], texture='',
                      basis='inventory', verdict='', certainty='')

def inventory(path: str, names: Optional[Dict[int, str]] = None) -> List[SamplerRow]:
    """Every sampler the capture creates: each signature's static array, then each written heap slot."""
    _info, stream, _how = rdc_cache.load_stream(path)
    if names is None:
        names = rdc_chunkmap.load_chunk_names()
    rows: List[SamplerRow] = []
    for rid, samplers in sorted(parse_static_samplers(stream, names).items()):
        for sampler in samplers:
            rows.append(_row('static res%d' % rid,
                             's%d s%d' % (sampler['register'], sampler['space']), sampler))
    for heap, slots in sorted(parse_sampler_slots(stream, names).items()):
        for index, sampler in sorted(slots.items()):
            rows.append(_row('heap %d slot %d' % (heap, index), 'slot %d' % index, sampler))
    return rows

def _is_depth_format(name: str) -> bool:
    """Whether a `DXGI_FORMAT` name is a depth format, by the three families D3D12 has (`D16`, `D24`, `D32`).

    A *typeless* format is deliberately not one: a depth buffer is usually created `R32_TYPELESS` and viewed
    as `D32_FLOAT`, so a comparison sampler over a typeless texture is the normal case and must not be
    reported.
    """
    return name.startswith(('D16', 'D24', 'D32'))

def sampler_verdict(sampler: Mapping[str, Any], mips: int, format_id: int,
                    formats: Dict[int, str]) -> Optional[Tuple[str, str]]:
    """`(certainty, verdict)` when this sampler cannot do with this texture what it is configured for.

    None means nothing is wrong that this *arithmetic* can see: a sampler whose range the texture covers is
    left alone, and so is one whose format the tool cannot name -- the chunk-name map is the only source of
    format names, and a missing one is a missing answer rather than a pass.
    """
    min_lod, max_lod = sampler['minLod'], sampler['maxLod']
    if max_lod < min_lod:
        return ('certain', 'the LOD range is inverted (min %g > max %g); D3D12 requires MinLOD <= MaxLOD'
                % (min_lod, max_lod))
    if mips > 1 and min_lod >= mips:
        return ('certain', 'asks for level >= %g while the texture has %d level(s) (0..%d): every fetch is '
                'clamped to the last one' % (min_lod, mips, mips - 1))
    if mips > 1 and max_lod < 1.0:
        return ('likely', 'mip mapping is off (max %g) although the texture has %d level(s): only the base '
                'level can be read' % (max_lod, mips))
    name = formats.get(format_id, '')
    if sampler['filterText'].startswith('comparison') and name and not _is_depth_format(name) \
            and 'TYPELESS' not in name:
        return ('likely', 'a comparison filter reads %s, which is not a depth format: the hardware compares '
                'instead of filtering' % name)
    return None

def _pair_event(out: List[SamplerRow], eid: int, state: DrawState, compute: bool,
                sig: Optional[RootSignature], static: Dict[int, List[StaticSampler]],
                heap_samplers: Dict[int, Dict[int, SamplerDesc]],
                heaps: Dict[int, Dict[int, DescriptorInfo]], resources: Dict[int, ResourceInfo],
                formats: Dict[int, str]) -> None:
    """Add every pair one event's tables and static samplers establish."""
    if sig is None:
        return
    srv_by_basis: Dict[str, List[Tuple[int, int]]] = {}
    for rp, (heap, base) in sorted((state['compTable'] if compute else state['gfxTable']).items()):
        if rp >= len(sig['params']) or sig['params'][rp]['kind'] != 'table':
            continue
        basis = 'table rp%d' % rp
        facts = [f for f in _table_slots(sig['params'][rp], heap, base, heaps) if f is not None]
        for fact in facts:
            if fact.kind == 'srv' and fact.resource:
                srv_by_basis.setdefault(basis, []).append((fact.resource, fact.space))
            elif fact.kind == 'sampler':
                sampler = heap_samplers.get(fact.heap, {}).get(fact.slot)
                if sampler is None:
                    continue
                for res, _space in srv_by_basis.get(basis, []):
                    out.append(_pair('heap %d slot %d' % (fact.heap, fact.slot),
                                     'slot %d' % fact.slot, sampler, res, basis, resources, formats))
    for sampler in static.get(sig['id'], []):
        basis = 'space %d' % sampler['space']
        for res, space in [pair for pairs in srv_by_basis.values() for pair in pairs]:
            if space != sampler['space']:
                continue
            out.append(_pair('static res%d' % sig['id'],
                             's%d s%d' % (sampler['register'], sampler['space']), sampler, res,
                             basis, resources, formats))

def _pair(source: str, register: str, sampler: Mapping[str, Any], res: int, basis: str,
          resources: Dict[int, ResourceInfo], formats: Dict[int, str]) -> SamplerRow:
    """One pair row, with the arithmetic already done."""
    info = resources.get(res, {})
    mips = int(info.get('mips', 0) or 0)
    verdict = sampler_verdict(sampler, mips, int(info.get('format', 0) or 0), formats)
    row = _row(source, register, sampler)
    row['texture'] = 'res%d%s %s' % (res, '[%s]' % info.get('name', '') if info.get('name') else '',
                                     'mips=%d' % mips if mips else info.get('kind', '?'))
    row['basis'] = basis
    if verdict is not None:
        row['verdict'], row['certainty'] = verdict[1], verdict[0]
    return row

@rdc_profile.timed('sampler pairs')
def pair_rows(path: str, names: Optional[Dict[int, str]] = None, eid: Optional[int] = None
              ) -> List[SamplerRow]:
    """The (sampler, texture) pairs the frame's bindings establish, with the arithmetic on each.

    The walk mirrors `rdc_sigcheck.file_check`: descriptor writes are applied *in stream order*, because a
    table binding has to resolve against what the slot held at that point -- UE re-uses descriptor memory --
    and each draw is read for the signature it binds. Static samplers pair by register space and table
    samplers by the table they share with an SRV, which is what the `basis` column says.
    """
    _info, stream, _how = rdc_cache.load_stream(path)
    if names is None:
        names = rdc_chunkmap.load_chunk_names()
    resources = parse_resource_table(stream, names)
    static = parse_static_samplers(stream, names)
    signatures = parse_root_signatures(stream, names)
    heap_samplers = parse_sampler_slots(stream, names)
    formats = load_format_names() or {}
    heaps: Dict[int, Dict[int, DescriptorInfo]] = {}
    states: Dict[int, DrawState] = {}
    out: List[SamplerRow] = []
    for at, ch in enumerate(iter_chunks(stream), 1):
        name = names.get(ch['id'], '')
        readable = (name in DRAW_CHUNKS or name in STATE_CHUNKS or name in DESCRIPTOR_KINDS
                    or name in DESCRIPTOR_COPY_CHUNKS)
        blob = chunk_payload(stream, ch) if readable else b''
        if name in DESCRIPTOR_KINDS or name in DESCRIPTOR_COPY_CHUNKS:
            apply_descriptor_chunk(name, blob, heaps)
        elif name in DRAW_CHUNKS:
            if eid is not None and at != eid:
                continue
            state = states.get(u64(blob, 0) if len(blob) >= 8 else 0)
            if state is None:
                continue
            compute = name in COMPUTE_CHUNKS
            sig_id = state['compSig'] if compute else state['gfxSig']
            _pair_event(out, at, state, compute, signatures.get(sig_id) if sig_id else None, static,
                        heap_samplers, heaps, resources, formats)
        elif _apply_state_chunk(name, blob, states):
            pass
    return out

def cmd_samplers(path: str, limit: int = 40, fmt: str = 'table', eid: Optional[int] = None) -> int:
    """Print the samplers a frame can read through, and the pairs that cannot do what they are set up for.

    Exit code 1 when a pair has a `certain` verdict, 0 otherwise, 2 when the question could not be asked
    (no chunk names): the same gate `hazards` gives, so either can be a sweep's pass/fail.
    """
    if not rdc_chunkmap.load_chunk_names():
        print('samplers: no chunk names -- the RenderDoc source tree is missing (README.md 1.1)')
        return 2
    rows = inventory(path)
    pairs = pair_rows(path, eid=eid)
    bad = [row for row in pairs if row['verdict']]
    notes = ['samplers: %d sampler(s) created; %d pair(s) at %s; %d with an arithmetic verdict'
             % (len(rows), len(pairs),
                'eid %d' % eid if eid is not None else 'the sampled events', len(bad))]
    shown: List[Tuple[str, ...]] = [(row['source'], row['register'], row['filterText'], row['address'],
                                     row['lod'], row['comparison'], '', '', '') for row in rows]
    shown.extend((row['source'], row['register'], row['filterText'], row['address'], row['lod'],
                  row['comparison'], row['texture'], row['basis'], row['verdict'])
                 for row in pairs[:limit])
    if len(pairs) > limit:
        notes.append('... %d more pair(s)' % (len(pairs) - limit))
    if fmt != 'table':
        rdc_table.emit(fmt, ('sampler', 'register', 'filter', 'address', 'lod', 'comparison', 'texture',
                             'basis', 'verdict'), shown, notes)
    else:
        print(notes[0])
        for row in shown:
            print('%-24s %-8s %-22s %-20s %-16s %-9s %-34s %-12s %s'
                  % (row[0][:24], row[1][:8], row[2][:22], row[3][:20], row[4][:16], row[5][:9],
                     row[6][:34], row[7][:12], row[8]))
        for note in notes[1:]:
            print(note)
    return 1 if any(row['certainty'] == 'certain' for row in bad) else 0

__all__ = [
    'FLT_MAX',
    'SamplerRow',
    'sampler_verdict',
    'cmd_samplers',
    'inventory',
    'pair_rows',
]
