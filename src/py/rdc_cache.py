"""The decompressed-stream cache: identity, lookup, store, stats and pruning, so a repeat command does not pay for decompression again (REFERENCE §4.8)."""

from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
import rdc_chunkmap
import rdc_profile

import hashlib
import mmap
import os
import struct
import sys

from typing import List, Optional, Tuple

CACHE_MAGIC = b'RDCCACHE'
#: 2 since 2026-09-18: the header is padded so the stream starts on an `mmap` offset boundary (below).
CACHE_VERSION = 2
CACHE_SUFFIX = '.rdcstream'

#: The stream has to start on a multiple of this for `mmap(offset=...)` to be legal -- on Windows the
#: offset must be a multiple of the allocation granularity (64 KB), not the page size. Version 1 headers
#: put the stream wherever the source path ended, so every read of one copied 1.5 GB out of the file.
CACHE_ALIGN = mmap.ALLOCATIONGRANULARITY or 4096

#: magic(8) version(I) headerLength(I) section(I) method(I) srcSize(Q) srcMtime(Q) streamLen(Q)
#: blocks(I), followed by `headerLength - CACHE_HEADER.size` bytes of UTF-8 source path and then
#: exactly `streamLen` bytes of decompressed stream.
CACHE_HEADER = struct.Struct('<8sIIIIQQQI')

#: Method codes stored in a cache header (see `_method_label`).
METHOD_RAW, METHOD_LZ4, METHOD_ZSTD = 0, 1, 2

#: Set once, so a broken cache directory warns a single time per process.
_CACHE_WARNED = False

#: The two environment variables that move or disable the cache (REFERENCE §4.8). Named here because a
#: caller that *wants* a cache-free run -- the goldens harness, so a transcript cannot depend on what this
#: machine has already decoded -- should not spell the variable itself.
CACHE_DIR_ENV = 'RDC_CACHE_DIR'
NO_CACHE_ENV = 'RDC_NO_CACHE'

def cache_dir() -> str:
    """Directory holding the cached streams (`$RDC_CACHE_DIR` overrides the platform default)."""
    env = os.environ.get(CACHE_DIR_ENV)
    if env:
        return env
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'rdc-tools', 'cache')

def _cache_enabled() -> bool:
    """False when `$RDC_NO_CACHE` is set to anything non-empty."""
    return not os.environ.get(NO_CACHE_ENV)

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
            # the header is padded to `CACHE_ALIGN` (see `cache_store`), so the path is NUL-terminated
            src_path = fh.read(hdr_len - CACHE_HEADER.size).split(b'\x00')[0].decode('utf-8', 'replace')
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

@rdc_profile.timed('stream: cache read')
def cache_lookup(path: str, info: CaptureInfo,
                 section_index: int = 0) -> Optional[Tuple[Buffer, str]]:
    """The cached stream and its label for this capture/section, or None on a miss.

    A hit is an `mmap` of the cache file rather than a copy of it: the stream is read-only, every
    decoder accepts a map, and mapping the 1.47 GB stream where the old code read it into memory saved
    0.30 s of every command that needs a stream at all, plus 1.5 GB of memory. A file that cannot be
    mapped, or whose stream is not on an `mmap` boundary, falls back to the read.
    """
    entry = _cache_entry(path, info, section_index)
    if entry is None:
        return None
    stream: Buffer
    try:
        with open(entry['file'], 'rb') as fh:
            if entry['streamLen'] > 0 and entry['hdrLen'] % CACHE_ALIGN == 0:
                stream = mmap.mmap(fh.fileno(), entry['streamLen'], access=mmap.ACCESS_READ,
                                   offset=entry['hdrLen'])
            else:
                fh.seek(entry['hdrLen'])
                stream = fh.read(entry['streamLen'])
    except (OSError, ValueError):
        _remove_file(entry['file'])
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

def stream_source(path: str, info: CaptureInfo, section_index: int = 0) -> Optional[CacheEntry]:
    """The cache file a stream of this capture/section is stored in, or None when there is none.

    A hit means the file holds exactly the stream `load_stream` returns, which is what lets a worker
    process read the bytes by mapping the file instead of being handed a copy of them (see
    `rdc_scan.scan_runs`). Callers must still treat the stream they already have as the truth: this is
    an optimisation and nothing about the answer depends on it.
    """
    return _cache_entry(path, info, section_index)

#: Suffixes of *derived* cache files: a small file beside a stream named `<stream stem><suffix>`, holding
#: an answer computed from that stream (`.bindnames.json` is the whole-stream DXBC search, REFERENCE 4.13;
#: `.psos.json` is the PSO/shader index, REFERENCE 4.22). Listed here, with the streams they belong to, so
#: `cache clear` reclaims them too -- and so `cache list` can say how much of the directory is an answer
#: rather than a stream.
DERIVED_SUFFIXES = ('.bindnames.json', '.psos.json')

def sidecar_path(source: CacheEntry, suffix: str) -> str:
    """Where a derived answer about `source`'s stream lives: the stream's own name, another suffix.

    The identity is in the file name -- the stream cache's name is a hash of the capture's path, size,
    mtime, section and cache version -- so a re-capture cannot read an answer computed from the file it
    replaced: it has no sidecar under the new name and the next command writes one. A creator should
    still stamp the stream it was computed from into the file (see `rdc_resources.shader_bind_names`),
    because a name can only be as unique as what goes into it.
    """
    return os.path.splitext(source['file'])[0] + suffix

#: How much of a stream a derived answer's identity covers, at each end. Hashing 1.5 GB per command would
#: cost more than the scan it saves; 64 KB at each end, on top of the stream cache's own name (which
#: already hashes the capture's path, size, mtime, section and cache version, REFERENCE 4.8), is what makes
#: a stale answer unreadable rather than wrong.
DERIVED_SAMPLE = 64 << 10

def stream_digest(stream: Buffer) -> str:
    """`sha256` of the stream's first and last `DERIVED_SAMPLE` bytes (the whole stream if it is shorter).

    What a derived sidecar stamps into itself (`rdc_resources.shader_bind_names`,
    `rdc_psos.shader_index`): the sidecar's *name* already ties it to the capture the stream was
    decompressed from, and this ties it to the bytes, so a stream that changed without the capture's mtime
    changing is still refused.
    """
    import hashlib
    if len(stream) <= 2 * DERIVED_SAMPLE:
        return hashlib.sha256(bytes(stream)).hexdigest()
    digest = hashlib.sha256(bytes(stream[:DERIVED_SAMPLE]))
    digest.update(bytes(stream[len(stream) - DERIVED_SAMPLE:]))
    return digest.hexdigest()

@rdc_profile.timed('stream: cache write')
def cache_store(path: str, info: CaptureInfo, section_index: int, stream: Buffer, method: int,
                blocks: int) -> Optional[str]:
    """Write a decompressed stream to the cache; returns the file written, or None.

    Raw sections are not cached (copying them would cost disk for nothing). The bytes go to a
    temporary name first and are renamed into place afterwards, so an interrupted run can never
    leave a half-written stream behind for the next one to read. The header is padded to `CACHE_ALIGN`
    so the stream starts on a boundary `mmap` will accept -- that is what makes a cache hit a mapping
    instead of a 1.5 GB read.
    """
    if method == METHOD_RAW or not _cache_enabled():
        return None
    ident = _cache_identity(path)
    if ident is None:
        return None
    size, mtime, abspath = ident
    cfile = _cache_file(abspath, size, mtime, section_index)
    src = abspath.encode('utf-8', 'replace')
    head_len = rdc_chunkmap.align_up(CACHE_HEADER.size + len(src), CACHE_ALIGN)
    head = CACHE_HEADER.pack(CACHE_MAGIC, CACHE_VERSION, head_len, section_index, method, size, mtime,
                             len(stream), blocks)
    tmp = '%s.tmp%d' % (cfile, os.getpid())
    try:
        os.makedirs(os.path.dirname(cfile), exist_ok=True)
        with open(tmp, 'wb') as fh:
            fh.write(head)
            fh.write(src)
            fh.write(b'\x00' * (head_len - CACHE_HEADER.size - len(src)))
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
    """Delete every cache file, derived answers included; returns `(files removed, bytes freed)`."""
    directory = cache_dir()
    count = freed = 0
    for name in _cache_names() + derived_names():
        cfile = os.path.join(directory, name)
        try:
            freed += os.path.getsize(cfile)
            os.remove(cfile)
            count += 1
        except OSError:
            pass
    return count, freed

def derived_names() -> List[str]:
    """Every derived (sidecar) file in the cache directory, complete or not.

    Separate from `_cache_names` because these are not streams: `cache list` counts them on their own
    line, and nothing that reads a cache header ever sees one. A half-written sidecar (`<name>.tmp`,
    as `cache_store` writes streams) is included, so a killed command leaves nothing to sweep later.
    """
    directory = cache_dir()
    if not os.path.isdir(directory):
        return []
    partial = tuple(suffix + '.tmp' for suffix in DERIVED_SUFFIXES)
    return sorted(name for name in os.listdir(directory)
                  if name.endswith(DERIVED_SUFFIXES) or name.endswith(partial))

def _method_label(method: int, blocks: int, cached: bool = False) -> str:
    """The label `get_stream` returns for a decompression method, plus `, cached` on a hit."""
    if method == METHOD_LZ4:
        return 'lz4(%d blocks%s)' % (blocks, ', cached' if cached else '')
    name = 'zstd' if method == METHOD_ZSTD else 'raw'
    return name + (', cached' if cached else '')

@rdc_profile.timed('stream: decompress')
def _decompress_section(info: CaptureInfo, section_index: int = 0) -> Tuple[Buffer, int, int]:
    """Decompress one section body; returns `(stream, method code, block count)`."""
    sec = info['sections'][section_index]
    blob = info['_data'][sec['dataOffset']:sec['dataOffset'] + sec['compLen']]
    if blob[:4] == ZSTD_MAGIC or blob[4:8] == ZSTD_MAGIC:
        return decompress_zstd(blob), METHOD_ZSTD, 0
    if sec['flags'] & 0x2:
        out, blocks = decompress_lz4(blob, sec['uncompLen'])
        return out, METHOD_LZ4, blocks
    return blob, METHOD_RAW, 0

def get_stream(info: CaptureInfo, section_index: int = 0) -> Tuple[Buffer, str]:
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

def load_stream(path: str, section_index: int = 0) -> Tuple[CaptureInfo, Buffer, str]:
    """Parse the container, decompress section 0 and return (info, stream, method).

    The stream is served from the disk cache when there is one (see `cache_dir`), so a repeat
    command skips decompression and its label reads `lz4(N blocks, cached)`; `$RDC_NO_CACHE=1`
    turns the cache off and `$RDC_CACHE_DIR` moves it. A hit is an `mmap` of the cache file, not a
    copy of it (see `cache_lookup`): anything that needs plain bytes -- to compare with `==`, or to
    keep after the call -- wraps it in `bytes(stream)`.

    A *miss* that could store a cache is served from the map too: the copy that produced the cache is
    released here rather than held for the rest of the command. That matters because the largest capture
    here is a 1.5 GB stream and every caller allocates on top of it (the chunk walk, `find_all`'s runs,
    the report), so a cold run would otherwise hold the whole stream on the heap while it works. The
    decompression itself still peaks at one copy -- producing the bytes *is* allocating them -- and the
    label stays the decompression's either way: it says how the bytes were made, not where they came from.
    """
    info = parse_container(path)
    hit = cache_lookup(path, info, section_index)
    if hit is not None:
        return info, hit[0], hit[1]
    stream, method, blocks = _decompress_section(info, section_index)
    cache_store(path, info, section_index, stream, method, blocks)
    warmed = cache_lookup(path, info, section_index)
    if warmed is not None:
        return info, warmed[0], _method_label(method, blocks)
    return info, stream, _method_label(method, blocks)    # no cache to map (see `cache_dir`): the heap copy

__all__ = [
    'CACHE_ALIGN',
    'CACHE_DIR_ENV',
    'CACHE_HEADER',
    'CACHE_MAGIC',
    'CACHE_SUFFIX',
    'CACHE_VERSION',
    'DERIVED_SAMPLE',
    'DERIVED_SUFFIXES',
    'METHOD_LZ4',
    'METHOD_RAW',
    'METHOD_ZSTD',
    'NO_CACHE_ENV',
    '_CACHE_WARNED',
    '_cache_enabled',
    '_cache_entry',
    '_cache_file',
    '_cache_identity',
    '_cache_names',
    '_cache_prune_stale',
    '_cache_warn',
    '_decompress_section',
    '_expected_len',
    '_method_label',
    '_read_cache_header',
    '_remove_file',
    'cache_clear',
    'cache_dir',
    'cache_entries',
    'cache_lookup',
    'cache_stats',
    'cache_store',
    'derived_names',
    'get_stream',
    'load_stream',
    'sidecar_path',
    'stream_digest',
    'stream_source',
    'stream_stats',
]
