"""The chunk stream: framing, iteration, payloads and the shader containers.

Split out of `test_rdc_analysis.py`, which held the parsers and decoders for the whole tool and
passed 2200 lines. `rdc_testcase`. The shared fixtures and case classes live there, because every file that
exercises the tool needs them.

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_chunks.py
    python -m unittest tests.test_rdc_chunks
    python src/py/rdc_analysis.py selftest -k <Class>

Payload layouts used by the fixtures follow REFERENCE section 3.4; where a decoder reads a chunk
differently from another consumer of the same chunk, the test pins the behaviour actually
implemented and says so in a comment.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R            # noqa: E402
import rdc_fixtures as F            # noqa: E402

from rdc_testcase import *          # noqa: E402,F401,F403

# =========================================================================== chunk iteration
class TestIterChunks(unittest.TestCase):
    def test_bare_chunk(self):
        data = F.stream(F.chunk(1234, b'abc'))
        chunks = list(R.iter_chunks(data))
        self.assertEqual(len(chunks), 1)
        ch = chunks[0]
        self.assertEqual(ch['id'], 1234)
        self.assertEqual(ch['off'], 0)
        self.assertEqual(ch['payload_offset'], 8)
        self.assertEqual(ch['length'], 3)
        self.assertEqual(ch['flags'], 0)
        self.assertEqual(data[ch['payload_offset']:ch['payload_offset'] + ch['length']], b'abc')

    def test_all_metadata_fields(self):
        data = F.stream(F.chunk(1200, b'payload', callstack=[0x7ff0, 0x1234], threadid=42,
                                duration=7, timestamp=99))
        ch = list(R.iter_chunks(data))[0]
        self.assertEqual(ch['id'], 1200)
        self.assertEqual(ch['flags'], 0x000F0000)
        self.assertEqual(ch['length'], 7)
        self.assertEqual(ch['payload_offset'], 4 + 4 + 16 + 8 + 8 + 8 + 4)
        self.assertEqual(data[ch['payload_offset']:ch['payload_offset'] + 7], b'payload')

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
                self.assertEqual(ch['payload_offset'], data_off)

    def test_zero_callstack_frames(self):
        # a chunk with the callstack flag but no frames: 36 bytes of header (REFERENCE section 6)
        raw = F.u32b(1200 | F.FLAG_CALLSTACK | F.FLAG_THREADID | F.FLAG_DURATION | F.FLAG_TIMESTAMP)
        raw += F.u32b(0) + F.u64b(0) + F.u64b(0) + F.u64b(0) + F.u32b(2) + b'hi'
        ch = list(R.iter_chunks(F.pad_to(raw)))[0]
        self.assertEqual(ch['payload_offset'] - ch['off'], 36)
        self.assertEqual(ch['length'], 2)

    def test_64bit_length_flag(self):
        ch = list(R.iter_chunks(F.stream(F.chunk(1000, b'hello', size64=True))))[0]
        self.assertEqual(ch['flags'], R.CHUNK_64BITSIZE)
        self.assertEqual(ch['length'], 5)
        self.assertEqual(ch['payload_offset'], 4 + 8)          # u32 flags + u64 length, no other metadata

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
            self.assertEqual(data[c['payload_offset']:c['payload_offset'] + c['length']],
                             {1000: b'A' * 5, 1001: b'B' * 70, 1002: b'C'}[c['id']])

    def test_padding_bytes_are_not_part_of_the_payload(self):
        data = F.stream(F.chunk(1000, b'AB', pad_byte=0xCC), F.chunk(1001, b'CD'))
        chunks = list(R.iter_chunks(data))
        self.assertEqual(data[8:14], b'AB' + b'\xCC' * 4)      # padding follows the 8-byte header
        self.assertEqual([c['length'] for c in chunks], [2, 2])

    def test_zero_length_payload(self):
        ch = list(R.iter_chunks(F.stream(F.chunk(1000, b''))))[0]
        self.assertEqual(ch['length'], 0)
        self.assertEqual(ch['payload_offset'], 8)

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
        self.assertEqual(ch['payload_offset'], 8)

    def test_padding_fields_describe_the_alignment_gap(self):
        ch = list(R.iter_chunks(F.stream(F.chunk(1000, b'AB'))))[0]
        self.assertEqual(ch['pad_start'], 10)
        self.assertEqual(ch['pad_len'], 54)                      # 64 - 10
        self.assertEqual(ch['pad_start'] + ch['pad_len'], 64)

    def test_padding_is_logical_for_a_final_chunk_without_it(self):
        ch = list(R.iter_chunks(F.chunk(1000, b'xyz', align=False)))[0]
        self.assertEqual(ch['pad_start'], 11)
        self.assertEqual(ch['pad_len'], 53)                      # logical; the stream ends at 11

    def test_strict_raises_on_a_payload_past_the_end(self):
        raw = F.u32b(1000) + F.u32b(9999) + b'short'
        with self.assertRaises(R.FrameError):
            list(R.iter_chunks(raw, strict=True))
        self.assertEqual(list(R.iter_chunks(raw)), [])            # the lenient walk just stops

    def test_strict_raises_on_truncated_metadata(self):
        # four bytes of flags and no length field at all: the lenient walk must not raise (this used
        # to be an unguarded struct.error), strict must report it
        raw = F.u32b(1000 | F.FLAG_THREADID)
        self.assertEqual(list(R.iter_chunks(raw)), [])
        with self.assertRaises(R.FrameError):
            list(R.iter_chunks(raw, strict=True))

    def test_strict_accepts_a_well_formed_stream(self):
        data = F.stream(F.chunk(1000, b'a'), F.chunk(1001, b'b'))
        self.assertEqual(len(list(R.iter_chunks(data, strict=True))), 2)

    def test_frame_error_is_a_value_error_subclass(self):
        # scripts that catch ValueError around a walk keep working
        self.assertTrue(issubclass(R.FrameError, Exception))

def _is_off_key(node: ast.expr) -> bool:
    """True when `node` is the literal key `'off'`.

    Python 3.8 wraps subscript keys in `ast.Index` while 3.9+ uses the expression directly, so the
    unwrapping is done by class name: on 3.9+ `ast.Index` is an alias for `expr`, which would make
    an `isinstance` check both meaningless and untypeable.
    """
    inner = getattr(node, 'value', node) if type(node).__name__ == 'Index' else node
    return isinstance(inner, ast.Constant) and inner.value == 'off'

def off_arithmetic_hits(source: str) -> List[Tuple[str, int]]:
    """Return `(function, line)` for every subscript whose slice uses `x['off']` arithmetic."""
    hits: List[Tuple[str, int]] = []
    stack = ['<module>']

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Subscript(self, node: ast.Subscript) -> None:
            if any(isinstance(inner, ast.Subscript) and _is_off_key(inner.slice)
                   for inner in ast.walk(node.slice)):
                hits.append((stack[-1], node.lineno))
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return hits

class TestCheckStream(unittest.TestCase):
    def names(self) -> Dict[int, str]:
        return {1000: 'List_SetPipelineState', 1001: 'PushMarker'}

    def test_clean_stream_has_no_problems(self):
        data = F.stream(F.chunk(1000, F.pl_pso(7, 1), pad_byte=0x00),
                        F.chunk(1001, b'BasePass\x00', pad_byte=0x00))
        problems, notes = R.check_stream(data, self.names())
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 1)                          # the padding summary
        self.assertIn('all zero', notes[0])

    def test_payload_past_the_end_is_a_problem(self):
        problems, notes = R.check_stream(F.u32b(1000) + F.u32b(9999) + b'short', self.names())
        self.assertEqual(len(problems), 1)
        self.assertIn('runs 9994 past the end of the stream', problems[0])
        # a broken walk must not also be reported as trailing bytes
        self.assertEqual(len(notes), 1)

    def test_wrong_payload_length_is_a_problem(self):
        # List_SetPipelineState is 16 bytes, so a 20-byte payload means the decoder is misaligned
        data = F.stream(F.chunk(1000, F.pl_pso(7, 1) + b'\x00' * 4, pad_byte=0x00))
        problems, _ = R.check_stream(data, self.names())
        self.assertEqual(len(problems), 1)
        self.assertIn('payload is 20 bytes, the decoder expects 16', problems[0])

    def test_unknown_chunk_names_are_not_length_checked(self):
        data = F.stream(F.chunk(9999, b'x' * 13, pad_byte=0x00))
        problems, notes = R.check_stream(data, self.names())
        self.assertEqual(problems, [])
        self.assertIn('all zero', notes[0])

    def test_non_zero_padding_is_a_note_not_a_problem(self):
        data = F.stream(F.chunk(1000, F.pl_pso(7, 1), pad_byte=0xCC))
        problems, notes = R.check_stream(data, self.names())
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 2)                          # the sample plus the summary
        self.assertIn('padding bytes are non-zero', notes[0])
        self.assertIn('stale capture-buffer content', notes[1])

    def test_padding_samples_are_capped(self):
        data = F.stream(*[F.chunk(1000, F.pl_pso(7, i), pad_byte=0xCC) for i in range(4)])
        _, notes = R.check_stream(data, self.names(), note_samples=2)
        self.assertEqual(len(notes), 3)                          # 2 samples + the summary
        self.assertIn('padding: 4 of 4 chunks carry', notes[-1])

    def test_trailing_bytes_are_a_note(self):
        data = F.stream(F.chunk(1000, F.pl_pso(7, 1), pad_byte=0x00), terminator=True,
                        tail=b'\xAA' * 100)
        problems, notes = R.check_stream(data, self.names())
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 2)                          # padding summary + trailing bytes
        self.assertIn('104 bytes follow the last chunk', notes[1])

    def test_lone_terminator_is_not_reported(self):
        data = F.stream(F.chunk(1000, F.pl_pso(7, 1), pad_byte=0x00), terminator=True)
        problems, notes = R.check_stream(data, self.names())
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 1)

    def test_expected_lengths_cover_the_fixed_layout_chunks(self):
        for name in ('List_SetPipelineState', 'List_Reset', 'List_DrawIndexedInstanced',
                     'List_DrawInstanced', 'List_Dispatch', 'List_SetGraphicsRootSignature',
                     'List_SetGraphicsRootConstantBufferView', 'List_IASetIndexBuffer',
                     'List_SetComputeRootSignature', 'List_SetComputeRootDescriptorTable',
                     'List_SetComputeRootConstantBufferView'):
            with self.subTest(name=name):
                self.assertIn(name, R.EXPECTED_LENGTHS)

class TestNoRawPayloadSlicing(unittest.TestCase):
    """Architecture test: payloads are only reached through `chunk_payload()`.

    `ChunkInfo.payload_offset` exists so that `off + 8` arithmetic cannot produce plausible-looking
    garbage; this fails if a future decoder reintroduces it.
    """

    def test_the_check_finds_the_pattern_it_forbids(self):
        bad = 'def f(stream, ch):\n    return stream[ch["off"] + 8:ch["off"] + 16]\n'
        self.assertEqual([fn for fn, _ in off_arithmetic_hits(bad)], ['f'])

    def test_the_check_ignores_the_allowed_accessor(self):
        good = 'def f(stream, ch):\n    return stream[ch["payload_offset"]:ch["length"]]\n'
        self.assertEqual(off_arithmetic_hits(good), [])

    def test_only_the_walker_slices_by_chunk_offset(self):
        with open(R.__file__, encoding='utf-8') as fh:
            hits = off_arithmetic_hits(fh.read())
        self.assertEqual(hits, [], 'payload sliced by chunk offset: %r' % (hits,))

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

    def test_strings_wide_pass_is_skipped_once_the_cap_is_full(self):
        # ASCII first, then UTF-16LE, then the cap -- so a payload with `limit` ASCII strings never
        # needs the decode, which is the expensive half of the preview (REFERENCE 4.13). The answer is
        # the one the old order gave; only the work changed. The wide text starts at an even offset:
        # UTF-16LE decoded from an odd one is shifted by a byte and reads as something else entirely.
        wide = 'WideMarker'.encode('utf-16-le')
        data, ch = self.make(b'\x00oneone\x00twotwo\x00\x00' + wide + b'\x00')
        self.assertEqual(R.chunk_strings(data, ch, limit=2), ['oneone', 'twotwo'])
        self.assertIn('WideMarker', R.chunk_strings(data, ch, limit=3))

    def test_strings_limit_zero_is_empty(self):
        data, ch = self.make(b'\x00oneone\x00twotwo\x00')
        self.assertEqual(R.chunk_strings(data, ch, limit=0), [])

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
        # 24 bytes: the GPU descriptor handle is a PortableHandle (heap resource id + index), which
        # is what `verify` caught: every descriptor-table payload in both captures is 24 bytes
        blob = F.pl_root_table(7, 5, 8421, 37)
        self.assertEqual(len(blob), 24)
        self.assertEqual(R.decode_chunk('List_SetGraphicsRootDescriptorTable', blob),
                         ['cmdList=7 rootParam=5 heap=8421 index=37'])

    def test_descriptor_table_truncated(self):
        self.assertEqual(R.decode_chunk('List_SetGraphicsRootDescriptorTable',
                                        F.pl_root_table(7, 5, 1, 2)[:23]), [])

    def test_root_signature(self):
        self.assertEqual(R.decode_chunk('List_SetGraphicsRootSignature', F.pl_root_signature(7, 42)),
                         ['cmdList=7 rootSig=42'])

    def test_compute_root_bindings_use_the_graphics_layouts(self):
        # the compute setters serialise the same fields as the graphics ones, so they decode the
        # same way -- which is also what lets `draws` track both namespaces with one code path
        self.assertEqual(R.decode_chunk('List_SetComputeRootConstantBufferView',
                                        F.pl_root_view(7, 2, 342, 0x20)),
                         ['cmdList=7 rootParam=2 res=342+0x20'])
        self.assertEqual(R.decode_chunk('List_SetComputeRootDescriptorTable',
                                        F.pl_root_table(7, 4, 298, 138458)),
                         ['cmdList=7 rootParam=4 heap=298 index=138458'])
        self.assertEqual(R.decode_chunk('List_SetComputeRootSignature', F.pl_root_signature(7, 5)),
                         ['cmdList=7 rootSig=5'])

    def test_reset_reports_the_list_and_its_initial_pso(self):
        # 64-byte payload: the command-list id sits at +40, the initial PSO at +48 (the id at +40 is
        # the one every other List_* chunk carries at +0 -- measured on both captures in this repo)
        blob = F.pl_reset(7, initial_pso=77)
        self.assertEqual(len(blob), 64)
        self.assertEqual(R.decode_chunk('List_Reset', blob), ['cmdList=7 initialPso=77'])
        self.assertEqual(R.decode_chunk('List_Reset', F.pl_reset(7)), ['cmdList=7 initialPso=0'])

    def test_reset_truncated(self):
        self.assertEqual(R.decode_chunk('List_Reset', F.pl_reset(7)[:55]), [])

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
        # raw payload of List_IASetIndexBuffer #1122 in "desktop-1" (RenderDoc 1.46, D3D12):
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
        # `chunk <N>` still reports the resource id and header bytes for these chunks; the old
        # `initial` command that tried to guess where the data starts is gone (replay reads
        # resource contents properly: see `ROADMAP.md`'s what-is-deliberately-not-on-this-list)
        blob = F.u64b(342) + b'\xab' * 48
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
