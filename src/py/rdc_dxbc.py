"""The DXBC/DXIL containers the capture carries: where they are, what parts they hold and what strings those parts contain."""

from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_cache import *  # noqa: F401,F403
from rdc_scan import *  # noqa: F401,F403  (the sliced byte find, which sits below this module)
import rdc_cache        # called qualified: `stream_source` is the cache's own answer
import rdc_scan         # called qualified: a test's `mock.patch.object` has to reach it

from typing import Dict, Iterator, List, Optional, Sequence, TypedDict

def parse_dxil_containers(stream: Buffer,
                          source: Optional[CacheEntry] = None) -> Iterator[DxbcContainer]:
    """Yield `(offset, size, hash_hex, parts)` for every DXBC/DXIL container in `stream`.

    Container header: 'DXBC' magic(4) | hash(16) | version(4) | size(4) | partCount(4) |
    partOffsets[partCount](4) ; each part at +offset: fourcc(4) | size(4) | data.

    Finding the containers is a `find` over the whole stream, which is 1.25 s of `desktop-2`'s
    1.47 GB `draws` and `dxbc` -- so with a `source` (the stream-cache entry, see `load_stream`) the
    search goes through `rdc_scan.find_all`, which splits it across processes for a stream big enough to
    pay for them. Without one it is the same serial loop as before, and the containers are identical
    either way: the offsets come back in the order this would have found them.
    """
    for i in rdc_scan.find_all(stream, b'DXBC', source):
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

def part_strings(blob: Buffer, off: int, ln: int, minlen: int = 4) -> List[str]:
    """Return the ASCII strings (>= `minlen`) inside `blob[off:off+ln]`."""
    return [s for _, s in string_runs(blob, minlen, off, off + ln)]

class DxbcRow(TypedDict):
    """Internal per-container row used by `cmd_dxbc` (never leaves the module)."""
    off: int
    size: int
    hash: str
    stage: str
    parts: List[str]

def cmd_dxbc(path: str) -> None:
    """Inventory the DXBC/DXIL containers: offset, size, stage, hash and the parts each carries.

    This is a *container* view, not a shader analysis: it says which shaders the capture embeds and
    where, so `dump-shaders` can extract the interesting ones. What a shader *reads* -- uniform
    names, bind points, signatures, disassembly -- is the shader reflection's job, and the reflection
    is the replay driver's (REFERENCE §9). The tool used to guess at it by scanning containers for
    GI-ish strings and `TEXCOORD6..12`; that harvest was a worse answer to a question replay answers
    exactly, so it was removed rather than kept.

    The rows come from `rdc_psos.container_list`, i.e. from the cached index when there is one: what
    discovers the containers is a whole-stream `find` (0.43 s of this command's 0.58 s on the 1.55 GB UE
    capture, and it grows with the stream), and the rows it yields *are* this command's answer -- so paying
    it once per capture instead of once per call is what the index is for. Imported inside the function
    because `rdc_psos` reads this module's parser, and importing it at the top would close a cycle.
    """
    import rdc_psos

    info, stream, how = load_stream(path)
    print('stream %d bytes [%s]' % (len(stream), how))
    rows: List[DxbcRow] = []
    for row in rdc_psos.container_list(stream, rdc_cache.stream_source(path, info)):
        names = [p[0] for p in row['parts']]
        osg = next((p for p in row['parts'] if p[0] == 'OSG1'), None)
        osg_s = part_strings(stream, osg[1], osg[2], 3) if osg else []
        stage = 'root-sig' if 'RTS0' in names else (
            'PS' if any('SV_Target' in s for s in osg_s) else
            'VS' if any('SV_Position' in s for s in osg_s) else
            'CS' if 'CS' in names else '?')
        rows.append({'off': row['offset'], 'size': row['size'], 'hash': row['hash'], 'stage': stage,
                     'parts': names})
    print('DXBC/DXIL containers: %d' % len(rows))
    by_stage: Dict[str, List[DxbcRow]] = {}
    for r in rows:
        by_stage.setdefault(r['stage'], []).append(r)
    for st in sorted(by_stage):
        print('  %-9s %d' % (st, len(by_stage[st])))
    print()
    print('%-4s %-10s %-8s %-8s %-34s %s' % ('#', 'offset', 'size', 'stage', 'hash', 'parts'))
    for idx, r in enumerate(rows):
        print('%-4d 0x%-8x %-8d %-8s %-34s %s'
              % (idx, r['off'], r['size'], r['stage'], r['hash'][:32], ','.join(r['parts'])))

def count_in(buf: Buffer, needle: bytes) -> int:
    """`bytes.count` for a buffer that may be an `mmap`, which has no `count`.

    Counts non-overlapping occurrences, exactly as `count` does, and answers `len(buf) + 1` for an empty
    needle (`bytes.count(b'')` does the same). A `find` loop is the only way to that number on a map.
    """
    if not needle:
        return len(buf) + 1
    hits = 0
    at = buf.find(needle)
    while at >= 0:
        hits += 1
        at = buf.find(needle, at + len(needle))
    return hits

def cmd_count(path: str, pats: Sequence[str]) -> None:
    """Print the occurrence count and first offset of each pattern (verbatim, `-1` included).

    The counts come from `rdc_scan.find_all_many` -- the same `find`-loop answer, split across
    processes when the stream is big enough to pay for them -- because each pattern is otherwise a full
    serial pass. One call for all the patterns, not one each: the pool, the mapping and the slice
    arithmetic are the same work every time, and only the passes multiply. Measured on `desktop-2`'s
    1.47 GB stream, three patterns, one session: 2.247 s one `find_all` at a time against 0.758 s here
    (the interpreter's own 0.13 s is under both). An empty pattern keeps the in-file loop: its
    `len(stream) + 1` is `bytes.count`'s answer for `b''`, and not something a find has an opinion
    about.
    """
    info, stream, how = load_stream(path)
    source = rdc_cache.stream_source(path, info)
    print('stream %d bytes [%s]' % (len(stream), how))
    needles = [p.encode() for p in pats]
    found = iter(rdc_scan.find_all_many(stream, [b for b in needles if b], source))
    for p, b in zip(pats, needles):
        if not b:
            print('  %-30s count=%-8d first=0x%x' % (p, count_in(stream, b), stream.find(b)))
            continue
        hits = next(found)
        print('  %-30s count=%-8d first=0x%x' % (p, len(hits), hits[0] if hits else -1))

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
__all__ = [
    'DxbcRow',
    'cmd_count',
    'cmd_dxbc',
    'cmd_hex',
    'parse_dxil_containers',
    'part_strings',
]
