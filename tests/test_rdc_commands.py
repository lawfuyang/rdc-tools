"""Tests for every `cmd_*` command in rdc_analysis.py, plus the main() dispatch.

The chunk-name map is stubbed with a fake `renderdoc-src` tree (see rdc_fixtures) so the tests are
hermetic: no capture files, no RenderDoc checkout, no GPU. Chunk ids are looked up by name, so the
assertions talk about `List_DrawIndexedInstanced`, not about 1212.

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_commands.py
    python -m unittest tests.test_rdc_commands
    python rdc_analysis.py selftest -k Draws
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import struct
import sys
import tempfile
import unittest
from typing import Any, Callable, ClassVar, Dict
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402
import rdc_fixtures as F          # noqa: E402
from test_rdc_analysis import TempDirCase, capture_text, capture_all   # noqa: E402


class CmdCase(TempDirCase):
    """Base class that points the tool at a fake RenderDoc source tree."""

    #: populated in `setUpClass` for every test in the class.
    _src_dir: ClassVar[str]
    src_root: ClassVar[str]
    names: ClassVar[Dict[int, str]]
    ids: ClassVar[Dict[str, int]]

    @classmethod
    def setUpClass(cls) -> None:
        cls._src_dir = tempfile.mkdtemp(prefix='rdc_src_')
        cls.src_root, cls.names = F.make_fake_src(cls._src_dir)
        cls.ids = F.invert(cls.names)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._src_dir, ignore_errors=True)

    def setUp(self) -> None:
        super().setUp()
        names = mock.patch.object(R, 'load_chunk_names',
                                  lambda src_root=None, driver='D3D12': dict(self.names))
        src = mock.patch.object(R, 'RENDERDOC_SRC', self.src_root)
        names.start()
        src.start()
        self.addCleanup(names.stop)
        self.addCleanup(src.stop)

    def ch(self, name: str, payload: bytes = b'', **kw: Any) -> bytes:
        return F.chunk(self.ids[name], payload, **kw)

    def cap(self, *chunks: bytes, **kw: Any) -> str:
        return self.path('capture.rdc', F.capture(chunks, **kw))

    def out(self, fn: Callable[..., object], *args: Any, **kwargs: Any) -> str:
        return capture_text(fn, *args, **kwargs)

    def out_and_code(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run `fn`, returning `(stdout, its return value)` -- for the commands that return a code."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fn(*args, **kwargs)
        return buf.getvalue(), code

    def line_with(self, text: str, needle: str) -> str:
        for line in text.splitlines():
            if needle in line:
                return line
        raise AssertionError('no line containing %r in:\n%s' % (needle, text))


# =========================================================================== container commands
class TestCmdSections(CmdCase):
    def test_reports_header_sections_and_stream(self):
        stream = F.stream(self.ch('PushMarker', b'BasePass\x00'))
        data = F.rdc([F.section('FrameCapture', stream),
                      F.section('Bookmarks', b'B' * 16, sec_type=3, flags=2)])
        path = self.path('c.rdc', data)
        out = self.out(R.cmd_sections, path)
        self.assertIn('file        : %s' % path, out)
        self.assertIn('rdc version : 0x10e   progVersion 1.46', out)
        self.assertIn('thumbnail   : 64x64 9 bytes', out)
        self.assertIn('driver      : D3D12 (id=4)', out)
        self.assertIn('FrameCapture', out)
        self.assertIn('Bookmarks', out)
        self.assertIn('type=3', out)
        self.assertIn('flags=0x2', out)
        self.assertIn('framecapture stream: %d bytes  (expected %d)  [raw]' % (len(stream), len(stream)),
                      out)

    def test_lz4_stream_is_reported(self):
        chunks = [self.ch('PushMarker', b'BasePass\x00')]
        path = self.cap(*chunks, lz4=True, block_count=2)
        out = self.out(R.cmd_sections, path)
        self.assertIn('[lz4(2 blocks)]', out)
        self.assertIn('framecapture stream: %d bytes' % len(b''.join(chunks)), out)

    def test_container_without_sections_raises(self):
        # documents the unguarded info['sections'][0] access
        path = self.path('empty.rdc', F.rdc([]))
        with self.assertRaises(IndexError):
            self.out(R.cmd_sections, path)

    def test_second_run_is_served_from_the_cache(self):
        chunks = [self.ch('PushMarker', b'BasePass\x00')]
        path = self.cap(*chunks, lz4=True, block_count=2)
        first = self.out(R.cmd_sections, path)
        second = self.out(R.cmd_sections, path)
        self.assertIn('[lz4(2 blocks)]', first)
        self.assertIn('[lz4(2 blocks, cached)]', second)
        expected = 'framecapture stream: %d bytes  (expected %d)' % (len(b''.join(chunks)),
                                                                    len(b''.join(chunks)))
        self.assertIn(expected, first)
        self.assertIn(expected, second)


class TestCmdBlocks(CmdCase):
    def test_lists_every_section_with_its_first_bytes(self):
        data = F.rdc([F.section('FrameCapture', b'A' * 32),
                      F.section('Notes', b'B' * 32, sec_type=4, flags=2)])
        out = self.out(R.cmd_blocks, self.path('c.rdc', data))
        self.assertIn('first16=' + '41' * 16, out)
        self.assertIn('first16=' + '42' * 16, out)
        self.assertIn('flags=0x0', out)
        self.assertIn('flags=0x2', out)
        self.assertIn('FrameCapture', out)
        self.assertIn('Notes', out)

    def test_zstd_magic_is_visible(self):
        data = F.rdc([F.section('FrameCapture', R.ZSTD_MAGIC + b'x' * 20)])
        self.assertIn('first16=28b52ffd', self.out(R.cmd_blocks, self.path('c.rdc', data)))

    def test_no_sections_prints_nothing(self):
        self.assertEqual(self.out(R.cmd_blocks, self.path('e.rdc', F.rdc([]))).strip(), '')

    def test_blocks_does_not_decompress_and_leaves_no_cache_file(self):
        # it only reads the section headers, so it stays instant and must not fill the cache
        path = self.cap(self.ch('PushMarker', b'Marker\x00'), lz4=True)
        self.out(R.cmd_blocks, path)
        self.assertEqual(R.cache_entries(), [])


class TestCmdCache(CmdCase):
    def garbage(self) -> str:
        """A file in the cache directory that is not a cache file."""
        os.makedirs(R.cache_dir(), exist_ok=True)
        return F.write_bytes(os.path.join(R.cache_dir(), 'garbage' + R.CACHE_SUFFIX), b'nope')

    def populated(self) -> str:
        """A capture whose stream has been cached, and the capture path."""
        path = self.cap(self.ch('PushMarker', b'Marker\x00'), lz4=True)
        R.load_stream(path)
        return path

    def test_empty_cache(self):
        out = self.out(R.cmd_cache)
        self.assertIn('cache dir : %s' % R.cache_dir(), out)
        self.assertIn('entries   : 0, 0.0 MB of streams', out)
        self.assertIn('nothing cached yet', out)

    def test_dir_prints_the_directory_alone(self):
        self.assertEqual(self.out(R.cmd_cache, ['dir']).strip(), R.cache_dir())

    def test_list_shows_the_entry_and_its_source(self):
        path = self.populated()
        out = self.out(R.cmd_cache, ['list'])
        self.assertIn('entries   : 1,', out)
        self.assertIn('lz4(1 blocks)', out)
        self.assertIn(os.path.abspath(path), out)

    def test_list_counts_unusable_files_separately(self):
        self.garbage()
        out = self.out(R.cmd_cache, ['list'])
        self.assertIn('entries   : 0,', out)
        self.assertIn('unusable  : 1', out)

    def test_clear_removes_the_entries(self):
        self.populated()
        out = self.out(R.cmd_cache, ['clear'])
        self.assertIn('removed 1 cache files', out)
        self.assertIn(R.cache_dir(), out)
        self.assertEqual(R.cache_entries(), [])
        self.assertIn('entries   : 0,', self.out(R.cmd_cache, ['list']))

    def test_unknown_subcommand_returns_2(self):
        out, code = self.out_and_code(R.cmd_cache, ['nope'])
        self.assertEqual(code, 2)
        self.assertIn('usage: rdc_analysis.py cache [list|dir|clear]', out)

    def test_verify_reports_a_cache_hit_on_the_second_run(self):
        path = self.cap(self.ch('PushMarker', b'Marker\x00'), lz4=True)
        self.out(R.cmd_verify, path)
        self.assertIn('[lz4(1 blocks, cached)]', self.out(R.cmd_verify, path))

    def test_main_dispatches_cache_without_a_capture_path(self):
        # with the real sys.exit() this stops there; the mocked one lets main() fall through to the
        # usage text, so only the first line is asserted
        with mock.patch.object(sys, 'argv', ['rdc_analysis.py', 'cache', 'dir']):
            with mock.patch.object(sys, 'exit') as exit_mock:
                out = self.out(R.main)
        exit_mock.assert_called_once_with(0)
        self.assertEqual(out.splitlines()[0], R.cache_dir())

    def test_main_dispatches_a_bare_cache_to_list(self):
        with mock.patch.object(sys, 'argv', ['rdc_analysis.py', 'cache']):
            with mock.patch.object(sys, 'exit') as exit_mock:
                out = self.out(R.main)
        exit_mock.assert_called_once_with(0)
        self.assertIn('cache dir :', out)


class TestLoadStream(CmdCase):
    def test_raw(self):
        chunks = [self.ch('PushMarker', b'Marker\x00')]
        info, stream, how = R.load_stream(self.cap(*chunks))
        self.assertEqual(how, 'raw')
        self.assertEqual(stream, b''.join(chunks))
        self.assertEqual(info['meta']['driverName'], 'D3D12')

    def test_lz4(self):
        chunks = [self.ch('PushMarker', b'Marker\x00')]
        _, stream, how = R.load_stream(self.cap(*chunks, lz4=True))
        self.assertEqual(stream, b''.join(chunks))
        self.assertEqual(how, 'lz4(1 blocks)')


# =========================================================================== text mining
class TestCmdStrings(CmdCase):
    def payload(self):
        return b'AAAAAA\x00AAAAAA\x00AAAAAA\x00BBBBBB\x00'

    def test_ranked_by_count_then_first_offset(self):
        path = self.cap(self.ch('PushMarker', self.payload()))
        out = self.out(R.cmd_strings, path)
        self.assertIn('unique ascii strings >= 6: 2', out)
        lines = [l for l in out.splitlines() if '@0x' in l]
        self.assertEqual(len(lines), 2)
        self.assertIn('AAAAAA', lines[0])
        self.assertIn('     3', lines[0])
        self.assertIn('BBBBBB', lines[1])
        self.assertIn('     1', lines[1])

    def test_maxlines_caps_the_output(self):
        path = self.cap(self.ch('PushMarker', self.payload()))
        out = self.out(R.cmd_strings, path, 6, 1)
        self.assertIn('AAAAAA', out)
        self.assertNotIn('BBBBBB', out)

    def test_minlen_filters_everything(self):
        path = self.cap(self.ch('PushMarker', self.payload()))
        self.assertIn('unique ascii strings >= 7: 0', self.out(R.cmd_strings, path, 7))

    def test_minlen_below_6_finds_shorter_strings(self):
        path = self.cap(self.ch('PushMarker', b'\x00Frm\x00abcde\x00ABCDEF\x00'))
        out = self.out(R.cmd_strings, path, 3)
        self.assertIn('unique ascii strings >= 3: 3', out)
        self.assertIn('Frm', out)
        self.assertIn('abcde', out)
        self.assertNotIn('Frm', self.out(R.cmd_strings, path, 4))

    def test_stream_size_and_method_are_printed(self):
        path = self.cap(self.ch('PushMarker', self.payload()), lz4=True)
        self.assertIn('[lz4(1 blocks)]', self.out(R.cmd_strings, path))


class TestCmdNames(CmdCase):
    def test_keyword_filtering(self):
        payload = b'\x00MobileBasePassShader\x00RandomStuffHere\x00'
        path = self.cap(self.ch('PushMarker', payload))
        out = self.out(R.cmd_names, path)
        self.assertIn('interesting name-like strings: 1', out)
        self.assertIn('MobileBasePassShader', out)
        self.assertNotIn('RandomStuffHere', out)

    def test_minlen(self):
        payload = b'\x00MobileBasePassShader\x00RandomStuffHere\x00'
        path = self.cap(self.ch('PushMarker', payload))
        self.assertIn('interesting name-like strings: 0', self.out(R.cmd_names, path, 30))

    def test_output_is_capped_at_400_names(self):
        payload = b''.join(b'\x00Shader%03d_ABC' % i for i in range(405))
        path = self.cap(self.ch('PushMarker', payload))
        out = self.out(R.cmd_names, path)
        self.assertIn('interesting name-like strings: 405', out)
        self.assertIn('Shader399_ABC', out)
        self.assertNotIn('Shader400_ABC', out)

    def test_names_are_printed_in_stream_order(self):
        payload = b'\x00ZZZShaderOne\x00AAAShaderTwo\x00'
        path = self.cap(self.ch('PushMarker', payload))
        out = self.out(R.cmd_names, path)
        self.assertLess(out.index('ZZZShaderOne'), out.index('AAAShaderTwo'))


class TestCmdGrep(CmdCase):
    def payload(self):
        return b'xxNEEDLEyy\x00\x01\x02NEEDLE\x00NEEDLE\x00'

    def test_hits_with_context(self):
        path = self.cap(self.ch('PushMarker', self.payload()))
        out = self.out(R.cmd_grep, path, 'NEEDLE', 12)
        self.assertIn('grep %r' % 'NEEDLE', out)
        self.assertEqual(out.count('--- hit '), 3)
        self.assertIn('--- hit 1 @0x', out)
        self.assertIn('xxNEEDLEyy', out)
        self.assertIn('(3 hits shown, cap 30)', out)

    def test_context_is_clipped_to_the_requested_size(self):
        path = self.cap(self.ch('PushMarker', self.payload()))
        out = self.out(R.cmd_grep, path, 'NEEDLE', 4)
        self.assertNotIn('xxNEEDLEyy', out)
        self.assertIn('..xxNEED', out)

    def test_context_renders_non_printable_as_dots(self):
        path = self.cap(self.ch('PushMarker', self.payload()))
        out = self.out(R.cmd_grep, path, 'NEEDLE', 12)
        self.assertIn('..', out)

    def test_cap(self):
        path = self.cap(self.ch('PushMarker', self.payload()))
        out = self.out(R.cmd_grep, path, 'NEEDLE', 4, 2)
        self.assertEqual(out.count('--- hit '), 2)
        self.assertIn('(2 hits shown, cap 2)', out)

    def test_not_found(self):
        path = self.cap(self.ch('PushMarker', self.payload()))
        out = self.out(R.cmd_grep, path, 'ABSENT')
        self.assertIn('NOT FOUND', out)
        self.assertNotIn('--- hit', out)


class TestCmdDump(CmdCase):
    def test_window_strings(self):
        path = self.cap(self.ch('PushMarker', b'\x00Alphaaa\x00Betaaaa\x00' + b'\x00' * 40))
        out = self.out(R.cmd_dump, path, 8, 40, 6)
        self.assertIn('window 0x8..0x30', out)
        self.assertIn('Alphaaa', out)
        self.assertIn('Betaaaa', out)

    def test_minlen_filters(self):
        path = self.cap(self.ch('PushMarker', b'\x00Alphaaa\x00' + b'\x00' * 40))
        self.assertIn('(no strings)', self.out(R.cmd_dump, path, 8, 40, 12))

    def test_empty_window(self):
        path = self.cap(self.ch('PushMarker', b'\x00' * 64))
        self.assertIn('(no strings)', self.out(R.cmd_dump, path, 8, 40))

    def test_window_clamped_to_stream_end(self):
        path = self.cap(self.ch('PushMarker', b'X'))
        self.assertIn('window 0x8..0x40', self.out(R.cmd_dump, path, 8, 10000))

    def test_start_past_end_of_stream(self):
        path = self.cap(self.ch('PushMarker', b'X'))
        self.assertIn('(no strings)', self.out(R.cmd_dump, path, 100000, 16))

    def test_truncates_after_500_strings(self):
        payload = b''.join(b'\x00Str%04d' % i for i in range(510))
        path = self.cap(self.ch('PushMarker', payload))
        out = self.out(R.cmd_dump, path, 8, len(payload), 4)
        self.assertIn('... truncated at 500 strings', out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith('  @0x')]), 501)

    def test_hex_prefixed_arguments_are_not_accepted(self):
        # README section 4.2 claims dump takes 0x-prefixed values; cmd_dump uses plain int()
        path = self.cap(self.ch('PushMarker', b'X'))
        with self.assertRaises(ValueError):
            self.out(R.cmd_dump, path, '0x8', '0x10')


class TestCmdCount(CmdCase):
    def test_counts_and_first_offsets(self):
        path = self.cap(self.ch('PushMarker', b'NEEDLE\x00NEEDLE\x00'))
        out = self.out(R.cmd_count, path, ['NEEDLE', 'ABSENT'])
        needle = self.line_with(out, 'NEEDLE')
        self.assertIn('count=2', needle)
        self.assertIn('first=0x8', needle)
        absent = self.line_with(out, 'ABSENT')
        self.assertIn('count=0', absent)
        self.assertIn('first=0x-1', absent)   # stream.find() returns -1 and is printed verbatim

    def test_no_patterns_just_prints_the_header(self):
        path = self.cap(self.ch('PushMarker', b'X'))
        self.assertIn('stream %d bytes' % 64, self.out(R.cmd_count, path, []))


class TestCmdHex(CmdCase):
    def test_hex_and_ascii_dump(self):
        path = self.cap(self.ch('PushMarker', b'ABCDEFGH'))
        out = self.out(R.cmd_hex, path, '0x8', '0x8')
        self.assertIn('window 0x8..0x10', out)
        row = self.line_with(out, '00000008')
        self.assertIn('41 42 43 44', row)
        self.assertIn('ABCDEFGH', row)

    def test_accepts_decimal_and_hex(self):
        path = self.cap(self.ch('PushMarker', b'ABCDEFGH'))
        self.assertIn('window 0x8..0x10', self.out(R.cmd_hex, path, '8', '8'))
        self.assertIn('window 0x8..0x10', self.out(R.cmd_hex, path, '0x8', '0x8'))

    def test_clamped_to_stream_length(self):
        path = self.cap(self.ch('PushMarker', b'ABCDEFGH'))
        out = self.out(R.cmd_hex, path, '0x0', '0x1000')
        self.assertEqual(len([l for l in out.splitlines() if l.startswith('0000')]), 4)

    def test_zero_length(self):
        path = self.cap(self.ch('PushMarker', b'ABCDEFGH'))
        out = self.out(R.cmd_hex, path, '0x8', '0')
        self.assertIn('window 0x8..0x8', out)
        self.assertEqual(len(out.strip().splitlines()), 1)


# =========================================================================== numeric search
class TestCmdFloat(CmdCase):
    def test_exact_bit_pattern_hits(self):
        payload = F.f32b(1.0) + b'\x00' * 4 + F.f32b(1.0)
        path = self.cap(self.ch('PushMarker', payload))
        out = self.out(R.cmd_float, path, '1.0')
        self.assertIn('exact float32 bits of 1.0 = %s' % struct.pack('<f', 1.0).hex(), out)
        self.assertIn('exact hits: 2', out)
        self.assertEqual(len([l for l in out.splitlines() if l.strip().startswith('0x')]), 2)

    def test_no_hits(self):
        path = self.cap(self.ch('PushMarker', b'\x00' * 64))
        self.assertIn('exact hits: 0', self.out(R.cmd_float, path, '123.5'))

    def test_context_is_limited_to_20_hits(self):
        payload = F.f32b(2.0) * 25
        path = self.cap(self.ch('PushMarker', payload))
        out = self.out(R.cmd_float, path, '2.0')
        self.assertIn('exact hits: 25', out)
        self.assertEqual(len([l for l in out.splitlines() if l.strip().startswith('0x')]), 20)

    def test_tolerance_argument_is_ignored(self):
        path = self.cap(self.ch('PushMarker', F.f32b(3.0)))
        self.assertIn('exact hits: 1', self.out(R.cmd_float, path, '3.0', 0.5))


class TestCmdPattern(CmdCase):
    def payload(self, extra=16):
        return F.SINGLEPROBE_SIG + F.f32b(*[float(i) for i in range(1, extra + 1)])

    def test_hit_and_following_floats(self):
        path = self.cap(self.ch('PushMarker', self.payload()))
        out = self.out(R.cmd_pattern, path, '0,0,0,1,1,1,0,0,0,1,1,1', 16)
        self.assertIn('searching 12 floats: 0,0,0,1,1,1,0,0,0,1,1,1', out)
        self.assertIn('hits: 1', out)
        rows = [l.strip() for l in out.splitlines() if l.strip().startswith('+')]
        self.assertTrue(rows[0].startswith('+0 '), rows[0])
        self.assertTrue(rows[1].startswith('+32 '), rows[1])
        self.assertIn('1.00000', rows[0])
        self.assertIn('8.00000', rows[0])
        self.assertIn('16.00000', rows[1])

    def test_cap_and_more_hits_line(self):
        chunks = [self.ch('PushMarker', F.SINGLEPROBE_SIG) for _ in range(41)]
        path = self.cap(*chunks)
        out = self.out(R.cmd_pattern, path, '0,0,0,1,1,1,0,0,0,1,1,1', 8)
        self.assertIn('hits: 41', out)
        self.assertIn('... 1 more hits', out)

    def test_truncated_at_stream_end(self):
        # no padding after the payload, so there are no floats left to dump after the hit
        path = self.cap(F.chunk(self.ids['PushMarker'], F.SINGLEPROBE_SIG, align=False))
        out = self.out(R.cmd_pattern, path, '0,0,0,1,1,1,0,0,0,1,1,1', 72)
        self.assertIn('hits: 1', out)
        self.assertNotIn('    +0', out)

    def test_no_hits(self):
        path = self.cap(self.ch('PushMarker', b'\x00' * 64))
        out = self.out(R.cmd_pattern, path, '1,2,3')
        self.assertIn('hits: 0', out)


# =========================================================================== shaders
def shader_capture_parts():
    """Parts for a PS container with GI reflection data, inputs and a source file."""
    return [
        ('RDEF', b'\x00MobileBasePass\x00'),
        ('RDAT', b'\x00IndirectLightingSHCoefficients0\x00SomeOtherUniform\x00'),
        ('ISG1', F.isg1_inputs()),
        ('OSG1', F.osg1_targets()),
        ('ILDN', b'\x00/Engine/Private/MobileBasePass.usf\x00'),
    ]


class TestCmdDxbc(CmdCase):
    def capture(self):
        blob = (F.dxbc(shader_capture_parts())
                + F.dxbc([('ISG1', F.signature([('POSITION', 0, 0)])), ('OSG1', F.osg1_position()),
                          ('ILDN', b'\x00')])
                + F.dxbc([('RTS0', b'\x02\x00\x00\x00')])
                + F.dxbc([('RDEF', b'\x00')]))
        return self.cap(self.ch('PushMarker', blob))

    def test_container_count_and_stage_histogram(self):
        out = self.out(R.cmd_dxbc, self.capture())
        self.assertIn('DXBC/DXIL containers: 4', out)
        self.assertIn('  PS        1', out)
        self.assertIn('  VS        1', out)
        self.assertIn('  root-sig  1', out)
        self.assertIn('  ?         1', out)

    def test_gi_variables_are_listed(self):
        out = self.out(R.cmd_dxbc, self.capture())
        self.assertIn('IndirectLightingSHCoefficients0', out)
        self.assertNotIn('SomeOtherUniform', out.split('--- [0]')[0])

    def test_detail_block(self):
        out = self.out(R.cmd_dxbc, self.capture())
        self.assertIn('    GI vars : IndirectLightingSHCoefficients0', out)
        self.assertIn('    VS input: POSITION, TEXCOORD0, TEXCOORD6', out)
        self.assertIn('    PS out  : SV_Target, SV_Target', out)
        self.assertIn('    files   : /Engine/Private/MobileBasePass.usf', out)

    def test_root_signature_containers_are_skipped_in_the_detail_block(self):
        out = self.out(R.cmd_dxbc, self.capture())
        self.assertEqual(out.count('--- ['), 3)          # 4 containers minus the root signature

    def test_verbose_prints_all_strings(self):
        plain = self.out(R.cmd_dxbc, self.capture())
        verbose = self.out(R.cmd_dxbc, self.capture(), 1)
        self.assertNotIn('    all str :', plain)
        self.assertIn('    all str :', verbose)
        self.assertIn('SomeOtherUniform', verbose)

    def test_cs_stage_is_unreachable_with_four_character_parts(self):
        # the 'CS' branch compares against a 4-byte fourcc, so it can never fire
        blob = F.dxbc([('CSxx', b'\x00'), ('ILDN', b'\x00')])
        out = self.out(R.cmd_dxbc, self.cap(self.ch('PushMarker', blob)))
        self.assertIn('  ?         1', out)
        self.assertNotIn('  CS        1', out)

    def test_no_containers(self):
        out = self.out(R.cmd_dxbc, self.cap(self.ch('PushMarker', b'\x00' * 32)))
        self.assertIn('DXBC/DXIL containers: 0', out)


class TestCmdSig(CmdCase):
    def capture(self):
        blob = (F.dxbc([('ISG1', F.isg1_inputs()), ('OSG1', F.osg1_targets())])
                + F.dxbc([('ISG1', F.signature([('POSITION', 0, 0)])), ('OSG1', F.osg1_position())])
                + F.dxbc([('RTS0', b'\x02\x00\x00\x00')]))
        return self.cap(self.ch('PushMarker', blob))

    def test_signatures_are_decoded(self):
        out = self.out(R.cmd_sig, self.capture())
        self.assertIn('IN : POSITION0(reg0), TEXCOORD00(reg1), TEXCOORD60(reg2)', out)
        self.assertIn('OUT: SV_Target0, SV_Target1', out)
        self.assertIn('OUT: SV_Position0, TEXCOORD00', out)

    def test_stage_is_inferred_from_the_output_signature(self):
        out = self.out(R.cmd_sig, self.capture())
        self.assertIn(' PS hash=', out)
        self.assertIn(' VS hash=', out)

    def test_root_signature_containers_are_skipped(self):
        self.assertEqual(self.out(R.cmd_sig, self.capture()).count('--- 0x'), 2)


class TestCmdReport(CmdCase):
    def test_pattern_counts_and_shader_names(self):
        payload = (b'\x00IndirectLightingCache\x00IndirectLightingCache\x00'
                   b'\x00FShaderCompileJob\x00SomeUnrelatedName\x00')
        path = self.cap(self.ch('PushMarker', payload))
        out = self.out(R.cmd_report, path)
        self.assertIn('=== pattern counts ===', out)
        line = self.line_with(out, 'IndirectLightingCache')
        self.assertIn('count=2', line)
        self.assertIn('=== shader-ish / policy-ish names (unique) ===', out)
        self.assertIn('FShaderCompileJob', out)
        self.assertNotIn('SomeUnrelatedName', out)
        self.assertIn('(1 unique)', out)

    def test_no_matches(self):
        path = self.cap(self.ch('PushMarker', b'\x00' * 64))
        out = self.out(R.cmd_report, path)
        self.assertIn('(0 unique)', out)
        self.assertIn('count=0', out)


# =========================================================================== chunk level
class TestCmdChunkDetail(CmdCase):
    def capture(self):
        return self.cap(self.ch('PushMarker', b'BasePass\x00'),
                        self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 2880, 1, 0, 0, 0),
                                callstack=[1], threadid=2, duration=3, timestamp=4),
                        self.ch('List_Dispatch', F.pl_dispatch(7, 1, 1, 1)))

    def test_header_and_payload_offsets(self):
        out = self.out(R.cmd_chunk_detail, self.capture(), 2)
        self.assertIn('chunk #2', out)
        self.assertIn('(List_DrawIndexedInstanced)', out)
        self.assertIn('id=%d' % self.ids['List_DrawIndexedInstanced'], out)
        self.assertIn('flags=0xf0000', out)
        self.assertIn('length=28', out)
        self.assertIn('header+metadata = 44 bytes', out)

    def test_decoded_fields_hex_and_strings(self):
        out = self.out(R.cmd_chunk_detail, self.capture(), 2)
        self.assertIn('cmdList=7 idx=2880 inst=1', out)
        self.assertIn('  0000  07 00 00 00 00 00 00 00 40 0b 00 00 01 00 00 00', out)
        marker = self.out(R.cmd_chunk_detail, self.capture(), 1)
        self.assertIn('strings:', marker)
        self.assertIn('BasePass', marker)

    def test_hexlen_limits_the_dump(self):
        short = self.out(R.cmd_chunk_detail, self.capture(), 2, 16)
        long = self.out(R.cmd_chunk_detail, self.capture(), 2, 160)
        self.assertEqual(len([l for l in short.splitlines() if l.startswith('  00')]), 1)
        self.assertGreater(len([l for l in long.splitlines() if l.startswith('  00')]), 1)

    def test_missing_chunk(self):
        out = self.out(R.cmd_chunk_detail, self.capture(), 99)
        self.assertIn('chunk #99 not found', out)
        self.assertIn('chunk #0 not found', self.out(R.cmd_chunk_detail, self.capture(), 0))

    def test_chunk_and_draws_agree_on_root_binding_offsets(self):
        # the acceptance gate for the decoder disagreements: one payload, one reading
        path = self.cap(
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x120000)),
            self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0)))
        self.assertIn('res=1907+0x120000', self.out(R.cmd_chunk_detail, path, 1))
        self.assertIn('rp10=res1907+0x120000', self.out(R.cmd_draws, path))

    def test_chunk_and_draws_agree_on_the_index_buffer(self):
        path = self.cap(
            self.ch('List_IASetIndexBuffer', F.pl_index_buffer(7, 80641, 0x3F2B0000, 12345, 57)),
            self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0)))
        self.assertIn('res=80641+0x3f2b0000 size=12345 fmt=57',
                      self.out(R.cmd_chunk_detail, path, 1))
        self.assertIn('IB : res80641+0x3f2b0000', self.out(R.cmd_draws, path))


class TestCmdChunks(CmdCase):
    def capture(self):
        return self.cap(self.ch('PushMarker', b'BasePass\x00'),
                        self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)),
                        self.ch('List_Dispatch', F.pl_dispatch(7, 1, 1, 1)))

    def test_lists_all_chunks_with_names_and_strings(self):
        out = self.out(R.cmd_chunks, self.capture())
        self.assertIn('known chunk names: %d' % len(self.names), out)
        self.assertIn('PushMarker', out)
        self.assertIn('List_DrawIndexedInstanced', out)
        self.assertIn('total chunks: 3 (shown 3)', out)
        self.assertIn('BasePass', out)

    def test_limit(self):
        out = self.out(R.cmd_chunks, self.capture(), 2)
        self.assertIn('total chunks: 3 (shown 2)', out)

    def test_name_filter_is_case_insensitive(self):
        out = self.out(R.cmd_chunks, self.capture(), 200, 'draw')
        self.assertIn('List_DrawIndexedInstanced', out)
        self.assertNotIn('PushMarker', out)
        self.assertIn('total chunks: 3 (shown 1)', out)

    def test_name_filter_matching_nothing(self):
        self.assertIn('total chunks: 3 (shown 0)',
                      self.out(R.cmd_chunks, self.capture(), 200, 'NotAChunkName'))

    def test_missing_source_tree_warns_and_uses_numeric_ids(self):
        with mock.patch.object(R, 'load_chunk_names', lambda src_root=None, driver='D3D12': {}):
            out = self.out(R.cmd_chunks, self.capture())
        self.assertIn('known chunk names: 0', out)
        self.assertIn('WARNING: RenderDoc source not found at', out)
        self.assertIn('Chunk%d' % self.ids['PushMarker'], out)


class TestCmdDraws(CmdCase):
    def full_frame(self):
        return [
            self.ch('PushMarker', b'BasePass\x00'),
            self.ch('List_SetPipelineState', F.pl_pso(7, 3042)),
            self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [(315, 0x3F7400, 6708, 12),
                                                                         (0, 0, 0, 0)])),
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x120000)),
            self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 11, 342, 0x3B000)),
            self.ch('List_IASetIndexBuffer', F.pl_index_buffer(7, 80641, 0x3F2B0000, 16, 57)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 2880, 1, 0, 0, 0)),
            self.ch('PopMarker', b''),
        ]

    def test_state_is_reported_for_each_draw(self):
        out = self.out(R.cmd_draws, self.cap(*self.full_frame()))
        row = self.line_with(out, 'DrawIndexedInstanced')
        self.assertIn('#7', row)
        self.assertIn('BasePass', row)
        self.assertIn('3042', row)
        self.assertIn('idx=2880 inst=1', row)
        self.assertIn('CBV: rp10=res1907+0x120000  rp11=res342+0x3b000', out)
        self.assertIn('VB : res315+0x3f7400(sz6708,st12)  res0+0x0(sz0,st0)', out)
        self.assertIn('IB : res80641+0x3f2b0000', out)
        self.assertIn('total draws/dispatches: 1', out)

    def test_state_persists_across_draws(self):
        # D3D12 bindings belong to the command list: a draw that binds nothing still has them
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertEqual(out.count('CBV: '), 2)
        self.assertIn('total draws/dispatches: 2', out)

    def test_a_binding_made_three_draws_earlier_is_still_reported(self):
        # the regression the roadmap asked for: inheritance, not "what changed since the last draw"
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10))]
        chunks += [self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))
                   for _ in range(3)]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertEqual(out.count('CBV: rp10=res1907+0x10'), 3)

    def test_pso_change_keeps_the_bindings(self):
        # SetPipelineState only changes the PSO: root arguments and IA bindings are list state
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10)),
                  self.ch('List_SetPipelineState', F.pl_pso(7, 99)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('CBV: rp10=res1907+0x10', out)
        self.assertIn(' 99 ', self.line_with(out, 'DrawIndexedInstanced'))

    def test_a_changed_root_signature_clears_the_root_bindings(self):
        chunks = [self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 5)),
                  self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10)),
                  self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 6)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertNotIn('CBV: ', out)

    def test_setting_the_same_root_signature_again_keeps_the_bindings(self):
        # "if the root signature is redundantly set to the same one, existing root signature
        # bindings do not become stale" -- D3D12 command-list semantics
        chunks = [self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 5)),
                  self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10)),
                  self.ch('List_SetGraphicsRootSignature', F.pl_root_signature(7, 5)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('CBV: rp10=res1907+0x10', out)

    def test_reset_clears_the_list_and_uses_its_initial_pso(self):
        chunks = [self.ch('List_SetPipelineState', F.pl_pso(7, 99)),
                  self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10)),
                  self.ch('List_Reset', F.pl_reset(7, initial_pso=77)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertNotIn('CBV: ', out)
        self.assertIn(' 77 ', self.line_with(out, 'DrawIndexedInstanced'))

    def test_reset_leaves_another_command_list_alone(self):
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10)),
                  self.ch('List_Reset', F.pl_reset(8)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('CBV: rp10=res1907+0x10', out)

    def test_a_reset_with_an_unknown_layout_clears_everything(self):
        # documents the fallback: an unrecognised Reset cannot be attributed to one list, so
        # nothing stale is allowed to survive it (and `verify` flags the odd payload length)
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10)),
                  self.ch('List_Reset', b'\x00' * 16),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertNotIn('CBV: ', out)

    def test_state_is_tracked_per_command_list(self):
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10)),
                  self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(8, 11, 342, 0x20)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(8, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        cbv_lines = [l.strip() for l in out.splitlines() if l.strip().startswith('CBV:')]
        self.assertEqual(cbv_lines, ['CBV: rp10=res1907+0x10', 'CBV: rp11=res342+0x20'])

    def test_vertex_buffer_slots_are_independent(self):
        # IASetVertexBuffers only touches [startSlot, startSlot + numViews): the rest keep theirs
        chunks = [self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [(315, 0x1000, 64, 12)])),
                  self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 2, [(316, 0x2000, 32, 8)])),
                  self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        line = self.line_with(out, 'VB : ')
        self.assertIn('res315+0x1000(sz64,st12)', line)
        self.assertIn('res316+0x2000(sz32,st8)', line)
        self.assertLess(line.index('res315'), line.index('res316'))     # printed in slot order

    def test_null_index_buffer_clears_the_binding(self):
        chunks = [self.ch('List_IASetIndexBuffer', F.pl_index_buffer(7, 80641, 0x1000, 16, 57)),
                  self.ch('List_IASetIndexBuffer', F.pl_index_buffer(7, 0, 0, 0, 0, present=False)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertNotIn('IB : ', out)

    def test_rebinding_a_root_parameter_replaces_it(self):
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 1907, 0x10)),
                  self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 10, 342, 0x20)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('CBV: rp10=res342+0x20', out)
        self.assertNotIn('res1907', out)

    def test_descriptor_tables_are_reported(self):
        chunks = [self.ch('List_SetGraphicsRootDescriptorTable', F.pl_root_table(7, 4, 298, 138458)),
                  self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('Table: rp4=heap298[138458]', out)

    def test_dispatch_reports_compute_bindings_and_not_graphics_ones(self):
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 1, 1907, 0x10)),
                  self.ch('List_SetComputeRootConstantBufferView', F.pl_root_view(7, 2, 342, 0x20)),
                  self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, [(315, 0, 4, 4)])),
                  self.ch('List_Dispatch', F.pl_dispatch(7, 8, 4, 1)),
                  self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        lines = out.splitlines()
        dispatch = lines.index(self.line_with(out, 'Dispatch'))
        self.assertEqual(lines[dispatch + 1].strip(), 'CBV: rp2=res342+0x20')
        draw = lines.index(self.line_with(out, 'DrawInstanced'))
        self.assertEqual(lines[draw + 1].strip(), 'CBV: rp1=res1907+0x10')
        self.assertIn('VB : ', lines[draw + 2])          # IA state is graphics-only

    def test_execute_indirect_reports_both_namespaces(self):
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.pl_root_view(7, 1, 1907, 0x10)),
                  self.ch('List_SetComputeRootConstantBufferView', F.pl_root_view(7, 2, 342, 0x20)),
                  self.ch('List_ExecuteIndirect', F.u64b(7) + F.u32b(1) + F.u32b(2) + F.u32b(3))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('CBV: rp2=res342+0x20', out)
        self.assertIn('CBV: rp1=res1907+0x10', out)

    def test_a_draw_with_no_state_at_all_reports_nothing(self):
        chunks = [self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertNotIn('CBV: ', out)
        self.assertNotIn('VB : ', out)
        self.assertNotIn('IB : ', out)

    def test_a_truncated_state_chunk_invents_no_bindings(self):
        chunks = [self.ch('List_SetGraphicsRootConstantBufferView', F.u64b(7) + F.u32b(1)),
                  self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertNotIn('CBV: ', out)
        self.assertIn('total draws/dispatches: 1', out)

    def test_max_draws_caps_the_rows_but_not_the_count(self):
        chunks = [self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 3, 1, 0, 0)) for _ in range(3)]
        out = self.out(R.cmd_draws, self.cap(*chunks), 2)
        self.assertEqual(out.count('DrawInstanced'), 2)
        self.assertIn('total draws/dispatches: 3', out)

    def test_draw_instanced_and_dispatch_args(self):
        chunks = [self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 3, 2, 5, 0)),
                  self.ch('List_Dispatch', F.pl_dispatch(7, 8, 4, 1))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('verts=3 inst=2', out)
        self.assertIn('x=8 y=4 z=1', out)

    def test_execute_indirect_uses_the_dispatch_argument_format(self):
        chunks = [self.ch('List_ExecuteIndirect', F.u64b(7) + F.u32b(1) + F.u32b(2) + F.u32b(3))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('x=1 y=2 z=3', out)
        self.assertIn('ExecuteIndirect', out)

    def test_marker_stack_paths(self):
        chunks = [self.ch('PushMarker', b'FramePass\x00'), self.ch('PushMarker', b'BasePass\x00'),
                  self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('FramePass / BasePass', self.line_with(out, 'DrawInstanced'))

    def test_short_marker_names_are_reported(self):
        # `draws` asks chunk_strings for 3+ character strings, so a short marker name survives
        chunks = [self.ch('PushMarker', b'Frm\x00'),
                  self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('Frm', self.line_with(out, 'DrawInstanced'))

    def test_marker_names_below_the_3_character_minimum_are_still_dropped(self):
        # the 3-character floor is now an explicit choice in `draws`, not a side effect of the regex
        chunks = [self.ch('PushMarker', b'Ab\x00'),
                  self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('? ', self.line_with(out, 'DrawInstanced'))
        self.assertNotIn('Ab', out)

    def test_pop_marker_on_empty_stack_is_harmless(self):
        chunks = [self.ch('PopMarker', b''),
                  self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        self.assertIn('total draws/dispatches: 1', self.out(R.cmd_draws, self.cap(*chunks)))

    def test_vertex_buffer_list_is_capped_at_16(self):
        views = [(i + 1, 0, 4, 4) for i in range(20)]
        chunks = [self.ch('List_IASetVertexBuffers', F.pl_vertex_buffers(7, 0, views)),
                  self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertEqual(self.line_with(out, 'VB : ').count('res'), 16)

    def test_unknown_chunks_are_ignored(self):
        chunks = [F.chunk(60000, b'\x00' * 8),
                  self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0))]
        out = self.out(R.cmd_draws, self.cap(*chunks))
        self.assertIn('total draws/dispatches: 1', out)


class TestCmdVerify(CmdCase):
    def clean_capture(self):
        # zero padding: stale padding is reported as a note, so it must not be in a "clean" fixture
        return self.cap(self.ch('List_SetPipelineState', F.pl_pso(7, 3042), pad_byte=0x00),
                        self.ch('List_DrawInstanced', F.pl_draw_instanced(7, 1, 1, 0, 0),
                                pad_byte=0x00))

    def verify(self, path):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.cmd_verify(path)
        return code, out.getvalue()

    def test_clean_capture_reports_no_problems(self):
        code, out = self.verify(self.clean_capture())
        self.assertEqual(code, 0)
        self.assertIn('problems: 0', out)
        self.assertIn('notes: 1', out)
        self.assertIn('padding: ', out)          # the padding totals are always shown

    def test_payload_length_mismatch_is_a_problem(self):
        path = self.cap(self.ch('List_SetPipelineState', F.pl_pso(7, 3042) + b'\x00' * 4,
                                pad_byte=0x00))
        code, out = self.verify(path)
        self.assertEqual(code, 1)
        self.assertIn('the decoder expects 16', out)

    def test_stale_padding_is_a_note_only(self):
        path = self.cap(self.ch('List_SetPipelineState', F.pl_pso(7, 3042), pad_byte=0xCC))
        code, out = self.verify(path)
        self.assertEqual(code, 0)
        self.assertIn('problems: 0', out)
        self.assertIn('notes:', out)
        self.assertIn('padding bytes are non-zero', out)

    def test_main_dispatches_verify_and_exits_with_its_code(self):
        path = self.clean_capture()
        with mock.patch.object(sys, 'argv', ['rdc_analysis.py', 'verify', path]):
            with mock.patch.object(sys, 'exit') as exit_mock:
                self.out(R.main)
        exit_mock.assert_called_once_with(0)


class TestCmdSummary(CmdCase):
    def capture(self, markers=1, draws=2):
        chunks = []
        for i in range(markers):
            chunks.append(self.ch('PushMarker', b'Marker%02d\x00' % i))
        for i in range(draws):
            chunks.append(self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 3, 1, 0, 0, 0)))
        chunks.append(self.ch('List_Dispatch', F.pl_dispatch(7, 1, 1, 1)))
        return self.cap(*chunks)

    def test_counts(self):
        out = self.out(R.cmd_summary, self.capture())
        self.assertIn('chunk count        : 4', out)
        self.assertIn('draw/dispatch calls: 3', out)
        self.assertIn('markers            : 1', out)

    def test_histogram_is_ordered_by_count(self):
        out = self.out(R.cmd_summary, self.capture())
        hist = out.split('--- chunk type histogram ---')[1].split('--- markers in order ---')[0]
        lines = [l.strip() for l in hist.strip().splitlines()]
        # 2 draws first, then the two single-occurrence chunk types in insertion order
        self.assertTrue(lines[0].startswith('List_DrawIndexedInstanced'))
        self.assertTrue(lines[1].startswith('PushMarker'))
        self.assertTrue(lines[2].startswith('List_Dispatch'))

    def test_markers_in_order(self):
        out = self.out(R.cmd_summary, self.capture(markers=2))
        first = self.line_with(out, 'Marker00')
        second = self.line_with(out, 'Marker01')
        self.assertIn('#1', first)
        self.assertIn('PushMarker', first)
        self.assertIn('#2', second)

    def test_marker_list_is_capped_at_120(self):
        out = self.out(R.cmd_summary, self.capture(markers=121, draws=0))
        self.assertIn('... 1 more', out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith('  #')]), 120)


class TestCmdMarkers(CmdCase):
    def test_only_marker_chunks_are_listed(self):
        chunks = [self.ch('PushMarker', b'BasePass\x00'),
                  self.ch('List_Dispatch', F.pl_dispatch(7, 1, 1, 1)),
                  self.ch('SetMarker', b'SubMarker\x00'),
                  self.ch('Queue_BeginEvent', b'QueueMarker\x00')]
        out = self.out(R.cmd_markers, self.cap(*chunks))
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)
        self.assertIn('BasePass', lines[0])
        self.assertIn('SubMarker', lines[1])
        self.assertIn('QueueMarker', lines[2])
        self.assertNotIn('List_Dispatch', out)

    def test_up_to_three_strings_are_joined(self):
        payload = b'MarkerOne\x00MarkerTwo\x00MarkerThree\x00MarkerFour\x00'
        out = self.out(R.cmd_markers, self.cap(self.ch('PushMarker', payload)))
        self.assertIn('MarkerOne | MarkerTwo | MarkerThree', out)
        self.assertNotIn('MarkerFour', out)

    def test_marker_without_strings(self):
        out = self.out(R.cmd_markers, self.cap(self.ch('PushMarker', b'\x00\x00')))
        self.assertIn('PushMarker', out)


class TestCmdRootconst(CmdCase):
    def test_constants_are_decoded_to_floats(self):
        # values sit after the inline u64 array count: length == 28 + 4n, values at +24
        values = F.fbits(1.0, 2.0, 3.0)
        chunks = [self.ch('List_SetGraphicsRoot32BitConstants',
                          F.pl_32bit_constants(7, 2, values, 4))]
        out = self.out(R.cmd_rootconst, self.cap(*chunks))
        self.assertIn('List_SetGraphicsRoot32BitConstants', out)
        self.assertIn('    rootParam=2 numValues=3 destOffset=4', out)
        row = [l for l in out.splitlines() if l.startswith('    +0')][0]
        self.assertIn('1.00000', row)
        self.assertIn('2.00000', row)
        self.assertIn('3.00000', row)
        self.assertIn('root-constant chunks total: 1 (shown 1)', out)

    def test_zero_values(self):
        chunks = [self.ch('List_SetGraphicsRoot32BitConstants', F.pl_32bit_constants(7, 0, [], 0))]
        out = self.out(R.cmd_rootconst, self.cap(*chunks))
        self.assertIn('    rootParam=0 numValues=0 destOffset=0', out)

    def test_length_sanity_check_falls_back_to_hex(self):
        payload = F.pl_32bit_constants(7, 2, F.fbits(1.0)) + b'\x00\x00'   # 28 + 4 + 2 bytes
        out = self.out(R.cmd_rootconst, self.cap(self.ch('List_SetGraphicsRoot32BitConstants', payload)))
        self.assertIn('hex:', out)
        self.assertNotIn('numValues=', out)

    def test_inline_array_count_mismatch_warns(self):
        payload = F.pl_32bit_constants(7, 2, F.fbits(1.0), 0, array_count=9)
        out = self.out(R.cmd_rootconst, self.cap(self.ch('List_SetGraphicsRoot32BitConstants', payload)))
        self.assertIn('warning: inline arrayCount=9 disagrees with numValues=1', out)
        self.assertIn('rootParam=2 numValues=1 destOffset=0', out)

    def test_single_constant_variant_prints_hex(self):
        chunks = [self.ch('List_SetGraphicsRoot32BitConstant', F.pl_32bit_constant(7, 1, 7, 0))]
        out = self.out(R.cmd_rootconst, self.cap(*chunks))
        self.assertIn('List_SetGraphicsRoot32BitConstant', out)
        self.assertIn('hex:', out)

    def test_max_chunks_caps_the_dump(self):
        chunks = [self.ch('List_SetGraphicsRoot32BitConstants', F.pl_32bit_constants(7, 1, [1], 0))
                  for _ in range(3)]
        out = self.out(R.cmd_rootconst, self.cap(*chunks), 1)
        self.assertIn('root-constant chunks total: 3 (shown 1)', out)
        self.assertEqual(out.count('--- chunk #'), 1)

    def test_no_root_constant_chunks(self):
        out = self.out(R.cmd_rootconst, self.cap(self.ch('PushMarker', b'X')))
        self.assertIn('root-constant chunks total: 0 (shown 0)', out)


class TestCmdDumpChunk(CmdCase):
    def test_payload_is_written_verbatim(self):
        payload = F.pl_draw_indexed(7, 2880, 1, 0, 0, 0)
        path = self.cap(self.ch('PushMarker', b'X'), self.ch('List_DrawIndexedInstanced', payload))
        outfile = os.path.join(self.tmp, 'chunk.bin')
        out = self.out(R.cmd_dump_chunk, path, 2, outfile)
        self.assertIn('chunk #2 List_DrawIndexedInstanced', out)
        self.assertIn('(28 bytes)', out)
        with open(outfile, 'rb') as f:
            self.assertEqual(f.read(), payload)

    def test_missing_chunk(self):
        path = self.cap(self.ch('PushMarker', b'X'))
        out = self.out(R.cmd_dump_chunk, path, 9, os.path.join(self.tmp, 'nope.bin'))
        self.assertIn('chunk #9 not found', out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'nope.bin')))


class TestCmdDumpShaders(CmdCase):
    def capture(self):
        blob = (F.dxbc(shader_capture_parts())
                + F.dxbc([('RTS0', b'\x02\x00\x00\x00')]))
        return self.cap(self.ch('PushMarker', blob))

    def test_writes_blobs_and_summary(self):
        outdir = os.path.join(self.tmp, 'nested', 'shaders')
        out = self.out(R.cmd_dump_shaders, self.capture(), outdir)
        self.assertIn('wrote 1 shader blobs + shaders.txt', out)
        files = os.listdir(outdir)
        self.assertEqual(len([f for f in files if f.endswith('.dxil')]), 1)
        self.assertIn('shaders.txt', files)
        with open(os.path.join(outdir, 'shaders.txt'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('parts=RDEF,RDAT,ISG1,OSG1,ILDN', text)
        self.assertIn('GI vars : IndirectLightingSHCoefficients0', text)
        self.assertIn('.dxil', text)

    def test_written_blob_is_the_container(self):
        outdir = os.path.join(self.tmp, 'shaders')
        self.out(R.cmd_dump_shaders, self.capture(), outdir)
        name = [f for f in os.listdir(outdir) if f.endswith('.dxil')][0]
        with open(os.path.join(outdir, name), 'rb') as f:
            self.assertEqual(f.read()[:4], b'DXBC')

    def test_root_signature_only_capture(self):
        path = self.cap(self.ch('PushMarker', F.dxbc([('RTS0', b'\x02\x00\x00\x00')])))
        outdir = os.path.join(self.tmp, 'shaders')
        out = self.out(R.cmd_dump_shaders, path, outdir)
        self.assertIn('wrote 0 shader blobs', out)
        self.assertTrue(os.path.exists(os.path.join(outdir, 'shaders.txt')))


# =========================================================================== main() dispatch
class TestMain(CmdCase):
    def rich_capture(self):
        blob = (F.dxbc(shader_capture_parts())
                + F.dxbc([('RTS0', b'\x02\x00\x00\x00')]))
        return self.cap(
            self.ch('PushMarker', b'BasePassMarker\x00'),
            self.ch('List_SetPipelineState', F.pl_pso(7, 3042)),
            self.ch('List_SetGraphicsRoot32BitConstants', F.pl_32bit_constants(7, 2, F.fbits(1.0), 0)),
            self.ch('List_DrawIndexedInstanced', F.pl_draw_indexed(7, 2880, 1, 0, 0, 0)),
            self.ch('PushMarker', blob),
            self.ch('List_Dispatch', F.pl_dispatch(7, 1, 1, 1)),
            self.ch('PushMarker', F.f32b(1.0) + b'\x00IndirectLightingCache\x00FShaderCompileJob\x00'),
        )

    def run_main(self, argv):
        with mock.patch.object(sys, 'argv', ['rdc_analysis.py'] + argv):
            return self.out(R.main)

    def test_no_arguments_prints_the_docstring(self):
        self.assertIn('Usage:', self.run_main([]))

    def test_single_argument_prints_the_docstring(self):
        self.assertIn('Usage:', self.run_main(['sections']))

    def test_unknown_command_prints_the_docstring(self):
        self.assertIn('Usage:', self.run_main(['bogus', self.rich_capture()]))

    def test_every_command_is_dispatched(self):
        path = self.rich_capture()
        cases = [
            (['sections', path], 'rdc version'),
            (['blocks', path], 'first16='),
            (['strings', path], 'unique ascii strings'),
            (['strings', path, '8', '5'], 'unique ascii strings >= 8'),
            (['names', path], 'interesting name-like strings'),
            (['names', path, '20'], 'interesting name-like strings'),
            (['grep', path, 'Marker'], '--- hit'),
            (['grep', path, 'Marker', '8'], '--- hit'),
            (['count', path, 'Marker'], 'count='),
            (['count', path, 'Marker', 'Absent'], 'count='),
            (['hex', path, '0x0', '0x10'], 'window 0x0..0x10'),
            (['float', path, '1.0'], 'exact hits:'),
            (['pattern', path, '0,0,0,1,1,1,0,0,0,1,1,1'], 'hits:'),
            (['pattern', path, '0,0,0,1,1,1,0,0,0,1,1,1', '8'], 'hits:'),
            (['dump', path, '0', '64'], 'window 0x0..0x40'),
            (['dump', path, '0', '64', '8'], 'window 0x0..0x40'),
            (['chunks', path], 'total chunks:'),
            (['chunks', path, '2', 'draw'], 'total chunks:'),
            (['chunk', path, '1'], 'chunk #1'),
            (['draws', path], 'total draws/dispatches:'),
            (['draws', path, '1'], 'total draws/dispatches:'),
            (['summary', path], 'chunk count'),
            (['markers', path], 'PushMarker'),
            (['rootconst', path], 'root-constant chunks total:'),
            (['rootconst', path, '2'], 'root-constant chunks total:'),
            (['dxbc', path], 'DXBC/DXIL containers:'),
            (['dxbc', path, '1'], 'all str'),
            (['sig', path], 'IN :'),
            (['report', path], 'pattern counts'),
            (['dump-chunk', path, '1', os.path.join(self.tmp, 'out.bin')], '->'),
            (['dump-shaders', path, os.path.join(self.tmp, 'shaders')], 'wrote'),
        ]
        for argv, expected in cases:
            with self.subTest(command=argv[0]):
                self.assertIn(expected, self.run_main(argv))

    def test_missing_arguments_raise_index_error(self):
        path = self.rich_capture()
        for argv in (['chunk', path], ['dump-chunk', path], ['dump-shaders', path],
                     ['grep', path], ['hex', path, '0x0'], ['pattern', path], ['float', path],
                     ['dump', path, '0'], ['draws', path, 'notanint']):
            with self.subTest(argv=argv[0]):
                with self.assertRaises((IndexError, ValueError)):
                    self.run_main(argv)


class TestSelfTestCommand(CmdCase):
    def run_silently(self, fn, *args):
        """Run a nested test suite without letting its report into this suite's output."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args)

    def test_filtered_run_reports_ok(self):
        # unittest writes its report to stderr, so both streams are captured here
        out = capture_all(R.cmd_selftest, ['-v', '-k', 'TestAlignUp'])
        self.assertIn('running tests from', out)
        self.assertIn('TestAlignUp', out)
        self.assertIn('OK', out)

    def test_filter_that_matches_nothing_still_passes(self):
        out = capture_all(R.cmd_selftest, ['-k', 'NoSuchTestName'])
        self.assertIn('Ran 0 tests', out)
        self.assertIn('OK', out)

    def test_unknown_option_returns_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.cmd_selftest(['--nonsense'])
        self.assertEqual(code, 2)
        self.assertIn('usage: rdc_analysis.py selftest', out.getvalue())

    def test_quiet_mode(self):
        self.assertEqual(self.run_silently(R.cmd_selftest, ['-q', '-k', 'TestAlignUp']), 0)

    def test_main_dispatches_selftest_and_exits_with_its_code(self):
        for alias in ('selftest', 'test'):
            with self.subTest(alias=alias):
                with mock.patch.object(sys, 'argv', ['rdc_analysis.py', alias, '-k', 'TestAlignUp']):
                    with mock.patch.object(sys, 'exit') as exit_mock:
                        self.run_silently(R.main)
                exit_mock.assert_called_once_with(0)


# =========================================================================== real capture
class TestRealCapture(unittest.TestCase):
    """End-to-end smoke test on a real capture, if one is pointed at via $RDC_TEST_CAPTURE."""

    #: set by `setUp` (a real `.rdc` path, or None when the test is skipped).
    path: str

    def setUp(self):
        found = os.environ.get('RDC_TEST_CAPTURE')
        if not found or not os.path.isfile(found):
            self.skipTest('set RDC_TEST_CAPTURE to a real .rdc file to run this')
        self.path = found

    def test_container_and_stream(self):
        info, stream, how = R.load_stream(self.path)
        self.assertGreater(len(info['sections']), 0)
        self.assertGreater(len(stream), 0)
        self.assertTrue(how)

    def test_chunks_are_walked(self):
        _, stream, _ = R.load_stream(self.path)
        chunks = list(R.iter_chunks(stream))
        self.assertGreater(len(chunks), 0)
        for ch in chunks[:50]:
            self.assertGreaterEqual(ch['id'], 1)
            self.assertLessEqual(ch['length'], len(stream))

    def test_commands_run_without_crashing(self):
        for cmd in (R.cmd_sections, R.cmd_summary, R.cmd_markers, R.cmd_chunks, R.cmd_draws,
                    R.cmd_dxbc, R.cmd_sig, R.cmd_rootconst):
            with self.subTest(cmd=cmd.__name__):
                self.assertTrue(capture_text(cmd, self.path).strip())


if __name__ == '__main__':
    unittest.main(verbosity=2)
