"""Offline RenderDoc .rdc analyser (no renderdoc.pyd needed).

Container layout (from renderdoc/serialise/rdcfile.cpp):
  FileHeader(32) | BinaryThumbnail(8 + jpg) | CaptureMetaData(13 + name) | CaptureTimeBase(16)
  then N x { BinarySectionHeader(40) | name | data }

Section flags observed: 0x2 = LZ4 (u32 block-size prefixes), 0x4 = Zstd (u32 prefix + zstd frames),
0x0 = uncompressed.

The frame-capture section is one structured-data (SDChunk) stream; we decompress it and analyse the
readable content (D3D12: resource names, shader debug names, cbuffer reflection strings, ...).

Usage:
  python rdc_analysis.py sections <rdc>
  python rdc_analysis.py strings  <rdc> [minlen] [maxlines]
  python rdc_analysis.py grep     <rdc> <pattern> [context]
  python rdc_analysis.py names    <rdc> [minlen]
  python rdc_analysis.py float    <rdc> <value> [tol]
  python rdc_analysis.py blocks   <rdc>          # LZ4/zstd block accounting
  python rdc_analysis.py chunks   <rdc> [limit] [nameFilter]
  python rdc_analysis.py summary  <rdc>
  python rdc_analysis.py markers  <rdc>
  python rdc_analysis.py rootconst <rdc> [maxChunks]
  python rdc_analysis.py dump-chunk <rdc> <chunkIndex> <outfile>
  python rdc_analysis.py dump-shaders <rdc> <outdir>
  python rdc_analysis.py selftest [-v] [-k <substring>]   # run the unit-test suite
"""
from __future__ import annotations

import os
import re
import struct
import sys
import unittest
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, TypedDict, Union, cast

ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
STR_RE = re.compile(rb'[\x20-\x7e]{6,}')


# ---------------------------------------------------------------------------
# Dict shapes.
#
# These TypedDicts document the plain `dict`s the parsers return. They exist only for the type
# checker: at runtime every value is an ordinary dict, so the shapes seen by the `cmd_*` printers,
# by the tests and by any external script are unchanged.
# ---------------------------------------------------------------------------
class SectionInfo(TypedDict):
    """One BinarySectionHeader plus where its (still compressed) payload lives.

    `dataOffset` is the absolute offset of the section body inside the container; `name` is the
    NUL-trimmed, UTF-8 decoded section name.
    """
    type: int
    compLen: int
    uncompLen: int
    version: int
    flags: int
    nameLen: int
    name: str
    dataOffset: int


class CaptureMetaData(TypedDict):
    """The CaptureMetaData + CaptureTimeBase records that follow the thumbnail."""
    driverID: int
    driverName: str
    timeBase: int
    timeFreq: float


class CaptureInfo(TypedDict):
    """A whole `.rdc` container: file header fields, thumbnail, metadata and the section table."""
    size: int
    version: int
    headerLength: int
    progVersion: str
    thumbnail: Tuple[int, int, int]
    meta: CaptureMetaData
    sections: List[SectionInfo]
    _data: bytes


class ChunkInfo(TypedDict):
    """One framed SDChunk.

    `off` is the chunk start and `data` is the payload offset *after* the per-chunk metadata
    (36 bytes in for the usual flags, see README 3.3) -- never assume `off + 8`.
    """
    off: int
    id: int
    flags: int
    length: int
    data: int


#: One part of a DXBC/DXIL container: `(fourcc, offset, length)`. `offset` is absolute, past the
#: part's own fourcc/size header; `length` is its data length.
DxbcPart = Tuple[str, int, int]

#: A validated DXBC/DXIL container: `(offset, size, hash_hex, parts)`.
DxbcContainer = Tuple[int, int, str, List[DxbcPart]]

#: One decoded ISG1/OSG1 element: `(name, semanticIndex, register)`.
SignatureElement = Tuple[str, int, int]


#: Any object `struct.unpack_from` accepts (bytes, bytearray, memoryview).
Buffer = Union[bytes, bytearray, memoryview]

#: Cached `rb'[\x20-\x7e]{minlen,}'` patterns, keyed by the effective minimum length. `STR_RE` is the
#: entry for the historical default of 6.
_RUN_PATTERNS: Dict[int, re.Pattern[bytes]] = {6: STR_RE}

#: The same cache for the text patterns used on UTF-16LE-decoded payloads.
_WIDE_PATTERNS: Dict[int, re.Pattern[str]] = {}


def _run_pattern(minlen: int) -> re.Pattern[bytes]:
    """Return the cached printable-ASCII run pattern for `minlen` (clamped to >= 1)."""
    m = max(minlen, 1)
    pat = _RUN_PATTERNS.get(m)
    if pat is None:
        pat = re.compile(rb'[\x20-\x7e]{%d,}' % m)
        _RUN_PATTERNS[m] = pat
    return pat


def _wide_pattern(minlen: int) -> 're.Pattern[str]':
    """`_run_pattern` for text, used on UTF-16LE-decoded payloads."""
    m = max(minlen, 1)
    pat = _WIDE_PATTERNS.get(m)
    if pat is None:
        pat = re.compile(r'[\x20-\x7e]{%d,}' % m)
        _WIDE_PATTERNS[m] = pat
    return pat


def string_runs(blob: Buffer, minlen: int = 6, start: int = 0,
                end: Optional[int] = None) -> Iterator[Tuple[int, str]]:
    """Yield `(offset, text)` for every run of `minlen` or more printable ASCII bytes in `blob`.

    `minlen` is honoured exactly: the old fixed `{6,}` scan silently dropped shorter strings, which is
    why marker names under 6 characters used to print as `?`. Offsets are absolute within `blob`;
    `start`/`end` only restrict the window that gets scanned.
    """
    stop = len(blob) if end is None else end
    for m in _run_pattern(minlen).finditer(blob, start, stop):
        yield m.start(), m.group().decode('ascii', 'replace')


def u16(b: Buffer, o: int) -> int:
    """Read a little-endian unsigned 16-bit value at offset `o`."""
    return struct.unpack_from('<H', b, o)[0]


def u32(b: Buffer, o: int) -> int:
    """Read a little-endian unsigned 32-bit value at offset `o`."""
    return struct.unpack_from('<I', b, o)[0]


def u64(b: Buffer, o: int) -> int:
    """Read a little-endian unsigned 64-bit value at offset `o`."""
    return struct.unpack_from('<Q', b, o)[0]


def parse_container(path: str) -> CaptureInfo:
    """Parse the `.rdc` container header, metadata, thumbnail and section table.

    The section walk stops at the first byte that is not 0: that byte is the end-of-sections
    marker (see README 3.1). Section names are decoded as UTF-8 and NUL-trimmed.
    """
    with open(path, 'rb') as fh:
        data: bytes = fh.read()
    assert data[:4] == b'RDOC', 'not an rdc file'

    info: CaptureInfo = {
        'size': len(data),
        'version': u32(data, 8),
        'headerLength': u32(data, 12),
        'progVersion': data[16:32].split(b'\x00')[0].decode('ascii', 'replace'),
        'thumbnail': (0, 0, 0),
        'meta': {'driverID': 0, 'driverName': '', 'timeBase': 0, 'timeFreq': 0.0},
        'sections': [],
        '_data': data,
    }
    thumb_w, thumb_h, thumb_len = u16(data, 32), u16(data, 34), u32(data, 36)
    info['thumbnail'] = (thumb_w, thumb_h, thumb_len)

    o = 40 + thumb_len
    driver_id = u32(data, o + 8)
    name_len = data[o + 12]
    driver_name = data[o + 13:o + 13 + name_len].split(b'\x00')[0].decode('ascii', 'replace')
    o += 13 + name_len
    info['meta'] = {
        'driverID': driver_id,
        'driverName': driver_name,
        'timeBase': u64(data, o),
        'timeFreq': struct.unpack_from('<d', data, o + 8)[0],
    }

    sections = info['sections']
    o = info['headerLength']
    while o + 40 <= len(data):
        if data[o] != 0:
            break
        sec: SectionInfo = {
            'type': u32(data, o + 4),
            'compLen': u64(data, o + 8),
            'uncompLen': u64(data, o + 16),
            'version': u64(data, o + 24),
            'flags': u32(data, o + 32),
            'nameLen': u32(data, o + 36),
            'name': '',
            'dataOffset': 0,
        }
        if not 0 < sec['nameLen'] <= 2048:
            break
        sec['name'] = data[o + 40:o + 40 + sec['nameLen'] - 1].decode('utf-8', 'replace')
        sec['dataOffset'] = o + 40 + sec['nameLen']
        sections.append(sec)
        o = sec['dataOffset'] + sec['compLen']
    return info


def lz4_block(src: bytes, out: bytearray) -> bytearray:
    """Decompress one raw LZ4 block, appending to `out` and returning it.

    The return value is the same `bytearray` object that was passed in (tests compare it against
    `bytes` literals, which works because `bytearray == bytes` compares contents).
    """
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if lit:
            out += src[i:i + lit]
            i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0:
            break
        mlen = token & 0x0F
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        if offset >= mlen:
            out += out[start:start + mlen]
        else:
            pat = bytes(out[start:])
            out += (pat * ((mlen + offset - 1) // offset))[:mlen]
    return out


def decompress_lz4(blob: bytes, expect: int) -> Tuple[bytes, int]:
    """Decompress a section body made of [u32 compressedBlockLength][raw LZ4 block]*.

    Returns the produced bytes and the number of blocks consumed. `expect` is the expected
    uncompressed size; 0 means "consume every block regardless of size".
    """
    out = bytearray()
    o = 0
    blocks = 0
    while o + 4 <= len(blob) and (expect == 0 or len(out) < expect):
        clen = u32(blob, o)
        o += 4
        if clen == 0 or o + clen > len(blob):
            break
        lz4_block(blob[o:o + clen], out)
        o += clen
        blocks += 1
    return bytes(out), blocks


def decompress_zstd(blob: bytes) -> bytes:
    """Decompress a Zstd section body.

    The optional dependency is imported *inside* the function on purpose: the module stays
    importable without `zstandard` installed, and calling this without it raises ImportError.
    """
    import zstandard  # optional dependency, imported lazily
    dctx = zstandard.ZstdDecompressor()
    # data may be a u32 block-size prefix followed by one or more zstd frames
    if blob[:4] == ZSTD_MAGIC:
        body = blob
    elif blob[4:8] == ZSTD_MAGIC:
        body = blob[4:]
    else:
        body = blob
    return dctx.stream_reader(body).read()


def get_stream(info: CaptureInfo, section_index: int = 0) -> Tuple[bytes, str]:
    """Return the (decompressed) body of one section and a human-readable method label."""
    sec = info['sections'][section_index]
    blob = info['_data'][sec['dataOffset']:sec['dataOffset'] + sec['compLen']]
    if blob[:4] == ZSTD_MAGIC or blob[4:8] == ZSTD_MAGIC:
        return decompress_zstd(blob), 'zstd'
    if sec['flags'] & 0x2:
        out, blocks = decompress_lz4(blob, sec['uncompLen'])
        return out, 'lz4(%d blocks)' % blocks
    return blob, 'raw'


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
    stream, how = get_stream(info)
    print('framecapture stream: %d bytes  (expected %d)  [%s]'
          % (len(stream), info['sections'][0]['uncompLen'], how))


def cmd_blocks(path: str) -> None:
    """Print per section: name, flags and the first 16 bytes hex (enough to spot compression)."""
    info = parse_container(path)
    for s in info['sections']:
        blob = info['_data'][s['dataOffset']:s['dataOffset'] + s['compLen']]
        print('%-40s flags=0x%x first16=%s' % (s['name'], s['flags'], blob[:16].hex()))


def load_stream(path: str) -> Tuple[CaptureInfo, bytes, str]:
    """Parse the container, decompress section 0 and return (info, stream, method)."""
    info = parse_container(path)
    stream, how = get_stream(info)
    return info, stream, how


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


GI_KEYS = [b'IndirectLightingSHCoefficients0', b'IndirectLightingSHCoefficients1',
           b'IndirectLightingSHCoefficients2', b'IndirectLightingSHSingleCoefficient',
           b'IndirectLightingCacheShowFlag', b'VolumetricLightmapBrickSHCoefficients',
           b'VolumetricLightmapIndirectionTexture', b'DirectionalLightShadowing',
           b'MobileBasePass', b'Primitive', b'View', b'IndirectLightingCache', b'PrecomputedLightingBuffer',
           b'LightmapResourceCluster']
INSTANCE_SEMS = [b'TEXCOORD6', b'TEXCOORD7', b'TEXCOORD8', b'TEXCOORD9', b'TEXCOORD10',
                 b'TEXCOORD11', b'TEXCOORD12']

#: Substrings that mark a reflection string as indirect-lighting related (`dxbc` / `dump-shaders`).
GI_SUBSTRINGS = ('IndirectLighting', 'VolumetricLightmap', 'DirectionalLightShadowing',
                 'PrecomputedIndirect', 'LightmapResourceCluster', 'SkyBentNormal')


def parse_dxil_containers(stream: bytes) -> Iterator[DxbcContainer]:
    """Yield `(offset, size, hash_hex, parts)` for every DXBC/DXIL container in `stream`.

    Container header: 'DXBC' magic(4) | hash(16) | version(4) | size(4) | partCount(4) |
    partOffsets[partCount](4) ; each part at +offset: fourcc(4) | size(4) | data.
    """
    pos = 0
    while True:
        i = stream.find(b'DXBC', pos)
        if i < 0:
            return
        pos = i + 4
        if i + 32 > len(stream):
            continue
        part_count = u32(stream, i + 28)
        if part_count == 0 or part_count > 64 or i + 32 + part_count * 4 > len(stream):
            continue
        parts: List[DxbcPart] = []
        ok = True
        for k in range(part_count):
            po = i + u32(stream, i + 32 + k * 4)
            if po + 8 > len(stream):
                ok = False
                break
            fourcc = stream[po:po + 4]
            plen = u32(stream, po + 4)
            if not all(32 <= c < 127 for c in fourcc) or po + 8 + plen > len(stream):
                ok = False
                break
            parts.append((fourcc.decode('ascii', 'replace'), po + 8, plen))
        if not ok or not parts:
            continue
        end = max(p[1] + p[2] for p in parts)
        yield i, end - i, stream[i + 4:i + 20].hex(), parts


def part_strings(blob: bytes, off: int, ln: int, minlen: int = 4) -> List[str]:
    """Return the ASCII strings (>= `minlen`) inside `blob[off:off+ln]`."""
    return [s for _, s in string_runs(blob, minlen, off, off + ln)]


class DxbcRow(TypedDict):
    """Internal per-container row used by `cmd_dxbc` (never leaves the module)."""
    off: int
    size: int
    hash: str
    stage: str
    parts: List[str]
    gi: List[str]
    inst: List[str]
    files: List[str]
    isg: List[str]
    osg: List[str]
    rd: List[str]


def cmd_dxbc(path: str, verbose: int = 0) -> None:
    """One row per DXBC/DXIL container (stage, hash, parts, GI cbuffer vars), then details."""
    _info, stream, how = load_stream(path)
    print('stream %d bytes [%s]' % (len(stream), how))
    rows: List[DxbcRow] = []
    for off, size, h, parts in parse_dxil_containers(stream):
        names = [p[0] for p in parts]

        def blob_of(fourcc: str) -> bytes:
            p = next((x for x in parts if x[0] == fourcc), None)
            if not p:
                return b''
            return stream[p[1]:p[1] + p[2]]

        isg = blob_of('ISG1')
        osg = blob_of('OSG1')
        il = blob_of('ILDN')
        whole = stream[off:off + size]
        rd = part_strings(whole, 0, len(whole), 4)
        isg_s = part_strings(isg, 0, len(isg), 2) if isg else []
        osg_s = part_strings(osg, 0, len(osg), 3) if osg else []
        il_s = part_strings(il, 0, len(il), 4) if il else []
        stage = 'root-sig' if 'RTS0' in names else (
            'PS' if any('SV_Target' in s for s in osg_s) else
            'VS' if any('SV_Position' in s for s in osg_s) else
            'CS' if 'CS' in names else '?')
        gi = sorted({s for s in rd if any(k in s for k in GI_SUBSTRINGS)})
        inst = sorted({s for s in isg_s if 'TEXCOORD' in s or 'COLOR' in s or 'POSITION' in s
                       or 'NORMAL' in s or 'TANGENT' in s})
        files = sorted({s for s in il_s if s.endswith(('.usf', '.ush', '.hlsl', '.h'))})
        rows.append({'off': off, 'size': size, 'hash': h, 'stage': stage, 'parts': names,
                     'gi': gi, 'inst': inst, 'files': files, 'isg': isg_s, 'osg': osg_s, 'rd': rd})
    print('DXBC/DXIL containers: %d' % len(rows))
    by_stage: Dict[str, List[DxbcRow]] = {}
    for r in rows:
        by_stage.setdefault(r['stage'], []).append(r)
    for st in sorted(by_stage):
        print('  %-9s %d' % (st, len(by_stage[st])))
    print()
    print('%-4s %-10s %-8s %-34s %s' % ('#', 'offset', 'stage', 'hash', 'GI cbuffer vars'))
    for idx, r in enumerate(rows):
        print('%-4d 0x%-8x %-8s %-34s %s' % (idx, r['off'], r['stage'], r['hash'][:32],
                                             ', '.join(r['gi']) or '(none)'))
    print()
    for idx, r in enumerate(rows):
        if r['stage'] == 'root-sig':
            continue
        print('--- [%d] 0x%x %s hash=%s' % (idx, r['off'], r['stage'], r['hash'][:16]))
        print('    GI vars : %s' % (', '.join(r['gi']) if r['gi'] else '(none)'))
        print('    VS input: %s' % (', '.join(r['inst']) if r['inst'] else '(none)'))
        print('    PS out  : %s' % ', '.join(r['osg'][:12]))
        if r['files']:
            print('    files   : %s' % ', '.join(r['files'][:10]))
        if verbose:
            print('    all str : %s' % ', '.join(r['rd'][:120]))


def cmd_count(path: str, pats: Sequence[str]) -> None:
    """Print the occurrence count and first offset of each pattern (verbatim, `-1` included)."""
    _info, stream, how = load_stream(path)
    print('stream %d bytes [%s]' % (len(stream), how))
    for p in pats:
        b = p.encode()
        print('  %-30s count=%-8d first=0x%x' % (p, stream.count(b), stream.find(b)))


def cmd_hex(path: str, start: str, length: str) -> None:
    """Hex + ASCII dump of `[start, start+length)` (both arguments accept decimal and 0x hex)."""
    _info, stream, _how = load_stream(path)
    start_i, length_i = int(start, 0), int(length, 0)
    print('window 0x%x..0x%x' % (start_i, start_i + length_i))
    for i in range(start_i, min(len(stream), start_i + length_i), 16):
        chunk = stream[i:i + 16]
        hx = ' '.join('%02x' % c for c in chunk)
        asc = ''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)
        print('%08x  %-47s  %s' % (i, hx, asc))


def parse_signature(blob: bytes) -> List[SignatureElement]:
    """Parse a D3D12 ISG1/OSG1 signature part: u32 count, then 24-byte elements, then the string table."""
    if len(blob) < 8:
        return []
    count = u32(blob, 0)
    if count > 64:
        return []
    strtab = 8 + count * 24
    out: List[SignatureElement] = []
    for i in range(count):
        o = 8 + i * 24
        if o + 24 > len(blob):
            break
        name_off = u32(blob, o)
        sem_idx = u32(blob, o + 4)
        reg = u32(blob, o + 8)
        name = '?'
        for base in (strtab, 8, u32(blob, 4)):
            p = base + name_off
            if 0 <= p < len(blob) and 32 <= blob[p] < 127:
                end = blob.find(b'\x00', p)
                cand = blob[p:end if end > 0 else p + 32].decode('ascii', 'replace')
                if cand and all(32 <= ord(c) < 127 for c in cand):
                    name = cand
                    break
        out.append((name, sem_idx, reg))
    return out


def cmd_sig(path: str) -> None:
    """Print the parsed input/output signatures of every shader container."""
    _info, stream, how = load_stream(path)
    print('stream %d bytes [%s]' % (len(stream), how))
    for off, _size, h, parts in parse_dxil_containers(stream):
        names = [p[0] for p in parts]
        if 'RTS0' in names:
            continue

        def blob_of(fourcc: str) -> bytes:
            p = next((x for x in parts if x[0] == fourcc), None)
            return stream[p[1]:p[1] + p[2]] if p else b''

        isg = parse_signature(blob_of('ISG1'))
        osg = parse_signature(blob_of('OSG1'))
        stage = 'PS' if any(n == 'SV_Target' for n, _, _ in osg) else 'VS'
        print('--- 0x%-8x %s hash=%s' % (off, stage, h[:16]))
        print('    IN : %s' % ', '.join('%s%d(reg%d)' % (n, i, r) for n, i, r in isg))
        print('    OUT: %s' % ', '.join('%s%d' % (n, i) for n, i, _ in osg))


def cmd_pattern(path: str, spec: str, count: int = 72, cap: int = 40) -> None:
    """Find every occurrence of a sequence of float32 values and dump the floats that follow.

    The ILC uniform buffer (FIndirectLightingCacheUniformParameters) written by
    SetupSingleProbeIndirectLightingParameters always starts with
    Add=(0,0,0) Scale=(1,1,1) MinUV=(0,0,0) MaxUV=(1,1,1), so
    '0,0,0,1,1,1,0,0,0,1,1,1' locates it and the following floats are
    PointSkyBentNormal, DirectionalLightShadowing and the SH coefficients.
    """
    _info, stream, how = load_stream(path)
    vals = [float(x) for x in spec.split(',')]
    pat = b''.join(struct.pack('<f', v) for v in vals)
    print('stream %d bytes [%s]' % (len(stream), how))
    print('searching %d floats: %s' % (len(vals), spec))
    hits: List[int] = []
    pos = 0
    while True:
        i = stream.find(pat, pos)
        if i < 0:
            break
        hits.append(i)
        pos = i + 1
    print('hits: %d' % len(hits))
    for h in hits[:cap]:
        print('--- hit @0x%x (stream pos, floats from hit+%d) ---' % (h, len(vals) * 4))
        base = h + len(vals) * 4
        row: List[float] = []
        for k in range(count):
            o = base + k * 4
            if o + 4 > len(stream):
                break
            row.append(struct.unpack_from('<f', stream, o)[0])
        for k in range(0, len(row), 8):
            print('   +%-3d %s' % (k * 4, ' '.join('%12.5f' % v for v in row[k:k + 8])))
    if len(hits) > cap:
        print('... %d more hits' % (len(hits) - cap))


def cmd_float(path: str, value: str, tol: float = 0.0) -> None:
    """Exact float32 bit-pattern search (the `tol` argument is accepted but has never been used)."""
    _info, stream, how = load_stream(path)
    target = struct.pack('<f', float(value))
    print('stream %d bytes [%s]; exact float32 bits of %s = %s'
          % (len(stream), how, value, target.hex()))
    hits: List[int] = []
    pos = 0
    while True:
        i = stream.find(target, pos)
        if i < 0:
            break
        hits.append(i)
        pos = i + 1
    print('exact hits: %d' % len(hits))
    for o in hits[:20]:
        lo, hi = max(0, o - 64), min(len(stream), o + 64)
        text = ''.join(chr(c) if 32 <= c < 127 else '.' for c in stream[lo:hi])
        print('  0x%08x | %s' % (o, text))


PATTERNS = [
    b'IndirectLightingCache', b'IndirectLightingSHCoefficients', b'IndirectLightingSHSingleCoefficient',
    b'InstanceGIDiffuse', b'InstanceSHCoefficients', b'USE_INSTANCED_SH_COEFFICIENTS',
    b'CACHED_POINT_INDIRECT_LIGHTING', b'PRECOMPUTED_IRRADIANCE_VOLUME_LIGHTING', b'MOBILE_SH_ORDER',
    b'VolumetricLightmap', b'VolumetricLightmapBrick', b'LightmapResourceCluster',
    b'PrecomputedLightingBuffer', b'MobileBasePass', b'BasePassPixelShader', b'MobileBasePassPixelShader',
    b'GetLightMapColorLQ', b'FNoLightMapPolicy', b'FCachedPointIndirectLightingPolicy',
    b'FPrecomputedVolumetricLightmapLightingPolicy', b'FMobileDirectionalLightAndSHIndirectPolicy',
    b'FLocalVertexFactory', b'FInstancedStaticMeshVertexFactory', b'DirectionalLightShadowing',
    b'PrimitiveSceneData', b'SceneColor', b'DrawIndexedInstanced', b'ShaderHash', b'ShaderName',
    b'TBasePassPS', b'TBasePassVS', b'FShader', b'HISM', b'Sphere', b'StaticMeshActor',
]


def cmd_report(path: str) -> None:
    """Pattern-count table plus the unique shader-ish / policy-ish strings in the stream."""
    _info, stream, how = load_stream(path)
    print('stream %d bytes [%s]' % (len(stream), how))
    print('=== pattern counts ===')
    for p in PATTERNS:
        c = stream.count(p)
        first = stream.find(p)
        print('  %-46s count=%-6d first=0x%x' % (p.decode(), c, first if first >= 0 else 0))
    print('=== shader-ish / policy-ish names (unique) ===')
    seen: Dict[str, int] = {}
    for off, s in string_runs(stream):
        if re.search(r'(FShader|ShaderType|BasePass|LightMapPolicy|LightmapPolicy|VertexFactory|'
                     r'Permutation|MaterialShader|TBasePass|GlobalShader)', s):
            seen.setdefault(s, off)
    for s in sorted(seen, key=lambda x: seen[x]):
        print('  @0x%-9x %s' % (seen[s], s[:170]))
    print('(%d unique)' % len(seen))


# ---------------------------------------------------------------------------
# Chunk (structured-data) stream walking
#
# Framing from renderdoc/serialise/serialiser.cpp (Serialiser<Reading>::BeginChunk):
#   u32 c              chunkID = c & 0xffff, flags = c & ~0xffff
#   if c & 0x10000     u32 numFrames; u64 frames[numFrames]     (callstack)
#   if c & 0x20000     u64 threadID
#   if c & 0x40000     u64 durationMicro
#   if c & 0x80000     i64 timestampMicro
#   if c & 0x100000    u64 length  else  u32 length
#   payload[length]
# and the stream is then aligned to Serialiser::ChunkAlignment (= 64).
#
# Chunk IDs come from the driver's `enum class D3D12Chunk : uint32_t` (starting at
# SystemChunk::FirstDriverChunk = 1000); we parse the enums straight out of the RenderDoc source
# so the names stay correct for whatever version produced the capture.
# ---------------------------------------------------------------------------
CHUNK_CALLSTACK = 0x00010000
CHUNK_THREADID = 0x00020000
CHUNK_DURATION = 0x00040000
CHUNK_TIMESTAMP = 0x00080000
CHUNK_64BITSIZE = 0x00100000
CHUNK_ALIGN = 64


def _find_renderdoc_src() -> str:
    """Locate the RenderDoc source tree ("real implementation of RenderDoc").

    The tool parses the chunk-name enums out of the RenderDoc source at runtime, so the names it prints match
    the RenderDoc version that produced the capture. **A copy of the RenderDoc source tree must be placed in
    the root folder as `renderdoc-src`** -- see README section 1. Expected layout:

        <root>/rdc-tools/rdc_analysis.py     this tool
        <root>/rdc-tools/renderdoc-src/      a copy of the RenderDoc source tree   <- the convention
          renderdoc/core/core.h                      (SystemChunk enum)
          renderdoc/driver/d3d12/d3d12_common.h      (D3D12Chunk enum)

    Search order: $RENDERDOC_SRC, then `<tool folder>/renderdoc-src`, then its parent's
    `renderdoc-src`, then the historical absolute default.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates: List[Optional[str]] = [os.environ.get('RENDERDOC_SRC'),
                                       os.path.join(here, 'renderdoc-src'),
                                       os.path.join(os.path.dirname(here), 'renderdoc-src'),
                                       r'C:\Workspace WIth Spaces\renderdoc-src']
    for cand in candidates:
        if cand and os.path.isfile(os.path.join(cand, 'renderdoc', 'core', 'core.h')):
            return cand
    # The documented convention is candidate #1; it is always a `str` even though the list may
    # hold `None` for an unset environment variable.
    return os.path.join(here, 'renderdoc-src')


RENDERDOC_SRC = _find_renderdoc_src()
MARKER_CHUNKS = ('PushMarker', 'SetMarker', 'Queue_BeginEvent', 'Queue_SetMarker')
_SRC_WARNED = False
DRAW_CHUNKS = ('List_DrawIndexedInstanced', 'List_DrawInstanced', 'List_Dispatch',
               'List_ExecuteIndirect')

#: `align_up`'s default (kept as a module constant so callers can name it).
ALIGN_UP_DEFAULT = CHUNK_ALIGN


def align_up(x: int, a: int = ALIGN_UP_DEFAULT) -> int:
    """Round `x` up to the next multiple of `a` (chunks are 64-byte aligned)."""
    return (x + a - 1) & ~(a - 1)


def parse_chunk_enum(text: str, enum_name: str) -> Dict[int, str]:
    """Parse a C++ `enum class <enum_name> : uint32_t { ... }` into an {id: name} map.

    Values are taken from explicit initialisers, from `FirstDriverChunk` (= 1000) and otherwise
    auto-incremented, exactly like C++ would.
    """
    m = re.search(r'enum class %s\s*:\s*uint32_t\s*\{(.*?)\n\};' % enum_name, text, re.S)
    if not m:
        return {}
    out: Dict[int, str] = {}
    val = 0
    for line in m.group(1).splitlines():
        line = line.split('//')[0].strip()
        mm = re.match(r'(\w+)\s*(?:=\s*([^,]+))?,', line)
        if not mm:
            continue
        name, expr = mm.group(1), (mm.group(2) or '').strip()
        if expr:
            if 'FirstDriverChunk' in expr:
                val = 1000
            elif re.match(r'^\d+$', expr):
                val = int(expr)
            else:
                val += 1
        out[val] = name
        val += 1
    return out


def load_chunk_names(src_root: str = RENDERDOC_SRC, driver: str = 'D3D12') -> Dict[int, str]:
    """Build the chunk-id -> name map from the RenderDoc source enums.

    Warns once on stderr when the source tree is missing; the tool then falls back to numeric ids
    and everything else keeps working (see README 1.1).
    """
    global _SRC_WARNED
    names: Dict[int, str] = {}
    core = os.path.join(src_root, 'renderdoc', 'core', 'core.h')
    if not os.path.isfile(core) and not _SRC_WARNED:
        _SRC_WARNED = True
        sys.stderr.write(
            'warning: RenderDoc source not found at %s\n'
            '         Put a copy of the RenderDoc source tree in the root folder as `renderdoc-src`,\n'
            '         i.e. <folder containing rdc_analysis.py>/renderdoc-src/ (see README section 1).\n'
            '         Searched: $RENDERDOC_SRC, <tool folder>/renderdoc-src, <parent>/renderdoc-src.\n'
            '         Chunk names will fall back to numeric ids; everything else still works.\n' % src_root)
    if os.path.isfile(core):
        with open(core, encoding='utf-8', errors='replace') as fh:
            names.update(parse_chunk_enum(fh.read(), 'SystemChunk'))
    d = driver.lower()
    drv = os.path.join(src_root, 'renderdoc', 'driver', d, d + '_common.h')
    if os.path.isfile(drv):
        with open(drv, encoding='utf-8', errors='replace') as fh:
            names.update(parse_chunk_enum(fh.read(), driver + 'Chunk'))
    return names


def iter_chunks(stream: bytes, limit: int = 0) -> Iterator[ChunkInfo]:
    """Yield one `ChunkInfo` per framed SDChunk in `stream` (see the framing note above)."""
    pos = 0
    n = 0
    while pos + 4 <= len(stream):
        start = pos
        c = u32(stream, pos)
        pos += 4
        cid = c & 0xFFFF
        if cid == 0:
            break
        if c & CHUNK_CALLSTACK:
            num = u32(stream, pos)
            pos += 4 + num * 8
        if c & CHUNK_THREADID:
            pos += 8
        if c & CHUNK_DURATION:
            pos += 8
        if c & CHUNK_TIMESTAMP:
            pos += 8
        if c & CHUNK_64BITSIZE:
            ln = u64(stream, pos)
            pos += 8
        else:
            ln = u32(stream, pos)
            pos += 4
        if ln > len(stream) - pos:
            break
        yield {'off': start, 'id': cid, 'flags': c & 0xFFFF0000, 'length': ln, 'data': pos}
        pos = align_up(pos + ln)
        n += 1
        if limit and n >= limit:
            break


def chunk_strings(stream: bytes, ch: ChunkInfo, minlen: int = 4, limit: int = 6) -> List[str]:
    """Strings inside a payload (ASCII **and** UTF-16LE), de-duplicated and capped at `limit`."""
    blob = chunk_payload(stream, ch)
    found = [s for _, s in string_runs(blob, minlen)]
    wide = blob.decode('utf-16-le', 'ignore')
    found += [m.group() for m in _wide_pattern(minlen).finditer(wide)]
    out: List[str] = []
    for s in found:
        if s not in out:
            out.append(s)
    return out[:limit]


def chunk_payload(stream: bytes, ch: ChunkInfo) -> bytes:
    """The payload bytes of a chunk (use this, never `off + 8`)."""
    return stream[ch['data']:ch['data'] + ch['length']]


def decode_chunk(name: Optional[str], blob: bytes) -> List[str]:
    """Decode the known D3D12 chunk payload layouts (each element is raw, ResourceId = u64)."""
    out: List[str] = []
    # The broad catch is deliberate: `blob` comes straight from a stream we only heuristically
    # validate, so any struct/Unicode/index error must surface as a printed `decode error: ...`
    # line rather than aborting the whole command (`chunk N` fed a str must not raise).
    try:
        if name == 'List_SetPipelineState' and len(blob) >= 16:
            out.append('cmdList=%d pso=%d' % (u64(blob, 0), u64(blob, 8)))
        elif name in ('List_DrawIndexedInstanced',) and len(blob) >= 28:
            out.append('cmdList=%d idx=%d inst=%d startIdx=%d baseVtx=%d startInst=%d'
                       % (u64(blob, 0), u32(blob, 8), u32(blob, 12), u32(blob, 16),
                          u32(blob, 20), u32(blob, 24)))
        elif name in ('List_DrawInstanced',) and len(blob) >= 24:
            out.append('cmdList=%d verts=%d inst=%d startVtx=%d startInst=%d'
                       % (u64(blob, 0), u32(blob, 8), u32(blob, 12), u32(blob, 16),
                          u32(blob, 20)))
        elif name == 'List_Dispatch' and len(blob) >= 20:
            out.append('cmdList=%d x=%d y=%d z=%d' % (u64(blob, 0), u32(blob, 8), u32(blob, 12),
                                                      u32(blob, 16)))
        elif name in ('List_SetGraphicsRootConstantBufferView',
                      'List_SetGraphicsRootShaderResourceView',
                      'List_SetGraphicsRootUnorderedAccessView') and len(blob) >= 28:
            # [u64 cmdList][u32 rootParam][u64 resourceId][u64 byteOffset] -- D3D12BufferLocation
            # serialises as Id + Offset (d3d12_serialise.cpp), so this is the same pair `draws` uses.
            out.append('cmdList=%d rootParam=%d res=%d+0x%x'
                       % (u64(blob, 0), u32(blob, 8), u64(blob, 12), u64(blob, 20)))
        elif name == 'List_SetGraphicsRootDescriptorTable' and len(blob) >= 16:
            out.append('cmdList=%d rootParam=%d gpuHandle=0x%x' % (u64(blob, 0), u32(blob, 8),
                                                                   u64(blob, 12)))
        elif name == 'List_SetGraphicsRootSignature' and len(blob) >= 16:
            out.append('cmdList=%d rootSig=%d' % (u64(blob, 0), u64(blob, 8)))
        elif name == 'List_IASetVertexBuffers' and len(blob) >= 24:
            # [u64 cmdList][u32 startSlot][u32 numViews][u64 arrayCount] then 24 bytes per view:
            # [u64 resourceId][u64 VA][u32 sizeInBytes][u32 strideInBytes]
            start_slot, num_views = u32(blob, 8), u32(blob, 12)
            out.append('cmdList=%d startSlot=%d numViews=%d' % (u64(blob, 0), start_slot, num_views))
            for i in range(min(num_views, 16)):
                o = 24 + i * 24
                if o + 24 > len(blob):
                    break
                out.append('    view[%d] res=%d VA=0x%x size=%d stride=%d'
                           % (start_slot + i, u64(blob, o), u64(blob, o + 8), u32(blob, o + 16),
                              u32(blob, o + 20)))
        elif name == 'List_IASetIndexBuffer' and len(blob) >= 9:
            # [u64 cmdList][u8 present][u64 resourceId][u64 byteOffset][u32 size][u32 format]
            # The view goes through SERIALISE_ELEMENT_OPT, which writes a "present" bool first
            # (serialiser.h), so the whole payload is 33 bytes and every field is one byte later
            # than the struct alone would suggest. `size` is indexCount * formatWidth.
            if not blob[8]:
                out.append('cmdList=%d (null index buffer view)' % u64(blob, 0))
            elif len(blob) >= 33:
                out.append('cmdList=%d res=%d+0x%x size=%d fmt=%d'
                           % (u64(blob, 0), u64(blob, 9), u64(blob, 17), u32(blob, 25),
                              u32(blob, 29)))
        elif name == 'Device_CreatePipelineState' and len(blob) >= 8:
            out.append('payload %d bytes; tail=%s' % (len(blob), blob[-24:].hex()))
        elif name in ('InitialContents', 'InitialContentsList') and len(blob) >= 32:
            out.append('id=%d hdr=%s' % (u64(blob, 0), blob[8:40].hex()))
    except Exception as exc:  # noqa: BLE001 - see the comment above
        out.append('decode error: %s' % exc)
    return out


def cmd_chunk_detail(path: str, index: int, hexlen: int = 160) -> None:
    """Full inspector for one chunk: header, decoded fields, hex dump and payload strings."""
    _info, stream, _how = load_stream(path)
    names = load_chunk_names()
    for i, ch in enumerate(iter_chunks(stream), 1):
        if i != index:
            continue
        nm = names.get(ch['id'], 'Chunk%d' % ch['id'])
        print('chunk #%d  @0x%x  id=%d (%s)  flags=0x%x  length=%d'
              % (i, ch['off'], ch['id'], nm, ch['flags'], ch['length']))
        print('payload @0x%x (header+metadata = %d bytes)' % (ch['data'], ch['data'] - ch['off']))
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
    names = load_chunk_names()
    print('stream %d bytes [%s]; known chunk names: %d' % (len(stream), how, len(names)))
    if not names:
        print('WARNING: RenderDoc source not found at %r -> numeric chunk ids only' % RENDERDOC_SRC)
    total = shown = 0
    for ch in iter_chunks(stream):
        total += 1
        nm = names.get(ch['id'], 'Chunk%d' % ch['id'])
        if name_filter and name_filter.lower() not in nm.lower():
            continue
        if shown < limit:
            strs = chunk_strings(stream, ch)
            print('#%-6d @0x%-10x %-40s len=%-8d%s'
                  % (total, ch['off'], nm, ch['length'], (' ' + ' | '.join(strs)) if strs else ''))
            shown += 1
    print('total chunks: %d (shown %d)' % (total, shown))


def cmd_draws(path: str, max_draws: int = 80) -> None:
    """Per-draw table: marker path, PSO, constant buffers (resourceId+offset), vertex streams, args."""
    _info, stream, _how = load_stream(path)
    names = load_chunk_names()
    stack: List[str] = []
    pso: Optional[int] = None
    vbs: List[Tuple[int, int, int, int]] = []
    ib: Optional[str] = None
    cbvs: List[Tuple[int, int, int]] = []
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
        elif nm == 'List_SetPipelineState' and len(blob) >= 16:
            pso = u64(blob, 8)
            vbs, ib, cbvs = [], None, []
        elif nm == 'List_IASetVertexBuffers' and len(blob) >= 24:
            n = u32(blob, 12)
            vbs = [(u64(blob, 24 + i * 24), u64(blob, 32 + i * 24), u32(blob, 40 + i * 24),
                    u32(blob, 44 + i * 24))
                   for i in range(min(n, 16)) if 24 + i * 24 + 24 <= len(blob)]
        elif nm == 'List_IASetIndexBuffer' and len(blob) >= 33 and blob[8]:
            # the view is serialised through SERIALISE_ELEMENT_OPT: [u8 present] then
            # (resourceId, byteOffset) - see decode_chunk for the full layout
            ib = 'res%d+0x%x' % (u64(blob, 9), u64(blob, 17))
        elif nm == 'List_SetGraphicsRootConstantBufferView' and len(blob) >= 28:
            cbvs.append((u32(blob, 8), u64(blob, 12), u64(blob, 20)))
        elif nm in DRAW_CHUNKS:
            n_draw += 1
            if n_draw <= max_draws:
                if nm == 'List_DrawIndexedInstanced' and len(blob) >= 28:
                    args = 'idx=%d inst=%d' % (u32(blob, 8), u32(blob, 12))
                elif nm == 'List_DrawInstanced' and len(blob) >= 24:
                    args = 'verts=%d inst=%d' % (u32(blob, 8), u32(blob, 12))
                else:
                    args = 'x=%d y=%d z=%d' % (u32(blob, 8), u32(blob, 12), u32(blob, 16))
                print('#%-6d %-24s %-8s %-9s %s'
                      % (idx, ' / '.join(stack)[-24:], pso, args, nm.replace('List_', '')))
                if cbvs:
                    print('        CBV: ' + '  '.join('rp%d=res%d+0x%x' % c for c in cbvs))
                if vbs:
                    print('        VB : ' + '  '.join('res%d+0x%x(sz%d,st%d)' % v for v in vbs))
                if ib:
                    print('        IB : %s' % ib)
            vbs, ib, cbvs = [], None, []
    print('total draws/dispatches: %d' % n_draw)


SINGLEPROBE_SIG = (0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0)


def cmd_initial(path: str, resid: int = 0, probe_offset: int = -1, length: int = 256) -> None:
    """List InitialContents chunks (resource id + size), or dump floats for one resource+offset.

    Payload layout: [u64 resourceId] then the resource/subresource description, then the data.
    The header length is not fixed across resource types, so the data start is found by validating
    the known SingleProbe ILC signature at `probe_offset`.
    """
    _info, stream, _how = load_stream(path)
    names = load_chunk_names()
    for idx, ch in enumerate(iter_chunks(stream), 1):
        if names.get(ch['id'], '') != 'InitialContents':
            continue
        blob = chunk_payload(stream, ch)
        rid = u64(blob, 0)
        if resid and rid != resid:
            continue
        if not resid:
            print('#%-5d @0x%-10x res=%-7d chunkLen=%-10d head=%s'
                  % (idx, ch['off'], rid, ch['length'], blob[8:40].hex()))
            continue
        print('chunk #%d res=%d chunkLen=%d payload@0x%x' % (idx, rid, ch['length'], ch['data']))
        if probe_offset < 0:
            return
        best: Optional[Tuple[int, int, Tuple[float, ...]]] = None
        for hdr in range(8, 200, 4):
            o = hdr + probe_offset
            if o + 48 > len(blob):
                break
            vals = struct.unpack_from('<12f', blob, o)
            score = sum(1 for a, b in zip(vals, SINGLEPROBE_SIG) if abs(a - b) < 1e-6)
            if best is None or score > best[0]:
                best = (score, hdr, vals)
            if score == 12:
                break
        # `best` stays None only when no header candidate fitted at all (payload too short). The
        # original code raised TypeError there and a test pins exactly that, so the subscript is
        # deliberately left unguarded (CHARACTERIZATION - do not "fix" this).
        best = cast(Tuple[int, int, Tuple[float, ...]], best)
        print('  best header guess: %d bytes, signature match %d/12' % (best[1], best[0]))
        data = blob[best[1]:]
        vals = struct.unpack_from('<%df' % (length // 4), data, probe_offset)
        print('  floats @+0x%x (SingleProbe ILC layout: Add|Scale|MinUV|MaxUV|SkyBentNormal|Shadow|SH0[3]|SH1[3]|SH2):'
              % probe_offset)
        for k in range(0, len(vals), 6):
            print('    +%-4d %s' % (k * 4, ' '.join('%11.5f' % v for v in vals[k:k + 6])))
        return
    print('resource %d not found in InitialContents chunks' % resid)


def cmd_summary(path: str) -> None:
    """Chunk/draw/marker counts, the chunk-type histogram (top 40) and all markers in order."""
    _info, stream, _how = load_stream(path)
    names = load_chunk_names()
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
    names = load_chunk_names()
    n = 0
    for ch in iter_chunks(stream):
        n += 1
        nm = names.get(ch['id'], '')
        if nm in MARKER_CHUNKS:
            print('#%-6d %-16s %s' % (n, nm, ' | '.join(chunk_strings(stream, ch, 3, 3))))


def cmd_rootconst(path: str, max_chunks: int = 8) -> None:
    """Dump SetGraphicsRoot32BitConstant(s) payloads.

    Payload layout (d3d12_command_list_wrap.cpp Serialise_SetGraphicsRoot32BitConstants):
      ResourceId pCommandList(u64) | RootParameterIndex(u32) | Num32BitValuesToSet(u32)
      | arrayCount(u64) | values[Num32BitValuesToSet](u32 each) | DestOffsetIn32BitValues(u32)
    `SERIALISE_ELEMENT_ARRAY` writes the element count before the values (serialiser.h), so
    length == 28 + 4*n -- which is used as a sanity check.
    """
    _info, stream, _how = load_stream(path)
    names = load_chunk_names()
    shown = 0
    total = 0
    for idx, ch in enumerate(iter_chunks(stream), 1):
        nm = names.get(ch['id'], '')
        if nm not in ('List_SetGraphicsRoot32BitConstants', 'List_SetGraphicsRoot32BitConstant'):
            continue
        total += 1
        if shown >= max_chunks:
            continue
        blob = stream[ch['data']:ch['data'] + ch['length']]
        print('--- chunk #%d @0x%x %s len=%d' % (idx, ch['off'], nm, ch['length']))
        if nm.endswith('Constants') and ch['length'] >= 28 and (ch['length'] - 28) % 4 == 0:
            root_param = u32(blob, 8)
            n = u32(blob, 12)
            array_count = u64(blob, 16)
            print('    rootParam=%d numValues=%d destOffset=%d'
                  % (root_param, n, u32(blob, 24 + n * 4) if 24 + n * 4 + 4 <= len(blob) else -1))
            if array_count != n:
                print('    warning: inline arrayCount=%d disagrees with numValues=%d'
                      % (array_count, n))
            if 24 + n * 4 <= len(blob):
                floats = struct.unpack_from('<%df' % n, blob, 24)
                for k in range(0, n, 8):
                    print('    +%-3d %s' % (k, ' '.join('%12.5f' % v for v in floats[k:k + 8])))
        else:
            print('    hex: %s' % blob[:96].hex())
        shown += 1
    print('root-constant chunks total: %d (shown %d)' % (total, shown))


def cmd_dump_chunk(path: str, index: int, outfile: str) -> None:
    """Write one chunk's payload verbatim to `outfile`."""
    _info, stream, _how = load_stream(path)
    names = load_chunk_names()
    for i, ch in enumerate(iter_chunks(stream), 1):
        if i == index:
            blob = stream[ch['data']:ch['data'] + ch['length']]
            with open(outfile, 'wb') as f:
                f.write(blob)
            print('chunk #%d %s (id=%d, flags=0x%x) -> %s (%d bytes)'
                  % (i, names.get(ch['id'], '?'), ch['id'], ch['flags'], outfile, len(blob)))
            return
    print('chunk #%d not found (stream has fewer chunks)' % index)


def cmd_dump_shaders(path: str, outdir: str) -> None:
    """Write every DXIL container plus a reflection summary to disk."""
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
        whole = blob
        strs = part_strings(whole, 0, len(whole), 4)
        gi = sorted({s for s in strs if any(k in s for k in GI_SUBSTRINGS)})
        lines.append('shader_%02d  hash=%s  size=%d  parts=%s' % (n, h, size, ','.join(names)))
        lines.append('    file    : %s' % fn)
        lines.append('    GI vars : %s' % (', '.join(gi) if gi else '(none)'))
    with open(os.path.join(outdir, 'shaders.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('wrote %d shader blobs + shaders.txt to %s' % (n, outdir))


def _filter_suite(suite: unittest.TestSuite, patterns: Sequence[str]) -> unittest.TestSuite:
    """Keep only the tests whose id contains one of `patterns` (like `unittest -k`)."""
    out = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            out.addTest(_filter_suite(test, patterns))
        elif any(p in test.id() for p in patterns):
            out.addTest(test)
    return out


def cmd_selftest(args: Optional[Sequence[str]] = None) -> int:
    """Run the unit-test suite in the `tests` folder next to this file.

    Extra arguments: `-v` for verbose, `-k <substring>` to run matching tests only.
    Returns a process exit code (0 = all passed).
    """
    argv = list(args or [])
    verbosity, patterns, unknown = 1, [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('-v', '--verbose'):
            verbosity = 2
        elif a in ('-q', '--quiet'):
            verbosity = 1
        elif a in ('-k', '--pattern') and i + 1 < len(argv):
            i += 1
            patterns.append(argv[i])
        else:
            unknown.append(a)
        i += 1
    if unknown:
        print('usage: rdc_analysis.py selftest [-v] [-k <substring>]')
        return 2
    here = os.path.dirname(os.path.abspath(__file__))
    tests_dir = os.path.join(here, 'tests')
    if not os.path.isdir(tests_dir):
        print('no tests folder next to %s (expected %s)' % (os.path.basename(__file__), tests_dir))
        return 1
    print('running tests from %s' % tests_dir)
    suite = unittest.TestLoader().discover(tests_dir, pattern='test_*.py', top_level_dir=tests_dir)
    if patterns:
        suite = _filter_suite(suite, patterns)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


def _arg(argv: Sequence[str], index: int, default: Optional[int] = None,
         base: int = 10) -> int:
    """Return `int(argv[index], base)`, or `default` when the argument is absent.

    Mirrors the original hand-rolled `int(sys.argv[n]) if len(sys.argv) > n else default` indexing:
    a missing argument uses the default, a present-but-unparsable one raises `ValueError` and a
    missing required argument (no default) raises `IndexError` -- both pinned by the CLI tests.
    """
    if index < len(argv):
        return int(argv[index], base)
    if default is None:
        raise IndexError('missing command argument #%d' % index)
    return default


def main() -> None:
    """CLI entry point: dispatch `sys.argv[1]` to the matching `cmd_*` function."""
    argv = sys.argv
    if len(argv) > 1 and argv[1] in ('test', 'selftest'):
        sys.exit(cmd_selftest(argv[2:]))
    if len(argv) < 3:
        print(__doc__)
        return
    cmd, path = argv[1], argv[2]
    if cmd == 'chunk':
        cmd_chunk_detail(path, int(argv[3]))
    elif cmd == 'draws':
        cmd_draws(path, _arg(argv, 3, 80))
    elif cmd == 'initial':
        cmd_initial(path, _arg(argv, 3, 0), _arg(argv, 4, -1, base=0))
    elif cmd == 'chunks':
        cmd_chunks(path, _arg(argv, 3, 200), argv[4] if len(argv) > 4 else None)
    elif cmd == 'summary':
        cmd_summary(path)
    elif cmd == 'markers':
        cmd_markers(path)
    elif cmd == 'rootconst':
        cmd_rootconst(path, _arg(argv, 3, 8))
    elif cmd == 'dump-chunk':
        cmd_dump_chunk(path, int(argv[3]), argv[4])
    elif cmd == 'dump-shaders':
        cmd_dump_shaders(path, argv[3])
    elif cmd == 'report':
        cmd_report(path)
    elif cmd == 'sections':
        cmd_sections(path)
    elif cmd == 'blocks':
        cmd_blocks(path)
    elif cmd == 'strings':
        cmd_strings(path, _arg(argv, 3, 6), _arg(argv, 4, 200))
    elif cmd == 'names':
        cmd_names(path, _arg(argv, 3, 10))
    elif cmd == 'grep':
        cmd_grep(path, argv[3], _arg(argv, 4, 200))
    elif cmd == 'dump':
        cmd_dump(path, int(argv[3], 0), int(argv[4], 0), _arg(argv, 5, 4))
    elif cmd == 'dxbc':
        cmd_dxbc(path, _arg(argv, 3, 0))
    elif cmd == 'count':
        cmd_count(path, argv[3:])
    elif cmd == 'hex':
        cmd_hex(path, argv[3], argv[4])
    elif cmd == 'sig':
        cmd_sig(path)
    elif cmd == 'pattern':
        cmd_pattern(path, argv[3], _arg(argv, 4, 72))
    elif cmd == 'float':
        cmd_float(path, argv[3])
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
