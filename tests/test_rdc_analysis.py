"""The container, the compression and the cache.

Split out of `test_rdc_analysis.py`, which held the parsers and decoders for the whole tool and
passed 2200 lines. `rdc_testcase`. The shared fixtures and case classes live there, because every file that
exercises the tool needs them.

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_analysis.py
    python -m unittest tests.test_rdc_analysis
    python src/py/rdc_analysis.py selftest -k <Class>

Payload layouts used by the fixtures follow REFERENCE section 3.4; where a decoder reads a chunk
differently from another consumer of the same chunk, the test pins the behaviour actually
implemented and says so in a comment.
"""
from __future__ import annotations

import contextlib
import ctypes
import io
import mmap
import os
import struct
import sys
import types
import unittest
from typing import Any, List, Optional
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R            # noqa: E402
import rdc_cache as cache           # the tests patch the module that *owns* a name: a star import
import rdc_fixtures as F            # noqa: E402
import rdc_stream as stream         # noqa: E402  (the LZ4 cases stand in for its `_lz4_c_function`)

from rdc_testcase import *          # noqa: E402,F401,F403

# =========================================================================== endian readers
class TestEndianReaders(unittest.TestCase):
    def test_u16_little_endian(self):
        self.assertEqual(R.u16(b'\x01\x02', 0), 0x0201)
        self.assertEqual(R.u16(b'\xff\xff', 0), 0xFFFF)

    def test_u32_little_endian(self):
        self.assertEqual(R.u32(b'\x01\x02\x03\x04', 0), 0x04030201)
        self.assertEqual(R.u32(b'\xde\xad\xbe\xef', 0), 0xEFBEADDE)

    def test_u64_little_endian(self):
        self.assertEqual(R.u64(b'\x01' * 8, 0), 0x0101010101010101)
        self.assertEqual(R.u64(b'\x00' * 7 + b'\x80', 0), 0x8000000000000000)

    def test_offsets_are_respected(self):
        blob = b'\x00\x00' + b'\x01\x00' + b'\x00\x00'
        self.assertEqual(R.u16(blob, 2), 1)
        blob = b'\xaa\xbb' + struct.pack('<I', 0x11223344) + b'\xcc'
        self.assertEqual(R.u32(blob, 2), 0x11223344)
        blob = b'\xaa' + struct.pack('<Q', 0x1122334455667788)
        self.assertEqual(R.u64(blob, 1), 0x1122334455667788)

    def test_works_on_bytearray_and_memoryview(self):
        self.assertEqual(R.u32(bytearray(b'\x01\x00\x00\x00'), 0), 1)
        self.assertEqual(R.u32(memoryview(b'\x02\x00\x00\x00'), 0), 2)

    def test_out_of_range_raises_struct_error(self):
        for fn, size in ((R.u16, 2), (R.u32, 4), (R.u64, 8)):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(struct.error):
                    fn(b'\x00' * (size - 1), 0)
                with self.assertRaises(struct.error):
                    fn(b'\x00' * size, 1)

    def test_alignment_padding_is_not_data(self):
        # the chunk stream pads to 64 bytes with stale bytes; readers must be offset-exact
        self.assertEqual(R.u32(b'\xcc' * 64 + b'\x07\x00\x00\x00', 64), 7)

# =========================================================================== container
class TestParseContainer(TempDirCase):
    def build(self, **kw: Any) -> bytes:
        sections = kw.pop('sections', [F.section('FrameCapture', b'HELLO')])
        return F.rdc(sections, **kw)

    def test_header_fields(self):
        data = self.build()
        info = R.parse_container(self.path('a.rdc', data))
        thumb = b'\xff\xd8THUMB\xff\xd9'
        self.assertEqual(info['size'], len(data))
        self.assertEqual(info['version'], 0x10E)
        self.assertEqual(info['progVersion'], '1.46')
        self.assertEqual(info['thumbnail'], (64, 64, len(thumb)))
        # `_data` is the container's bytes, and since 2026-09-18 that may be an `mmap` of the file
        # rather than a copy of it (REFERENCE 4.14): `bytes()` is the way to ask for a plain copy.
        self.assertEqual(bytes(info['_data']), data)
        # 40 (file header) + thumbnail + 13 + nameLen (metadata) + 16 (timebase)
        self.assertEqual(info['headerLength'], 40 + len(thumb) + 13 + len('D3D12') + 1 + 16)
        self.assertEqual(info['sections'][0]['dataOffset'],
                         info['headerLength'] + 40 + len('FrameCapture') + 1)

    def test_metadata_and_timebase(self):
        info = R.parse_container(self.path('a.rdc', self.build(driver_id=7, driver_name='Vulkan',
                                                               time_base=99, time_freq=2.5)))
        self.assertEqual(info['meta']['driverID'], 7)
        self.assertEqual(info['meta']['driverName'], 'Vulkan')
        self.assertEqual(info['meta']['timeBase'], 99)
        self.assertEqual(info['meta']['timeFreq'], 2.5)

    def test_progversion_is_nul_terminated(self):
        info = R.parse_container(self.path('a.rdc', self.build(prog='1.46\x00junk')))
        self.assertEqual(info['progVersion'], '1.46')

    def test_section_fields_and_data_offset(self):
        info = R.parse_container(self.path('a.rdc', self.build()))
        self.assertEqual(len(info['sections']), 1)
        sec = info['sections'][0]
        self.assertEqual(sec['name'], 'FrameCapture')
        self.assertEqual(sec['type'], F.SECTION_FRAMECAPTURE)
        self.assertEqual(sec['flags'], 0)
        self.assertEqual(sec['version'], 1)
        self.assertEqual(sec['nameLen'], len('FrameCapture') + 1)
        self.assertEqual(sec['compLen'], 5)
        self.assertEqual(sec['uncompLen'], 5)
        self.assertEqual(sec['dataOffset'], info['headerLength'] + 40 + sec['nameLen'])
        self.assertEqual(info['_data'][sec['dataOffset']:sec['dataOffset'] + 5], b'HELLO')

    def test_multiple_sections_chain(self):
        secs = [F.section('FrameCapture', b'A' * 8),
                F.section('Bookmarks', b'B' * 3, sec_type=3, flags=2, version=5),
                F.section('Notes', b'', sec_type=4)]
        info = R.parse_container(self.path('a.rdc', self.build(sections=secs)))
        self.assertEqual([s['name'] for s in info['sections']], ['FrameCapture', 'Bookmarks', 'Notes'])
        self.assertEqual(info['sections'][1]['flags'], 2)
        self.assertEqual(info['sections'][1]['version'], 5)
        self.assertEqual(info['sections'][2]['uncompLen'], 0)
        self.assertEqual(info['sections'][1]['dataOffset'],
                         info['sections'][0]['dataOffset'] + info['sections'][0]['compLen'] + 40
                         + info['sections'][1]['nameLen'])
        self.assertEqual(info['_data'][info['sections'][2]['dataOffset']:][:4], b'\x01\x00\x00\x00')

    def test_zero_sections(self):
        info = R.parse_container(self.path('a.rdc', self.build(sections=[])))
        self.assertEqual(info['sections'], [])

    def test_nonzero_first_byte_ends_the_section_list(self):
        data = F.rdc([F.section('FrameCapture', b'X')], end=b'\xAA' + b'\x00' * 39)
        self.assertEqual(len(R.parse_container(self.path('a.rdc', data))['sections']), 1)

    def test_zero_namelen_ends_the_section_list(self):
        # data[o] == 0 but nameLen == 0 -> the parser must stop instead of looping forever
        data = F.rdc([F.section('FrameCapture', b'X')], end=b'\x00' * 40)
        self.assertEqual(len(R.parse_container(self.path('a.rdc', data))['sections']), 1)

    def test_oversized_namelen_ends_the_section_list(self):
        data = F.rdc([], end=F.section('X', b'Y', name_len=2049))
        self.assertEqual(R.parse_container(self.path('a.rdc', data))['sections'], [])

    def test_truncated_section_header_is_ignored(self):
        data = F.rdc([], end=b'\x00' * 39)
        self.assertEqual(R.parse_container(self.path('a.rdc', data))['sections'], [])

    def test_section_name_is_utf8_decoded(self):
        data = F.rdc([F.section('', b'', name_bytes=b'Caf\xc3\xa9\x00')])
        self.assertEqual(R.parse_container(self.path('a.rdc', data))['sections'][0]['name'], 'Caf\u00e9')

    def test_complen_shorter_than_data(self):
        data = F.rdc([F.section('FrameCapture', b'A' * 10, comp_len=4)])
        sec = R.parse_container(self.path('a.rdc', data))['sections'][0]
        self.assertEqual(sec['compLen'], 4)
        self.assertEqual(sec['uncompLen'], 10)

    def test_bad_magic_asserts(self):
        with self.assertRaises(AssertionError):
            R.parse_container(self.path('a.rdc', b'NOTRDC' + b'\x00' * 64))

    def test_missing_file_raises(self):
        with self.assertRaises(IOError):
            R.parse_container(os.path.join(self.tmp, 'nope.rdc'))

# =========================================================================== LZ4
def _real_lz4_c() -> Any:
    """The library's entry point, or None when no lz4 library is there to load."""
    return stream._lz4_c_function()

def _literal_only(block: bytes) -> bytes:
    """Decode a literal-only LZ4 block -- all `F.lz4_literal_block` ever writes.

    The fakes below are handed nothing else, and what those cases are about is the *addressing* the
    caller does rather than LZ4 itself, so this deliberately understands one sequence shape.
    """
    n = block[0] >> 4
    i = 1
    if n == 15:
        while block[i] == 255:
            n += 255
            i += 1
        n += block[i]
        i += 1
    return block[i:i + n]

class TestLz4WithoutALibrary(unittest.TestCase):
    """No library is not an empty stream: it is a failure that names the fix.

    `decompress_lz4` decodes every section body there is and has no second decoder to fall back to, so
    a machine without `bin/rdc_lz4.dll` and without a system lz4 has to be told once, with the command
    that does something about it.
    """

    def test_no_library_raises_with_the_fix_in_the_message(self):
        with mock.patch.object(stream, '_lz4_c_function', return_value=None):
            with self.assertRaises(R.FrameError) as caught:
                R.decompress_lz4(b'anything at all', 10)
        message = str(caught.exception)
        self.assertIn('cmake --build', message)
        self.assertIn('RDC_LZ4_DLL', message)

    def test_a_section_that_declares_nothing_is_empty_and_takes_no_library(self):
        with mock.patch.object(stream, '_lz4_c_function', return_value=None):
            self.assertEqual(R.decompress_lz4(b'', 0), (bytearray(), 0))

class TestLz4Pages(unittest.TestCase):
    """What `decompress_lz4` asks the library for, page by page, seen through a stand-in.

    The fake stands in for `LZ4_decompress_safe_usingDict` and decodes through the pointers the real one
    is given -- the block read with `string_at`, the answer written with `memmove` -- so what these
    cases check is the arithmetic the caller does. That is the part that would corrupt a stream rather
    than fail loudly, and it is the same arithmetic the real library runs.
    """

    #: 3 blocks of 256 bytes: enough to tell the blocks apart, cheap to decode in Python.
    payload = bytes(range(256)) * 8
    per = (len(payload) + 2) // 3

    def fake_c(self, calls: List[Any], refuse: int = 0) -> Any:
        """A stand-in for the library's entry point, decoding honestly through the given pointers."""

        def fn(src: int, dst: int, srcsize: int, cap: int, dstart: int, dsize: int) -> int:
            calls.append((srcsize, cap, dst, dstart, dsize))
            if len(calls) == refuse:
                return -1
            data = _literal_only(ctypes.string_at(src, srcsize))
            ctypes.memmove(dst, data, len(data))
            return len(data)

        return fn

    def blob(self, block_count: int = 3) -> bytes:
        return F.lz4_container(self.payload, block_count=block_count)

    def test_every_page_goes_through_the_library(self):
        calls: List[Any] = []
        with mock.patch.object(stream, '_lz4_c_function', return_value=self.fake_c(calls)):
            out, blocks = R.decompress_lz4(self.blob(), len(self.payload))
        self.assertEqual((out, blocks), (self.payload, 3))
        self.assertEqual(len(calls), 3)

    def test_each_page_is_written_where_the_previous_one_ended(self):
        calls: List[Any] = []
        with mock.patch.object(stream, '_lz4_c_function', return_value=self.fake_c(calls)):
            R.decompress_lz4(self.blob(), len(self.payload))
        # the dictionary ends exactly where the page is written, which is the contiguity LZ4 has a
        # fast path for, and it is the output so far up to 64 KB
        self.assertEqual([c[3] + c[4] for c in calls], [c[2] for c in calls])
        self.assertEqual([c[4] for c in calls], [0, self.per, 2 * self.per])
        # and the room offered is everything the section still expects
        self.assertEqual([c[1] for c in calls],
                         [len(self.payload), len(self.payload) - self.per,
                          len(self.payload) - 2 * self.per])

    def test_the_dictionary_stops_at_64_kb(self):
        # a real section's pages never reach the limit, so it is pinned where it is decided
        self.assertEqual(stream._lz4_dict_size(0), 0)
        self.assertEqual(stream._lz4_dict_size(1234), 1234)
        self.assertEqual(stream._lz4_dict_size(1 << 16), 1 << 16)
        self.assertEqual(stream._lz4_dict_size(1 << 24), 1 << 16)

    def test_a_refused_page_raises_and_says_which_one(self):
        calls: List[Any] = []
        with mock.patch.object(stream, '_lz4_c_function', return_value=self.fake_c(calls, refuse=2)):
            with self.assertRaises(R.FrameError) as caught:
                R.decompress_lz4(self.blob(), len(self.payload))
        self.assertEqual(len(calls), 2)                 # it stopped asking at the refusal
        self.assertIn('page 1', str(caught.exception))  # 0-based, and the page is what went wrong

    def test_a_section_that_comes_out_short_raises(self):
        # a truncated stream returned as if it were whole is a stream every command would read as fact
        calls: List[Any] = []
        with mock.patch.object(stream, '_lz4_c_function', return_value=self.fake_c(calls)):
            with self.assertRaises(R.FrameError) as caught:
                R.decompress_lz4(self.blob(), len(self.payload) + 1)
        self.assertIn(str(len(self.payload)), str(caught.exception))

@unittest.skipUnless(_real_lz4_c() is not None, 'no lz4 library to load (build bin/rdc_lz4.dll)')
class TestLz4LibraryReal(unittest.TestCase):
    """The library itself rather than a stand-in: `bin/rdc_lz4.dll`, or a system liblz4."""

    payload = bytes(range(256)) * 8

    def test_a_section_decodes_to_the_size_it_declares(self):
        blob = F.lz4_container(self.payload, block_count=3)
        out, blocks = R.decompress_lz4(blob, len(self.payload))
        self.assertEqual((bytes(out), blocks), (self.payload, 3))

    def test_a_body_that_ends_early_raises(self):
        # short in the framing and short in the last page: both are the same failure to the caller
        blob = F.lz4_container(self.payload, block_count=3)
        with self.assertRaises(R.FrameError):
            R.decompress_lz4(blob, len(self.payload) + 1)
        with self.assertRaises(R.FrameError):
            R.decompress_lz4(blob[:-4], len(self.payload))

    def test_a_page_the_library_refuses_names_the_page(self):
        # a match at offset 8 with a single trailing literal: LZ4 refuses a block whose last sequence
        # is a match, and one whose match nibble is 0 -- the two rules `lz4_match_block` exists to
        # respect, and the reason a hand-built block is not the same as a valid one
        bad = b'\x14' + b'Z' + b'\x08\x00'
        with self.assertRaises(R.FrameError) as caught:
            R.decompress_lz4(F.u32b(len(bad)) + bad, 9)
        self.assertIn('page 0', str(caught.exception))

    def test_a_page_may_match_into_the_previous_one(self):
        # the property the whole design rests on: the 64 KB before the write position is the
        # dictionary, so a page whose match reaches into the page before it decodes to exactly the
        # bytes it was compressed from
        page1 = bytes(range(64))
        first = F.lz4_literal_block(page1)
        # 4 literals, then 8 bytes matched 20 back -- a match that starts 16 bytes before the end of
        # page 1 -- and the 5 literal bytes LZ4 requires a block to end with
        second = F.lz4_match_block(page1[0:4], 20, 8, b'!!!!!')
        blob = F.u32b(len(first)) + first + F.u32b(len(second)) + second
        out, blocks = R.decompress_lz4(blob, len(page1) + 17)
        self.assertEqual((blocks, bytes(out)),
                         (2, page1 + page1[0:4] + page1[48:56] + b'!!!!!'))

# =========================================================================== zstd
class TestDecompressZstd(unittest.TestCase):
    """`zstandard` is optional; a stub module exercises all three magic branches."""

    def fake_module(self, recorder: List[bytes]) -> types.ModuleType:
        mod = types.ModuleType('zstandard')

        class _Reader:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def read(self) -> bytes:
                recorder.append(self.body)
                return self.body

        class ZstdDecompressor:
            def stream_reader(self, body: bytes) -> _Reader:
                return _Reader(body)

        # setattr (not attribute assignment): a bare ModuleType has no declared attribute, and the
        # stub has to look like the real `zstandard` module to `decompress_zstd`.
        setattr(mod, 'ZstdDecompressor', ZstdDecompressor)
        return mod

    def test_magic_at_offset_zero(self):
        seen = []
        with mock.patch.dict(sys.modules, {'zstandard': self.fake_module(seen)}):
            out = R.decompress_zstd(R.ZSTD_MAGIC + b'BODY')
        self.assertEqual(out, R.ZSTD_MAGIC + b'BODY')
        self.assertEqual(seen, [R.ZSTD_MAGIC + b'BODY'])

    def test_magic_after_u32_prefix(self):
        seen = []
        with mock.patch.dict(sys.modules, {'zstandard': self.fake_module(seen)}):
            out = R.decompress_zstd(F.u32b(1234) + R.ZSTD_MAGIC + b'BODY')
        self.assertEqual(out, R.ZSTD_MAGIC + b'BODY')
        self.assertEqual(seen, [R.ZSTD_MAGIC + b'BODY'])

    def test_no_magic_passes_blob_through(self):
        seen = []
        with mock.patch.dict(sys.modules, {'zstandard': self.fake_module(seen)}):
            out = R.decompress_zstd(b'RAWBODY')
        self.assertEqual(out, b'RAWBODY')
        self.assertEqual(seen, [b'RAWBODY'])

    def test_missing_module_propagates_import_error(self):
        with mock.patch.dict(sys.modules, {'zstandard': None}):
            with self.assertRaises(ImportError):
                R.decompress_zstd(R.ZSTD_MAGIC + b'BODY')

class TestGetStream(TempDirCase):
    def test_raw_section(self):
        path = self.capture_path([F.chunk(1000, b'payload')])
        info = R.parse_container(path)
        stream, how = R.get_stream(info)
        self.assertEqual(how, 'raw')
        self.assertEqual(stream, info['_data'][info['sections'][0]['dataOffset']:][:len(stream)])

    def test_lz4_section(self):
        chunks = [F.chunk(1000, b'payload-' * 20)]
        path = self.capture_path(chunks, lz4=True, block_count=2)
        stream, how = R.get_stream(R.parse_container(path))
        self.assertEqual(stream, b''.join(chunks))
        self.assertEqual(how, 'lz4(2 blocks)')

    def test_zstd_section(self):
        body = R.ZSTD_MAGIC + b'COMPRESSED'
        data = F.rdc([F.section('FrameCapture', body)])
        seen = []
        mod = types.ModuleType('zstandard')

        class _Reader:
            def __init__(self, b: bytes) -> None:
                self.b = b

            def read(self) -> bytes:
                return self.b

        class _Decompressor:
            def stream_reader(self, b: bytes) -> _Reader:
                seen.append(b)
                return _Reader(b)

        setattr(mod, 'ZstdDecompressor', _Decompressor)
        with mock.patch.dict(sys.modules, {'zstandard': mod}):
            stream, how = R.get_stream(R.parse_container(self.path('z.rdc', data)))
        self.assertEqual(how, 'zstd')
        self.assertEqual(stream, body)
        self.assertEqual(seen, [body])

    def test_section_index_selects_other_sections(self):
        secs = [F.section('FrameCapture', b'AAAA'),
                F.section('Notes', b'BBBB')]
        info = R.parse_container(self.path('m.rdc', F.rdc(secs)))
        self.assertEqual(R.get_stream(info)[0], b'AAAA')
        self.assertEqual(R.get_stream(info, 1)[0], b'BBBB')

    def test_out_of_range_index_raises(self):
        info = R.parse_container(self.path('m.rdc', F.rdc([])))
        with self.assertRaises(IndexError):
            R.get_stream(info)

# =========================================================================== cache
class CacheCase(TempDirCase):
    """An lz4 capture plus the pieces the cache functions need.

    `TempDirCase` points `$RDC_CACHE_DIR` at a scratch directory, so these tests never touch the
    real user cache and never see each other's entries.
    """

    #: the chunks the cached stream is made of.
    chunks: List[bytes]
    #: path of the capture, its parsed container, and the decompressed stream.
    capture: str
    info: R.CaptureInfo
    stream: bytes

    def setUp(self) -> None:
        super().setUp()
        self.chunks = [F.chunk(1000, b'payload-' * 8), F.chunk(1001, b'second-' * 8)]
        self.capture = self.path('c.rdc', F.capture(self.chunks, lz4=True, block_count=2))
        self.info = R.parse_container(self.capture)
        self.stream = b''.join(self.chunks)

    def store(self, stream: Optional[bytes] = None, method: int = R.METHOD_LZ4,
              blocks: int = 2, section: int = 0) -> Optional[str]:
        """Cache `stream` (by default the capture's real stream) and return the file written."""
        return R.cache_store(self.capture, self.info, section,
                             self.stream if stream is None else stream, method, blocks)

    def cache_file(self, section: int = 0) -> str:
        """Where the cache file for this capture/section has to be."""
        st = os.stat(self.capture)
        return R._cache_file(os.path.normcase(os.path.abspath(self.capture)), st.st_size,
                             st.st_mtime_ns, section)

    def touch(self, name: str, data: bytes = b'x') -> str:
        """Write a file into the cache directory (creating it) and return its path."""
        os.makedirs(self.cache, exist_ok=True)
        return F.write_bytes(os.path.join(self.cache, name), data)

    def write_cache(self, stream: bytes = b'', src: Optional[str] = None,
                    magic: bytes = R.CACHE_MAGIC, version: int = R.CACHE_VERSION,
                    section: int = 0, at_section: Optional[int] = None, method: int = R.METHOD_LZ4,
                    blocks: int = 2, src_size: Optional[int] = None,
                    src_mtime: Optional[int] = None, stream_len: Optional[int] = None) -> str:
        """Write a cache file by hand, so a single header field can be made wrong.

        The header is padded exactly as `cache_store` pads it, so these hand-written files go down the
        same `mmap` path a real hit does (and a wrong header field is still wrong where it counts).
        """
        st = os.stat(self.capture)
        default = os.path.normcase(os.path.abspath(self.capture))
        path_bytes = (default if src is None else src).encode('utf-8')
        head_len = R.align_up(R.CACHE_HEADER.size + len(path_bytes), R.CACHE_ALIGN)
        head = R.CACHE_HEADER.pack(magic, version, head_len, section,
                                   method, st.st_size if src_size is None else src_size,
                                   st.st_mtime_ns if src_mtime is None else src_mtime,
                                   len(stream) if stream_len is None else stream_len, blocks)
        cfile = self.cache_file(section if at_section is None else at_section)
        os.makedirs(os.path.dirname(cfile), exist_ok=True)
        with open(cfile, 'wb') as fh:
            fh.write(head)
            fh.write(path_bytes)
            fh.write(b'\x00' * (head_len - R.CACHE_HEADER.size - len(path_bytes)))
            fh.write(stream)
        return cfile

    def release_container(self) -> None:
        """Let go of the capture's mapping: Windows will not let a mapped file be rewritten.

        `parse_container` maps the capture instead of reading it (REFERENCE 4.14), so a test that
        replaces or truncates the capture on disk has to close that map first.
        """
        data = self.info['_data']
        if isinstance(data, mmap.mmap):
            data.close()

class TestCacheDir(unittest.TestCase):
    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {'RDC_CACHE_DIR': r'D:\somewhere'}):
            self.assertEqual(R.cache_dir(), r'D:\somewhere')

    def test_default_is_an_absolute_rdc_tools_folder(self):
        # the platform base directories are blanked (not the whole environment: `expanduser` needs
        # the home variables), so the fallback under the home directory is what gets tested
        blanked = {'RDC_CACHE_DIR': '', 'LOCALAPPDATA': '', 'XDG_CACHE_HOME': ''}
        with mock.patch.dict(os.environ, blanked):
            path = R.cache_dir()
        self.assertTrue(os.path.isabs(path), path)
        self.assertTrue(path.replace('\\', '/').endswith('rdc-tools/cache'), path)

    def test_on_unless_rdc_no_cache_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(R._cache_enabled())
        for value in ('1', 'yes', '0'):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {'RDC_NO_CACHE': value}):
                    self.assertFalse(R._cache_enabled())

class TestMethodLabel(unittest.TestCase):
    def test_one_label_per_method(self):
        self.assertEqual(R._method_label(R.METHOD_RAW, 0), 'raw')
        self.assertEqual(R._method_label(R.METHOD_LZ4, 7), 'lz4(7 blocks)')
        self.assertEqual(R._method_label(R.METHOD_ZSTD, 0), 'zstd')

    def test_cached_suffix(self):
        self.assertEqual(R._method_label(R.METHOD_LZ4, 7, cached=True), 'lz4(7 blocks, cached)')
        self.assertEqual(R._method_label(R.METHOD_RAW, 0, cached=True), 'raw, cached')

class TestCacheStoreAndLookup(CacheCase):
    def test_store_then_lookup_returns_the_stream_and_a_cached_label(self):
        cfile = self.store()
        self.assertIsNotNone(cfile)
        self.assertTrue(os.path.isfile(cfile or ''))
        hit = R.cache_lookup(self.capture, self.info)
        self.assertIsNotNone(hit)
        # a hit is an `mmap` of the cache file, not a copy of it, so compare the bytes it stands for
        self.assertEqual(bytes(hit[0]) if hit else None, self.stream)
        self.assertEqual(hit[1] if hit else '', 'lz4(2 blocks, cached)')

    def test_stats_answer_from_the_header(self):
        self.store()
        self.assertEqual(R.cache_stats(self.capture, self.info),
                         (len(self.stream), 'lz4(2 blocks, cached)'))

    def test_no_file_is_a_miss(self):
        self.assertIsNone(R.cache_lookup(self.capture, self.info))
        self.assertIsNone(R.cache_stats(self.capture, self.info))

    def test_raw_sections_are_never_cached(self):
        self.assertIsNone(self.store(method=R.METHOD_RAW))
        self.assertEqual(R._cache_names(), [])

    def test_a_zstd_label_round_trips(self):
        self.store(method=R.METHOD_ZSTD, blocks=0)
        hit = R.cache_lookup(self.capture, self.info)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1] if hit else '', 'zstd, cached')

    def test_a_disabled_cache_writes_and_reads_nothing(self):
        with mock.patch.dict(os.environ, {'RDC_NO_CACHE': '1'}):
            self.assertIsNone(self.store())
            self.assertIsNone(R.cache_lookup(self.capture, self.info))
            self.assertEqual(R._cache_names(), [])

    def test_the_write_is_atomic(self):
        self.store()
        self.assertEqual([n for n in R._cache_names() if '.tmp' in n], [])

    def test_the_header_records_the_identity_and_the_lengths(self):
        entry = R._read_cache_header(self.store() or '')
        self.assertIsNotNone(entry)
        assert entry is not None                     # narrow for the type checker
        self.assertEqual(entry['srcPath'], os.path.normcase(os.path.abspath(self.capture)))
        self.assertEqual(entry['srcSize'], os.path.getsize(self.capture))
        self.assertEqual(entry['srcMtime'], os.stat(self.capture).st_mtime_ns)
        self.assertEqual(entry['section'], 0)
        self.assertEqual(entry['method'], R.METHOD_LZ4)
        self.assertEqual(entry['blocks'], 2)
        self.assertEqual(entry['streamLen'], len(self.stream))
        # the header is padded so the stream starts on an mmap boundary (REFERENCE 4.14)
        self.assertEqual(entry['hdrLen'],
                         R.align_up(R.CACHE_HEADER.size + len(os.path.abspath(self.capture)),
                                    R.CACHE_ALIGN))

    def test_the_payload_follows_the_header(self):
        cfile = self.store() or ''
        entry = R._read_cache_header(cfile)
        assert entry is not None
        with open(cfile, 'rb') as fh:
            fh.seek(entry['hdrLen'])
            self.assertEqual(fh.read(), self.stream)

    def test_another_section_is_a_separate_entry(self):
        self.store()
        self.assertIsNone(R.cache_lookup(self.capture, self.info, 1))

    @unittest.skipUnless(os.name == 'nt', 'case-insensitive paths are a Windows property')
    def test_a_differently_cased_path_is_the_same_entry(self):
        # found on a real capture: the shell gives `c:\x.rdc`, `Resolve-Path` gives `C:\x.rdc`, and
        # without normcase that cached a 1.4 GB stream twice
        self.store()
        other = self.capture[0].swapcase() + self.capture[1:]
        self.assertEqual(R._cache_identity(other), R._cache_identity(self.capture))
        hit = R.cache_lookup(other, R.parse_container(other))
        self.assertIsNotNone(hit)
        self.assertEqual(bytes(hit[0]) if hit else None, self.stream)
        self.assertEqual(len(R._cache_names()), 1)
        self.store()
        self.assertIsNone(R.cache_lookup(self.capture, self.info, 1))

class TestCacheValidation(CacheCase):
    def touch_capture(self) -> None:
        """Give the capture a new mtime, the way editing it would."""
        st = os.stat(self.capture)
        os.utime(self.capture, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))

    def test_touching_the_capture_invalidates_the_entry(self):
        cfile = self.store()
        self.touch_capture()
        self.assertIsNone(R.cache_lookup(self.capture, self.info))
        # the stale file is keyed to the old mtime, so it is not even looked up -- see the prune test
        self.assertTrue(os.path.exists(cfile or ''))

    def test_a_rebuild_removes_the_stale_entry_for_the_same_capture(self):
        stale = self.store()
        self.touch_capture()
        self.store()
        self.assertFalse(os.path.exists(stale or ''))
        self.assertEqual(len(R._cache_names()), 1)

    def test_pruning_keeps_another_section_of_the_same_capture(self):
        other = self.write_cache(stream=b'other section', section=1)
        self.store()
        self.assertTrue(os.path.exists(other))

    def test_a_stream_shorter_than_the_section_claims_is_rejected(self):
        # This is the check that caught the block-parallel decoder: it wrote a stream 4.9 MB short
        # of `uncompLen`, so every read of it was refused instead of silently analysing half a frame.
        self.write_cache(stream=self.stream[:-4])
        self.assertIsNone(R.cache_lookup(self.capture, self.info))
        self.assertEqual(R._cache_names(), [])

    def test_a_truncated_payload_is_rejected(self):
        cfile = self.write_cache(stream=self.stream)
        with open(cfile, 'r+b') as fh:
            fh.truncate(os.path.getsize(cfile) - 4)
        self.assertIsNone(R.cache_lookup(self.capture, self.info))
        self.assertEqual(R._cache_names(), [])

    def test_a_short_file_is_not_a_cache_file(self):
        cfile = self.touch(os.path.basename(self.cache_file()), b'RDCCACHE')
        self.assertIsNone(R._read_cache_header(cfile))
        self.assertIsNone(R.cache_lookup(self.capture, self.info))

    def test_a_foreign_magic_is_not_a_cache_file(self):
        cfile = self.write_cache(stream=self.stream, magic=b'NOTACACH')
        self.assertIsNone(R._read_cache_header(cfile))

    def test_another_format_version_is_not_a_cache_file(self):
        cfile = self.write_cache(stream=self.stream, version=R.CACHE_VERSION + 1)
        self.assertIsNone(R._read_cache_header(cfile))

    def test_a_different_source_path_is_rejected(self):
        self.write_cache(stream=self.stream, src=r'D:\other.rdc')
        self.assertIsNone(R.cache_lookup(self.capture, self.info))

    def test_a_different_source_size_is_rejected(self):
        self.write_cache(stream=self.stream, src_size=os.path.getsize(self.capture) + 1)
        self.assertIsNone(R.cache_lookup(self.capture, self.info))

    def test_a_different_source_mtime_is_rejected(self):
        self.write_cache(stream=self.stream, src_mtime=os.stat(self.capture).st_mtime_ns + 1)
        self.assertIsNone(R.cache_lookup(self.capture, self.info))

    def test_a_header_for_another_section_is_rejected(self):
        self.write_cache(stream=self.stream, section=1, at_section=0)
        self.assertIsNone(R.cache_lookup(self.capture, self.info, 0))

    def test_expected_len_is_zero_for_an_index_that_does_not_exist(self):
        self.assertEqual(R._expected_len(self.info, 0), len(self.stream))
        self.assertEqual(R._expected_len(self.info, 5), 0)
        self.assertEqual(R._expected_len(self.info, -1), 0)

class TestCacheEntries(CacheCase):
    def test_no_directory_is_an_empty_list(self):
        self.assertEqual(R.cache_entries(), [])
        self.assertEqual(R.cache_clear(), (0, 0))

    def test_entries_are_sorted_by_stream_length(self):
        for name, payload in (('small.rdc', b'small' * 4), ('big.rdc', b'bigger' * 40)):
            path = self.path(name, F.capture([F.chunk(1000, payload)], lz4=True))
            info = R.parse_container(path)
            stream, _how = R.get_stream(info)
            R.cache_store(path, info, 0, stream, R.METHOD_LZ4, 1)
        entries = R.cache_entries()
        self.assertEqual([os.path.basename(e['srcPath']) for e in entries],
                         ['big.rdc', 'small.rdc'])

    def test_unusable_files_are_not_listed(self):
        self.store()
        self.touch('garbage' + R.CACHE_SUFFIX, b'nope')
        self.assertEqual(len(R.cache_entries()), 1)
        self.assertEqual(len(R._cache_names()), 2)

    def test_temporary_files_are_not_listed(self):
        self.touch('x' + R.CACHE_SUFFIX + '.tmp99', b'half a stream')
        self.assertEqual(R.cache_entries(), [])

    def test_clear_reports_and_removes_everything(self):
        self.store()
        expected = os.path.getsize(self.cache_file())
        count, freed = R.cache_clear()
        self.assertEqual(count, 1)
        self.assertEqual(freed, expected)
        self.assertEqual(R._cache_names(), [])

    def test_clear_also_removes_interrupted_writes(self):
        self.store()
        self.touch('x' + R.CACHE_SUFFIX + '.tmp99', b'half a stream')
        count, _freed = R.cache_clear()
        self.assertEqual(count, 2)
        self.assertEqual(R._cache_names(), [])

    def test_a_derived_file_is_not_a_stream_and_is_cleared_with_one(self):
        # A sidecar is an *answer* about a stream (`.bindnames.json`), named after the stream's own
        # file: it is never listed as an entry, it is counted on its own, and `cache clear` -- the one
        # thing that reclaims the streams -- takes it too, so no orphan is left behind.
        self.store()
        entry = R.cache_entries()[0]
        sidecar = R.sidecar_path(entry, '.bindnames.json')
        with open(sidecar, 'w', encoding='utf-8') as fh:
            fh.write('{"version": 1}')
        self.assertEqual(len(R.cache_entries()), 1)
        self.assertEqual(R.derived_names(), [os.path.basename(sidecar)])
        expected = os.path.getsize(self.cache_file()) + len('{"version": 1}')
        count, freed = R.cache_clear()
        self.assertEqual(count, 2)
        self.assertEqual(freed, expected)
        self.assertEqual(R.derived_names(), [])
        self.assertEqual(R._cache_names(), [])

    def test_a_half_written_sidecar_is_named_too(self):
        self.touch('x.bindnames.json.tmp', b'{')
        self.assertEqual(R.derived_names(), ['x.bindnames.json.tmp'])
        self.assertEqual(R.cache_entries(), [])
        self.assertEqual(len(R._cache_names()), 0)

class TestStreamCaching(CacheCase):
    def test_first_call_decompresses_and_the_second_comes_from_the_cache(self):
        _, first, how_first = R.load_stream(self.capture)
        _, second, how_second = R.load_stream(self.capture)
        # the second read is a hit, and a hit is an `mmap` of the cache file (REFERENCE 4.14)
        self.assertEqual(bytes(first), self.stream)
        self.assertEqual(bytes(second), self.stream)
        self.assertEqual(how_first, 'lz4(2 blocks)')
        self.assertEqual(how_second, 'lz4(2 blocks, cached)')

    def test_nothing_is_written_when_the_cache_is_off(self):
        with mock.patch.dict(os.environ, {'RDC_NO_CACHE': '1'}):
            R.load_stream(self.capture)
            _, _, how = R.load_stream(self.capture)
            self.assertEqual(R._cache_names(), [])
        self.assertEqual(how, 'lz4(2 blocks)')

    def test_a_raw_capture_never_writes_a_cache_file(self):
        path = self.capture_path([F.chunk(1000, b'raw')])
        _, _, how = R.load_stream(path)
        self.assertEqual(how, 'raw')
        self.assertEqual(R._cache_names(), [])

    def test_stream_stats_fills_the_cache_too(self):
        self.assertEqual(R.stream_stats(self.capture, self.info),
                         (len(self.stream), 'lz4(2 blocks)'))
        self.assertEqual(len(R._cache_names()), 1)
        self.assertEqual(R.stream_stats(self.capture, self.info),
                         (len(self.stream), 'lz4(2 blocks, cached)'))

    def test_stream_stats_answers_from_the_header_without_the_payload(self):
        R.load_stream(self.capture)
        cfile = self.cache_file()
        with open(cfile, 'r+b') as fh:                      # break the payload, keep the header
            fh.truncate(os.path.getsize(cfile) - 4)
        self.assertEqual(R.stream_stats(self.capture, self.info),
                         (len(self.stream), 'lz4(2 blocks, cached)'))
        self.assertIsNone(R.cache_lookup(self.capture, self.info))

    def test_stream_stats_raises_for_a_section_that_does_not_exist(self):
        info = R.parse_container(self.path('empty.rdc', F.rdc([])))
        with self.assertRaises(IndexError):
            R.stream_stats(self.capture, info)

    def test_a_deleted_cache_file_is_rebuilt(self):
        R.load_stream(self.capture)
        os.remove(self.cache_file())
        _, stream, how = R.load_stream(self.capture)
        self.assertEqual(stream, self.stream)
        self.assertEqual(how, 'lz4(2 blocks)')
        self.assertTrue(os.path.isfile(self.cache_file()))

    def test_a_changed_capture_is_decompressed_again(self):
        R.load_stream(self.capture)
        other = [F.chunk(1000, b'different-' * 8)]
        self.release_container()
        F.write_bytes(self.capture, F.capture(other, lz4=True))
        _, stream, how = R.load_stream(self.capture)
        self.assertEqual(stream, b''.join(other))
        self.assertEqual(how, 'lz4(1 blocks)')

    def test_an_unusable_cache_directory_only_warns_once(self):
        blocked = self.path('blocked')                      # a file where the directory must be
        F.write_bytes(blocked, b'not a directory')
        buf = io.StringIO()
        with mock.patch.object(cache, 'cache_dir', lambda: blocked):
            with mock.patch.object(cache, '_CACHE_WARNED', False):
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    _, first, _ = R.load_stream(self.capture)
                    _, second, _ = R.load_stream(self.capture)
        self.assertEqual(first, self.stream)
        self.assertEqual(second, self.stream)
        self.assertEqual(buf.getvalue().count('warning: cannot write the stream cache'), 1)

if __name__ == '__main__':
    unittest.main(verbosity=2)
