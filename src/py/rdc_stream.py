"""The container and the frame stream: sections, LZ4/Zstd decompression, and the chunk framing the rest of the tool walks."""

from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_profile

import re
import struct

from typing import Dict, Iterator, List, Optional, Tuple

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

@rdc_profile.timed('container parse')
def parse_container(path: str) -> CaptureInfo:
    """Parse the `.rdc` container header, metadata, thumbnail and section table.

    The section walk stops at the first byte that is not 0: that byte is the end-of-sections
    marker (see REFERENCE 3.1). Section names are decoded as UTF-8 and NUL-trimmed.
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

    The one `out` buffer is carried across blocks on purpose: the blocks are **not** independent.
    RenderDoc writes a section as a single continuous LZ4 stream cut into 1 MB pages and both ends
    use the streaming API (`LZ4_compress_fast_continue` / `LZ4_decompress_safe_continue` with a
    shared stream context, serialise/lz4io.cpp), so a match can point up to 64 KB back into the
    previous page. Decoding blocks on their own therefore loses data -- a block-parallel version of
    this function returned 625,911,281 bytes for the PC capture in this repo where the section
    declares (and the serial walk produces) 630,790,592. That is why there is no parallel decoder
    here: the speedup is the stream cache instead (`cache_dir`), measured at 3.6 s -> 0.27 s on
    that capture.
    """
    out = bytearray()
    o = 0
    blocks = 0
    bar = rdc_profile.progress('lz4 decode (%d bytes of blocks)' % len(blob), expect or len(blob),
                               'bytes')
    bar.begin()
    while o + 4 <= len(blob) and (expect == 0 or len(out) < expect):
        clen = u32(blob, o)
        o += 4
        if clen == 0 or o + clen > len(blob):
            break
        lz4_block(blob[o:o + clen], out)
        o += clen
        blocks += 1
        bar.tick(len(out))
    bar.done(len(out))
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

# ---------------------------------------------------------------------------
# Decompressed-stream cache.
#
# Decompressing a frame-capture section costs seconds (pure-Python LZ4 over a few hundred MB of
# blocks) and every command needs the same stream, so the decompressed bytes are cached on disk
# and keyed by the capture's identity: absolute path + size + mtime + section index + format
# version. A hit shows up in the method label as `lz4(N blocks, cached)`.
#
# The cache is pure optimisation. `$RDC_NO_CACHE=1` disables it, `$RDC_CACHE_DIR` moves it, an
# unusable directory only warns (once), and a stale, truncated or foreign file is detected and
# rebuilt rather than trusted. Nothing else a command prints depends on it.
# ---------------------------------------------------------------------------
class FrameError(Exception):
    """A chunk frame that cannot be true -- raised by `iter_chunks(strict=True)`, collected by
    `check_stream()`. The default walk stops at such a frame instead (see `iter_chunks`)."""

def _read_frame(stream: bytes, pos: int) -> Optional[Tuple[ChunkInfo, int]]:
    """Parse the frame at `pos`: return `(chunk, next_pos)`, or None at the end of the stream.

    Raises `FrameError` when the frame claims bytes the stream does not hold (truncated metadata or
    a payload that runs past the end). `ChunkInfo.pad_start`/`pad_len` describe the 64-byte
    alignment padding that follows the payload; for the last chunk that padding may be absent from
    the stream, so callers must clamp with `min(pad_len, len(stream) - pad_start)`.
    """
    start = pos
    c = u32(stream, pos)
    pos += 4
    cid = c & 0xFFFF
    if cid == 0:
        return None
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
        if pos + 8 > len(stream):
            raise FrameError('chunk @0x%x (id %d): metadata runs past the end of the stream'
                             % (start, cid))
        ln = u64(stream, pos)
        pos += 8
    else:
        if pos + 4 > len(stream):
            raise FrameError('chunk @0x%x (id %d): metadata runs past the end of the stream'
                             % (start, cid))
        ln = u32(stream, pos)
        pos += 4
    if ln > len(stream) - pos:
        raise FrameError('chunk @0x%x (id %d): payload of %d bytes runs %d past the end of the stream'
                         % (start, cid, ln, ln - (len(stream) - pos)))
    pad_start = pos + ln
    chunk: ChunkInfo = {'off': start, 'id': cid, 'flags': c & 0xFFFF0000, 'length': ln,
                        'payload_offset': pos, 'pad_start': pad_start,
                        'pad_len': align_up(pad_start) - pad_start}
    return chunk, align_up(pad_start)

def iter_chunks(stream: bytes, limit: int = 0, strict: bool = False) -> Iterator[ChunkInfo]:
    """Yield one `ChunkInfo` per framed SDChunk in `stream` (see the framing note above).

    The walk is lenient by default: a frame that cannot be true ends the iteration, which is what a
    capture with trailing garbage needs. With `strict=True` such a frame raises `FrameError`
    instead; `check_stream()` reports *all* of them rather than stopping at the first.
    """
    pos = 0
    n = 0
    while pos + 4 <= len(stream):
        try:
            frame = _read_frame(stream, pos)
        except FrameError:
            if strict:
                raise
            return
        if frame is None:
            return
        ch, pos = frame
        yield ch
        n += 1
        if limit and n >= limit:
            return

def chunk_strings(stream: bytes, ch: ChunkInfo, minlen: int = 4, limit: int = 6) -> List[str]:
    """Strings inside a payload (ASCII **and** UTF-16LE), de-duplicated and capped at `limit`.

    The cap is applied *while* collecting, and the wide half is skipped once `limit` ASCII strings are
    in -- which is the same answer the old order produced (ASCII first, then UTF-16LE, then the cap),
    for a fraction of the work. It used to collect every run, then de-duplicate with `not in`, then
    cap: a stream holds 8.4 M runs of four printable bytes or more (measured on the hobby capture), so
    that was quadratic per payload and, with the UTF-16 decode it did not need, 13 ms per chunk across
    its 29,212 chunks -- six and a half minutes for `chunks <rdc> 0`, none of it in the decode.
    """
    if limit <= 0:
        return []
    blob = chunk_payload(stream, ch)
    out: List[str] = []
    for _, s in string_runs(blob, minlen):
        if s not in out:
            out.append(s)
            if len(out) >= limit:
                return out
    wide = blob.decode('utf-16-le', 'ignore')
    for m in _wide_pattern(minlen).finditer(wide):
        s = m.group()
        if s not in out:
            out.append(s)
            if len(out) >= limit:
                break
    return out

def chunk_payload(stream: bytes, ch: ChunkInfo) -> bytes:
    """The payload bytes of a chunk (use this, never `off + 8`)."""
    return stream[ch['payload_offset']:ch['payload_offset'] + ch['length']]

@rdc_profile.timed('verify: walk')
def check_stream(stream: bytes, names: Optional[Dict[int, str]] = None,
                 note_samples: int = 5) -> Tuple[List[str], List[str]]:
    """Walk `stream` and return `(problems, notes)`; used by `verify`.

    Unlike the lenient walk this does not stop at the first bad frame, so a corrupt capture gets a
    full report.

    `problems` mean the parse cannot be trusted and should be fixed:

    * a frame that cannot be true (truncated metadata, payload past the end of the stream);
    * a payload whose length disagrees with the layout the decoder expects (`EXPECTED_LENGTHS`) --
      the chunk length is the checksum for the decoder (REFERENCE 3.4).

    `notes` are legal but worth knowing:

    * non-zero alignment padding. Padding is stale capture-buffer content, so this is normal; it is
      reported because a decoder that read past `length` would see those bytes as plausible data.
      `note_samples` caps how many chunks are listed, the rest are counted.
    * bytes left over after the last chunk (only when there is more than a terminator word).
    """
    if names is None:
        names = rdc_chunkmap.load_chunk_names()
    problems: List[str] = []
    notes: List[str] = []
    stale_chunks = 0
    stale_bytes = 0
    padding_bytes = 0
    broken = False
    pos = 0
    n = 0
    while pos + 4 <= len(stream):
        try:
            frame = _read_frame(stream, pos)
        except FrameError as exc:
            problems.append(str(exc))
            broken = True
            break
        if frame is None:
            break
        ch, pos = frame
        n += 1
        name = names.get(ch['id'], 'Chunk%d' % ch['id'])
        pad_len = min(ch['pad_len'], max(0, len(stream) - ch['pad_start']))
        pad = stream[ch['pad_start']:ch['pad_start'] + pad_len]
        padding_bytes += pad_len
        stale = sum(1 for b in pad if b)
        if stale:
            stale_chunks += 1
            stale_bytes += stale
            if len(notes) < note_samples:
                notes.append('chunk #%d @0x%x (%s): %d of %d padding bytes are non-zero (%s)'
                             % (n, ch['off'], name, stale, pad_len, pad[:16].hex()))
        expected = EXPECTED_LENGTHS.get(name)
        if expected is not None and ch['length'] not in expected:
            problems.append('chunk #%d @0x%x (%s): payload is %d bytes, the decoder expects %s'
                            % (n, ch['off'], name, ch['length'],
                               ' or '.join(str(e) for e in expected)))
    if stale_chunks:
        notes.append('padding: %d of %d chunks carry %d non-zero bytes of %d checked - padding is '
                     'stale capture-buffer content, not data'
                     % (stale_chunks, n, stale_bytes, padding_bytes))
    else:
        notes.append('padding: %d bytes checked in %d chunks, all zero' % (padding_bytes, n))
    trailing = len(stream) - pos
    if trailing > 4 and not broken:
        notes.append('%d bytes follow the last chunk (first 16: %s)'
                     % (trailing, stream[pos:pos + 16].hex()))
    return problems, notes

__all__ = [
    'FrameError',
    '_read_frame',
    '_run_pattern',
    '_wide_pattern',
    'check_stream',
    'chunk_payload',
    'chunk_strings',
    'decompress_lz4',
    'decompress_zstd',
    'iter_chunks',
    'lz4_block',
    'parse_container',
    'string_runs',
    'u16',
    'u32',
    'u64',
]
