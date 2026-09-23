"""`psos`: the pipeline state objects a capture creates, and the shaders each one holds -- by hash.

The question this answers -- "which shader is this, and which pipelines use it?" -- is asked of the
*file*, not of a replay session. A capture's `Device_CreatePipelineState` chunks carry the whole
pipeline description, each stage's DXBC/DXIL container verbatim included, so the PSO -> shader edges
are already in the `.rdc`: 14 payloads holding 606 KB over the UE capture's 374 MB stream, 7 holding
2.4 MB over HobbyRenderer's 1.47 GB, found by a chunk walk of 0.04 s and parsed by `parse_dxil_containers`
-- the same parser `dxbc` uses -- in 1-3 ms. Nothing is decompiled, nothing is written to disk, and no
device is opened: a `trace` session costs a replay session (1.8-3.7 s measured, of which the debug
attempt itself is noise), and this costs the payloads.

**Two hashes, and they are different values.** Every container carries both:

* the **header hash**, bytes 4..20 -- what `dxbc` prints and `dump-shaders` names files after, and the
  key the rest of the tool uses (30 unique over 33 containers on the UE capture, 79 over 85 on
  HobbyRenderer);
* the **`HASH` part** -- 20 bytes on every container measured here, the digest being its last 16 -- which
  is what a *shader* is called outside the file. `trace`'s refusal on the UE capture names
  `ec6e6433f96a985d5086cd232bf42fd8.pdb`, and that value is this part of one pixel shader's container,
  not any header hash: it is the name `dxc -Fd <dir>` gives a PDB (PIX's documented convention), so a
  hash copied out of a log or a symbol folder is *this* one. `--hash` takes either, by prefix, and says
  which it matched.

There is a **third**, and it is the one this tool's *own* other half prints: a bundle's
`states/<eid>.shaders.json` carries a `hash` per stage which is `sha256(rawBytes)` (`commands_state.cpp`)
-- the digest of the container's bytes. Measured on `desktop-1`'s event 1003: all three of its stages'
hashes landed on a container in the capture, and each container's `size` equalled the bundle's `bytes`, so
the file side and the driver side join by a hash neither computes the same way. `--hash` takes that too,
and every answer names all three identities, which is what makes a hash copied out of *any* of them
usable.

**The stage of each container comes from the engine's serialiser, not from a guess.** The stream-desc form
is written `pRootSignature | RootSigBlob | VS,PS,DS,HS,GS,AS,MS | ...state... | CS`
(`d3d12_serialise.cpp`), and the tail is `riid(16) | pPipelineState(8) | InlineShaderIDs[8]`
(`d3d12_device_wrap2.cpp`), whose own array order is VS,HS,DS,GS,PS,CS,AS,MS. So the containers appear in
the payload in a known order and the non-empty stages are known from the tail: pair them and each
container has its stage. Two things keep that honest. The number of containers found must equal the number
of non-empty stages, or the stages are left unknown rather than guessed. And on the captures here the
pairing agrees with every container's own `OSG1` strings (`SV_Target` -> ps, `SV_Position` -> vs): 16
agreements, no mismatch, on the UE capture -- including the pixel shader whose PDB the engine went looking
for. The 7 "mismatches" HobbyRenderer's check reports are the *check* being ambiguous and not the pairing:
those pipelines are ps+ms, and a mesh shader writes `SV_Position` exactly as a vertex shader does.

The `ILDB` part is carried through as one flag per container, because it is the fact that decides whether
`trace` can run at all: a DXIL shader is stepped *through* the debug bitcode in its own container, so 46
of HobbyRenderer's 53 containers need nothing else, while all 22 of the UE capture's need their PDB found
and named (`--pdb <dir>`, REFERENCE 9.4).

**Limits, stated rather than implied.** Three chunk forms create a pipeline object and all three are read
(`PSO_FORMS`): the stream-desc `Device_CreatePipelineState`, and the older
`Device_CreateGraphicsPipeline`/`Device_CreateComputePipeline`. The older pair is not a guess either -- 36
of HobbyRenderer's 43 pipelines come through the older graphics chunk, and every pixel shader among them
confirms its own stage through `OSG1`. A payload whose tail does not validate -- a capture older than the
inline shader ids, whose version gates differ per form -- is *counted* and said rather than quietly
missing. Ray-tracing state objects are not pipelines and are not counted as ones. The `--hash` answer is a
lookup in a cached index, so it costs no scan of the stream: the index is written beside the stream cache
(`.psos.json`, `rdc_cache.sidecar_path`) and keyed on the stream's length and a digest of its ends, like
the bind names' own sidecar.
"""

from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_cache import *  # noqa: F401,F403
from rdc_dxbc import *  # noqa: F401,F403
import rdc_cache       # called qualified: `sidecar_path`/`stream_source` are the cache's own answers
import rdc_chunkmap    # called qualified: the chunk names come from the bundled table
import rdc_table      # called qualified: the format the entry point parsed and checked
import os
import sys

from typing import Dict, List, Optional, Sequence, Tuple, TypedDict

#: The sidecar's name and version: the stream cache file's stem with this suffix (and it must be listed in
#: `rdc_cache.DERIVED_SUFFIXES`, or `cache clear` would leave it behind), and the version that makes an
#: older file unreadable rather than half-read.
#:
#: The version covers *how the index is built* as well as what is stored in it, which is not hypothetical:
#: it has moved twice while this module was written, once when reading the older chunk forms turned 7 PSOs
#: into 43 and once when the ids' framing was corrected, and both times the sidecar already on disk held an
#: answer the new code would happily have served. A digest cannot catch that -- the stream did not change,
#: the answer did -- and neither can care: the number is the only thing that can say "this file is not what
#: this build computes".
PSOS_SUFFIX = '.psos.json'
PSOS_VERSION = 5

#: The stage order the stream desc's bytecodes are written in (`d3d12_serialise.cpp`), and the order of the
#: engine's own `InlineShaderIDs` array (`d3d12_device_wrap2.cpp`). They differ -- a payload's order is not
#: the ids' order -- which is why both are named here rather than one being derived from the other.
STAGE_BYTECODE_ORDER: Tuple[str, ...] = ('VS', 'PS', 'DS', 'HS', 'GS', 'AS', 'MS', 'CS')
INLINE_STAGE_ORDER: Tuple[str, ...] = ('VS', 'HS', 'DS', 'GS', 'PS', 'CS', 'AS', 'MS')

#: Every pipeline-creation chunk this decodes, as `(how many inline ids its tail holds, whether those ids
#: are a *C array* or a single value, the order of that array, the order the payload's bytecodes are in)`.
#:
#: Three chunks create a pipeline object and all three end `riid(16) | pPipelineState(8) | <the ids>`, but
#: the counts, the framing and both stage orders differ: the stream-desc form writes eight ids
#: (`d3d12_device_wrap2.cpp`), the older `Device_CreateGraphicsPipeline`/`Device_CreateComputePipeline`
#: write five and one (`d3d12_device_wrap.cpp`), and each form's bytecode order comes from its own
#: serialiser. The framing is the part that bit: a C array is written with its **element count** first, so
#: eight ids are 8 + 64 bytes and the id before them sits at `n - 80` -- reading it at `n - 72` gave the
#: count word, which made seven pipelines of HobbyRenderer answer to the id `8`. A single `ResourceId` has
#: no count, so its id is at `n - 16`. That count word is also the tail's own check: it must be the number
#: of ids this form has, or the payload is refused.
#:
#: HobbyRenderer FlyingWorld.rdc uses all three forms -- 7 stream-desc, 1 older graphics and 35 older
#: compute -- which is what a first version of this module got wrong: it counted those 36 as "not decoded"
#: on a capture it could simply have read.
#:
#: The version gates matter and are why a payload can be refused rather than mislabelled: the older graphics
#: form needs capture version 0x16 for its five ids and the compute form 0x18 for its single one, so an
#: older capture has no tail to read at all.
PSO_FORMS: Dict[str, Tuple[int, bool, Tuple[str, ...], Tuple[str, ...]]] = {
    'Device_CreatePipelineState':
        (8, True, INLINE_STAGE_ORDER, STAGE_BYTECODE_ORDER),
    'Device_CreateGraphicsPipeline':
        (5, True, ('VS', 'HS', 'DS', 'GS', 'PS'), ('VS', 'PS', 'DS', 'HS', 'GS')),
    'Device_CreateComputePipeline':
        (1, False, ('CS',), ('CS',)),
}
#: A container is a shader if it carries one of these parts -- `RTS0` alone is a root signature, which the
#: same payloads also hold.
SHADER_PARTS: Tuple[str, ...] = ('DXIL', 'SHDR')
#: The `HASH` part's digest length, and the part length every container here uses (`HASH` is 20 bytes on
#: all 22 of the UE capture's containers and all 53 of HobbyRenderer's, the digest being the last 16).
HASH_DIGEST_BYTES = 16

#: How a matched identity is named in the answer: the container's own header field, the `HASH` part that a
#: PDB is named after, and the digest of the bytes that a bundle's per-stage `hash` is.
HASH_KIND_TEXT: Dict[str, str] = {
    'container': 'container hash',
    'shader': 'shader hash',
    'bytes': 'bytes hash (sha256)',
}
#: `List_SetPipelineState`'s payload: the command list's id, then the PSO's (`rdc_payloads.py`).
SET_PSO_LEN = 16

class ShaderRow(TypedDict):
    """One stage of one PSO: the stage, the container's header hash, and whether it is self-debuggable.

    The hash is the header hash -- the identity the container inventory and every other command use -- and
    the container's other hash is on the container row, not repeated per use.
    """
    stage: str
    hash: str

class PsoRow(TypedDict):
    """One pipeline state object: the resource id a command list binds, and its stages.

    `refs` counts the `List_SetPipelineState` payloads that name it, which is the offline half of "which
    draws use this" -- `draws` reports the bound PSO per event, so the two join on the id.
    """
    id: int
    offset: int
    kind: str
    refs: int
    shaders: List[ShaderRow]

class ContainerRow(TypedDict):
    """One DXBC/DXIL container in the stream: where it is, all three of its identities, its parts, its debug.

    `ilbd` is the `ILDB` part, i.e. the debug bitcode `trace` steps a DXIL shader *through*: a container
    without it can only be debugged if its PDB is found by name (`--pdb`), and `shaderHash` is the name
    that PDB takes.

    `sha256` is the digest of the container's *bytes* -- the third identity, and the one this tool's own
    other half uses: a bundle's `states/<eid>.shaders.json` carries `hash` per stage, and that is
    `sha256(rawBytes)` (`commands_state.cpp`). Measured: the three stages of `desktop-1`'s event 1003
    matched this way, 3 of 3, each landing on a container whose `size` equalled the bundle's `bytes` -- so
    the file side and the driver side can be joined by a hash neither of them computes the same way.
    """
    offset: int
    size: int
    hash: str
    shaderHash: str
    sha256: str
    parts: List[Tuple[str, int, int]]
    ilbd: bool

class PsoIndex(TypedDict):
    """The whole answer for one stream: the PSOs, every container, and what could not be decoded."""
    psos: List[PsoRow]
    containers: List[ContainerRow]
    undecoded: int

def container_list(stream: Buffer, source: Optional[CacheEntry] = None) -> List[ContainerRow]:
    """The stream's containers, from the cached index when there is one.

    This is what `dxbc` and `dump-shaders` read instead of running their own whole-stream `find`: the
    container rows *are* that answer -- offsets, sizes, hashes, and each part with its own offset and
    length -- so a command that needs them pays the find once per capture rather than once per call.
    Measured on the 1.55 GB UE capture, the find is 0.43 s of `dxbc`'s 0.58 s, and it grows with the
    stream while a sidecar read does not.
    """
    return shader_index(stream, source)['containers']

def _shader_hash(stream: Buffer, parts: Sequence[DxbcPart]) -> str:
    """The `HASH` part's digest as hex, or '' when the container has no such part.

    The part's own length is not assumed: the digest is its *last* 16 bytes, which is where it sits in the
    20-byte part every container here writes, and which stays right if a future part adds more before it.
    """
    part = next((p for p in parts if p[0] == 'HASH'), None)
    if part is None or part[2] < HASH_DIGEST_BYTES:
        return ''
    return stream[part[1] + part[2] - HASH_DIGEST_BYTES:part[1] + part[2]].hex()

def container_rows(stream: Buffer, source: Optional[CacheEntry] = None) -> List[ContainerRow]:
    """Every DXBC/DXIL container in the stream, in offset order, with all three hashes and the `ILDB` flag."""
    import hashlib
    rows: List[ContainerRow] = []
    for offset, size, h, parts in parse_dxil_containers(stream, source):
        declared = u32(stream, offset + 24)        # the container's own total size, which is what was hashed
        extent = declared if size <= declared <= len(stream) - offset else size
        rows.append({'offset': offset, 'size': extent, 'hash': h,
                     'shaderHash': _shader_hash(stream, parts),
                     'sha256': hashlib.sha256(bytes(stream[offset:offset + extent])).hexdigest(),
                     'parts': [(str(p[0]), int(p[1]), int(p[2])) for p in parts],
                     'ilbd': any(p[0] == 'ILDB' for p in parts)})
    return rows

def _plausible_id(value: int) -> bool:
    """Whether `value` can be a resource id this tool will print: a real one, not a truncated field."""
    return 0 < value < (1 << 32)

def parse_pso_payload(blob: Buffer, form: str = 'Device_CreatePipelineState'
                      ) -> Optional[Tuple[int, str, List[Tuple[str, str]]]]:
    """`(id, kind, [(stage, container hash)])` for one pipeline-creation payload, or None.

    `form` names which chunk it is, because the tail's length and both stage orders are per-chunk
    (`PSO_FORMS`). The tail is read first, because it is what makes the rest trustworthy: its last 8 x
    count bytes are the engine's inline shader ids and the 8 before them are the PSO's own id, so a
    payload from a serialiser version older than that form's gate -- or one whose layout is not this --
    fails the plausibility check and is refused rather than mislabelled. The stages are then the
    bytecodes' own order filtered by which inline ids are non-zero, and the pair is accepted only when the
    number of containers found equals the number of non-empty stages: a count that disagrees means
    something was found that is not a stage bytecode (or missed), and unknown stages are a better answer
    than wrong ones. A container with no DXIL/SHDR part is skipped, so a root signature blob that happens
    to sit in the same payload is not mistaken for a stage.
    """
    count, is_array, inline_order, bytecode_order = PSO_FORMS.get(
        form, PSO_FORMS['Device_CreatePipelineState'])
    n = len(blob)
    ids_bytes = 8 * count
    prefix = 8 if is_array else 0        # a C array is serialised with its element count first
    if n < prefix + ids_bytes + 8:
        return None
    inline = [u64(blob, n - ids_bytes + 8 * i) for i in range(count)]
    pso_id = u64(blob, n - prefix - ids_bytes - 8)
    if is_array and u64(blob, n - prefix - ids_bytes) != count:
        return None      # a capture older than this form's version gate, or not this layout at all
    if not _plausible_id(pso_id) or not any(inline):
        return None
    if any(not _plausible_id(v) for v in inline if v):
        return None

    found = [h for _off, _size, h, parts in parse_dxil_containers(blob)
             if any(p[0] in SHADER_PARTS for p in parts)]
    present = [inline_order[i] for i in range(count) if inline[i]]
    order = [s for s in bytecode_order if s in present]
    stages = order if len(order) == len(found) else ['?'] * len(found)

    kind = 'compute' if present == ['CS'] else 'graphics'
    return pso_id, kind, list(zip(stages, found))

def _references(stream: Buffer, names: Dict[int, str]) -> Dict[int, int]:
    """How many `List_SetPipelineState` payloads name each PSO id -- the offline half of "which draws"."""
    counts: Dict[int, int] = {}
    for ch in iter_chunks(stream):
        if names.get(ch['id'], '') != 'List_SetPipelineState' or ch['length'] < SET_PSO_LEN:
            continue
        pso = u64(chunk_payload(stream, ch), 8)
        counts[pso] = counts.get(pso, 0) + 1
    return counts

def build_index(stream: Buffer, source: Optional[CacheEntry] = None) -> PsoIndex:
    """Build the index: one pass over the chunk stream and one over the containers.

    The container scan is the whole-stream `find` `dxbc` pays (`rdc_scan.find_all`, split across processes
    when the stream is big enough to pay for them); the payloads are 0.6-2.4 MB of it. With `source` the
    find goes through that path; without one it is the same serial loop, and the containers are identical.
    """
    names = rdc_chunkmap.load_chunk_names()
    containers = container_rows(stream, source)

    psos: List[PsoRow] = []
    undecoded = 0
    for ch in iter_chunks(stream):
        form = names.get(ch['id'], '')
        if form not in PSO_FORMS:
            continue
        parsed = parse_pso_payload(chunk_payload(stream, ch), form)
        if parsed is None:
            undecoded += 1
            continue
        pso_id, kind, shaders = parsed
        psos.append({'id': pso_id, 'offset': ch['off'], 'kind': kind, 'refs': 0,
                     'shaders': [{'stage': stage, 'hash': h} for stage, h in shaders]})

    refs = _references(stream, names)
    for row in psos:
        row['refs'] = refs.get(row['id'], 0)
    psos.sort(key=lambda r: r['id'])
    return {'psos': psos, 'containers': containers, 'undecoded': undecoded}

def _load_index(stream: Buffer, source: CacheEntry) -> Optional[PsoIndex]:
    """The index from `source`'s sidecar, or None when there is none for *this* stream.

    Every refusal falls back to the scan, the same way `rdc_resources._load_bind_names` does: no file, a
    version or stream digest that is not this one, or a file that is not the document written here. A
    cache can only ever save work, so a broken one is never an error.
    """
    import json
    try:
        with open(rdc_cache.sidecar_path(source, PSOS_SUFFIX), encoding='utf-8') as fh:
            document = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get('version') != PSOS_VERSION:
        return None
    if document.get('streamLen') != len(stream):
        return None
    if document.get('digest') != rdc_cache.stream_digest(stream):
        return None
    psos, containers = document.get('psos'), document.get('containers')
    if not isinstance(psos, list) or not isinstance(containers, list):
        return None
    try:
        out: PsoIndex = {
            'psos': [{'id': int(row[0]), 'offset': int(row[1]), 'kind': str(row[2]),
                      'refs': int(row[3]),
                      'shaders': [{'stage': str(s[0]), 'hash': str(s[1])} for s in row[4]]}
                     for row in psos],
            'containers': [{'offset': int(row[0]), 'size': int(row[1]), 'hash': str(row[2]),
                            'shaderHash': str(row[3]), 'sha256': str(row[4]),
                            'parts': [(str(p[0]), int(p[1]), int(p[2])) for p in row[5]],
                            'ilbd': bool(row[6])}
                           for row in containers],
            'undecoded': int(document.get('undecoded', 0)),
        }
    except (TypeError, ValueError, IndexError):
        return None
    return out

def _store_index(stream: Buffer, source: CacheEntry, index: PsoIndex) -> None:
    """Write the sidecar: a temporary name renamed into place, and never an error if it fails.

    Rows are arrays rather than objects (the bind names' precedent), because a JSON object cannot key on
    what the lookup uses and the keys would be most of the file. Sorted, so two runs write byte-identical
    files.
    """
    import json
    path = rdc_cache.sidecar_path(source, PSOS_SUFFIX)
    document = {
        'version': PSOS_VERSION,
        'streamLen': len(stream),
        'digest': rdc_cache.stream_digest(stream),
        'undecoded': index['undecoded'],
        'psos': [[row['id'], row['offset'], row['kind'], row['refs'],
                  [[s['stage'], s['hash']] for s in row['shaders']]] for row in index['psos']],
        'containers': [[row['offset'], row['size'], row['hash'], row['shaderHash'], row['sha256'],
                        [[p[0], p[1], p[2]] for p in row['parts']], int(row['ilbd'])]
                       for row in index['containers']],
    }
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(document, fh, separators=(',', ':'), sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass

def shader_index(stream: Buffer, source: Optional[CacheEntry] = None) -> PsoIndex:
    """The index for `stream`, built once and then read back from the sidecar (`--hash` costs no scan)."""
    if source is not None:
        cached = _load_index(stream, source)
        if cached is not None:
            return cached
    out = build_index(stream, source)
    if source is not None:
        _store_index(stream, source, out)
    return out

def _by_hash(containers: Sequence[ContainerRow]) -> Dict[str, List[Tuple[ContainerRow, str]]]:
    """`hash -> [(container, which identity it is)]`, all three, so a lookup can say which one matched.

    Three rather than two because the *driver* names a shader a third way -- `sha256` of its bytes, which is
    what a bundle's per-stage `hash` is (`commands_state.cpp`) -- and a caller holding that hash should not
    be told it is not in the capture. See `ContainerRow`.
    """
    out: Dict[str, List[Tuple[ContainerRow, str]]] = {}
    for row in containers:
        out.setdefault(row['hash'], []).append((row, 'container'))
        if row['shaderHash']:
            out.setdefault(row['shaderHash'], []).append((row, 'shader'))
        out.setdefault(row['sha256'], []).append((row, 'bytes'))
    return out

def _hash_matches(containers: Sequence[ContainerRow], want: str) -> List[Tuple[ContainerRow, str]]:
    """The containers whose either hash starts with `want` (case-insensitive), and which hash matched.

    A prefix rather than a whole hash because the two are written differently wherever a reader copies
    one from -- a log line, a `.pdb` name, `dxbc`'s own output -- and an 8-character prefix of a 16-byte
    digest is already specific in a capture this size. Ambiguity is *said*, not resolved: every match is
    printed.
    """
    want = want.lower()
    if len(want) < 8:
        return []
    index = _by_hash(containers)
    if want in index:
        return list(index[want])
    return [(row, which) for digest, entries in sorted(index.items()) if digest.startswith(want)
            for row, which in entries]

def _uses(index: PsoIndex, want_hash: str) -> List[Tuple[PsoRow, str]]:
    """The PSOs (and stages) that bind the container with this hash."""
    out: List[Tuple[PsoRow, str]] = []
    for row in index['psos']:
        for shader in row['shaders']:
            if shader['hash'] == want_hash:
                out.append((row, shader['stage']))
    return out

def _size_text(size: int) -> str:
    """A byte count as a reader wants it: `11.0 K`, `1.5 M`."""
    if size < 1024:
        return '%d B' % size
    if size < 1024 * 1024:
        return '%.1f K' % (size / 1024.0)
    return '%.1f M' % (size / (1024.0 * 1024.0))

def _debug_text(container: Optional[ContainerRow]) -> str:
    """`ilbd` (the container carries its debug bitcode), `pdb` (it needs one) or `-` (not a shader)."""
    if container is None:
        return '-'
    if container['ilbd']:
        return 'ilbd'
    if any(p[0] in SHADER_PARTS for p in container['parts']):
        return 'pdb'
    return '-'

def cmd_psos(path: str, limit: int = 40, fmt: str = 'table',
             want_hash: Optional[str] = None) -> int:
    """Print the capture's PSOs and their shaders, or answer one hash. Exit 1 when a hash is not found.

    The index is what makes the hash question O(1) after one build; the *stream* is still opened, because
    the sidecar's identity is stamped from it (length and digest), and a cache that cannot be trusted is
    worse than none.
    """
    info, stream, _how = load_stream(path)
    source = rdc_cache.stream_source(path, info)
    index = shader_index(stream, source)
    containers = index['containers']
    known = {row['hash']: row for row in containers}
    for row in containers:
        if row['shaderHash']:
            known.setdefault(row['shaderHash'], row)
        known.setdefault(row['sha256'], row)

    if want_hash:
        return _print_hash(index, want_hash)

    shaders = [s for row in index['psos'] for s in row['shaders']]
    graphics = sum(1 for row in index['psos'] if row['kind'] == 'graphics')
    dbg = [known.get(s['hash']) for s in shaders]
    embedded = sum(1 for c in dbg if c is not None and c['ilbd'])
    rows: List[Sequence[object]] = []
    for row in index['psos'][:limit]:
        stages = ', '.join('%s %s' % (s['stage'], s['hash'][:12]) for s in row['shaders'])
        texts = [_debug_text(known.get(s['hash'])) for s in row['shaders']]
        if not texts:
            debug = '-'
        elif all(t == texts[0] for t in texts):
            debug = texts[0]
        else:
            debug = 'mixed'
        rows.append((row['id'], row['kind'], row['refs'], debug, stages))

    notes: List[str] = []
    notes.append('psos: %d pso(s) -- %d graphics, %d compute; %d shader binding(s) over %d container(s)'
                 % (len(index['psos']), graphics, len(index['psos']) - graphics, len(shaders),
                    len(containers)))
    notes.append('debug: %d of %d pso-bound shader(s) carry ILDB (their own debug bitcode); the rest '
                 'need a PDB named after their shader hash (`--hash <h>` says which, `--pdb <dir>` is '
                 'where one is looked for)' % (embedded, len(shaders)))
    if index['undecoded']:
        notes.append('note: %d pipeline payload(s) had no tail this reads -- a serialiser older than the '
                     'inline shader ids, or a form not in PSO_FORMS -- counted, not guessed at'
                     % index['undecoded'])
    if len(index['psos']) > limit:
        notes.append('shown: the first %d of %d pso(s)' % (limit, len(index['psos'])))

    if fmt != 'table':
        rdc_table.emit(fmt, ('pso', 'kind', 'refs', 'debug', 'shaders'), rows, notes)
        return 0

    for note in notes:
        print(note)
    print('%-8s %-9s %5s %-7s %s' % ('pso', 'kind', 'refs', 'debug', 'shaders'))
    for row in rows:
        print('%-8d %-9s %5d %-7s %s' % (row[0], row[1], row[2], row[3], row[4]))
    return 0

def _print_hash(index: PsoIndex, want: str) -> int:
    """One hash's whole answer: the container, both hashes, its debug data and every PSO that uses it.

    Grouped by container *hash*, because a stream holds a copy wherever a container is serialised and two
    offsets are not two shaders: the UE capture's pixel shader -- the one whose PDB the engine went looking
    for -- appears twice, and the answer to a lookup of it is one shader that two pipelines bind.
    """
    matches = _hash_matches(index['containers'], want)
    if not matches:
        print('hash %s: not in this capture (%d container hash(es) known)'
              % (want, len(index['containers'])), file=sys.stderr)
        return 1

    groups: Dict[str, List[ContainerRow]] = {}
    order: List[Tuple[str, str]] = []
    for row, which in matches:
        if row['hash'] not in groups:
            groups[row['hash']] = []
            order.append((row['hash'], which))
        groups[row['hash']].append(row)
    if len(order) > 1:
        print('%d container(s) match that prefix:' % len(order))

    for digest, which in order:
        copies = groups[digest]
        row = copies[0]
        uses = _uses(index, digest)
        print('hash %s (%s)' % (digest, HASH_KIND_TEXT.get(which, which)))
        print('    container : %s, %d bytes, parts %s%s'
              % (', '.join('@%d' % c['offset'] for c in copies), row['size'],
                 ','.join(p[0] for p in row['parts']),
                 ('' if len(copies) == 1 else '   (%d identical copies in the stream)' % len(copies))))
        if row['shaderHash'] and row['shaderHash'] != digest:
            print('    shader    : %s   (the name a PDB for this shader takes)' % row['shaderHash'])
        if row['sha256'] != digest:
            print('    bytes     : %s   (sha256 of the container, which is what a bundle\'s per-stage '
                  '`hash` is)' % row['sha256'])
        print('    debug data: %s'
              % ('embedded (the ILDB part) -- `trace` needs nothing else' if row['ilbd'] else
                 'not embedded -- a DXIL shader is stepped through its debug data, so this one needs '
                 'the PDB named above: `--pdb <the folder holding it>`'))
        for pso, stage in uses:
            print('    used by   : pso %d (%s), %s' % (pso['id'], pso['kind'], stage))
        if uses:
            print('    referenced: %d SetPipelineState call(s)'
                  % sum(pso['refs'] for pso, _stage in uses))
        else:
            print('    used by   : no pipeline in this capture binds it (a root signature blob, or a '
                  'shader compiled but never used)')
    return 0

__all__ = [
    'ContainerRow',
    'HASH_KIND_TEXT',
    'INLINE_STAGE_ORDER',
    'PSO_FORMS',
    'PSOS_SUFFIX',
    'PSOS_VERSION',
    'PsoIndex',
    'PsoRow',
    'ShaderRow',
    'STAGE_BYTECODE_ORDER',
    'build_index',
    'cmd_psos',
    'container_list',
    'container_rows',
    'parse_pso_payload',
    'shader_index',
]
