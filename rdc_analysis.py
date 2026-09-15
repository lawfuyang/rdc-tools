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
  python rdc_analysis.py blocks   <rdc>          # per-section compression accounting
  python rdc_analysis.py resources <rdc> [limit] [nameFilter]   # id -> kind/size/name
  python rdc_analysis.py descriptors <rdc> [limit] [heapFilter] # descriptor heap contents
  python rdc_analysis.py verify   <rdc>          # framing/padding/payload checks, exit 1 on problems
  python rdc_analysis.py summary  <rdc>
  python rdc_analysis.py markers  <rdc>
  python rdc_analysis.py chunks   <rdc> [limit] [nameFilter]
  python rdc_analysis.py chunk    <rdc> <chunkIndex>
  python rdc_analysis.py draws    <rdc> [maxDraws]
  python rdc_analysis.py rootconst <rdc> [maxChunks]
  python rdc_analysis.py strings  <rdc> [minlen] [maxlines]
  python rdc_analysis.py names    <rdc> [minlen]
  python rdc_analysis.py grep     <rdc> <pattern> [context]
  python rdc_analysis.py dump     <rdc> <start> <length> [minlen]
  python rdc_analysis.py count    <rdc> <pattern> [pattern ...]
  python rdc_analysis.py hex      <rdc> <start> <length>
  python rdc_analysis.py float    <rdc> <value>
  python rdc_analysis.py pattern  <rdc> <f0,f1,...> [count]
  python rdc_analysis.py report   <rdc>
  python rdc_analysis.py dxbc     <rdc> [verbose]
  python rdc_analysis.py sig      <rdc>
  python rdc_analysis.py dump-chunk <rdc> <chunkIndex> <outfile>
  python rdc_analysis.py dump-shaders <rdc> <outdir>
  python rdc_analysis.py cache    [list|dir|clear]         # decompressed-stream cache
  python rdc_analysis.py selftest [-v] [-k <substring>]   # run the unit-test suite
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import sys
import time
import unittest
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, TypedDict, Union

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

    `off` is the chunk start and `payload_offset` is where the payload begins, *after* the per-chunk
    metadata (36 bytes in for the usual flags, see README 3.3) -- never assume `off + 8`. The
    payload is `length` bytes at `payload_offset`; `pad_start`/`pad_len` describe the 64-byte
    alignment padding after it (stale buffer bytes, not data -- see `check_stream`).
    """
    off: int
    id: int
    flags: int
    length: int
    payload_offset: int
    pad_start: int
    pad_len: int


class CacheEntry(TypedDict):
    """One cached decompressed stream: where it is and what it was built from.

    `hdrLen` is where the payload starts, `streamLen` how long it is; `srcPath`/`srcSize`/`srcMtime`
    identify the capture it belongs to and are re-checked on every read.
    """
    file: str
    srcPath: str
    hdrLen: int
    section: int
    method: int
    srcSize: int
    srcMtime: int
    streamLen: int
    blocks: int


class ResourceInfo(TypedDict):
    """One D3D12 resource: what it is, how big, and what the capture calls it.

    `kind` is `buffer` / `texture1d` / `texture2d` / `texture3d` / `blas` / `tlas` / `unknown` (the
    last one for ids that are only *named*: heaps, queues, fences, PSOs). `size` is the byte size of
    a buffer or an acceleration structure, and 0 for textures, which are described by
    `width`/`height`/`depth` (depth doubles as array size), `mips` and `format` instead. `name` is
    empty when the capture never named the resource, and `gpuAddress` is the base VA of a buffer.
    """
    kind: str
    name: str
    size: int
    width: int
    height: int
    depth: int
    mips: int
    format: int
    gpuAddress: int


class DescriptorInfo(TypedDict):
    """One written descriptor-heap slot: what kind of view it holds and which resource it names.

    `kind` is `cbv` / `srv` / `uav` / `rtv` / `dsv` / `sampler`. `resource` is 0 for a sampler (a
    sampler points at no resource) and for a slot the capture never wrote -- heaps are created with
    up to a million slots, and only written ones are recorded.
    """
    kind: str
    resource: int


class DrawState(TypedDict):
    """The D3D12 command-list state `draws` reports at each draw.

    Bindings belong to the *command list*, not to the PSO and not to one draw: they survive
    `SetPipelineState` and every draw, and only change when something rebinds them. Two events
    invalidate the root bindings: `Reset()` (a fresh list) and a root signature that actually
    differs from the current one -- "if a root signature is changed on a command list, all previous
    root arguments become stale", which is what RenderDoc's own replay implements
    (`d3d12_command_list_wrap.cpp`). Graphics and compute root parameters are separate namespaces.
    """
    pso: Optional[int]
    gfxSig: Optional[int]
    compSig: Optional[int]
    gfxCbv: Dict[int, Tuple[int, int]]
    compCbv: Dict[int, Tuple[int, int]]
    gfxTable: Dict[int, Tuple[int, int]]
    compTable: Dict[int, Tuple[int, int]]
    vbs: Dict[int, Tuple[int, int, int, int]]
    ib: Optional[Tuple[int, int]]


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
CACHE_MAGIC = b'RDCCACHE'
CACHE_VERSION = 1
CACHE_SUFFIX = '.rdcstream'

#: magic(8) version(I) headerLength(I) section(I) method(I) srcSize(Q) srcMtime(Q) streamLen(Q)
#: blocks(I), followed by `headerLength - CACHE_HEADER.size` bytes of UTF-8 source path and then
#: exactly `streamLen` bytes of decompressed stream.
CACHE_HEADER = struct.Struct('<8sIIIIQQQI')

#: Method codes stored in a cache header (see `_method_label`).
METHOD_RAW, METHOD_LZ4, METHOD_ZSTD = 0, 1, 2

#: Set once, so a broken cache directory warns a single time per process.
_CACHE_WARNED = False


def cache_dir() -> str:
    """Directory holding the cached streams (`$RDC_CACHE_DIR` overrides the platform default)."""
    env = os.environ.get('RDC_CACHE_DIR')
    if env:
        return env
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'rdc-tools', 'cache')


def _cache_enabled() -> bool:
    """False when `$RDC_NO_CACHE` is set to anything non-empty."""
    return not os.environ.get('RDC_NO_CACHE')


def _cache_file(abspath: str, size: int, mtime: int, section_index: int) -> str:
    """Cache file for one (capture, section): the identity key hashed into a file name."""
    key = '%s|%d|%d|%d|%d' % (abspath, size, mtime, section_index, CACHE_VERSION)
    return os.path.join(cache_dir(),
                        hashlib.sha1(key.encode('utf-8', 'replace')).hexdigest() + CACHE_SUFFIX)


def _cache_identity(path: str) -> Optional[Tuple[int, int, str]]:
    """`(size, mtime_ns, absolute path)` of the capture, or None when it cannot be stat'ed.

    The path is case-normalised (`os.path.normcase`: lowercased on Windows, a no-op elsewhere), so
    `C:\\x.rdc` and `c:\\x.rdc` are one cache entry. Without that, asking for a capture through two
    spellings of its path caches the whole stream twice -- on Windows a shell prompt and a
    `Resolve-Path` disagree about the drive letter, and the entry that is not looked up again is
    several hundred MB of dead weight.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns, os.path.normcase(os.path.abspath(path))


def _read_cache_header(cfile: str) -> Optional[CacheEntry]:
    """Parse a cache file header; None when the file is missing, short or not a cache file."""
    try:
        with open(cfile, 'rb') as fh:
            raw = fh.read(CACHE_HEADER.size)
            if len(raw) < CACHE_HEADER.size:
                return None
            magic, version, hdr_len, section, method, src_size, src_mtime, stream_len, blocks = \
                CACHE_HEADER.unpack(raw)
            if magic != CACHE_MAGIC or version != CACHE_VERSION or hdr_len < CACHE_HEADER.size:
                return None
            src_path = fh.read(hdr_len - CACHE_HEADER.size).decode('utf-8', 'replace')
    except OSError:
        return None
    return CacheEntry(file=cfile, srcPath=src_path, hdrLen=hdr_len, section=section, method=method,
                      srcSize=src_size, srcMtime=src_mtime, streamLen=stream_len, blocks=blocks)


def _expected_len(info: CaptureInfo, section_index: int) -> int:
    """`uncompLen` of one section, or 0 for an out-of-range index (the caller raises, as before)."""
    sections = info['sections']
    if 0 <= section_index < len(sections):
        return sections[section_index]['uncompLen']
    return 0


def _cache_entry(path: str, info: CaptureInfo, section_index: int) -> Optional[CacheEntry]:
    """The usable cache entry for this capture/section, or None.

    An entry is usable only when it was built from exactly this file (same absolute path, size and
    mtime), for this section, and holds a stream at least as long as the section claims to
    decompress to. A file that fails any check is deleted, so nothing stale survives a rebuild.
    """
    if not _cache_enabled():
        return None
    ident = _cache_identity(path)
    if ident is None:
        return None
    size, mtime, abspath = ident
    entry = _read_cache_header(_cache_file(abspath, size, mtime, section_index))
    if entry is None:
        return None
    expect = _expected_len(info, section_index)
    if (entry['srcPath'] != abspath or entry['srcSize'] != size or entry['srcMtime'] != mtime
            or entry['section'] != section_index or (expect and entry['streamLen'] < expect)):
        _remove_file(entry['file'])
        return None
    return entry


def cache_lookup(path: str, info: CaptureInfo,
                 section_index: int = 0) -> Optional[Tuple[bytes, str]]:
    """The cached stream and its label for this capture/section, or None on a miss."""
    entry = _cache_entry(path, info, section_index)
    if entry is None:
        return None
    try:
        with open(entry['file'], 'rb') as fh:
            fh.seek(entry['hdrLen'])
            stream = fh.read(entry['streamLen'])
    except OSError:
        return None
    if len(stream) != entry['streamLen']:
        _remove_file(entry['file'])
        return None
    return stream, _method_label(entry['method'], entry['blocks'], cached=True)


def cache_stats(path: str, info: CaptureInfo,
                section_index: int = 0) -> Optional[Tuple[int, str]]:
    """`(stream length, label)` from a cache header alone -- no payload read."""
    entry = _cache_entry(path, info, section_index)
    if entry is None:
        return None
    return entry['streamLen'], _method_label(entry['method'], entry['blocks'], cached=True)


def cache_store(path: str, info: CaptureInfo, section_index: int, stream: bytes, method: int,
                blocks: int) -> Optional[str]:
    """Write a decompressed stream to the cache; returns the file written, or None.

    Raw sections are not cached (copying them would cost disk for nothing). The bytes go to a
    temporary name first and are renamed into place afterwards, so an interrupted run can never
    leave a half-written stream behind for the next one to read.
    """
    if method == METHOD_RAW or not _cache_enabled():
        return None
    ident = _cache_identity(path)
    if ident is None:
        return None
    size, mtime, abspath = ident
    cfile = _cache_file(abspath, size, mtime, section_index)
    src = abspath.encode('utf-8', 'replace')
    head = CACHE_HEADER.pack(CACHE_MAGIC, CACHE_VERSION, CACHE_HEADER.size + len(src), section_index,
                             method, size, mtime, len(stream), blocks)
    tmp = '%s.tmp%d' % (cfile, os.getpid())
    try:
        os.makedirs(os.path.dirname(cfile), exist_ok=True)
        with open(tmp, 'wb') as fh:
            fh.write(head)
            fh.write(src)
            fh.write(stream)
        os.replace(tmp, cfile)
    except OSError as exc:
        _cache_warn(exc)
        _remove_file(tmp)
        return None
    _cache_prune_stale(abspath, section_index, cfile)
    return cfile


def _remove_file(cfile: str) -> None:
    """Delete a file, ignoring failure -- everything here is disposable."""
    try:
        os.remove(cfile)
    except OSError:
        pass


def _cache_warn(exc: OSError) -> None:
    """Report a cache problem once per process, on stderr: it must never fail a command."""
    global _CACHE_WARNED
    if not _CACHE_WARNED:
        _CACHE_WARNED = True
        print('warning: cannot write the stream cache: %s' % exc, file=sys.stderr)


def _cache_names() -> List[str]:
    """Every file in the cache directory that belongs to the cache (complete or not)."""
    directory = cache_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(name for name in os.listdir(directory)
                  if name.endswith(CACHE_SUFFIX) or CACHE_SUFFIX + '.tmp' in name)


def cache_entries() -> List[CacheEntry]:
    """Every readable cache file, biggest stream first; unusable files are not listed."""
    directory = cache_dir()
    entries: List[CacheEntry] = []
    for name in _cache_names():
        if name.endswith(CACHE_SUFFIX):
            entry = _read_cache_header(os.path.join(directory, name))
            if entry is not None:
                entries.append(entry)
    entries.sort(key=lambda e: e['streamLen'], reverse=True)
    return entries


def _cache_prune_stale(abspath: str, section_index: int, keep: str) -> int:
    """Delete cached streams for the same capture and section built from a different version of it.

    Only the newest entry for a capture/section is worth keeping, and a changed capture leaves the
    old one behind (its file name encodes the old size/mtime, so the new identity never looks it
    up). `cache_store` prunes after a successful write, so an edited capture does not accumulate a
    several-hundred-MB orphan per edit; `cache clear` remains the sledgehammer.
    """
    removed = 0
    for entry in cache_entries():
        if (entry['file'] != keep and entry['srcPath'] == abspath
                and entry['section'] == section_index):
            _remove_file(entry['file'])
            removed += 1
    return removed


def cache_clear() -> Tuple[int, int]:
    """Delete every cache file; returns `(files removed, bytes freed)`."""
    directory = cache_dir()
    count = freed = 0
    for name in _cache_names():
        cfile = os.path.join(directory, name)
        try:
            freed += os.path.getsize(cfile)
            os.remove(cfile)
            count += 1
        except OSError:
            pass
    return count, freed


def _method_label(method: int, blocks: int, cached: bool = False) -> str:
    """The label `get_stream` returns for a decompression method, plus `, cached` on a hit."""
    if method == METHOD_LZ4:
        return 'lz4(%d blocks%s)' % (blocks, ', cached' if cached else '')
    name = 'zstd' if method == METHOD_ZSTD else 'raw'
    return name + (', cached' if cached else '')


def _decompress_section(info: CaptureInfo, section_index: int = 0) -> Tuple[bytes, int, int]:
    """Decompress one section body; returns `(stream, method code, block count)`."""
    sec = info['sections'][section_index]
    blob = info['_data'][sec['dataOffset']:sec['dataOffset'] + sec['compLen']]
    if blob[:4] == ZSTD_MAGIC or blob[4:8] == ZSTD_MAGIC:
        return decompress_zstd(blob), METHOD_ZSTD, 0
    if sec['flags'] & 0x2:
        out, blocks = decompress_lz4(blob, sec['uncompLen'])
        return out, METHOD_LZ4, blocks
    return blob, METHOD_RAW, 0


def get_stream(info: CaptureInfo, section_index: int = 0) -> Tuple[bytes, str]:
    """Return the (decompressed) body of one section and a human-readable method label.

    This always decompresses: the disk cache is applied by `load_stream` / `stream_stats`.
    """
    stream, method, blocks = _decompress_section(info, section_index)
    return stream, _method_label(method, blocks)


def stream_stats(path: str, info: CaptureInfo, section_index: int = 0) -> Tuple[int, str]:
    """Size and method label of one section, decompressing only on a cache miss.

    `sections` uses this instead of the stream itself: on a hit the cache header answers both, so a
    repeat run neither reads nor decompresses the stream.
    """
    hit = cache_stats(path, info, section_index)
    if hit is not None:
        return hit
    stream, method, blocks = _decompress_section(info, section_index)
    cache_store(path, info, section_index, stream, method, blocks)
    return len(stream), _method_label(method, blocks)


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
        print(cache_dir())
        return 0
    if what == 'clear':
        count, freed = cache_clear()
        print('removed %d cache files (%.1f MB) from %s'
              % (count, freed / 1048576.0, cache_dir()))
        return 0
    if what != 'list':
        print('usage: rdc_analysis.py cache [list|dir|clear]')
        return 2
    entries = cache_entries()
    print('cache dir : %s' % cache_dir())
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


def _resource_size(info: ResourceInfo, formats: Dict[int, str]) -> str:
    """The size column of `resources`: bytes for a buffer or an AS, dimensions + format for a texture."""
    if info['kind'] in ('buffer', 'blas', 'tlas'):
        return '%d B' % info['size']
    if info['kind'] == 'unknown':
        return '-'
    return '%dx%dx%d mips=%d fmt=%s' % (info['width'], info['height'], info['depth'], info['mips'],
                                        formats.get(info['format'], str(info['format'])))


def _name_suffix(table: Dict[int, ResourceInfo], rid: int, width: int = 24) -> str:
    """`[Name]` for a resource the capture named, else '' (used by `draws`, truncated to `width`)."""
    entry = table.get(rid)
    if entry is None or not entry['name']:
        return ''
    return '[%s]' % entry['name'][:width]


def cmd_descriptors(path: str, limit: int = 200, heap_filter: Optional[str] = None) -> None:
    """List the written slots of every descriptor heap: heap, slot, kind and resource.

    This is what makes a `draws` line like `rp0=heap298[138458]` readable: the binding names a slot
    in a heap, and this says what the capture wrote into it. Only written slots are listed (a heap
    can have a million), and the filter matches either the heap id or its name.
    """
    _info, stream, _how = load_stream(path)
    names = load_chunk_names()
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
                'res%d%s' % (info['resource'], _name_suffix(resources, info['resource'])))
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
    table = parse_resource_table(stream, load_chunk_names())
    formats = load_format_names()
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
                  % (rid, info['kind'], _resource_size(info, formats), info['name'] or '-'))
            shown += 1
    print('total resources: %d (shown %d)' % (len(table), shown))


def load_stream(path: str, section_index: int = 0) -> Tuple[CaptureInfo, bytes, str]:
    """Parse the container, decompress section 0 and return (info, stream, method).

    The stream is served from the disk cache when there is one (see `cache_dir`), so a repeat
    command skips decompression and its label reads `lz4(N blocks, cached)`; `$RDC_NO_CACHE=1`
    turns the cache off and `$RDC_CACHE_DIR` moves it.
    """
    info = parse_container(path)
    hit = cache_lookup(path, info, section_index)
    if hit is not None:
        return info, hit[0], hit[1]
    stream, method, blocks = _decompress_section(info, section_index)
    cache_store(path, info, section_index, stream, method, blocks)
    return info, stream, _method_label(method, blocks)


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

#: The draw chunks that are always compute. The rest of `DRAW_CHUNKS` are graphics, and an
#: `ExecuteIndirect` can be either, so `draws` reports both namespaces for it.
COMPUTE_CHUNKS = ('List_Dispatch',)

#: Command-list chunks that change the state `cmd_draws` reports (see `_apply_state_chunk`).
STATE_SETTERS = ('List_SetPipelineState', 'List_SetGraphicsRootSignature',
                 'List_SetGraphicsRootConstantBufferView', 'List_SetGraphicsRootDescriptorTable',
                 'List_SetComputeRootSignature', 'List_SetComputeRootConstantBufferView',
                 'List_SetComputeRootDescriptorTable', 'List_IASetVertexBuffers',
                 'List_IASetIndexBuffer')

#: Payload lengths the decoders expect for the chunks with a fixed layout, used as a checksum by
#: `verify` (README 3.4: "chunk length is a checksum for your decoder"). Chunks carrying arrays or
#: variable-length data are deliberately absent.
EXPECTED_LENGTHS: Dict[str, Tuple[int, ...]] = {
    'List_SetPipelineState': (16,),
    'List_Reset': (64,),                   # +40 = cmdList id, +48 = initial PSO
    'List_DrawIndexedInstanced': (28,),
    'List_DrawInstanced': (24,),
    'List_Dispatch': (20,),
    'List_SetGraphicsRootSignature': (16,),
    'List_SetGraphicsRootDescriptorTable': (24,),
    'List_SetGraphicsRootConstantBufferView': (28,),
    'List_SetGraphicsRootShaderResourceView': (28,),
    'List_SetGraphicsRootUnorderedAccessView': (28,),
    'List_SetComputeRootSignature': (16,),
    'List_SetComputeRootDescriptorTable': (24,),
    'List_SetComputeRootConstantBufferView': (28,),
    'List_IASetIndexBuffer': (9, 33),      # 9 = null view, 33 = present flag + view
}

#: `align_up`'s default (kept as a module constant so callers can name it).
ALIGN_UP_DEFAULT = CHUNK_ALIGN

#: Resource-creation chunks and where their `D3D12_RESOURCE_DESC` starts in the payload: committed
#: = heap props (20 bytes) + heap flags (4), placed = heap id (8) + heap offset (8), reserved =
#: straight in (d3d12_device_rescreate_wrap.cpp). Every one of them ends with
#: `IID(16) | resourceId(8) | gpuAddress(8)`, so the id is at `length - 16` and a buffer's base VA
#: at `length - 8`.
#:
#: The `1`/`2`/`3` variants differ only in what *follows* the descriptor (`D3D12_RESOURCE_DESC1`,
#: a protected session, a castable-format list), and that struct starts with the same 48 bytes as
#: `D3D12_RESOURCE_DESC`, so one offset covers them all -- the descriptor fields are read from the
#: first 48 bytes either way. Verified against a capture for `...3` (149-byte payloads, every
#: dimension field valid) and `...2`; the rest follow the same serialiser shape in the source.
RESOURCE_CHUNKS: Dict[str, int] = {
    'Device_CreateCommittedResource': 24,
    'Device_CreateCommittedResource1': 24,
    'Device_CreateCommittedResource2': 24,
    'Device_CreateCommittedResource3': 24,
    'Device_CreatePlacedResource': 16,
    'Device_CreatePlacedResource1': 16,
    'Device_CreatePlacedResource2': 16,
    'Device_CreateReservedResource': 0,
    'Device_CreateReservedResource1': 0,
    'Device_CreateReservedResource2': 0,
}

#: `CreateAS`: an acceleration structure is a *sub-range of a buffer*, not a resource. The payload
#: is `u64 buffer, u64 offset, u32 type, u64 byteSize, u64 asId` (d3d12_device.cpp
#: `Serialise_CreateAS`), and `asId` is the id the frame references. The type is a
#: `D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE`, where **0 is TOP_LEVEL** and 1 BOTTOM_LEVEL --
#: the capture that exercises this has 5205 small type-0 structures rebuilt every frame (RTXDI's
#: per-light TLASes) and 3 large type-1 ones (the static BLASes), which is what those two are.
AS_KINDS: Dict[int, str] = {0: 'tlas', 1: 'blas'}

#: `D3D10_RESOURCE_DIMENSION` (common/dds_readwrite.cpp): what a descriptor's Dimension field means.
RESOURCE_KINDS: Dict[int, str] = {1: 'buffer', 2: 'texture1d', 3: 'texture2d', 4: 'texture3d'}

#: Descriptor-write chunks and the kind of descriptor each one writes (d3d12_device_wrap.cpp).
#: A `Device_Create*View` payload holds the descriptor first and *ends* with the destination
#: `PortableHandle`; the resource id is at +16 in every view payload (`D3D12Descriptor`'s
#: serialiser writes type, heap, index, then the view, whose first field is the resource --
#: d3d12_serialise.cpp). `Device_CreateSampler` writes a sampler: no resource.
DESCRIPTOR_KINDS: Dict[str, str] = {
    'Device_CreateConstantBufferView': 'cbv',
    'Device_CreateShaderResourceView': 'srv',
    'Device_CreateUnorderedAccessView': 'uav',
    'Device_CreateRenderTargetView': 'rtv',
    'Device_CreateDepthStencilView': 'dsv',
    'Device_CreateSampler': 'sampler',
    'Device_CreateSampler2': 'sampler',
}

#: Descriptor-copy chunks: `u64 count` then `count x (u32 heapType, dst PortableHandle, src)`.
DESCRIPTOR_COPY_CHUNKS = ('Device_CopyDescriptors', 'Device_CopyDescriptorsSimple')

#: Bytes of one `DynamicDescriptorCopy`: the heap type, then two PortableHandles.
_DESCRIPTOR_COPY_SIZE = 28

#: Least a descriptor write can be: the resource field (ends at +24) plus the destination handle.
_DESCRIPTOR_WRITE_MIN = 36

#: Bytes of `D3D12_RESOURCE_DESC` to read, and the least that can follow it in a creation payload
#: (the initial state, the optional-clear flag, the IID and the two trailing u64s).
_RESOURCE_DESC_SIZE = 48
_RESOURCE_TAIL = 32


def align_up(x: int, a: int = ALIGN_UP_DEFAULT) -> int:
    """Round `x` up to the next multiple of `a` (chunks are 64-byte aligned)."""
    return (x + a - 1) & ~(a - 1)


def parse_chunk_enum(text: str, enum_name: str) -> Dict[int, str]:
    """Parse a C++ enum into an {id: name} map.

    Handles both `enum class <name> : uint32_t { ... }` (the chunk enums) and a plain
    `enum <name> { ... }` (RenderDoc's copy of `DXGI_FORMAT`). Values come from explicit
    initialisers, from `FirstDriverChunk` (= 1000) and otherwise auto-increment like C++ would.
    """
    m = re.search(r'enum(?:\s+class)?\s+%s\s*(?::\s*uint32_t\s*)?\{(.*?)\n\};' % enum_name,
                  text, re.S)
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
    return stream[ch['payload_offset']:ch['payload_offset'] + ch['length']]


def check_stream(stream: bytes, names: Optional[Dict[int, str]] = None,
                 note_samples: int = 5) -> Tuple[List[str], List[str]]:
    """Walk `stream` and return `(problems, notes)`; used by `verify`.

    Unlike the lenient walk this does not stop at the first bad frame, so a corrupt capture gets a
    full report.

    `problems` mean the parse cannot be trusted and should be fixed:

    * a frame that cannot be true (truncated metadata, payload past the end of the stream);
    * a payload whose length disagrees with the layout the decoder expects (`EXPECTED_LENGTHS`) --
      the chunk length is the checksum for the decoder (README 3.4).

    `notes` are legal but worth knowing:

    * non-zero alignment padding. Padding is stale capture-buffer content, so this is normal; it is
      reported because a decoder that read past `length` would see those bytes as plausible data.
      `note_samples` caps how many chunks are listed, the rest are counted.
    * bytes left over after the last chunk (only when there is more than a terminator word).
    """
    if names is None:
        names = load_chunk_names()
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
                      'List_SetGraphicsRootUnorderedAccessView',
                      'List_SetComputeRootConstantBufferView') and len(blob) >= 28:
            # [u64 cmdList][u32 rootParam][u64 resourceId][u64 byteOffset] -- D3D12BufferLocation
            # serialises as Id + Offset (d3d12_serialise.cpp), so this is the same pair `draws` uses.
            out.append('cmdList=%d rootParam=%d res=%d+0x%x'
                       % (u64(blob, 0), u32(blob, 8), u64(blob, 12), u64(blob, 20)))
        elif name in ('List_SetGraphicsRootDescriptorTable',
                      'List_SetComputeRootDescriptorTable') and len(blob) >= 24:
            # [u64 cmdList][u32 rootParam][PortableHandle: u64 heapId, u32 descriptorIndex]:
            # a D3D12_GPU_DESCRIPTOR_HANDLE is serialised as (heap resource, index) rather than as
            # a raw pointer (d3d12_serialise.cpp DoSerialise + PortableHandle in d3d12_manager.h),
            # so the payload is 24 bytes and there is no pointer to print
            out.append('cmdList=%d rootParam=%d heap=%d index=%d'
                       % (u64(blob, 0), u32(blob, 8), u64(blob, 12), u32(blob, 20)))
        elif name in ('List_SetGraphicsRootSignature',
                      'List_SetComputeRootSignature') and len(blob) >= 16:
            out.append('cmdList=%d rootSig=%d' % (u64(blob, 0), u64(blob, 8)))
        elif name == 'List_Reset' and len(blob) >= 56:
            # 64-byte payload; the command-list id at +40 is the one the other List_* chunks carry
            # at +0 (see `_apply_state_chunk`), and +48 is the optional initial PSO
            out.append('cmdList=%d initialPso=%d' % (u64(blob, 40), u64(blob, 48)))
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


def load_format_names(src_root: Optional[str] = None) -> Dict[int, str]:
    """DXGI format id -> short name (`R8G8B8A8_UNORM`), from RenderDoc's own copy of the enum.

    `DXGI_FORMAT` belongs to the Windows SDK, but `common/dds_readwrite.cpp` carries a copy with
    explicit values. Without the source tree this returns `{}` and formats print as numbers, exactly
    like chunk names do (README 1.1). `src_root` defaults to `RENDERDOC_SRC` at call time, so tests
    (and `$RENDERDOC_SRC`) can point it somewhere else.
    """
    path = os.path.join(RENDERDOC_SRC if src_root is None else src_root,
                        'renderdoc', 'common', 'dds_readwrite.cpp')
    if not os.path.isfile(path):
        return {}
    with open(path, encoding='utf-8', errors='replace') as fh:
        raw = parse_chunk_enum(fh.read(), 'DXGI_FORMAT')
    return {fmt_id: name.replace('DXGI_FORMAT_', '', 1) for fmt_id, name in raw.items()}


def _parse_resource(blob: bytes, desc_off: int) -> Optional[ResourceInfo]:
    """Decode the `D3D12_RESOURCE_DESC` of one creation payload, or None if it does not fit.

    The dimension field is the check that this really is a descriptor (0/unknown means it is not),
    which keeps the decoder honest without pinning a payload length that varies with the optional
    clear value.
    """
    if len(blob) < desc_off + _RESOURCE_DESC_SIZE + _RESOURCE_TAIL:
        return None
    dim = u32(blob, desc_off)
    if dim not in RESOURCE_KINDS:
        return None
    width = u64(blob, desc_off + 12)
    return ResourceInfo(kind=RESOURCE_KINDS[dim], name='', size=width if dim == 1 else 0,
                        width=width, height=u32(blob, desc_off + 20),
                        depth=u16(blob, desc_off + 24), mips=u16(blob, desc_off + 26),
                        format=u32(blob, desc_off + 28), gpuAddress=u64(blob, len(blob) - 8))


def _parse_acceleration_structure(blob: bytes) -> Optional[Tuple[int, ResourceInfo]]:
    """`(id, info)` of one `CreateAS` payload, or None when it does not fit the layout.

    The buffer and offset it lives at are decoded by `AS_KINDS`' caller but not recorded: the id is
    what the rest of the tool prints, and `size` is the acceleration structure's own byte size.
    """
    if len(blob) < 36:
        return None
    kind = AS_KINDS.get(u32(blob, 16))
    if kind is None:
        return None
    return u64(blob, 28), ResourceInfo(kind=kind, name='', size=u64(blob, 20), width=0, height=0,
                                       depth=0, mips=0, format=0, gpuAddress=0)


def parse_resource_table(stream: bytes,
                         names: Optional[Dict[int, str]] = None) -> Dict[int, ResourceInfo]:
    """Build the resource table (id -> description) from the creation chunks and `SetName`.

    `RESOURCE_CHUNKS` says where each creation payload's descriptor starts, and the resource id is
    always at `length - 16`; `CreateAS` adds acceleration structures, whose ids the frame references
    just like resources. `SetName` names any D3D12 object, so ids that are only named -- heaps,
    queues, fences, PSOs -- end up in the table too, with `kind == 'unknown'` and no size; that is
    what `draws` prints as `heap298[279377]`.
    """
    if names is None:
        names = load_chunk_names()
    table: Dict[int, ResourceInfo] = {}
    named: Dict[int, str] = {}
    for ch in iter_chunks(stream):
        nm = names.get(ch['id'], '')
        if nm == 'SetName':
            blob = chunk_payload(stream, ch)
            nlen = u32(blob, 8) if len(blob) >= 12 else 0
            if nlen <= len(blob) - 12:
                named[u64(blob, 0)] = blob[12:12 + nlen].decode('utf-8', 'replace')
        elif nm in RESOURCE_CHUNKS:
            blob = chunk_payload(stream, ch)
            info = _parse_resource(blob, RESOURCE_CHUNKS[nm])
            if info is not None:
                table[u64(blob, len(blob) - 16)] = info
        elif nm == 'CreateAS':
            parsed = _parse_acceleration_structure(chunk_payload(stream, ch))
            if parsed is not None:
                table[parsed[0]] = parsed[1]
    for rid, name in named.items():
        entry = table.get(rid)
        if entry is None:
            table[rid] = ResourceInfo(kind='unknown', name=name, size=0, width=0, height=0,
                                      depth=0, mips=0, format=0, gpuAddress=0)
        else:
            entry['name'] = name
    return table


def _portable_handle(blob: bytes, offset: int) -> Optional[Tuple[int, int]]:
    """`(heapId, index)` of the `PortableHandle` at `offset`, or None when it does not fit.

    A `PortableHandle` is `u64 heapId, u32 index` (`d3d12_manager.h`): 12 bytes, no padding, and the
    same pair a descriptor-table binding carries (README 3.4).
    """
    if offset < 0 or offset + 12 > len(blob):
        return None
    return u64(blob, offset), u32(blob, offset + 8)


def parse_descriptor_heaps(stream: bytes, names: Optional[Dict[int, str]] = None
                           ) -> Dict[int, Dict[int, DescriptorInfo]]:
    """Build `heapId -> {index: DescriptorInfo}` from the descriptor writes and copies.

    Writes and copies are applied in stream order, which is the order D3D12 applies them in: a slot
    written twice ends up holding the second write, and a copy reads the slot as it is *at that
    point* in the frame. Only written slots are recorded -- heaps are created with up to a million
    slots and the rest stay undefined, which is also what an unresolved `draws` binding means.
    """
    if names is None:
        names = load_chunk_names()
    heaps: Dict[int, Dict[int, DescriptorInfo]] = {}
    for ch in iter_chunks(stream):
        nm = names.get(ch['id'], '')
        kind = DESCRIPTOR_KINDS.get(nm)
        if kind is not None:
            blob = chunk_payload(stream, ch)
            if len(blob) < _DESCRIPTOR_WRITE_MIN:
                continue
            dst = _portable_handle(blob, len(blob) - 12)
            if dst is None:
                continue
            resource = 0 if kind == 'sampler' else u64(blob, 16)
            heaps.setdefault(dst[0], {})[dst[1]] = DescriptorInfo(kind=kind, resource=resource)
        elif nm in DESCRIPTOR_COPY_CHUNKS:
            blob = chunk_payload(stream, ch)
            count = u64(blob, 0) if len(blob) >= 8 else 0
            for i in range(count):
                entry = 8 + i * _DESCRIPTOR_COPY_SIZE
                dst = _portable_handle(blob, entry + 4)
                src = _portable_handle(blob, entry + 16)
                if dst is None or src is None:
                    break
                info = heaps.get(src[0], {}).get(src[1])
                if info is not None:
                    heaps.setdefault(dst[0], {})[dst[1]] = info
    return heaps


def _descriptor_label(heaps: Dict[int, Dict[int, DescriptorInfo]],
                      resources: Dict[int, ResourceInfo], heap: int, index: int) -> str:
    """` -> srv res2233[Name]` for a descriptor-table slot, or '' when it was never written."""
    info = heaps.get(heap, {}).get(index)
    if info is None:
        return ''
    if info['kind'] == 'sampler':
        return ' -> sampler'
    return ' -> %s res%d%s' % (info['kind'], info['resource'],
                               _name_suffix(resources, info['resource']))


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
    names = load_chunk_names()
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


def _draw_state() -> DrawState:
    """A fresh command-list state: nothing bound, no PSO, no root signature."""
    return DrawState(pso=None, gfxSig=None, compSig=None, gfxCbv={}, compCbv={}, gfxTable={},
                     compTable={}, vbs={}, ib=None)


def _apply_state_chunk(name: str, blob: bytes, states: Dict[int, DrawState]) -> bool:
    """Apply one state-changing `List_*` chunk; True when `name` is one of the tracked setters.

    Every command-list payload starts with the `u64` resource id of its command list, so the state
    is tracked *per command list*: two lists recorded in one capture cannot leak into each other.
    `List_Reset` is the exception -- its 64-byte payload carries that same id at +40 and the
    optional initial PSO at +48 (measured on both captures in this repo: +40 matches the id the
    other `List_*` chunks carry for all 170 setter chunks of the PC capture).
    """
    if name == 'List_Reset':
        # Reset() is what clears a command list in D3D12 -- not SetPipelineState, not the draws
        if len(blob) >= 56:
            fresh = _draw_state()
            fresh['pso'] = u64(blob, 48) or None
            states[u64(blob, 40)] = fresh
        else:
            # an unknown Reset layout: assume it is the only list, so nothing stale can survive
            states.clear()
        return True
    if name not in STATE_SETTERS or len(blob) < 8:
        return False
    st = states.setdefault(u64(blob, 0), _draw_state())
    if name == 'List_SetPipelineState' and len(blob) >= 16:
        st['pso'] = u64(blob, 8)
    elif name in ('List_SetGraphicsRootSignature',
                  'List_SetComputeRootSignature') and len(blob) >= 16:
        sig = u64(blob, 8)
        if name == 'List_SetGraphicsRootSignature':
            # a *changed* signature makes every root argument stale; setting the same one again
            # keeps them (D3D12 command-list semantics, and what RenderDoc's replay implements)
            if st['gfxSig'] != sig:
                st['gfxSig'], st['gfxCbv'], st['gfxTable'] = sig, {}, {}
        elif st['compSig'] != sig:
            st['compSig'], st['compCbv'], st['compTable'] = sig, {}, {}
    elif name in ('List_SetGraphicsRootConstantBufferView',
                  'List_SetComputeRootConstantBufferView') and len(blob) >= 28:
        # [u64 cmdList][u32 rootParam][u64 resourceId][u64 byteOffset] for both pipelines
        target = st['gfxCbv'] if name == 'List_SetGraphicsRootConstantBufferView' else st['compCbv']
        target[u32(blob, 8)] = (u64(blob, 12), u64(blob, 20))
    elif name in ('List_SetGraphicsRootDescriptorTable',
                  'List_SetComputeRootDescriptorTable') and len(blob) >= 24:
        # [u64 cmdList][u32 rootParam][PortableHandle: u64 heapId, u32 descriptorIndex]
        tables = (st['gfxTable'] if name == 'List_SetGraphicsRootDescriptorTable'
                  else st['compTable'])
        tables[u32(blob, 8)] = (u64(blob, 12), u32(blob, 20))
    elif name == 'List_IASetVertexBuffers' and len(blob) >= 24:
        # [u64 cmdList][u32 startSlot][u32 numViews][u64 arrayCount] then 24 bytes per view; the
        # slots outside [startSlot, startSlot + numViews) keep whatever they had bound
        start, count = u32(blob, 8), u32(blob, 12)
        for i in range(min(count, 16)):
            o = 24 + i * 24
            if o + 24 > len(blob):
                break
            st['vbs'][start + i] = (u64(blob, o), u64(blob, o + 8), u32(blob, o + 16),
                                    u32(blob, o + 20))
    elif name == 'List_IASetIndexBuffer' and len(blob) >= 9:
        # the view goes through SERIALISE_ELEMENT_OPT: [u8 present] then (resourceId, byteOffset).
        # A null view (9-byte payload) *clears* the binding -- leaving the previous one in place
        # would report an index buffer the draw does not have.
        if not blob[8]:
            st['ib'] = None
        elif len(blob) >= 33:
            st['ib'] = (u64(blob, 9), u64(blob, 17))
    return True


def _print_draw_state(state: Optional[DrawState], compute: bool, resources: Dict[int, ResourceInfo],
                      heaps: Dict[int, Dict[int, DescriptorInfo]]) -> None:
    """Print the bindings in effect for one draw or dispatch (the indented lines under its row).

    `compute` selects the namespace: a dispatch uses the compute root parameters, a draw the
    graphics ones. Vertex streams and the index buffer are graphics-only state. Every resource id
    that the capture named gets its name appended (`_name_suffix`); a descriptor-table binding gets
    the descriptor written into the slot it names (`_descriptor_label`), or the heap's name when the
    slot was never written.
    """
    if state is None:
        return
    cbvs = state['compCbv'] if compute else state['gfxCbv']
    tables = state['compTable'] if compute else state['gfxTable']
    if cbvs:
        print('        CBV: ' + '  '.join(
            'rp%d=res%d+0x%x%s' % (rp, res, off, _name_suffix(resources, res))
            for rp, (res, off) in sorted(cbvs.items())))
    if tables:
        print('        Table: ' + '  '.join(
            'rp%d=heap%d[%d]%s' % (rp, heap, idx, _descriptor_label(heaps, resources, heap, idx)
                                   or _name_suffix(resources, heap))
            for rp, (heap, idx) in sorted(tables.items())))
    if compute:
        return
    if state['vbs']:
        print('        VB : ' + '  '.join(
            'res%d+0x%x(sz%d,st%d)%s' % (view + (_name_suffix(resources, view[0]),))
            for _, view in sorted(state['vbs'].items())))
    if state['ib']:
        ib_res, ib_off = state['ib']
        print('        IB : res%d+0x%x%s' % (ib_res, ib_off, _name_suffix(resources, ib_res)))


def cmd_draws(path: str, max_draws: int = 80) -> None:
    """Per-draw table: marker path, PSO, constant buffers (resourceId+offset), vertex streams, args.

    The state printed for a draw is the state of its *command list* at that point -- everything that
    is still bound, not only what changed since the previous draw (see `DrawState`). Dispatches
    report the compute root bindings, draws the graphics ones plus the vertex streams and the index
    buffer. A root descriptor table is reported as `heap<id>[index]` plus what the capture wrote
    into that slot (`-> srv res2233[SkyViewLut]`, see `parse_descriptor_heaps`); the descriptors
    after the first are the root signature's business (ROADMAP 3.1). Bound resources are annotated
    with the name the capture gave them, when it has one (`[SceneUniformBuffer]`, see
    `parse_resource_table`).
    """
    _info, stream, _how = load_stream(path)
    names = load_chunk_names()
    resources = parse_resource_table(stream, names)
    heaps = parse_descriptor_heaps(stream, names)
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
                    _print_draw_state(st, compute=True, resources=resources, heaps=heaps)
                    _print_draw_state(st, compute=False, resources=resources, heaps=heaps)
                else:
                    _print_draw_state(st, compute=nm in COMPUTE_CHUNKS, resources=resources,
                                      heaps=heaps)
        elif _apply_state_chunk(nm, blob, states):
            pass                       # a tracked setter: it changes the state, it prints nothing
    print('total draws/dispatches: %d' % n_draw)


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
        blob = chunk_payload(stream, ch)
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
            blob = chunk_payload(stream, ch)
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
    if len(argv) > 1 and argv[1] == 'cache':
        sys.exit(cmd_cache(argv[2:]))
    if len(argv) < 3:
        print(__doc__)
        return
    cmd, path = argv[1], argv[2]
    if cmd == 'chunk':
        cmd_chunk_detail(path, int(argv[3]))
    elif cmd == 'draws':
        cmd_draws(path, _arg(argv, 3, 80))
    elif cmd == 'chunks':
        cmd_chunks(path, _arg(argv, 3, 200), argv[4] if len(argv) > 4 else None)
    elif cmd == 'verify':
        sys.exit(cmd_verify(path))
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
    elif cmd == 'resources':
        cmd_resources(path, _arg(argv, 3, 200), argv[4] if len(argv) > 4 else None)
    elif cmd == 'descriptors':
        cmd_descriptors(path, _arg(argv, 3, 200), argv[4] if len(argv) > 4 else None)
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
