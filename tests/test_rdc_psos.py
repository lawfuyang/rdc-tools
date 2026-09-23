"""`psos`: the PSO/shader index, the hash lookup, and the sidecar that makes the lookup O(1).

The index is a property of the *stream* -- which is what lets it be cached beside the stream cache entry
(`rdc_cache.sidecar_path`) -- and what it holds is read from three places at once: the payload's tail
(whose id, and which stages), the payloads' containers (which hashes), and the stream's own container
inventory (where each container is, its `HASH` part, and whether it carries `ILDB`). Every one of those
can be read *plausibly* and be wrong, which is what this file pins:

* the tail's framing: an array form writes its element count before the ids, so the id sits at `n - 80`
  for the eight-id form -- reading `n - 72` answers with the count word (the id `8` on a real capture) and
  every PSO then looks bound to nothing;
* the pairing: a stage is only *labelled* when the number of containers found equals the number of
  non-empty inline ids, and a root-signature blob in the same payload is not a stage;
* the two hashes: a container's header hash and its `HASH` part's digest are different values, and a
  lookup by either has to find the same container -- the digests' own convention (`dxc -Fd` names a PDB
  after the second) is what a reader copies out of a log;
* the cache: a sidecar can only ever answer with what this build computes, so a wrong version, a stream it
  was not written for, or corruption falls back to the scan.

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_psos.py
    python -m unittest tests.test_rdc_psos
    python src/py/rdc_analysis.py selftest -k psos
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import unittest
from typing import Any, Dict, Optional, Sequence, Tuple
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R            # noqa: E402
import rdc_cache                    # noqa: E402  (the sidecar naming, and the suffix list)
import rdc_fixtures as F            # noqa: E402
import rdc_psos                     # noqa: E402  (patched by name: the index scans through it)

from rdc_testcase import *          # noqa: E402,F401,F403

# =========================================================================== the index
class PsosCase(CmdCase):
    """Base: fixtures that build a capture whose PSOs and containers a test can reason about."""

    #: A container's header hash (the tool's own key) and a `HASH` part digest (what a PDB is named after),
    #: as six-byte prefixes the assertions can use -- six because the listing prints twelve hex characters.
    HEADER_A = bytes(range(16))
    HEADER_B = bytes(range(16, 32))
    SHADER_A = bytes(range(32, 48))
    SHADER_B = bytes(range(48, 64))

    def shader(self, header: bytes, shader_hash: Optional[bytes] = None, ilbd: bool = False,
               parts: Sequence[Tuple[str, bytes]] = ()) -> bytes:
        """One DXBC/DXIL container as the captures carry them: its parts, both hashes, the `ILDB` part.

        `HASH` is 20 bytes on every container measured in this repo's captures, the digest being its last
        16 -- which is why the fixture writes a four-byte prefix rather than the digest alone.
        """
        body: list = [('DXIL', b'\x01\x02\x03\x04'), ('SFI0', b'\x00' * 8)] + list(parts)
        if shader_hash is not None:
            body.append(('HASH', b'\x00' * 4 + shader_hash))
        if ilbd:
            body.append(('ILDB', b'\xab' * 16))
        return F.dxbc(body, hash_=header)    # type: ignore[arg-type]

    def set_pso(self, cmd_list: int, pso: int) -> bytes:
        """A `List_SetPipelineState` payload: the command list's id, then the PSO's (`rdc_payloads.py`)."""
        return F.u64b(cmd_list) + F.u64b(pso)

    def stream_of(self, *chunks: bytes) -> R.Buffer:
        path = self.cap(*chunks)
        _info, stream, _how = R.load_stream(path)
        return stream

    def source_for(self, stream: R.Buffer, name: str = 'psos.bin') -> R.CacheEntry:
        """A cache-file stand-in beside the stream: the sidecar lands next to it, in this test's root."""
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as fh:
            fh.write(b'RDCCACHE' + b'\x00' * 8)
            fh.write(stream)
        return {'file': path, 'hdrLen': 16, 'streamLen': len(stream), 'section': 0, 'method': 1,
                'blocks': 1, 'srcPath': 'test', 'srcSize': len(stream), 'srcMtime': 1}

    def sidecar_document(self, source: R.CacheEntry) -> Dict[str, Any]:
        path = rdc_cache.sidecar_path(source, R.PSOS_SUFFIX)
        self.assertTrue(os.path.exists(path), 'no sidecar was written at %s' % path)
        with open(path, encoding='utf-8') as fh:
            document: Dict[str, Any] = json.load(fh)
        return document

    def rewrite_sidecar(self, source: R.CacheEntry, document: Dict[str, Any]) -> None:
        with open(rdc_cache.sidecar_path(source, R.PSOS_SUFFIX), 'w', encoding='utf-8') as fh:
            json.dump(document, fh)


class TestBuildIndex(PsosCase):
    """What one pass over the file answers: ids, stages, hashes, the debug flag and the bindings."""

    def test_each_pso_is_listed_with_its_stages(self):
        vs, ps = self.shader(self.HEADER_A), self.shader(self.HEADER_B)
        path = self.cap(self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(2294, [('VS', vs), ('PS', ps)])))
        out = capture_text(R.cmd_psos, path)
        self.assertIn('2294', out)
        self.assertIn('graphics', out)
        self.assertIn('VS %s' % self.HEADER_A.hex()[:12], out)
        self.assertIn('PS %s' % self.HEADER_B.hex()[:12], out)

    def test_a_compute_pso_is_one_stage_and_says_compute(self):
        path = self.cap(self.ch('Device_CreateComputePipeline',
                                F.pl_create_pso_stages(418, [('CS', self.shader(self.HEADER_A))],
                                                form='Device_CreateComputePipeline')))
        out = capture_text(R.cmd_psos, path)
        self.assertIn('418', out)
        self.assertIn('compute', out)
        self.assertIn('CS %s' % self.HEADER_A.hex()[:12], out)
        self.assertIn('1 pso(s)', out)

    def test_an_older_graphics_form_is_read_with_its_own_framing(self):
        # The form's five ids are an array too, so the id sits at `n - 56`; and its bytecode order (VS, PS,
        # DS, HS, GS) differs from its ids' (VS, HS, DS, GS, PS), so a payload whose stages are paired by
        # the wrong order would label these backwards.
        vs, ps = self.shader(self.HEADER_A), self.shader(self.HEADER_B)
        path = self.cap(self.ch('Device_CreateGraphicsPipeline',
                                F.pl_create_pso_stages(11366, [('PS', ps), ('VS', vs)],
                                                form='Device_CreateGraphicsPipeline')))
        out = capture_text(R.cmd_psos, path)
        self.assertIn('11366', out)
        first = out.index('VS %s' % self.HEADER_A.hex()[:12])
        second = out.index('PS %s' % self.HEADER_B.hex()[:12])
        self.assertLess(first, second, 'the vertex stage was not listed first')

    def test_the_bindings_a_command_list_makes_are_counted(self):
        vs = self.shader(self.HEADER_A)
        path = self.cap(self.ch('Device_CreatePipelineState', F.pl_create_pso_stages(2294, [('VS', vs)])),
                        self.ch('List_SetPipelineState', self.set_pso(7, 2294)),
                        self.ch('List_SetPipelineState', self.set_pso(8, 2294)))
        out = capture_text(R.cmd_psos, path)
        row = next(line for line in out.splitlines() if line.startswith('2294'))
        self.assertEqual(row.split()[2], '2')

    def test_a_container_carrying_ilbd_needs_no_pdb_and_one_without_does(self):
        path = self.cap(self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(1, [('VS', self.shader(self.HEADER_A, ilbd=True))])),
                        self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(2, [('PS', self.shader(self.HEADER_B,
                                                                       self.SHADER_A))])))
        out = capture_text(R.cmd_psos, path)
        rows = {line.split()[0]: line for line in out.splitlines() if line[:1].isdigit()}
        self.assertIn('ilbd', rows['1'])
        self.assertIn('pdb', rows['2'])
        self.assertIn('1 of 2 pso-bound shader(s) carry ILDB', out)

    def test_a_root_signature_blob_in_the_payload_is_not_a_stage(self):
        # A `RTS0` container is a DXBC container too, and a payload holds one in its root-signature field:
        # counted as a stage it would shift every label, so it is skipped by its parts.
        vs, ps = self.shader(self.HEADER_A), self.shader(self.HEADER_B)
        rts = F.dxbc([('RTS0', b'\x01\x00' * 8)])
        path = self.cap(self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(9, [('VS', vs), ('PS', ps)], root_blob=rts)))
        out = capture_text(R.cmd_psos, path)
        self.assertIn('VS %s' % self.HEADER_A.hex()[:12], out)
        self.assertIn('PS %s' % self.HEADER_B.hex()[:12], out)

    def test_a_tail_that_is_not_this_layout_is_counted_not_guessed(self):
        # An older capture has no inline ids at all: its tail is the desc's own fields, so the count word
        # is not there and the plausibility check refuses the payload -- which must be *said*, because a
        # listing that quietly omits a PSO looks like a capture that has none.
        vs = self.shader(self.HEADER_A)
        path = self.cap(self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(2294, [('VS', vs)], count_word=7)))
        out = capture_text(R.cmd_psos, path)
        self.assertIn('0 pso(s)', out)
        self.assertIn('1 pipeline payload(s) had no tail this reads', out)

    def test_a_pso_whose_tail_plausibly_parses_but_holds_no_shader_is_listed_empty(self):
        # Not a refusal: the tail is a real one (a compute pipeline may legitimately have one container),
        # so the row exists with no stages rather than being dropped.
        path = self.cap(self.ch('Device_CreateComputePipeline',
                                F.pl_create_pso_stages(418, [('CS', b'')],
                                                form='Device_CreateComputePipeline')))
        out = capture_text(R.cmd_psos, path)
        self.assertIn('418', out)


class TestHashLookup(PsosCase):
    """`--hash`: either hash, by prefix, with the debug answer and every pipeline that uses it."""

    def capture(self, ilbd: bool = False) -> str:
        return self.cap(self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(3042, [('VS', self.shader(self.HEADER_A)),
                                                       ('PS', self.shader(self.HEADER_B,
                                                                          self.SHADER_A,
                                                                          ilbd=ilbd))])),
                        self.ch('List_SetPipelineState', self.set_pso(7, 3042)))

    def test_the_shader_hash_finds_the_container_and_names_the_pdb(self):
        path = self.capture()
        out = capture_text(R.cmd_psos, path, 40, 'table', self.SHADER_A.hex())
        self.assertIn('shader hash', out)
        self.assertIn(self.SHADER_A.hex(), out)
        self.assertIn('the name a PDB for this shader takes', out)
        self.assertIn('pso 3042 (graphics), PS', out)
        self.assertIn('1 SetPipelineState call(s)', out)

    def test_the_header_hash_finds_the_same_container(self):
        path = self.capture()
        out = capture_text(R.cmd_psos, path, 40, 'table', self.HEADER_B.hex())
        self.assertIn('container hash', out)
        self.assertIn(self.HEADER_B.hex(), out)
        self.assertIn('pso 3042 (graphics), PS', out)

    def test_the_two_hashes_are_not_the_same_value(self):
        # The whole point of the lookup carrying both: an engine's log names a PDB after the `HASH` part,
        # and that value is not the container's header hash.
        path = self.capture()
        out = capture_text(R.cmd_psos, path, 40, 'table', self.SHADER_A.hex())
        self.assertIn('container : @', out)
        self.assertNotIn('(%s hash)' % 'container', out)
        self.assertNotEqual(self.SHADER_A, self.HEADER_B)

    def test_a_container_serialised_twice_is_one_answer_with_two_offsets(self):
        # A stream holds a copy wherever a container is serialised: two pipelines using one shader give that
        # shader two offsets, and reporting them as two shaders would be wrong.
        path = self.cap(self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(1, [('VS', self.shader(self.HEADER_A))])),
                        self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(2, [('VS', self.shader(self.HEADER_A))])))
        out = capture_text(R.cmd_psos, path, 40, 'table', self.HEADER_A.hex()[:8])
        self.assertIn('2 identical copies in the stream', out)
        self.assertIn('used by   : pso 1 (graphics), VS', out)
        self.assertIn('used by   : pso 2 (graphics), VS', out)

    def test_a_prefix_that_matches_two_containers_prints_both_and_says_how_many(self):
        # An eight-character prefix is specific enough to be worth answering, and short enough to be
        # ambiguous: both matches are printed, under one line that says how many there were.
        first = bytes([0, 1, 2, 3]) + bytes(range(64, 76))
        second = bytes([0, 1, 2, 3]) + bytes(range(80, 92))
        path = self.cap(self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(1, [('VS', self.shader(first))])),
                        self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(2, [('VS', self.shader(second))])))
        out = capture_text(R.cmd_psos, path, 40, 'table', first.hex()[:8])
        self.assertIn('2 container(s) match that prefix', out)
        self.assertIn(first.hex(), out)
        self.assertIn(second.hex(), out)

    def test_a_hash_that_is_not_in_the_capture_exits_1(self):
        path = self.capture()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = R.cmd_psos(path, 40, 'table', 'deadbeefdeadbeef')
        self.assertEqual(code, 1)
        self.assertIn('not in this capture', err.getvalue())

    def test_a_container_no_pipeline_binds_says_so(self):
        # The stream is full of containers no pipeline binds -- root-signature blobs are DXBC containers
        # too -- and the answer has to survive being asked about one, rather than reporting an empty list.
        rts_hash = bytes([0x11] * 16)          # the container `pl_create_root_sig` builds
        path = self.cap(self.ch('Device_CreateRootSignature', F.pl_create_root_sig(90, b'\x01\x00' * 8)),
                        self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(2294, [('VS', self.shader(self.HEADER_A))])))
        out = capture_text(R.cmd_psos, path, 40, 'table', rts_hash.hex())
        self.assertIn('no pipeline in this capture binds it', out)
        self.assertIn('RTS0', out)

    def test_the_bytes_hash_is_the_digest_of_the_container(self):
        # The identity the tool's *other* half prints: a bundle's per-stage `hash` is `sha256(rawBytes)`
        # (`commands_state.cpp`), measured to land on a container of the capture it came from -- 3 of 3
        # stages of `desktop-1`'s event 1003. A caller holding one of those must not be told it is not in
        # the capture, which is what the lookup would say with only the two container-side hashes keyed.
        vs = self.shader(self.HEADER_A)
        digest = hashlib.sha256(vs).hexdigest()
        path = self.cap(self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(2294, [('VS', vs)])))
        out = capture_text(R.cmd_psos, path, 40, 'table', digest[:16])
        self.assertIn('bytes hash (sha256)', out)
        self.assertIn('pso 2294 (graphics), VS', out)
        self.assertIn('sha256 of the container', out)

    def test_the_debug_answer_says_which_case_it_is(self):
        embedded = capture_text(R.cmd_psos, self.capture(ilbd=True), 40, 'table',
                                self.SHADER_A.hex())
        self.assertIn('embedded (the ILDB part)', embedded)
        missing = capture_text(R.cmd_psos, self.capture(), 40, 'table', self.SHADER_A.hex())
        self.assertIn('not embedded', missing)
        self.assertIn('--pdb', missing)


class TestIndexCache(PsosCase):
    """The sidecar: written beside the stream, reused, and never trusted beyond what it can prove."""

    def stream(self) -> R.Buffer:
        return self.stream_of(self.ch('Device_CreatePipelineState',
                                      F.pl_create_pso_stages(2294, [('VS', self.shader(self.HEADER_A))])))

    def test_the_index_is_written_beside_the_stream_and_reused(self):
        stream = self.stream()
        source = self.source_for(stream)
        index = R.shader_index(stream, source)
        self.assertEqual([row['id'] for row in index['psos']], [2294])
        document = self.sidecar_document(source)
        self.assertEqual(document['version'], R.PSOS_VERSION)
        self.assertEqual(document['streamLen'], len(stream))
        self.assertEqual(document['psos'][0][0], 2294)
        self.assertEqual(document['containers'][0][2], self.HEADER_A.hex())
        self.assertEqual(len(document['containers'][0]), 7, 'a container row grew or lost a column')
        self.assertEqual(len(document['containers'][0][4]), 64, 'the sha256 column is not a digest')
        with mock.patch.object(rdc_psos, 'parse_dxil_containers',
                               side_effect=AssertionError('scanned the stream again')):
            self.assertEqual([row['id'] for row in R.shader_index(stream, source)['psos']], [2294])

    def test_no_source_means_no_sidecar(self):
        # Nothing is written without a stream-cache entry to name the file after -- the `$RDC_NO_CACHE`
        # path and the goldens harness's path both take this branch.
        before = rdc_cache.derived_names()
        self.assertEqual([row['id'] for row in R.shader_index(self.stream())['psos']], [2294])
        self.assertEqual(rdc_cache.derived_names(), before)

    def test_a_sidecar_from_another_version_is_ignored(self):
        stream = self.stream()
        source = self.source_for(stream)
        R.shader_index(stream, source)
        document = self.sidecar_document(source)
        document['version'] = R.PSOS_VERSION + 1
        self.rewrite_sidecar(source, document)
        with mock.patch.object(rdc_psos, 'parse_dxil_containers',
                               wraps=rdc_psos.parse_dxil_containers) as scan:
            R.shader_index(stream, source)
        self.assertTrue(scan.called, 'a sidecar from another version was trusted')

    def test_a_sidecar_from_another_stream_is_ignored(self):
        stream = self.stream()
        source = self.source_for(stream)
        R.shader_index(stream, source)
        document = self.sidecar_document(source)
        document['digest'] = 'not this stream'
        self.rewrite_sidecar(source, document)
        with mock.patch.object(rdc_psos, 'parse_dxil_containers',
                               wraps=rdc_psos.parse_dxil_containers) as scan:
            R.shader_index(stream, source)
        self.assertTrue(scan.called, 'a sidecar for another stream was trusted')

    def test_a_corrupt_sidecar_falls_back_to_the_scan(self):
        stream = self.stream()
        source = self.source_for(stream)
        R.shader_index(stream, source)
        path = rdc_cache.sidecar_path(source, R.PSOS_SUFFIX)
        for junk in ('not json at all', '[]', '{"version": 3, "psos": "nope"}',
                     '{"version": 3, "streamLen": 0, "digest": "", "psos": [], "containers": []}',
                     '{"version": 3, "streamLen": %d, "digest": "%s", "psos": [[1]], "containers": [[]]}'
                     % (len(stream), rdc_cache.stream_digest(stream))):
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(junk)
            self.assertEqual([row['id'] for row in R.shader_index(stream, source)['psos']], [2294], junk)

    def test_the_sidecar_suffix_is_one_the_cache_clears(self):
        # `cache clear` sweeps the suffixes `rdc_cache` knows; a sidecar outside that list would be an
        # orphan the moment the stream it describes was reclaimed.
        self.assertIn(R.PSOS_SUFFIX, rdc_cache.DERIVED_SUFFIXES)


class TestPsosCli(PsosCase):
    """The entry point: the usage errors, and the CSV shape that has to stay machine-readable."""

    def run_main(self, argv: Sequence[str]) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, 'argv', list(argv)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    R.main()
                except SystemExit as exit_code:
                    code = exit_code.code if isinstance(exit_code.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def capture(self) -> str:
        return self.cap(self.ch('Device_CreatePipelineState',
                                F.pl_create_pso_stages(2294, [('VS', self.shader(self.HEADER_A))])))

    def test_an_unknown_format_is_a_usage_error(self):
        # The usage lines go to stdout here, as every other command's do (the tests of `cache`'s own
        # unknown subcommand read it the same way); a bad `--format` is a startup error, not a table.
        code, out, _err = self.run_main(['rdc_analysis.py', 'psos', self.capture(), '--format', 'xml'])
        self.assertEqual(code, 2)
        self.assertIn('usage: rdc_analysis.py psos', out)

    def test_a_hash_with_no_value_is_a_usage_error(self):
        code, out, _err = self.run_main(['rdc_analysis.py', 'psos', self.capture(), '--hash'])
        self.assertEqual(code, 2)
        self.assertIn('--hash <hash>', out)

    def test_csv_is_the_table_alone_with_the_prose_on_stderr(self):
        code, out, err = self.run_main(['rdc_analysis.py', 'psos', self.capture(), '--format', 'csv'])
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], 'pso,kind,refs,debug,shaders')
        self.assertEqual(len(lines), 2)
        self.assertIn('psos: 1 pso(s)', err)
        self.assertNotIn('psos: 1 pso(s)', out)

    def test_a_hash_that_is_missing_leaves_through_the_entry_point_as_1(self):
        code, _out, err = self.run_main(['rdc_analysis.py', 'psos', self.capture(), '--hash',
                                         'deadbeefdeadbeef'])
        self.assertEqual(code, 1)
        self.assertIn('not in this capture', err)


if __name__ == '__main__':
    unittest.main()
