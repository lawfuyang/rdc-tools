"""The container and the frame stream: sections, LZ4/Zstd decompression, and the chunk framing the rest of the tool walks."""

from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_profile

import mmap
import os
import re
import struct

from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

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

def string_runs(blob: BufferLike, minlen: int = 6, start: int = 0,
                end: Optional[int] = None) -> Iterator[Tuple[int, str]]:
    """Yield `(offset, text)` for every run of `minlen` or more printable ASCII bytes in `blob`.

    `minlen` is honoured exactly: the old fixed `{6,}` scan silently dropped shorter strings, which is
    why marker names under 6 characters used to print as `?`. Offsets are absolute within `blob`;
    `start`/`end` only restrict the window that gets scanned.
    """
    stop = len(blob) if end is None else end
    for m in _run_pattern(minlen).finditer(blob, start, stop):
        yield m.start(), m.group().decode('ascii', 'replace')

def u16(b: BufferLike, o: int) -> int:
    """Read a little-endian unsigned 16-bit value at offset `o`."""
    return struct.unpack_from('<H', b, o)[0]

def u32(b: BufferLike, o: int) -> int:
    """Read a little-endian unsigned 32-bit value at offset `o`."""
    return struct.unpack_from('<I', b, o)[0]

def u64(b: BufferLike, o: int) -> int:
    """Read a little-endian unsigned 64-bit value at offset `o`."""
    return struct.unpack_from('<Q', b, o)[0]

def f32(b: BufferLike, o: int) -> float:
    """Read a little-endian `float` at offset `o`.

    A `D3D12_SAMPLER_DESC`'s LOD fields and its border colour are floats rather than words, so the word
    readers are not enough for the sampler decode; nothing else in the stream is read this way.
    """
    return struct.unpack_from('<f', b, o)[0]

def _read_container(path: str) -> Buffer:
    """The container's bytes: a read-only `mmap` when the file can be mapped, else a plain read.

    Nothing here needs the whole file at once -- the header, the section table, and (on a cache miss)
    one section body -- so mapping it costs nothing and saves the 630 MB read the 601 MB capture used to
    pay per command. `mmap` is also what a 32-bit or heavily loaded machine needs: the bytes stay on
    disk until something touches them.
    """
    try:
        with open(path, 'rb') as fh:
            return mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError):       # an empty file, a share, a lock: read it instead
        with open(path, 'rb') as fh:
            return fh.read()

@rdc_profile.timed('container parse')
def parse_container(path: str) -> CaptureInfo:
    """Parse the `.rdc` container header, metadata, thumbnail and section table.

    The section walk stops at the first byte that is not 0: that byte is the end-of-sections
    marker (see REFERENCE 3.1). Section names are decoded as UTF-8 and NUL-trimmed.
    """
    data: Buffer = _read_container(path)
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

#: An LZ4 match may reach 64 KB back, so that much of the output is the dictionary a block needs.
_LZ4_DICT = 1 << 16

#: Past this a section cannot be addressed by the C API's `int` sizes; it takes the per-block paths.
_LZ4_C_MAX = 0x7FFFFFFF

#: The library names to try after `bin/rdc_lz4.dll`: what a system package installs, on Windows, Linux and
#: macOS. `liblz4.so.1` deliberately before `liblz4.so`, which only exists with a -dev package.
_LZ4_LIBS = ('lz4.dll', 'liblz4.so.1', 'liblz4.so', 'liblz4.dylib')


def _lz4_dict_size(written: int) -> int:
    """How much of the output so far a block's matches may reach back into: the last 64 KB of it.

    LZ4's own limit, and the reason a page can be decoded knowing only the page before it -- see
    `decompress_lz4`. Named because it is one line of arithmetic that a wrong sign or a wrong constant
    would turn into a wrong stream, and the tests pin it where the pages do not reach it.
    """
    return written if written < _LZ4_DICT else _LZ4_DICT


def _lz4_c_function() -> Optional[Callable[..., int]]:
    """`LZ4_decompress_safe_usingDict` from whatever library has it, or None.

    `$RDC_LZ4_DLL` names one explicitly; otherwise the `bin/rdc_lz4.dll` this repository's build writes is
    tried (it is built from the vendored decoder in `src/cpp/third_party/lz4`), then the names a system
    package uses -- so a machine with `liblz4` needs no build of its own. A library older than 1.8 has no
    `usingDict` and is refused by the symbol lookup rather than by a version check.

    The signature is set rather than left to ctypes' guessing, which would truncate the pointers this call
    is entirely made of. `ctypes` itself is imported here, not at module level: the common run reads a
    cached stream and never comes near this.
    """
    import ctypes

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    candidates = [os.path.join(root, 'bin', 'rdc_lz4.dll')]
    named = os.environ.get('RDC_LZ4_DLL')
    if named:
        candidates.insert(0, named)
    candidates.extend(_LZ4_LIBS)

    for name in candidates:
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        fn = getattr(lib, 'LZ4_decompress_safe_usingDict', None)
        if fn is None:
            continue
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                       ctypes.c_int]
        return fn
    return None


def decompress_lz4(blob: Buffer, expect: int) -> Tuple[bytearray, int]:
    """Decompress a section body made of [u32 compressedBlockLength][raw LZ4 block]*.

    The decoder is the C library (`_lz4_c_function`), asked for the whole section at once: one
    destination of exactly `expect` bytes, one call per page, and the 64 KB before the write position as
    the dictionary. So the dictionary is the destination's own tail -- the contiguous case LZ4 has a
    fast path for -- and nothing is allocated or copied per page. Measured on this repo's captures:
    **0.54 s** for `desktop-2`'s 1.47 GB and **0.14 s** for `desktop-1`'s 631 MB, the decode itself
    being 0.219 s / 0.078 s (6.4 and 7.7 GB/s).

    `expect` is the section's declared `uncompLen`, and the stream has to produce exactly that. A page
    the library refuses, a body that ends early, and a section declaring more than it decodes all raise
    `FrameError`: a truncated stream returned as if it were whole is a stream every command downstream
    would then read as fact. A section declaring no bytes is empty whatever the body says, and takes no
    library at all.

    The one destination is carried across the pages on purpose: they are **not** independent.
    RenderDoc writes a section as a single continuous LZ4 stream cut into 1 MB pages and both ends
    use the streaming API (`LZ4_compress_fast_continue` / `LZ4_decompress_safe_continue` with a
    shared stream context, serialise/lz4io.cpp), so a match can point up to 64 KB back into the
    previous page. Decoding pages on their own therefore loses data -- a block-parallel version of
    this function returned 625,911,281 bytes for the PC capture in this repo where the section
    declares (and the serial walk produces) 630,790,592 -- which is why there is no parallel decoder
    here. The speedup for repeat work is the stream cache (`cache_dir`), free for every command after
    the first.
    """
    import ctypes

    if expect <= 0:
        return bytearray(), 0

    fn = _lz4_c_function()
    if fn is None:
        raise FrameError('no LZ4 decoder to decompress the section with: build bin/rdc_lz4.dll with '
                         '`cmake --build build`, or point $RDC_LZ4_DLL at a library exporting '
                         'LZ4_decompress_safe_usingDict (a system lz4.dll / liblz4.so.1 works)')
    if expect > _LZ4_C_MAX or len(blob) > _LZ4_C_MAX:
        raise FrameError('section too large for the LZ4 decoder: %d byte(s) of body, %d byte(s) of '
                         'output, and the C API takes int sizes' % (len(blob), expect))

    out = bytearray(expect)
    dst_ref = ctypes.c_char.from_buffer(out)    # held for the call: an export stops the buffer moving
    dst = ctypes.addressof(dst_ref)

    if isinstance(blob, bytearray):
        src_ref = ctypes.c_char.from_buffer(blob)   # exported where it lies, so no copy at all
    else:
        # A `c_char_p` points into `bytes` without copying and keeps it alive for as long as it is held,
        # and a read-only buffer -- a memory view over a read-only map -- is copied once into one. Both
        # real captures arrive as `bytes`: the container map is sliced, and an mmap slice is a copy, so
        # the copying branch is the one a test takes.
        src_ref = ctypes.c_char_p(blob if isinstance(blob, bytes) else bytes(blob))
    src = ctypes.cast(src_ref, ctypes.c_void_p).value
    if src is None:     # ctypes types a pointer's value as Optional; a live buffer's is never None
        raise FrameError('the LZ4 decoder was given no buffer to read')

    bar = rdc_profile.progress('lz4 decode (%d bytes of blocks)' % len(blob), expect, 'bytes')
    bar.begin()
    o = 0
    blocks = 0
    written = 0
    while o + 4 <= len(blob) and written < expect:
        clen = u32(blob, o)
        o += 4
        if clen == 0 or o + clen > len(blob):
            break
        dsize = _lz4_dict_size(written)
        produced = fn(src + o, dst + written, clen, expect - written, dst + written - dsize, dsize)
        if produced < 0:
            bar.done(written)
            raise FrameError('page %d of the section did not decode (%d of %d byte(s) of output, %d '
                             'byte(s) of input from offset %d)' % (blocks, written, expect, clen, o))
        written += produced
        o += clen
        blocks += 1
        bar.tick(written)
    bar.done(written)
    if written != expect:
        raise FrameError('the section decoded to %d byte(s) but declares %d' % (written, expect))
    return out, blocks

def decompress_zstd(blob: Buffer) -> bytes:
    """Decompress a Zstd section body.

    The optional dependency is imported *inside* the function on purpose: the module stays importable
    without `zstandard` installed, so an LZ4 capture -- which is what this tool's own captures are -- needs
    nothing extra. What a Zstd capture gets without it is the refusal below rather than a bare
    `ModuleNotFoundError`: the module's absence is a *missing decoder*, and the message has to say which
    capture needs one and what the fix is, because the alternative reading ("the tool is broken") is the
    one a traceback invites.

    Why a message rather than a vendored decoder: Zstd's entropy stage (FSE plus Huffman, two interleaved
    bitstreams) is an order of magnitude more code than the LZ4 decoder in `third_party/lz4`, and the
    project vendors the decoder it can read. `pip install zstandard` is one command, and a capture that
    needs it says so before anything else happens.
    """
    try:
        import zstandard  # optional dependency, imported lazily
    except ImportError:
        raise FrameError(
            "this capture's frame section is Zstd-compressed and no Zstd decoder is installed: "
            "`pip install zstandard` is the whole fix -- nothing else in the tool uses it, and a capture "
            "compressed with LZ4 needs nothing extra (that is what `bin/rdc_lz4.dll` is for, and what every "
            'capture this project has is compressed with)') from None
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
# Decompressing a frame-capture section costs seconds (LZ4 over a few hundred MB of blocks -- 0.54 s for the
# `desktop-2`'s 1.47 GB, through the built library) and every command needs the
# same stream, so the decompressed bytes are cached on disk and keyed by the capture's identity:
# absolute path + size + mtime + section index + format version. A hit shows up in the method label as
# `lz4(N blocks, cached)`.
#
# The cache is pure optimisation. `$RDC_NO_CACHE=1` disables it, `$RDC_CACHE_DIR` moves it, an
# unusable directory only warns (once), and a stale, truncated or foreign file is detected and
# rebuilt rather than trusted. Nothing else a command prints depends on it.
# ---------------------------------------------------------------------------
class FrameError(Exception):
    """A chunk frame that cannot be true -- raised by `iter_chunks(strict=True)`, collected by
    `check_stream()`. The default walk stops at such a frame instead (see `iter_chunks`).

    Also what `decompress_lz4` raises when a section body cannot be decoded -- there is no library to
    decode it with, or the bytes do not come out at the size the section declares."""

def _read_frame(stream: Buffer, pos: int) -> Optional[Tuple[ChunkInfo, int]]:
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

def iter_chunks(stream: Buffer, limit: int = 0, strict: bool = False) -> Iterator[ChunkInfo]:
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

def chunk_strings(stream: Buffer, ch: ChunkInfo, minlen: int = 4, limit: int = 6) -> List[str]:
    """Strings inside a payload (ASCII **and** UTF-16LE), de-duplicated and capped at `limit`.

    The cap is applied *while* collecting, and the wide half is skipped once `limit` ASCII strings are
    in -- which is the same answer the old order produced (ASCII first, then UTF-16LE, then the cap),
    for a fraction of the work. It used to collect every run, then de-duplicate with `not in`, then
    cap: a stream holds 8.4 M runs of four printable bytes or more (measured on `desktop-2`), so
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

def chunk_payload(stream: Buffer, ch: ChunkInfo) -> Union[bytes, bytearray]:
    """The payload bytes of a chunk (use this, never `off + 8`).

    A slice of an `mmap` is `bytes` and a slice of a `bytearray` is a `bytearray`; both decode, index
    and unpack the same way, which is all any caller here does.
    """
    return stream[ch['payload_offset']:ch['payload_offset'] + ch['length']]

@rdc_profile.timed('verify: walk')
def check_stream(stream: Buffer, names: Optional[Dict[int, str]] = None,
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
        # `len - count(0)` rather than a per-byte loop: this is one of the few places the walk reads every
        # padding byte of the stream (up to 63 per chunk, 11,923 chunks on the 1.55 GB capture), and the
        # loop was pure Python over all of them.
        stale = len(pad) - pad.count(0)
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
    'f32',
    'decompress_zstd',
    'iter_chunks',
    'parse_container',
    'string_runs',
    'u16',
    'u32',
    'u64',
]
