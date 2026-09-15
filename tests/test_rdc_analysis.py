"""Unit tests for the parsers/decoders in rdc_analysis.py.

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_analysis.py
    python -m unittest tests.test_rdc_analysis
    python rdc_analysis.py selftest -k test_lz4

Payload layouts used by the fixtures follow README section 3.4; where a decoder reads a chunk
differently from another consumer of the same chunk, the test pins the behaviour actually
implemented and says so in a comment.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from typing import Any, Callable, Dict, List, Optional, Sequence
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402
import rdc_fixtures as F          # noqa: E402


def capture_text(func: Callable[..., object], *args: Any, **kwargs: Any) -> str:
    """Run `func` and return everything it printed on stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


def capture_all(func: Callable[..., object], *args: Any, **kwargs: Any) -> str:
    """Run `func` and return everything it printed on stdout *and* stderr.

    unittest's TextTestRunner writes its report to stderr, so the selftest command needs this.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        func(*args, **kwargs)
    return out.getvalue() + err.getvalue()


class TempDirCase(unittest.TestCase):
    #: scratch directory created in `setUp` and removed by a cleanup hook.
    tmp: str

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='rdc_unit_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def path(self, name: str, data: Optional[bytes] = None) -> str:
        p = os.path.join(self.tmp, name)
        if data is not None:
            F.write_bytes(p, data)
        return p

    def capture_path(self, chunks: Sequence[bytes], name: str = 'capture.rdc',
                     **kw: Any) -> str:
        return self.path(name, F.capture(chunks, **kw))


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
        self.assertEqual(info['_data'], data)
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
class TestLz4Block(unittest.TestCase):
    def test_literals_only(self):
        src = bytes([6 << 4]) + b'abcdef'
        self.assertEqual(R.lz4_block(src, bytearray()), b'abcdef')

    def test_empty_input(self):
        self.assertEqual(R.lz4_block(b'', bytearray()), b'')

    def test_literal_length_extension(self):
        lit = b'A' * 528
        src = b'\xF0\xFF\xFF\x03' + lit      # 15 + 255 + 255 + 3
        self.assertEqual(R.lz4_block(src, bytearray()), lit)

    def test_match_with_offset_at_least_matchlength(self):
        # 4 literals then a match of length 4 at offset 4 (offsets are little endian)
        out = R.lz4_block(b'\x40abcd' + b'\x04\x00', bytearray())
        self.assertEqual(out, b'abcdabcd')

    def test_overlapping_match_repeats_pattern(self):
        # offset 2 < matchlength 6 -> the LZ4 "repeat the tail" case
        out = R.lz4_block(bytes([(2 << 4) | 2]) + b'ab' + b'\x02\x00', bytearray())
        self.assertEqual(out, b'abababab')

    def test_match_length_extension(self):
        src = b'\x0F' + b'\x01\x00' + b'\xFF\x05'   # mlen = 15 + 255 + 5 + 4
        out = R.lz4_block(src, bytearray(b'X'))
        self.assertEqual(out, b'X' * 280)

    def test_zero_offset_stops_decoding(self):
        self.assertEqual(R.lz4_block(b'\x00\x00\x00', bytearray(b'abc')), b'abc')

    def test_appends_to_existing_output(self):
        # offset 3, match length 4: copies "xyz" then wraps around for the last byte
        out = R.lz4_block(b'\x30xyz' + b'\x03\x00', bytearray(b'abc'))
        self.assertEqual(out, b'abcxyzxyzx')

    def test_trailing_literals_without_match(self):
        out = R.lz4_block(b'\x40abcd' + b'\x04\x00' + b'\x30xyz', bytearray())
        self.assertEqual(out, b'abcdabcdxyz')

    def test_literal_count_past_end_copies_what_is_there(self):
        # lenient: a literal run longer than the block copies the remainder
        self.assertEqual(R.lz4_block(b'\x90ab', bytearray()), b'ab')


class TestDecompressLz4(unittest.TestCase):
    #: sample payload shared by every test in this class.
    payload: bytes

    def setUp(self) -> None:
        self.payload = bytes(range(256)) * 2

    def test_single_block_roundtrip(self):
        blob = F.lz4_container(self.payload)
        out, blocks = R.decompress_lz4(blob, len(self.payload))
        self.assertEqual(out, self.payload)
        self.assertEqual(blocks, 1)

    def test_multi_block_roundtrip_and_block_count(self):
        blob = F.lz4_container(self.payload, block_count=3)
        out, blocks = R.decompress_lz4(blob, len(self.payload))
        self.assertEqual(out, self.payload)
        self.assertEqual(blocks, 3)

    def test_expect_zero_consumes_every_block(self):
        blob = F.lz4_container(self.payload, block_count=3)
        out, blocks = R.decompress_lz4(blob, 0)
        self.assertEqual(out, self.payload)
        self.assertEqual(blocks, 3)

    def test_stops_once_expect_is_reached(self):
        per_block = (len(self.payload) + 2) // 3
        blob = F.lz4_container(self.payload, block_count=3)
        out, blocks = R.decompress_lz4(blob, per_block)
        self.assertEqual(blocks, 1)
        self.assertEqual(out, self.payload[:per_block])

    def test_zero_block_length_stops(self):
        self.assertEqual(R.decompress_lz4(F.u32b(0) + b'junkjunk', 100), (b'', 0))

    def test_block_length_past_end_stops(self):
        self.assertEqual(R.decompress_lz4(F.u32b(999) + b'\x00' * 4, 100), (b'', 0))

    def test_short_blob(self):
        self.assertEqual(R.decompress_lz4(b'\x01\x02', 100), (b'', 0))
        self.assertEqual(R.decompress_lz4(b'', 100), (b'', 0))

    def test_empty_payload(self):
        self.assertEqual(R.decompress_lz4(b'', 0), (b'', 0))


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


# =========================================================================== misc helpers
class TestAlignUp(unittest.TestCase):
    def test_default_alignment_is_64(self):
        self.assertEqual(R.CHUNK_ALIGN, 64)
        for value, expected in ((0, 0), (1, 64), (63, 64), (64, 64), (65, 128), (127, 128),
                                (128, 128), (129, 192), (1000, 1024)):
            with self.subTest(value=value):
                self.assertEqual(R.align_up(value), expected)

    def test_custom_alignment(self):
        self.assertEqual(R.align_up(1, 8), 8)
        self.assertEqual(R.align_up(8, 8), 8)
        self.assertEqual(R.align_up(9, 8), 16)
        self.assertEqual(R.align_up(3, 1), 3)


class TestChunkFlagConstants(unittest.TestCase):
    def test_flag_values_match_the_serialiser(self):
        self.assertEqual(R.CHUNK_CALLSTACK, 0x00010000)
        self.assertEqual(R.CHUNK_THREADID, 0x00020000)
        self.assertEqual(R.CHUNK_DURATION, 0x00040000)
        self.assertEqual(R.CHUNK_TIMESTAMP, 0x00080000)
        self.assertEqual(R.CHUNK_64BITSIZE, 0x00100000)
        self.assertEqual(R.CHUNK_ALIGN, 64)
        self.assertEqual(R.ZSTD_MAGIC, b'\x28\xb5\x2f\xfd')

    def test_chunk_name_sets(self):
        self.assertIn('PushMarker', R.MARKER_CHUNKS)
        self.assertIn('Queue_BeginEvent', R.MARKER_CHUNKS)
        self.assertIn('List_DrawIndexedInstanced', R.DRAW_CHUNKS)
        self.assertIn('List_Dispatch', R.DRAW_CHUNKS)


# =========================================================================== enum parsing
ENUM_TEXT = '''
enum class SystemChunk : uint32_t
{
  // 0 is reserved as a 'null' chunk that is only for debug
  DriverInit = 1,
  InitialContentsList,
  InitialContents,
  CaptureBegin,
  CaptureScope,
  CaptureEnd,

  FirstDriverChunk = 1000,
};
'''


class TestParseChunkEnum(unittest.TestCase):
    def test_explicit_implicit_and_sentinel(self):
        self.assertEqual(R.parse_chunk_enum(ENUM_TEXT, 'SystemChunk'),
                         {1: 'DriverInit', 2: 'InitialContentsList', 3: 'InitialContents',
                          4: 'CaptureBegin', 5: 'CaptureScope', 6: 'CaptureEnd',
                          1000: 'FirstDriverChunk'})

    def test_comments_and_blank_lines_are_skipped(self):
        self.assertNotIn('comment', R.parse_chunk_enum(ENUM_TEXT, 'SystemChunk').values())

    def test_commented_out_entry_is_ignored(self):
        text = 'enum class E : uint32_t\n{\n  // Hidden = 5,\n  Shown = 7,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {7: 'Shown'})

    def test_firstdriverchunk_expression_sets_1000(self):
        text = 'enum class E : uint32_t\n{\n  SetName = (uint32_t)SystemChunk::FirstDriverChunk,\n  Next,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {1000: 'SetName', 1001: 'Next'})

    def test_unknown_expression_increments(self):
        text = 'enum class E : uint32_t\n{\n  A = SOME_OTHER_ENUM,\n  B,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {1: 'A', 2: 'B'})

    def test_line_without_trailing_comma_is_skipped(self):
        text = 'enum class E : uint32_t\n{\n  A = 3,\n  B\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {3: 'A'})

    def test_non_matching_lines_are_skipped(self):
        text = 'enum class E : uint32_t\n{\n  int Fake = 1;\n  A = 2,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'E'), {2: 'A'})

    def test_missing_enum_returns_empty(self):
        self.assertEqual(R.parse_chunk_enum(ENUM_TEXT, 'NoSuchEnum'), {})
        self.assertEqual(R.parse_chunk_enum('', 'SystemChunk'), {})

    def test_plain_enum_without_class_does_not_match(self):
        text = 'enum SystemChunk : uint32_t\n{\n  A = 1,\n};\n'
        self.assertEqual(R.parse_chunk_enum(text, 'SystemChunk'), {})

    def test_only_the_named_enum_is_parsed(self):
        text = ('enum class A : uint32_t\n{\n  X = 1,\n};\n'
                'enum class B : uint32_t\n{\n  Y = 2,\n};\n')
        self.assertEqual(R.parse_chunk_enum(text, 'B'), {2: 'Y'})


class TestLoadChunkNames(TempDirCase):
    #: root of the fake `renderdoc-src` tree and the {id: name} map parsed from it.
    src: str
    names: Dict[int, str]

    def setUp(self) -> None:
        super().setUp()
        self.src, self.names = F.make_fake_src(os.path.join(self.tmp, 'renderdoc-src'))

    def test_system_and_driver_enums_are_merged(self):
        self.assertEqual(self.names[1], 'DriverInit')
        self.assertEqual(self.names[3], 'InitialContents')
        # the driver enum starts at FirstDriverChunk and overwrites the sentinel entry
        self.assertEqual(self.names[1000], 'SetName')
        self.assertEqual(self.names[1001], 'PushMarker')
        self.assertEqual(self.names[1002], 'SetMarker')
        self.assertEqual(self.names[1003], 'PopMarker')

    def test_explicit_values_resume_counting(self):
        self.assertEqual(self.names[1200], 'List_SetPipelineState')
        self.assertEqual(self.names[1201], 'List_SetGraphicsRootSignature')
        self.assertEqual(self.names[1212], 'List_DrawIndexedInstanced')
        self.assertEqual(self.names[1214], 'List_ExecuteIndirect')

    def test_driver_parameter_switches_the_header(self):
        names = R.load_chunk_names(self.src, 'D3D11')
        self.assertEqual(names[1], 'DriverInit')
        self.assertEqual(names[1001], 'PushMarker')
        self.assertEqual(names[1002], 'DrawIndexed')

    def test_missing_source_warns_once_on_stderr(self):
        with mock.patch.object(R, '_SRC_WARNED', False):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                names = R.load_chunk_names(os.path.join(self.tmp, 'does-not-exist'))
                self.assertEqual(names, {})
                first = err.getvalue()
            err2 = io.StringIO()
            with contextlib.redirect_stderr(err2):
                R.load_chunk_names(os.path.join(self.tmp, 'does-not-exist'))
            self.assertIn('warning: RenderDoc source not found', first)
            self.assertIn('renderdoc-src', first)
            self.assertEqual(err2.getvalue(), '')

    def test_missing_driver_header_still_returns_system_chunks(self):
        os.remove(os.path.join(self.src, 'renderdoc', 'driver', 'd3d12', 'd3d12_common.h'))
        with mock.patch.object(R, '_SRC_WARNED', False):
            names = R.load_chunk_names(self.src)
        self.assertEqual(names[1], 'DriverInit')
        self.assertEqual(names[1000], 'FirstDriverChunk')
        self.assertNotIn('PushMarker', names.values())

    def test_missing_core_header_still_returns_driver_chunks(self):
        os.remove(os.path.join(self.src, 'renderdoc', 'core', 'core.h'))
        with mock.patch.object(R, '_SRC_WARNED', False):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                names = R.load_chunk_names(self.src)
        self.assertIn('warning', err.getvalue())
        self.assertEqual(names[1001], 'PushMarker')
        self.assertNotIn('DriverInit', names.values())


class TestFindRenderdocSrc(unittest.TestCase):
    #: tool folder plus the two `renderdoc-src` locations the search order tries.
    here: str
    tool_candidate: str
    parent_candidate: str

    def setUp(self) -> None:
        self.here = os.path.dirname(os.path.abspath(R.__file__))
        self.tool_candidate = os.path.join(self.here, 'renderdoc-src')
        self.parent_candidate = os.path.join(os.path.dirname(self.here), 'renderdoc-src')

    def core_of(self, root: str) -> str:
        return os.path.join(root, 'renderdoc', 'core', 'core.h')

    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {'RENDERDOC_SRC': r'D:\src\renderdoc'}):
            with mock.patch.object(os.path, 'isfile', lambda p: p == self.core_of(r'D:\src\renderdoc')):
                self.assertEqual(R._find_renderdoc_src(), r'D:\src\renderdoc')

    def test_falls_through_to_tool_folder(self):
        with mock.patch.dict(os.environ, {'RENDERDOC_SRC': r'D:\nope'}):
            with mock.patch.object(os.path, 'isfile', lambda p: p == self.core_of(self.tool_candidate)):
                self.assertEqual(R._find_renderdoc_src(), self.tool_candidate)

    def test_falls_through_to_parent_folder(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(os.path, 'isfile', lambda p: p == self.core_of(self.parent_candidate)):
                self.assertEqual(R._find_renderdoc_src(), self.parent_candidate)

    def test_documented_fallback_when_nothing_exists(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(os.path, 'isfile', lambda p: False):
                self.assertEqual(R._find_renderdoc_src(), self.tool_candidate)

    def test_empty_env_var_is_ignored(self):
        with mock.patch.dict(os.environ, {'RENDERDOC_SRC': ''}, clear=True):
            with mock.patch.object(os.path, 'isfile', lambda p: False):
                self.assertEqual(R._find_renderdoc_src(), self.tool_candidate)


# =========================================================================== chunk iteration
class TestIterChunks(unittest.TestCase):
    def test_bare_chunk(self):
        data = F.stream(F.chunk(1234, b'abc'))
        chunks = list(R.iter_chunks(data))
        self.assertEqual(len(chunks), 1)
        ch = chunks[0]
        self.assertEqual(ch['id'], 1234)
        self.assertEqual(ch['off'], 0)
        self.assertEqual(ch['data'], 8)
        self.assertEqual(ch['length'], 3)
        self.assertEqual(ch['flags'], 0)
        self.assertEqual(data[ch['data']:ch['data'] + ch['length']], b'abc')

    def test_all_metadata_fields(self):
        data = F.stream(F.chunk(1200, b'payload', callstack=[0x7ff0, 0x1234], threadid=42,
                                duration=7, timestamp=99))
        ch = list(R.iter_chunks(data))[0]
        self.assertEqual(ch['id'], 1200)
        self.assertEqual(ch['flags'], 0x000F0000)
        self.assertEqual(ch['length'], 7)
        self.assertEqual(ch['data'], 4 + 4 + 16 + 8 + 8 + 8 + 4)
        self.assertEqual(data[ch['data']:ch['data'] + 7], b'payload')

    def test_individual_flag_combinations(self):
        cases = [
            (dict(threadid=1), 0x00020000, 4 + 8 + 4),
            (dict(duration=1), 0x00040000, 4 + 8 + 4),
            (dict(timestamp=1), 0x00080000, 4 + 8 + 4),
            (dict(callstack=[1]), 0x00010000, 4 + 4 + 8 + 4),
            (dict(callstack=[1], threadid=1, duration=1, timestamp=1), 0x000F0000, 4 + 4 + 8 + 8 + 8 + 8 + 4),
        ]
        for kwargs, flags, data_off in cases:
            with self.subTest(kwargs=kwargs):
                ch = list(R.iter_chunks(F.stream(F.chunk(1000, b'X', **kwargs))))[0]
                self.assertEqual(ch['flags'], flags)
                self.assertEqual(ch['data'], data_off)

    def test_zero_callstack_frames(self):
        # a chunk with the callstack flag but no frames: 36 bytes of header (README section 6)
        raw = F.u32b(1200 | F.FLAG_CALLSTACK | F.FLAG_THREADID | F.FLAG_DURATION | F.FLAG_TIMESTAMP)
        raw += F.u32b(0) + F.u64b(0) + F.u64b(0) + F.u64b(0) + F.u32b(2) + b'hi'
        ch = list(R.iter_chunks(F.pad_to(raw)))[0]
        self.assertEqual(ch['data'] - ch['off'], 36)
        self.assertEqual(ch['length'], 2)

    def test_64bit_length_flag(self):
        ch = list(R.iter_chunks(F.stream(F.chunk(1000, b'hello', size64=True))))[0]
        self.assertEqual(ch['flags'], R.CHUNK_64BITSIZE)
        self.assertEqual(ch['length'], 5)
        self.assertEqual(ch['data'], 4 + 8)          # u32 flags + u64 length, no other metadata

    def test_unknown_high_flag_bits_are_reported(self):
        raw = F.u32b(1000 | 0x00200000) + F.u32b(3) + b'abc'
        ch = list(R.iter_chunks(F.pad_to(raw)))[0]
        self.assertEqual(ch['flags'], 0x00200000)
        self.assertEqual(ch['length'], 3)

    def test_flags_are_masked_out_of_the_id(self):
        ch = list(R.iter_chunks(F.stream(F.chunk(0x0FFF, b'', threadid=1))))[0]
        self.assertEqual(ch['id'], 0x0FFF)

    def test_multiple_chunks_are_64_byte_aligned(self):
        data = F.stream(F.chunk(1000, b'A' * 5), F.chunk(1001, b'B' * 70), F.chunk(1002, b'C'))
        chunks = list(R.iter_chunks(data))
        self.assertEqual([c['id'] for c in chunks], [1000, 1001, 1002])
        self.assertEqual([c['off'] for c in chunks], [0, 64, 192])
        for c in chunks:
            self.assertEqual(c['off'] % 64, 0)
            self.assertEqual(data[c['data']:c['data'] + c['length']],
                             {1000: b'A' * 5, 1001: b'B' * 70, 1002: b'C'}[c['id']])

    def test_padding_bytes_are_not_part_of_the_payload(self):
        data = F.stream(F.chunk(1000, b'AB', pad_byte=0xCC), F.chunk(1001, b'CD'))
        chunks = list(R.iter_chunks(data))
        self.assertEqual(data[8:14], b'AB' + b'\xCC' * 4)      # padding follows the 8-byte header
        self.assertEqual([c['length'] for c in chunks], [2, 2])

    def test_zero_length_payload(self):
        ch = list(R.iter_chunks(F.stream(F.chunk(1000, b''))))[0]
        self.assertEqual(ch['length'], 0)
        self.assertEqual(ch['data'], 8)

    def test_limit_stops_iteration(self):
        data = F.stream(F.chunk(1000, b'a'), F.chunk(1001, b'b'), F.chunk(1002, b'c'))
        self.assertEqual([c['id'] for c in R.iter_chunks(data, limit=2)], [1000, 1001])
        self.assertEqual(len(list(R.iter_chunks(data, limit=0))), 3)

    def test_chunk_id_zero_terminates(self):
        data = F.stream(F.chunk(1000, b'a'), F.chunk(0, b'b'), F.chunk(1001, b'c'))
        self.assertEqual([c['id'] for c in R.iter_chunks(data)], [1000])

    def test_id_zero_with_flags_still_terminates(self):
        raw = F.u32b(F.FLAG_THREADID) + F.u64b(1) + F.u32b(0) + b'x'
        self.assertEqual(list(R.iter_chunks(raw)), [])

    def test_explicit_terminator_word(self):
        data = F.stream(F.chunk(1000, b'a'), terminator=True, tail=b'trailing garbage here')
        self.assertEqual([c['id'] for c in R.iter_chunks(data)], [1000])

    def test_length_past_end_stops(self):
        raw = F.u32b(1000) + F.u32b(9999) + b'short'
        self.assertEqual(list(R.iter_chunks(raw)), [])

    def test_stream_shorter_than_one_chunk(self):
        self.assertEqual(list(R.iter_chunks(b'')), [])
        self.assertEqual(list(R.iter_chunks(b'\x01\x02\x03')), [])

    def test_last_chunk_without_padding(self):
        ch = list(R.iter_chunks(F.chunk(1000, b'xyz', align=False)))[0]
        self.assertEqual(ch['length'], 3)
        self.assertEqual(ch['data'], 8)


class TestChunkPayloadAndStrings(unittest.TestCase):
    def make(self, payload: bytes, **kw: Any) -> tuple[bytes, R.ChunkInfo]:
        data = F.stream(F.chunk(1000, payload, **kw))
        return data, list(R.iter_chunks(data))[0]

    def test_payload_slice(self):
        data, ch = self.make(b'payload-bytes')
        self.assertEqual(R.chunk_payload(data, ch), b'payload-bytes')
        self.assertEqual(len(R.chunk_payload(data, ch)), ch['length'])

    def test_payload_of_empty_chunk(self):
        data, ch = self.make(b'')
        self.assertEqual(R.chunk_payload(data, ch), b'')

    def test_strings_ascii(self):
        data, ch = self.make(b'\x00Alphaaa\x00Betaaaa\x00')
        self.assertEqual(R.chunk_strings(data, ch), ['Alphaaa', 'Betaaaa'])

    def test_strings_minlen(self):
        data, ch = self.make(b'\x00abcdef\x00abcdefgh\x00')
        self.assertEqual(R.chunk_strings(data, ch, minlen=8), ['abcdefgh'])
        self.assertEqual(R.chunk_strings(data, ch, minlen=6), ['abcdef', 'abcdefgh'])

    def test_minlen_is_honoured_exactly(self):
        # the scanner builds the run pattern for the requested length, so short strings are found
        data, ch = self.make(b'\x00abcde\x00abcd\x00abc\x00')
        self.assertEqual(R.chunk_strings(data, ch, minlen=5), ['abcde'])
        self.assertEqual(R.chunk_strings(data, ch, minlen=4), ['abcde', 'abcd'])
        self.assertEqual(R.chunk_strings(data, ch, minlen=3), ['abcde', 'abcd', 'abc'])
        self.assertEqual(R.chunk_strings(data, ch, minlen=2), ['abcde', 'abcd', 'abc'])

    def test_short_utf16_strings_are_found_too(self):
        data, ch = self.make(b'\x01\x02' + 'Frm'.encode('utf-16-le') + b'\x01\x02')
        self.assertIn('Frm', R.chunk_strings(data, ch, minlen=3))
        self.assertEqual(R.chunk_strings(data, ch, minlen=4), [])

    def test_strings_are_deduplicated(self):
        data, ch = self.make(b'Dupdup\x00Dupdup\x00Dupdup\x00')
        self.assertEqual(R.chunk_strings(data, ch), ['Dupdup'])

    def test_strings_limit(self):
        data, ch = self.make(b'\x00oneone\x00twotwo\x00threethree\x00fourfour\x00')
        self.assertEqual(R.chunk_strings(data, ch, limit=2), ['oneone', 'twotwo'])

    def test_strings_utf16le(self):
        wide = 'WideMarker'.encode('utf-16-le')
        data, ch = self.make(b'\x01\x02' + wide + b'\x01\x02')
        self.assertIn('WideMarker', R.chunk_strings(data, ch))

    def test_strings_empty_payload(self):
        data, ch = self.make(b'')
        self.assertEqual(R.chunk_strings(data, ch), [])


class TestStringRuns(unittest.TestCase):
    def test_offsets_and_text(self):
        # 0-1 pad, 2-8 'Alphaaa', 9-10 pad, 11-16 'Betaxx', 17 pad
        blob = b'\x00\x00Alphaaa\x00\x00Betaxx\x00'
        self.assertEqual(list(R.string_runs(blob, 6)),
                         [(2, 'Alphaaa'), (11, 'Betaxx')])

    def test_offsets_are_absolute_when_a_window_is_given(self):
        blob = b'\x00\x00Alphaaa\x00\x00Betaxx\x00'
        self.assertEqual(list(R.string_runs(blob, 6, 11, 17)), [(11, 'Betaxx')])
        self.assertEqual(list(R.string_runs(blob, 6, 0, 9)), [(2, 'Alphaaa')])
        # a run that only partially fits the window is not reported
        self.assertEqual(list(R.string_runs(blob, 6, 11, 16)), [])

    def test_minlen_is_exact(self):
        blob = b'ab\x00abc\x00abcd\x00'
        self.assertEqual([s for _, s in R.string_runs(blob, 2)], ['ab', 'abc', 'abcd'])
        self.assertEqual([s for _, s in R.string_runs(blob, 3)], ['abc', 'abcd'])
        self.assertEqual([s for _, s in R.string_runs(blob, 5)], [])

    def test_minlen_below_one_is_clamped(self):
        self.assertEqual([s for _, s in R.string_runs(b'a\x00b\x00', 0)], ['a', 'b'])
        self.assertEqual([s for _, s in R.string_runs(b'a\x00b\x00', -5)], ['a', 'b'])

    def test_patterns_are_cached_per_length(self):
        self.assertIs(R._run_pattern(6), R.STR_RE)
        self.assertIs(R._run_pattern(3), R._run_pattern(3))
        self.assertIsNot(R._run_pattern(3), R._run_pattern(4))

    def test_no_runs(self):
        self.assertEqual(list(R.string_runs(b'\x00\x01\x02', 1)), [])
        self.assertEqual(list(R.string_runs(b'', 1)), [])

    def test_non_ascii_bytes_split_runs(self):
        self.assertEqual([s for _, s in R.string_runs(b'abc\xffdef\x00', 3)], ['abc', 'def'])

# =========================================================================== payload decoding
class TestDecodeChunk(unittest.TestCase):
    def test_set_pipeline_state(self):
        self.assertEqual(R.decode_chunk('List_SetPipelineState', F.pl_pso(7, 3042)),
                         ['cmdList=7 pso=3042'])

    def test_set_pipeline_state_truncated(self):
        self.assertEqual(R.decode_chunk('List_SetPipelineState', F.pl_pso(7, 3042)[:15]), [])

    def test_draw_indexed_instanced(self):
        blob = F.pl_draw_indexed(7, 2880, 1, 12, 4294967295, 2)
        self.assertEqual(R.decode_chunk('List_DrawIndexedInstanced', blob),
                         ['cmdList=7 idx=2880 inst=1 startIdx=12 baseVtx=4294967295 startInst=2'])

    def test_draw_indexed_instanced_truncated(self):
        self.assertEqual(R.decode_chunk('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 1, 1, 0, 0, 0)[:27]), [])

    def test_draw_instanced(self):
        self.assertEqual(R.decode_chunk('List_DrawInstanced', F.pl_draw_instanced(7, 3, 2, 5, 0)),
                         ['cmdList=7 verts=3 inst=2 startVtx=5 startInst=0'])

    def test_draw_instanced_truncated(self):
        self.assertEqual(R.decode_chunk('List_DrawInstanced', F.pl_draw_instanced(7, 3, 2, 5, 0)[:23]), [])

    def test_dispatch(self):
        self.assertEqual(R.decode_chunk('List_Dispatch', F.pl_dispatch(7, 8, 4, 1)),
                         ['cmdList=7 x=8 y=4 z=1'])

    def test_dispatch_truncated(self):
        self.assertEqual(R.decode_chunk('List_Dispatch', F.pl_dispatch(7, 8, 4, 1)[:19]), [])

    def test_root_constant_buffer_view(self):
        # D3D12BufferLocation serialises as (resourceId, byteOffset) - d3d12_serialise.cpp - which is
        # exactly the pair `draws` prints, so both commands now report the same thing
        blob = F.pl_root_view(7, 10, 1907, 0x120000)
        self.assertEqual(R.decode_chunk('List_SetGraphicsRootConstantBufferView', blob),
                         ['cmdList=7 rootParam=10 res=1907+0x120000'])

    def test_root_srv_uav_views(self):
        for name in ('List_SetGraphicsRootShaderResourceView', 'List_SetGraphicsRootUnorderedAccessView'):
            with self.subTest(name=name):
                blob = F.pl_root_view(7, 3, 256, 0x40)
                self.assertEqual(R.decode_chunk(name, blob), ['cmdList=7 rootParam=3 res=256+0x40'])

    def test_root_view_truncated(self):
        blob = F.pl_root_view(7, 3, 256, 0x40)[:27]      # one byte short of the 28-byte payload
        for name in ('List_SetGraphicsRootConstantBufferView', 'List_SetGraphicsRootShaderResourceView'):
            with self.subTest(name=name):
                self.assertEqual(R.decode_chunk(name, blob), [])

    def test_descriptor_table(self):
        self.assertEqual(R.decode_chunk('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 5, 0xDEADBEEF)),
                         ['cmdList=7 rootParam=5 gpuHandle=0xdeadbeef'])

    def test_descriptor_table_truncated(self):
        self.assertEqual(R.decode_chunk('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 5, 1)[:15]), [])

    def test_root_signature(self):
        self.assertEqual(R.decode_chunk('List_SetGraphicsRootSignature', F.pl_root_signature(7, 42)),
                         ['cmdList=7 rootSig=42'])

    def test_vertex_buffers(self):
        blob = F.pl_vertex_buffers(7, 1, [(315, 0x3F7400, 6708, 12), (0, 0, 0, 0)])
        self.assertEqual(R.decode_chunk('List_IASetVertexBuffers', blob),
                         ['cmdList=7 startSlot=1 numViews=2',
                          '    view[1] res=315 VA=0x3f7400 size=6708 stride=12',
                          '    view[2] res=0 VA=0x0 size=0 stride=0'])

    def test_vertex_buffers_no_views(self):
        self.assertEqual(R.decode_chunk('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [])),
                         ['cmdList=7 startSlot=0 numViews=0'])

    def test_vertex_buffers_capped_at_16_views(self):
        views = [(i + 1, i * 4, 100, 8) for i in range(20)]
        out = R.decode_chunk('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, views))
        self.assertEqual(len(out), 17)
        self.assertIn('view[15]', out[-1])
        self.assertNotIn('view[16]', ' '.join(out))

    def test_vertex_buffers_truncated_view_list(self):
        blob = F.pl_vertex_buffers(7, 0, [(315, 16, 32, 4)]) + b'\x00' * 8   # numViews lies
        blob = F.u64b(7) + F.u32b(0) + F.u32b(4) + F.u64b(4) + blob[24:24 + 24]
        out = R.decode_chunk('List_IASetVertexBuffers', blob)
        self.assertEqual(len(out), 2)

    def test_vertex_buffers_truncated_header(self):
        self.assertEqual(R.decode_chunk('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [])[:23]), [])

    def test_index_buffer(self):
        # [u64 cmdList][u8 present][u64 resId][u64 offset][u32 size][u32 fmt] = 33 bytes; the present
        # bool comes from SERIALISE_ELEMENT_OPT, so every field is one byte later than the struct alone
        blob = F.pl_index_buffer(7, 315, 0x3FA60000, 5760, 57)
        self.assertEqual(len(blob), 33)
        self.assertEqual(R.decode_chunk('List_IASetIndexBuffer', blob),
                         ['cmdList=7 res=315+0x3fa60000 size=5760 fmt=57'])

    def test_index_buffer_without_the_present_flag_is_not_decoded(self):
        # the 32-byte form (struct without the present bool) is not a valid payload
        blob = F.u64b(7) + F.u64b(315) + F.u64b(0x3FA60000) + F.u32b(5760) + F.u32b(57)
        self.assertEqual(len(blob), 32)
        self.assertEqual(R.decode_chunk('List_IASetIndexBuffer', blob), [])
        self.assertEqual(R.decode_chunk('List_IASetIndexBuffer', F.pl_index_buffer(7, 1, 2, 3, 4)[:32]), [])

    def test_index_buffer_bytes_from_a_real_capture(self):
        # raw payload of List_IASetIndexBuffer #1122 in "PC Renderer.rdc" (RenderDoc 1.46, D3D12):
        # the draw that uses it is `idx=2880`, and size == 2880 * 2 with fmt == R16_UINT, so this
        # also cross-checks the offsets against the capture rather than against our own fixture
        blob = bytes.fromhex(
            '4409000000000000013b010000000000'      # cmdList=2372, present=1, resId=315
            '0000a63f00000000008016000039000000')   # offset=0x3fa600, size=5760, format=57
        self.assertEqual(len(blob), 33)
        self.assertEqual(R.decode_chunk('List_IASetIndexBuffer', blob),
                         ['cmdList=2372 res=315+0x3fa600 size=5760 fmt=57'])
        self.assertEqual(R.u32(blob, 25), 2880 * 2)          # indexCount * sizeof(R16_UINT)

    def test_null_index_buffer_view(self):
        blob = F.pl_index_buffer(7, 0, 0, 0, 0, present=False)
        self.assertEqual(len(blob), 9)
        self.assertEqual(R.decode_chunk('List_IASetIndexBuffer', blob),
                         ['cmdList=7 (null index buffer view)'])

    def test_index_buffer_truncated(self):
        self.assertEqual(R.decode_chunk('List_IASetIndexBuffer', F.u64b(7) + F.u64b(1) + F.u32b(2)), [])

    def test_create_pipeline_state(self):
        blob = F.pl_create_pso(3042, b'\xAB' * 24)
        self.assertEqual(R.decode_chunk('Device_CreatePipelineState', blob),
                         ['payload %d bytes; tail=%s' % (len(blob), ('ab' * 24))])

    def test_create_pipeline_state_truncated(self):
        self.assertEqual(R.decode_chunk('Device_CreatePipelineState', b'\x00' * 7), [])

    def test_initial_contents(self):
        blob = F.pl_initial_contents(342, F.SINGLEPROBE_SIG)
        expected = 'id=342 hdr=%s' % blob[8:40].hex()
        self.assertEqual(R.decode_chunk('InitialContents', blob), [expected])
        self.assertEqual(R.decode_chunk('InitialContentsList', blob), [expected])

    def test_initial_contents_truncated(self):
        self.assertEqual(R.decode_chunk('InitialContents', b'\x00' * 31), [])

    def test_unknown_chunk_name(self):
        self.assertEqual(R.decode_chunk('List_SomethingElse', b'\x00' * 64), [])
        self.assertEqual(R.decode_chunk('', b'\x00' * 64), [])

    def test_none_name_is_tolerated(self):
        self.assertEqual(R.decode_chunk(None, b'\x00' * 64), [])

    def test_decode_error_is_reported_not_raised(self):
        # deliberately the wrong type: decode_chunk must catch broadly and report, never raise
        out = R.decode_chunk('List_SetPipelineState', 'x' * 16)  # type: ignore[arg-type]
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith('decode error: '), out)


# =========================================================================== DXBC containers
class TestParseDxilContainers(unittest.TestCase):
    def test_single_container(self):
        blob = F.dxbc([('RDEF', b'RDEFDATA'), ('ILDN', b'ILDNAB')])
        out = list(R.parse_dxil_containers(blob))
        self.assertEqual(len(out), 1)
        off, size, h, parts = out[0]
        self.assertEqual(off, 0)
        self.assertEqual(size, len(blob))
        self.assertEqual(h, '11' * 16)
        self.assertEqual([p[0] for p in parts], ['RDEF', 'ILDN'])
        self.assertEqual(blob[parts[0][1]:parts[0][1] + parts[0][2]], b'RDEFDATA')
        self.assertEqual(blob[parts[1][1]:parts[1][1] + parts[1][2]], b'ILDNAB')

    def test_hash_comes_from_bytes_4_to_20(self):
        blob = F.dxbc([('RDEF', b'')], hash_=bytes(range(16)))
        self.assertEqual(list(R.parse_dxil_containers(blob))[0][2], '000102030405060708090a0b0c0d0e0f')

    def test_size_ignores_the_header_size_field(self):
        blob = F.dxbc([('RDEF', b'1234')], size=99999)
        self.assertEqual(list(R.parse_dxil_containers(blob))[0][1], len(blob))

    def test_container_offset_inside_a_stream(self):
        prefix = b'\xCC' * 77
        out = list(R.parse_dxil_containers(prefix + F.dxbc([('RDEF', b'X')])))
        self.assertEqual(out[0][0], 77)
        self.assertEqual(out[0][3][0][1], 77 + 32 + 4 + 8)

    def test_multiple_containers(self):
        blob = F.dxbc([('RDEF', b'one')]) + b'gap' + F.dxbc([('ILDN', b'two')])
        out = list(R.parse_dxil_containers(blob))
        self.assertEqual(len(out), 2)
        self.assertEqual([p[0] for _, _, _, parts in out for p in parts], ['RDEF', 'ILDN'])

    def test_no_containers(self):
        self.assertEqual(list(R.parse_dxil_containers(b'nothing to see here')), [])

    def test_dxbc_magic_at_the_very_end(self):
        self.assertEqual(list(R.parse_dxil_containers(b'pad' + b'DXBC')), [])

    def test_truncated_header_is_skipped_but_search_continues(self):
        broken = b'DXBC' + b'\x00' * 10
        good = F.dxbc([('RDEF', b'ok')])
        out = list(R.parse_dxil_containers(broken + good))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], len(broken))

    def test_part_count_zero_is_skipped(self):
        broken = F.dxbc([('RDEF', b'x')], part_count=0)
        out = list(R.parse_dxil_containers(broken + F.dxbc([('RDEF', b'y')])))
        self.assertEqual(len(out), 1)

    def test_part_count_over_64_is_skipped(self):
        broken = F.dxbc([('RDEF', b'x')], part_count=65)
        self.assertEqual(list(R.parse_dxil_containers(broken)), [])

    def test_part_offset_table_past_end_is_skipped(self):
        # partCount says 8 but the offset table runs off the end of the stream
        blob = b'DXBC' + b'\x00' * 16 + F.u32b(0x40) + F.u32b(100) + F.u32b(8) + b'\x00' * 8
        self.assertEqual(list(R.parse_dxil_containers(blob)), [])

    def test_non_printable_fourcc_is_skipped(self):
        blob = F.dxbc([('RDEF', b'x')])
        blob = blob[:32] + b'\x00\x01\x02\x03' + F.u32b(1) + b'x'
        self.assertEqual(list(R.parse_dxil_containers(blob)), [])

    def test_part_length_past_end_is_skipped(self):
        blob = F.dxbc([('RDEF', b'x')])
        blob = blob[:32] + b'RDEF' + F.u32b(9999) + b'x'
        self.assertEqual(list(R.parse_dxil_containers(blob)), [])

    def test_empty_part_data(self):
        out = list(R.parse_dxil_containers(F.dxbc([('RDEF', b'')])))
        # 32-byte container header + 4-byte offset table + 4-byte fourcc + 4-byte length
        self.assertEqual(out[0][3], [('RDEF', 44, 0)])
        self.assertEqual(out[0][1], 44)

    def test_nested_magic_is_found_too(self):
        inner = F.dxbc([('RDEF', b'inner')])
        outer = F.dxbc([('RDAT', inner)])
        out = list(R.parse_dxil_containers(outer))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1][0], 32 + 4 + 8)

    def test_rts0_part_is_recognised(self):
        out = list(R.parse_dxil_containers(F.dxbc([('RTS0', b'\x02\x00\x00\x00')])))
        self.assertIn('RTS0', [p[0] for p in out[0][3]])


class TestPartStrings(unittest.TestCase):
    def test_strings_inside_window(self):
        blob = b'\x00\x00alphaxx\x00\x00betaxx\x00\x00'
        self.assertEqual(R.part_strings(blob, 2, 15, 6), ['alphaxx', 'betaxx'])

    def test_minlen_filters(self):
        blob = b'abcdef\x00abcdefgh\x00'
        self.assertEqual(R.part_strings(blob, 0, len(blob), 8), ['abcdefgh'])
        self.assertEqual(R.part_strings(blob, 0, len(blob), 6), ['abcdef', 'abcdefgh'])

    def test_minlen_is_honoured_exactly(self):
        blob = b'abcde\x00abcd\x00abc\x00'
        self.assertEqual(R.part_strings(blob, 0, len(blob), 5), ['abcde'])
        self.assertEqual(R.part_strings(blob, 0, len(blob), 4), ['abcde', 'abcd'])
        self.assertEqual(R.part_strings(blob, 0, len(blob), 3), ['abcde', 'abcd', 'abc'])

    def test_window_bounds_are_respected(self):
        blob = b'aaaaaaaa\x00bbbbbbbb\x00'
        self.assertEqual(R.part_strings(blob, 0, 8, 6), ['aaaaaaaa'])
        self.assertEqual(R.part_strings(blob, 9, 8, 6), ['bbbbbbbb'])

    def test_out_of_range_window_is_empty(self):
        self.assertEqual(R.part_strings(b'abcdef', 100, 10, 6), [])

    def test_zero_length_window(self):
        self.assertEqual(R.part_strings(b'abcdef', 0, 0, 6), [])


# =========================================================================== signatures
class TestParseSignature(unittest.TestCase):
    def test_short_blob(self):
        self.assertEqual(R.parse_signature(b''), [])
        self.assertEqual(R.parse_signature(b'\x01\x00\x00\x00'), [])

    def test_count_over_64_is_rejected(self):
        self.assertEqual(R.parse_signature(F.u32b(65) + b'\x00' * 4 + b'\x00' * (65 * 24)), [])

    def test_zero_count(self):
        self.assertEqual(R.parse_signature(F.u32b(0) + F.u32b(8)), [])

    def test_entries_with_string_table(self):
        blob = F.signature([('POSITION', 0, 0), ('TEXCOORD6', 1, 3)])
        self.assertEqual(R.parse_signature(blob), [('POSITION', 0, 0), ('TEXCOORD6', 1, 3)])

    def test_truncated_element_list_breaks(self):
        # count says 5 but only 2 elements are present: the loop stops at the end of the blob and the
        # string-table guess (8 + count*24) no longer lands on the names, so they come out as '?'
        blob = F.signature([('POSITION', 0, 0), ('TEXCOORD0', 0, 1)], count_override=5)
        self.assertEqual(R.parse_signature(blob), [('?', 0, 0), ('?', 0, 1)])

    def test_name_offsets_relative_to_the_element_array(self):
        # names live at 8+name_off: the strtab guess misses, the base-8 fallback resolves it
        elems = F.u32b(24) + F.u32b(0) + F.u32b(0) + b'\x00' * 12
        blob = F.u32b(1) + F.u32b(0) + elems + b'HELLO\x00'
        self.assertEqual(R.parse_signature(blob), [('HELLO', 0, 0)])

    def test_declared_string_table_offset_is_used_as_a_fallback(self):
        elems = (F.u32b(0) + F.u32b(0) + F.u32b(0) + b'\x00' * 12
                 + F.u32b(0) + F.u32b(0) + F.u32b(0) + b'\x00' * 12)
        blob = F.u32b(2) + F.u32b(32) + elems + b'HELLO\x00'
        self.assertEqual(R.parse_signature(blob)[0], ('HELLO', 0, 0))

    def test_unresolvable_name_becomes_question_mark(self):
        elems = F.u32b(0) + F.u32b(0) + F.u32b(0) + b'\x00' * 12
        blob = F.u32b(1) + F.u32b(0) + elems + b'\x01\x02\x03\x04\x05\x06'
        self.assertEqual(R.parse_signature(blob), [('?', 0, 0)])

    def test_non_printable_candidate_is_rejected(self):
        elems = F.u32b(0) + F.u32b(0) + F.u32b(0) + b'\x00' * 12
        blob = F.u32b(1) + F.u32b(0) + elems + b'AB\x01CD\x00'
        self.assertEqual(R.parse_signature(blob), [('?', 0, 0)])

    def test_name_without_nul_terminator_reads_32_bytes(self):
        elems = F.u32b(0) + F.u32b(0) + F.u32b(0) + b'\x00' * 12
        blob = F.u32b(1) + F.u32b(0) + elems + b'ABCDEF'
        self.assertEqual(R.parse_signature(blob), [('ABCDEF', 0, 0)])

    def test_semantic_index_and_register_are_decoded(self):
        blob = F.signature([('TEXCOORD', 7, 12)])
        self.assertEqual(R.parse_signature(blob), [('TEXCOORD', 7, 12)])


# =========================================================================== real source tree
class TestRealRenderdocSource(unittest.TestCase):
    """Integration check against the real `renderdoc-src` checkout, when present."""

    def setUp(self):
        src = R.RENDERDOC_SRC
        if not os.path.isfile(os.path.join(src, 'renderdoc', 'core', 'core.h')):
            self.skipTest('renderdoc-src not present next to the tool')

    def test_system_chunks_parse(self):
        names = R.load_chunk_names()
        self.assertEqual(names[1], 'DriverInit')
        self.assertEqual(names[3], 'InitialContents')
        self.assertEqual(names[1000], 'SetName')

    def test_driver_chunks_parse(self):
        names = R.load_chunk_names()
        values = set(names.values())
        for expected in ('PushMarker', 'SetMarker', 'PopMarker', 'List_DrawIndexedInstanced',
                         'List_DrawInstanced', 'List_Dispatch', 'List_SetPipelineState',
                         'List_IASetVertexBuffers', 'List_IASetIndexBuffer',
                         'List_SetGraphicsRootConstantBufferView', 'Device_CreatePipelineState'):
            self.assertIn(expected, values)

    def test_names_used_by_the_tool_resolve(self):
        names = R.load_chunk_names()
        values = set(names.values())
        for chunk_set in (R.MARKER_CHUNKS, R.DRAW_CHUNKS):
            for name in chunk_set:
                self.assertIn(name, values, '%s is not in the parsed enum' % name)

    def test_d3d12_chunk_ids_start_at_firstdriverchunk(self):
        names = R.load_chunk_names()
        ids = [i for i, n in names.items() if n == 'PushMarker']
        self.assertEqual(ids, [1001])


if __name__ == '__main__':
    unittest.main(verbosity=2)
