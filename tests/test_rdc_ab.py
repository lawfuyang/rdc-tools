"""Tests for the offline A/B: `passdiff` (two captures' marker trees) and `replaydiff` (two bundles).

Three areas, one file, because they are one feature: the marker tree the two capture-side commands share
(`rdc_passdiff`), the comparison of two bundles (`rdc_ab`) and the pictures it compares (`rdc_image`).

The `.rdc` side is built chunk by chunk through `CmdCase`'s fake chunk-name tree -- a real capture's
markers are a stream of `PushMarker`/`PopMarker` chunks, so a fixture that writes them is the same shape
the tool reads. The bundle side reuses `test_rdc_report`'s bundle writer rather than a second copy of it:
the two files would otherwise be two answers to "what does `dump` write", and only one of them is checked
against the driver.

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_ab.py
    python -m unittest tests.test_rdc_ab
    python rdc_analysis.py selftest -k Ab
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from typing import Any, Dict, List, Optional, Sequence
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402
import rdc_chunkmap as chunkmap   # noqa: E402
import rdc_fixtures as F          # noqa: E402
import rdc_image                  # noqa: E402
from rdc_testcase import CmdCase as _CmdCase   # noqa: E402
from test_rdc_report import cbuffer, event, resource, write_bundle   # noqa: E402


def capture_text(func: Any, *args: Any, **kwargs: Any) -> str:
    """Run `func` and return everything it printed on stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


# =========================================================================== the PNG reader and writer
def png_with_filters(width: int, height: int, rows: Sequence[Sequence[int]],
                     filters: Sequence[int], rows_in_data: Optional[int] = None) -> bytes:
    """A hand-built 8-bit RGBA PNG that applies the given filter to each row.

    Written here rather than through `rdc_image.encode_png` (which only writes filter 0) because the
    *decoder* is what is under test: to check that it undoes Sub, Up, Average and Paeth, something has to
    produce those rows, and a reader that tested itself with its own writer would only ever see filter 0.

    `rows_in_data` writes fewer rows into the image data than the header declares, which is how a
    truncated file looks to a decoder that has already read the header.
    """
    channels = 4
    stride = width * channels
    rows_written = height if rows_in_data is None else rows_in_data
    body = bytearray()
    previous = bytearray(stride)
    for y in range(rows_written):
        line = bytearray(rows[y])
        kind = filters[y]
        filtered = bytearray(stride)
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            above = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            if kind == 0:
                predicted = 0
            elif kind == 1:
                predicted = left
            elif kind == 2:
                predicted = above
            elif kind == 3:
                predicted = (left + above) >> 1
            else:
                estimate = left + above - corner
                distances = (abs(estimate - left), abs(estimate - above), abs(estimate - corner))
                predicted = (left, above, corner)[distances.index(min(distances))]
            filtered[i] = (line[i] - predicted) & 0xFF
        body.append(kind)
        body += filtered
        previous = line

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack('>I', len(payload)) + kind + payload
                + struct.pack('>I', zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return (rdc_image.PNG_SIGNATURE + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(bytes(body))) + chunk(b'IEND', b''))


def rgba(*pixels: Sequence[int]) -> bytes:
    """One flat RGBA buffer out of per-pixel tuples."""
    out = bytearray()
    for pixel in pixels:
        out += bytes(pixel)
    return bytes(out)


class ImageCase(unittest.TestCase):
    def image(self, width: int, height: int, fill: Sequence[int]) -> rdc_image.Image:
        return rdc_image.Image(width, height, rgba(*([fill] * (width * height))))

    def test_every_row_filter_is_undone(self):
        """All five filters, each on its own row, decode to the pixels that went in."""
        rows = [[(x * 7 + y * 11) % 256 for x in range(12)] for y in range(5)]
        pixels = [tuple(row[x * 4:x * 4 + 4]) for row in rows for x in range(3)]
        blob = png_with_filters(3, 5, rows, [0, 1, 2, 3, 4])
        image = rdc_image.decode_png(blob)
        self.assertEqual((image.width, image.height), (3, 5))
        self.assertEqual(image.rgba, rgba(*pixels))

    def test_paeth_predicts_from_the_closest_neighbour(self):
        """The row-4 case that the other four filters cannot produce: a single changed pixel."""
        rows = [[10, 20, 30, 255, 11, 21, 31, 255, 12, 22, 32, 255],
                [40, 50, 60, 255, 41, 51, 61, 255, 42, 52, 62, 255]]
        image = rdc_image.decode_png(png_with_filters(3, 2, rows, [0, 4]))
        self.assertEqual(image.rgba, rgba(tuple(rows[0][0:4]), tuple(rows[0][4:8]),
                                          tuple(rows[0][8:12]), tuple(rows[1][0:4]),
                                          tuple(rows[1][4:8]), tuple(rows[1][8:12])))

    def test_a_written_png_reads_back_as_what_was_written(self):
        path = os.path.join(os.environ.get('TEMP', '/tmp'), 'rdc_ab_roundtrip.png')
        original = rdc_image.Image(2, 2, rgba((1, 2, 3, 4), (5, 6, 7, 8), (9, 10, 11, 12),
                                              (13, 14, 15, 16)))
        try:
            rdc_image.write_png(path, original)
            self.assertEqual(rdc_image.read_png(path), original)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_a_shape_this_reader_does_not_decode_is_refused_with_what_it_is(self):
        header = struct.pack('>IIBBBBB', 4, 4, 16, 6, 0, 0, 0)
        blob = (rdc_image.PNG_SIGNATURE + struct.pack('>I', len(header)) + b'IHDR' + header
                + struct.pack('>I', zlib.crc32(b'IHDR' + header) & 0xFFFFFFFF))
        with self.assertRaises(rdc_image.PngError) as caught:
            rdc_image.decode_png(blob)
        self.assertIn('16-bit', str(caught.exception))

    def test_an_interlaced_or_paletted_image_is_refused_by_name(self):
        for depth, colour, why in ((8, 3, 'palette'), (8, 6, 'interlaced')):
            header = struct.pack('>IIBBBBB', 4, 4, depth, colour, 0, 0, 1 if why == 'interlaced' else 0)
            blob = (rdc_image.PNG_SIGNATURE + struct.pack('>I', len(header)) + b'IHDR' + header
                    + struct.pack('>I', zlib.crc32(b'IHDR' + header) & 0xFFFFFFFF))
            with self.assertRaises(rdc_image.PngError) as caught:
                rdc_image.decode_png(blob)
            self.assertIn(why if why == 'interlaced' else 'colour type 3',
                          str(caught.exception))

    def test_a_file_that_is_not_a_png_is_refused_at_the_signature(self):
        with self.assertRaises(rdc_image.PngError) as caught:
            rdc_image.decode_png(b'BM\x00\x00 not a png at all')
        self.assertIn('no signature', str(caught.exception))

    def test_an_image_with_fewer_rows_than_it_declares_is_refused_rather_than_half_read(self):
        rows = [[1, 2, 3, 255] * 3, [4, 5, 6, 255] * 3]
        blob = png_with_filters(3, 2, rows, [0], rows_in_data=1)
        with self.assertRaises(rdc_image.PngError) as caught:
            rdc_image.decode_png(blob)
        self.assertIn('needs', str(caught.exception))

    def test_the_delta_counts_every_channel_and_reports_none_across_sizes(self):
        one = rdc_image.Image(1, 1, rgba((10, 10, 10, 10)))
        same = rdc_image.Image(1, 1, rgba((10, 10, 10, 10)))
        alpha = rdc_image.Image(1, 1, rgba((10, 10, 10, 40)))
        other = rdc_image.Image(2, 1, rgba((10, 10, 10, 10), (10, 10, 10, 10)))
        compared = rdc_image.image_delta(one, same)
        assert compared is not None
        self.assertEqual((compared[0].differing, compared[0].max_delta, compared[0].sum_delta),
                         (0, 0, 0))
        changed = rdc_image.image_delta(one, alpha)
        assert changed is not None
        self.assertEqual((changed[0].differing, changed[0].max_delta), (1, 30))
        self.assertIsNone(rdc_image.image_delta(one, other))

    def test_the_heat_map_lights_up_where_the_picture_moved(self):
        one = rdc_image.Image(2, 1, rgba((0, 0, 0, 255), (0, 0, 0, 255)))
        two = rdc_image.Image(2, 1, rgba((0, 0, 0, 255), (60, 0, 0, 255)))
        compared = rdc_image.image_delta(one, two, heat=True)
        assert compared is not None
        heat = compared[1]
        assert heat is not None
        self.assertEqual(heat.rgba[0:4], bytes((0, 0, 0, 255)))
        self.assertEqual(heat.rgba[5], 255)      # 60 * 8 is past full brightness
        self.assertEqual(heat.rgba[7], 255)

    def test_the_hash_is_stable_and_moves_with_the_picture(self):
        """A perceptual hash, so it reports a *layout* change: a picture that brightens left-to-right and
        one that brightens right-to-left disagree in every bit, while two flats agree in all of them."""
        flat = self.image(9, 8, (100, 100, 100, 255))
        brighter_to_the_right = rdc_image.Image(
            9, 8, rgba(*[(x * 20, 0, 0, 255) for _ in range(8) for x in range(9)]))
        brighter_to_the_left = rdc_image.Image(
            9, 8, rgba(*[(255 - x * 20, 0, 0, 255) for _ in range(8) for x in range(9)]))
        self.assertEqual(rdc_image.difference_hash(flat), rdc_image.difference_hash(flat))
        self.assertEqual(rdc_image.hash_distance(rdc_image.difference_hash(flat),
                                                 rdc_image.difference_hash(flat)), 0)
        self.assertGreater(rdc_image.hash_distance(rdc_image.difference_hash(brighter_to_the_right),
                                                   rdc_image.difference_hash(brighter_to_the_left)), 0)

    def test_downscaling_never_scales_up_and_averages_the_source(self):
        small = self.image(2, 2, (10, 20, 30, 40))
        self.assertIs(rdc_image.downscale(small, 9, 8), small)
        big = rdc_image.Image(2, 2, rgba((0, 0, 0, 0), (100, 100, 100, 100),
                                         (200, 200, 200, 200), (255, 255, 255, 255)))
        shrunk = rdc_image.downscale(big, 1, 1)
        self.assertEqual((shrunk.width, shrunk.height), (1, 1))
        self.assertEqual(shrunk.rgba, bytes((138, 138, 138, 138)))

    def test_the_median_of_a_list_of_percentages(self):
        self.assertEqual(rdc_image.percentile([], 0.5), 0)
        self.assertEqual(rdc_image.percentile([5], 0.5), 5)
        self.assertEqual(rdc_image.percentile([1, 2, 3, 4], 0.5), 3)


# =========================================================================== passdiff (two captures)
class PassDiffCase(_CmdCase):
    """A scratch cache, the fake chunk-name tree, and two captures built chunk by chunk."""

    def marker(self, name: str) -> bytes:
        return self.ch('PushMarker', name.encode('utf-8') + b'\x00')

    def pop(self) -> bytes:
        return self.ch('PopMarker', b'')

    def draw(self, cmdlist: int = 1) -> bytes:
        return self.ch('List_DrawInstanced', F.pl_draw_instanced(cmdlist, 3, 1, 0, 0))

    def dispatch(self, cmdlist: int = 1) -> bytes:
        return self.ch('List_Dispatch', F.pl_dispatch(cmdlist, 8, 8, 1))

    def capture_named(self, name: str, chunks: Sequence[bytes]) -> str:
        return self.path(name, F.capture(list(chunks)))


class TestMarkerTree(PassDiffCase):
    def test_nested_markers_become_passes_with_their_own_paths(self):
        """The parent is a pass too: it is a marker with calls under it, which is the rule `sheet` groups
        a frame by -- so a path this prints can be pasted into `--at-marker`."""
        path = self.capture_named('tree.rdc', [
            self.marker('Scene'),
            self.marker('Shadow'), self.draw(), self.pop(),
            self.marker('BasePass'), self.draw(), self.dispatch(), self.pop(),
            self.pop()])
        passes = R.marker_passes(path)
        assert passes is not None
        self.assertEqual([entry.path for entry in passes],
                         ['Scene > Shadow', 'Scene > BasePass', 'Scene'])
        self.assertEqual([entry.calls for entry in passes], [1, 2, 3])
        self.assertEqual([entry.depth for entry in passes], [1, 1, 0])
        self.assertEqual((passes[1].first_chunk, passes[1].last_chunk), (5, 7))

    def test_a_parent_counts_the_calls_of_its_children(self):
        path = self.capture_named('tree.rdc', [
            self.marker('Scene'),
            self.marker('Shadow'), self.draw(), self.pop(),
            self.marker('BasePass'), self.draw(), self.draw(), self.pop(),
            self.pop()])
        passes = R.marker_passes(path)
        assert passes is not None
        self.assertEqual([(entry.path, entry.calls) for entry in passes],
                         [('Scene > Shadow', 1), ('Scene > BasePass', 2), ('Scene', 3)])

    def test_a_marker_name_shorter_than_three_characters_reads_as_unnamed(self):
        """The floor that keeps the framing bytes out of a name (`MARKER_MINLEN`, measured on both real
        captures): a payload's frame decodes as one- and two-character runs before the name does, and a
        marker genuinely called `A` is the price. Reported as unnamed rather than as `B`."""
        path = self.capture_named('tree.rdc', [
            self.marker('A'), self.draw(), self.pop(),
            self.marker('BasePass'), self.draw(), self.pop()])
        passes = R.marker_passes(path)
        assert passes is not None
        self.assertEqual([entry.path for entry in passes],
                         ['%s' % R.UNNAMED, 'BasePass'])

    def test_a_marker_with_no_call_inside_it_is_a_heading_and_not_a_pass(self):
        path = self.capture_named('tree.rdc', [
            self.marker('Scene'), self.marker('Empty'), self.pop(), self.pop(), self.draw()])
        passes = R.marker_passes(path)
        assert passes is not None
        self.assertEqual(passes, [])

    def test_a_set_marker_is_not_walked_and_a_pop_with_nothing_pushed_is_ignored(self):
        path = self.capture_named('tree.rdc', [
            self.ch('SetMarker', b'Sets a name\x00'), self.draw(), self.pop(),
            self.marker('Real'), self.draw(), self.pop()])
        passes = R.marker_passes(path)
        assert passes is not None
        self.assertEqual([entry.path for entry in passes], ['Real'])

    def test_a_marker_still_open_where_the_stream_ends_is_still_a_pass(self):
        """The frame was cut: dropping it would hide every pass of that frame (the report's
        marker-balance detector is what reports the imbalance itself)."""
        path = self.capture_named('tree.rdc', [self.marker('Scene'), self.draw()])
        passes = R.marker_passes(path)
        assert passes is not None
        self.assertEqual([entry.path for entry in passes], ['Scene'])

    def test_no_chunk_name_map_is_not_looked_at_rather_than_empty(self):
        path = self.capture_named('tree.rdc', [self.marker('Scene'), self.draw(), self.pop()])
        with mock.patch.object(chunkmap, 'load_chunk_names', lambda *a, **k: {}):
            self.assertIsNone(R.marker_passes(path))
            out = capture_text(R.cmd_passdiff, path, path)
        self.assertIn('no chunk-name map', out)


class TestPassAlignment(PassDiffCase):
    def rows(self, a: Sequence[bytes], b: Sequence[bytes]) -> List[R.PassRow]:
        left = R.marker_passes(self.capture_named('a.rdc', a))
        right = R.marker_passes(self.capture_named('b.rdc', b))
        assert left is not None and right is not None
        return R.align_passes(left, right)

    def test_the_same_path_pairs_and_reports_the_range_shift(self):
        left = [self.marker('Scene'), self.draw(), self.pop()]
        right = [self.draw(), self.draw(), self.marker('Scene'), self.draw(), self.pop()]
        rows = self.rows(left, right)
        self.assertEqual([row.status for row in rows], ['same'])
        self.assertEqual(rows[0].a_index, 0)
        self.assertEqual(rows[0].b_index, 0)
        self.assertIn('+2 chunk(s)', rows[0].note)

    def test_a_path_used_twice_pairs_with_its_own_occurrence(self):
        left = [self.marker('Shadow'), self.draw(), self.pop(),
                self.marker('Shadow'), self.draw(), self.draw(), self.pop()]
        right = [self.marker('Shadow'), self.draw(), self.pop(),
                 self.marker('Shadow'), self.draw(), self.draw(), self.draw(), self.pop()]
        rows = self.rows(left, right)
        self.assertEqual([row.status for row in rows], ['same', 'same'])
        self.assertEqual([row.a.calls for row in rows if row.a], [1, 2])
        self.assertEqual([row.b.calls for row in rows if row.b], [1, 3])

    def test_a_pass_only_one_side_has_is_added_or_removed(self):
        shadow = [self.marker('Shadow'), self.draw(), self.pop()]
        base = [self.marker('BasePass'), self.draw(), self.pop()]
        removed = self.rows(shadow + base, shadow)
        self.assertEqual([row.status for row in removed], ['same', 'removed'])
        added = self.rows(shadow, shadow + base)
        self.assertEqual([row.status for row in added], ['same', 'added'])

    def test_the_innermost_name_matches_a_pass_whose_full_path_moved(self):
        left = [self.marker('Scene'), self.marker('BasePass'), self.draw(), self.pop(), self.pop()]
        right = [self.marker('Frame'), self.marker('Scene'), self.marker('BasePass'),
                 self.draw(), self.pop(), self.pop(), self.pop()]
        rows = self.rows(left, right)
        self.assertEqual(rows[0].status, 'same')
        self.assertEqual(rows[0].path, 'Scene > BasePass')
        self.assertIn('the full marker path differs', rows[0].note)
        self.assertIn('`BasePass` is the innermost name of both', rows[0].note)

    def test_a_rename_is_suggested_only_with_the_slot_the_parent_and_the_calls(self):
        left = [self.marker('Scene'), self.marker('OldName'), self.draw(), self.pop(), self.pop()]
        right = [self.marker('Scene'), self.marker('NewName'), self.draw(), self.pop(), self.pop()]
        rows = self.rows(left, right)
        self.assertEqual(rows[0].status, 'renamed?')
        self.assertEqual(rows[0].path, 'Scene > OldName  ->  Scene > NewName')
        self.assertIn('the same parent marker (`Scene`)', rows[0].note)
        # The parent marker is a pass on both sides too, and it matches by name.
        self.assertEqual(rows[1].status, 'same')
        self.assertEqual(rows[1].path, 'Scene')

    def test_a_rename_needs_the_same_parent_and_the_same_call_count(self):
        """Each of the three is evidence on its own only in the sense that it *happens* to hold: measured
        on a real mobile-vs-PC pair, slot and call count alone suggested nineteen renames between two
        frames that share almost no passes."""
        left = [self.marker('Scene'), self.marker('OldName'), self.draw(), self.pop(), self.pop()]
        under_other = [self.marker('Other'), self.marker('NewName'), self.draw(),
                       self.pop(), self.pop()]
        self.assertEqual([row.status for row in self.rows(left, under_other)][0], 'removed')
        twice = [self.marker('Scene'), self.marker('NewName'), self.draw(), self.draw(),
                 self.pop(), self.pop()]
        self.assertEqual([row.status for row in self.rows(left, twice)][0], 'removed')

    def test_two_top_level_passes_are_never_a_rename(self):
        # "The same parent" has to be something that was true: both of these have no parent at all.
        left = [self.marker('Gallery'), self.draw(), self.pop()]
        right = [self.marker('Overlay'), self.draw(), self.pop()]
        self.assertEqual([row.status for row in self.rows(left, right)], ['removed', 'added'])


class TestPassDiffCommand(PassDiffCase):
    def test_the_report_says_what_it_compared_and_on_what(self):
        left = self.capture_named('a.rdc', [self.marker('Scene'), self.draw(), self.pop()])
        right = self.capture_named('b.rdc', [self.marker('Scene'), self.draw(), self.pop()])
        out = capture_text(R.cmd_passdiff, left, right)
        self.assertIn('passes    : 1 in A, 1 in B', out)
        self.assertIn('1 path(s) in both', out)
        self.assertIn('Scene', out)
        self.assertIn("chunk indices are the file's numbering", out)

    def test_a_capture_whose_stream_cannot_be_read_is_an_error_and_not_an_empty_answer(self):
        """A section flagged LZ4 whose body is not: the decode is what fails, and the command reports it
        rather than answering "this capture has no passes"."""
        good = self.capture_named('a.rdc', [self.marker('Scene'), self.draw(), self.pop()])
        broken = self.path('broken.rdc', F.rdc([F.section('FrameCapture', struct.pack('<I', 0),
                                                          flags=0x2, uncomp_len=4096)]))
        out = capture_text(R.cmd_passdiff, broken, good)
        self.assertIn('error:', out)
        self.assertIn('declares 4096', out)


# =========================================================================== replaydiff (two bundles)
class BundleCase(unittest.TestCase):
    """Two bundles in one scratch directory, written by hand from the shapes `dump` produces."""

    tmp: str

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='rdc_ab_')
        self.addCleanup(self._remove)

    def _remove(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def bundle(self, name: str, events: Optional[List[Dict[str, Any]]] = None, **kw: Any) -> str:
        root = self.path(name)
        write_bundle(root, events=events, **kw)
        return root

    def state(self, eid: int, root_parameters: Optional[List[str]] = None,
              shaders: Optional[List[str]] = None, targets: Optional[List[str]] = None,
              depth: str = '0') -> Dict[str, Any]:
        return {'eid': eid, 'marker': '', 'rootSignature': '99',
                'rootParameters': list(root_parameters or []),
                'shaders': list(shaders or []),
                'renderTargets': list(targets or []),
                'depthTarget': depth}

    def shaders_doc(self, eid: int, stages: List[Dict[str, Any]]) -> Dict[str, Any]:
        for stage in stages:
            stage.setdefault('encoding', 6)
            stage.setdefault('bytes', 100)
            stage.setdefault('constantBlocks', [])
            stage.setdefault('readOnlyResources', [])
            stage.setdefault('readWriteResources', [])
            stage.setdefault('inputSignature', [])
            stage.setdefault('outputSignature', [])
        return {'eid': eid, 'marker': '', 'stages': stages}

    def write_image(self, root: str, eid: int, slot: int, image: rdc_image.Image) -> str:
        directory = os.path.join(root, 'rt')
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, '%d_%d.png' % (eid, slot))
        rdc_image.write_png(path, image)
        return path

    def image(self, fill: Sequence[int], width: int = 2, height: int = 2) -> rdc_image.Image:
        return rdc_image.Image(width, height, rgba(*([fill] * (width * height))))


class TestReplayDiffAlignment(BundleCase):
    def test_the_same_marker_path_pairs_and_the_row_carries_both_ranges(self):
        a = R.load_side(self.bundle('a', events=[event(1, marker='Scene > BasePass',
                                                       targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                                    resources=[resource('11', first=1)]))
        b = R.load_side(self.bundle('b', events=[event(5, marker='Scene > BasePass',
                                                       targets=['12 64x64x1 R8G8B8A8_UNORM'])],
                                    resources=[resource('12', first=5)]))
        rows = R.align(a, b)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, 'same')
        self.assertEqual(rows[0].path, 'Scene > BasePass')
        self.assertEqual(rows[0].note, '')

    def test_the_innermost_name_matches_when_the_paths_carry_different_text(self):
        """A real pair has `SkyAtmosphereLUTs > TransmittanceLut` on one side and
        `Scene > Scene > SkyAtmosphereLUTs > TransmittanceLut` on the other: the parents carry the
        frame's own structure, the innermost name is the pass."""
        a = R.load_side(self.bundle('a', events=[event(1, marker='MobileSceneRender > SkyAtmosphereLUTs'
                                                             ' > TransmittanceLut')]))
        b = R.load_side(self.bundle('b', events=[event(1, marker='Scene > Scene > SkyAtmosphereLUTs'
                                                             ' > TransmittanceLut')]))
        rows = R.align(a, b)
        self.assertEqual(rows[0].status, 'same')
        self.assertIn('innermost marker name', rows[0].note)

    def test_a_pass_named_the_deepest_child_aligns_by_that_name(self):
        """A pass labelled with a deep path (its first marked event is inside a child marker) matches the
        other side's pass of the same name -- the reason the name rule exists at all."""
        a = R.load_side(self.bundle('a', events=[event(1, marker='Frame > A > B'),
                                                 event(2, marker='Frame > A > B')]))
        b = R.load_side(self.bundle('b', events=[event(9, marker='Other > A > B')]))
        rows = R.align(a, b)
        self.assertEqual(rows[0].status, 'same')
        self.assertIn('innermost marker name', rows[0].note)

    def test_the_marker_of_a_pass_whose_first_event_sits_in_none(self):
        """The ids between two command lists carry state but sit in no marker (REFERENCE §9), so a pass
        that begins on one is labelled by its first *marked* event."""
        a = R.load_side(self.bundle('a', events=[event(1, marker='', targets=['11 64x64x1 R8G8B8A8_UNORM']),
                                                 event(2, marker='Scene > Shadow',
                                                       targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                                    resources=[resource('11', first=1)]))
        self.assertEqual(R.pass_markers(a), {0: 'Scene > Shadow'})

    def test_the_resources_both_passes_write_are_the_third_rule(self):
        a = R.load_side(self.bundle('a', events=[event(1, marker='OldName',
                                                       targets=['11 64x64x1 R8G8B8A8_UNORM'])],
                                    resources=[resource('11', name='SceneColour', first=1)]))
        b = R.load_side(self.bundle('b', events=[event(1, marker='NewName',
                                                       targets=['21 64x64x1 R8G8B8A8_UNORM'])],
                                    resources=[resource('21', name='SceneColour', first=1)]))
        rows = R.align(a, b)
        self.assertEqual(rows[0].status, 'same')
        self.assertIn('resources both passes write', rows[0].note)

    def test_call_order_pairs_only_passes_of_the_same_call_kind(self):
        a = R.load_side(self.bundle('a', events=[event(1, kind='graphics', targets=[])]))
        b = R.load_side(self.bundle('b', events=[event(1, kind='compute')]))
        rows = R.align(a, b)
        self.assertEqual([row.status for row in rows], ['removed', 'added'])

    def test_an_unaligned_pass_is_added_or_removed_and_never_compared(self):
        a = R.load_side(self.bundle('a', events=[event(1, marker='Only > Here')]))
        b = R.load_side(self.bundle('b', events=[event(1, marker='Only > There')]))
        rows = R.align(a, b)
        self.assertEqual([row.status for row in rows], ['removed', 'added'])
        self.assertIsNone(rows[0].b)
        self.assertIsNotNone(rows[1].b)


class TestReplayDiffSections(BundleCase):
    def sides(self, a_events: List[Dict[str, Any]], b_events: List[Dict[str, Any]],
              a_extra: Optional[Dict[str, Any]] = None,
              b_extra: Optional[Dict[str, Any]] = None) -> Any:
        base: Dict[str, Any] = dict(states=None, cbuffers=None, resources=None)
        a_kw = dict(base)
        a_kw.update(a_extra or {})
        b_kw = dict(base)
        b_kw.update(b_extra or {})
        side_a = R.load_side(self.bundle('a', events=a_events, **a_kw))
        side_b = R.load_side(self.bundle('b', events=b_events, **b_kw))
        return side_a, side_b

    def test_a_constant_value_that_moved_is_reported_member_by_member(self):
        event_row = [event(1, marker='Scene > BasePass')]
        side_a, side_b = self.sides(
            event_row, event_row,
            a_extra={'cbuffers': {'1_ps_0.json': cbuffer(1, variables=['float Intensity = 1.0',
                                                                      'float3 Tint = 1, 1, 1'])}},
            b_extra={'cbuffers': {'1_ps_0.json': cbuffer(1, variables=['float Intensity = 2.5',
                                                                      'float3 Tint = 1, 1, 1'])}})
        rows = R.compare_cbuffers(side_a, side_b, side_a.passes[0], side_b.passes[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].block, 'ps_0')
        self.assertEqual([(value.member, value.a, value.b) for value in rows[0].values],
                         [('float Intensity', 'float Intensity = 1.0', 'float Intensity = 2.5')])

    def test_a_block_only_one_side_binds_is_reported_with_its_members(self):
        event_row = [event(1, marker='Scene > BasePass')]
        side_a, side_b = self.sides(event_row, event_row,
                                    a_extra={'cbuffers': {'1_ps_3.json': cbuffer(1, slot=3,
                                                                                 variables=['float X = 1'])}},
                                    b_extra={'cbuffers': {}})
        rows = R.compare_cbuffers(side_a, side_b, side_a.passes[0], side_b.passes[0])
        self.assertEqual([note for note in rows[0].notes], ['only in A: nothing is bound to this block '
                                                            'over here'])
        self.assertEqual(rows[0].values[0].b, '(not present)')

    def test_a_shader_that_is_the_same_by_hash_and_one_that_is_not(self):
        stages = lambda digest: [{'stage': 'ps', 'resource': '7', 'entry': 'Main', 'hash': digest}]
        event_row = [event(1, marker='P')]
        side_a, side_b = self.sides(event_row, event_row,
                                    a_extra={'states': {1: {'shaders': self.shaders_doc(
                                        1, stages('aa' * 32))}}},
                                    b_extra={'states': {1: {'shaders': self.shaders_doc(
                                        1, stages('aa' * 32))}}})
        same = R.compare_shaders(side_a, side_b, side_a.passes[0], side_b.passes[0])
        self.assertEqual([(row.stage, row.verdict) for row in same], [('ps', 'same')])
        side_c = R.load_side(self.bundle('c', events=event_row,
                                         states={1: {'shaders': self.shaders_doc(
                                             1, stages('bb' * 32))}}))
        different = R.compare_shaders(side_a, side_c, side_a.passes[0], side_c.passes[0])
        self.assertEqual([row.verdict for row in different], ['different'])
        self.assertEqual(different[0].a_hash, 'aa' * 32)

    def test_a_bundle_without_the_hash_says_so_rather_than_calling_it_the_same(self):
        event_row = [event(1, marker='P')]
        side_a, side_b = self.sides(
            event_row, event_row,
            a_extra={'states': {1: {'shaders': self.shaders_doc(1, [{'stage': 'vs', 'resource': '7',
                                                                     'entry': 'Main'}])}}},
            b_extra={'states': {1: {'shaders': self.shaders_doc(1, [{'stage': 'vs', 'resource': '9',
                                                                     'entry': 'Main'}])}}})
        rows = R.compare_shaders(side_a, side_b, side_a.passes[0], side_b.passes[0])
        self.assertEqual([row.verdict for row in rows], ['no-hash-in-this-bundle'])

    def test_a_reflection_row_that_moved_is_named_with_the_field_it_belongs_to(self):
        event_row = [event(1, marker='P')]
        side_a, side_b = self.sides(
            event_row, event_row,
            a_extra={'states': {1: {'shaders': self.shaders_doc(
                1, [{'stage': 'ps', 'resource': '7', 'entry': 'Main', 'hash': 'cc' * 32,
                     'constantBlocks': ['cbuffer[0] View b0 s0 32 bytes, 1 variables']}])}}},
            b_extra={'states': {1: {'shaders': self.shaders_doc(
                1, [{'stage': 'ps', 'resource': '8', 'entry': 'Main', 'hash': 'cc' * 32,
                     'constantBlocks': ['cbuffer[0] Light b1 s0 16 bytes, 1 variables']}])}}})
        rows = R.compare_shaders(side_a, side_b, side_a.passes[0], side_b.passes[0])
        fields = [change.field for change in rows[0].changes]
        self.assertIn('constantBlocks', fields)
        self.assertEqual(rows[0].a_bytes, rows[0].b_bytes)

    def test_the_state_rows_are_annotated_with_the_name_the_capture_gave(self):
        side = R.load_side(self.bundle('a', events=[event(1, marker='P')],
                                       resources=[resource('11', name='SceneColour', first=1)],
                                       states={1: {'state': self.state(1, targets=['slot 0  res11'])}}))
        self.assertEqual(R.state_rows(side, 1, 'renderTargets'), ['slot 0  res11[SceneColour]'])
        self.assertEqual(R.annotate('nothing to annotate here', side.names),
                         'nothing to annotate here')
        self.assertEqual(R.annotate_id('11', side.names), 'res11[SceneColour]')
        self.assertEqual(R.annotate_id('0', side.names), '0')

    def test_a_structure_change_is_reported_field_by_field(self):
        a = R.load_side(self.bundle('a', events=[event(1, marker='P', targets=['11 8x8x1 R8G8B8A8_UNORM'],
                                                       depth='0')],
                                    resources=[resource('11', first=1)]))
        b = R.load_side(self.bundle('b', events=[event(1, marker='P', targets=['21 8x8x1 R8G8B8A8_UNORM'],
                                                       depth='22')],
                                    resources=[resource('21', first=1),
                                               resource('22', kind='texture', first=1)]))
        changes = {change.field: (change.a, change.b)
                   for change in R.structure_changes(a.passes[0], b.passes[0], a.names, b.names)}
        self.assertIn('depth', changes)
        self.assertEqual(changes['depth'], ('0', 'res22'))


class TestReplayDiffImages(BundleCase):
    def pair(self, image_a: rdc_image.Image, image_b: rdc_image.Image,
             images_a: bool = True, images_b: bool = True) -> Any:
        a = self.bundle('a', events=[event(1, marker='Scene > BasePass')])
        b = self.bundle('b', events=[event(1, marker='Scene > BasePass')])
        if images_a:
            self.write_image(a, 1, 0, image_a)
        if images_b:
            self.write_image(b, 1, 0, image_b)
        return R.load_side(a), R.load_side(b)

    def compare(self, side_a: Any, side_b: Any, out: str = '', detail: int = 8,
                with_images: bool = True) -> List[R.AbImageRow]:
        files_a, files_b = R.rt_files(side_a), R.rt_files(side_b)
        return R.compare_images(side_a, side_b, files_a, files_b, side_a.passes[0], side_b.passes[0],
                                'Scene > BasePass', out, with_images, [detail])

    def test_identical_bytes_are_identical_pixels_without_decoding(self):
        side_a, side_b = self.pair(self.image((1, 2, 3, 4)), self.image((1, 2, 3, 4)))
        rows = self.compare(side_a, side_b)
        self.assertEqual([row.verdict for row in rows], ['identical'])
        self.assertEqual(rows[0].a_eid, 1)

    def test_a_different_picture_is_decoded_and_measured_when_detail_is_left(self):
        a = self.image((0, 0, 0, 255))
        b = rdc_image.Image(2, 2, rgba((30, 0, 0, 255), (0, 0, 0, 255), (0, 0, 0, 255),
                                       (0, 0, 0, 255)))
        side_a, side_b = self.pair(a, b)
        out = self.path('out')
        rows = self.compare(side_a, side_b, out=out)
        self.assertEqual(rows[0].verdict, 'different')
        self.assertEqual(rows[0].differing, 1)
        self.assertEqual(rows[0].max_delta, 30)
        # The heat map's path is relative to the output directory (no absolute path in the document).
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, rows[0].heat_map)), rows[0].heat_map)

    def test_an_image_nobody_looked_at_says_so_instead_of_reading_as_unchanged(self):
        side_a, side_b = self.pair(self.image((0, 0, 0, 255)),
                                   rdc_image.Image(2, 2, rgba((9, 0, 0, 255), (0, 0, 0, 255),
                                                              (0, 0, 0, 255), (0, 0, 0, 255))))
        rows = self.compare(side_a, side_b, detail=0)
        self.assertEqual(rows[0].verdict, 'different')
        self.assertIn('not compared pixel by pixel', rows[0].note)

    def test_two_sizes_are_reported_as_two_sizes_not_as_no_difference(self):
        side_a, side_b = self.pair(self.image((0, 0, 0, 255)),
                                   self.image((0, 0, 0, 255), 3, 1))
        rows = self.compare(side_a, side_b)
        self.assertEqual(rows[0].verdict, 'sizes-differ')
        self.assertIn('not defined across sizes', rows[0].note)

    def test_a_pass_that_produced_a_picture_on_one_side_only(self):
        side_a, side_b = self.pair(self.image((0, 0, 0, 255)), self.image((0, 0, 0, 255)),
                                   images_b=False)
        rows = self.compare(side_a, side_b)
        self.assertEqual(rows[0].verdict, 'only-in-a')

    def test_without_the_flag_no_image_is_even_looked_for(self):
        side_a, side_b = self.pair(self.image((1, 2, 3, 4)), self.image((5, 6, 7, 8)))
        self.assertEqual(self.compare(side_a, side_b, with_images=False), [])

    def test_the_readback_of_a_pass_is_the_last_one_it_wrote(self):
        a = self.bundle('a', events=[event(1, marker='P'), event(5, marker='P')])
        self.write_image(a, 1, 0, self.image((7, 7, 7, 255)))
        self.write_image(a, 5, 0, self.image((9, 9, 9, 255)))
        side = R.load_side(a)
        files = R.rt_files(side)
        self.assertEqual([(eid, slot) for eid, slot, _path in files], [(1, 0), (5, 0)])
        self.assertEqual(R.last_readbacks(files, 1, 5)[0][0], 5)


class TestReplayDiffCommand(BundleCase):
    def test_the_command_writes_both_documents_and_says_what_it_found(self):
        event_row = [event(1, marker='Scene > BasePass',
                           targets=['11 64x64x1 R8G8B8A8_UNORM'])]
        a = self.bundle('a', events=event_row, resources=[resource('11', first=1)])
        b = self.bundle('b', events=event_row, resources=[resource('11', first=1)],
                        manifest={'captureSha256': 'cd' * 32})
        out = self.path('diff')
        code = R.cmd_replaydiff(a, b, ['--out', out])
        self.assertEqual(code, 0)
        with open(os.path.join(out, 'replaydiff.json'), encoding='utf-8') as handle:
            document = json.load(handle)
        self.assertEqual(document['schemaVersion'], R.AB_SCHEMA_VERSION)
        self.assertEqual(document['summary']['same'], 1)
        self.assertFalse(document['sameCapture'])
        with open(os.path.join(out, 'replaydiff.md'), encoding='utf-8') as handle:
            markdown = handle.read()
        self.assertIn('# replaydiff', markdown)
        self.assertIn('## What this cannot say', markdown)

    def test_the_json_twin_is_schema_valid(self):
        """The A/B's own schema against the document it describes (the acceptance gate of ROADMAP §5).

        Like the report's, the schema lives in `rdc_schemas.py` rather than in `schema/`: that folder is
        what the *driver* publishes, and this document is the offline tool's. The schema is exhaustive and
        closed, so a member a change dropped or renamed fails here rather than reaching a consumer as a
        missing column -- and the two halves of the document that only a run with images produces are
        exercised by asking for images.
        """
        event_row = [event(1, marker='Scene > BasePass', targets=['11 64x64x1 R8G8B8A8_UNORM']),
                     event(5, marker='Scene > BasePass', targets=['11 64x64x1 R8G8B8A8_UNORM'])]
        a = self.bundle('a', events=event_row, resources=[resource('11', first=1)])
        b = self.bundle('b', events=event_row, resources=[resource('11', first=1)],
                        manifest={'captureSha256': 'cd' * 32})
        self.write_image(a, 5, 0, self.image((1, 2, 3, 255)))
        self.write_image(b, 5, 0, self.image((1, 2, 9, 255)))
        out = self.path('diff')
        self.assertEqual(R.cmd_replaydiff(a, b, ['--out', out, '--with-images']), 0)
        with open(os.path.join(out, 'replaydiff.json'), encoding='utf-8') as handle:
            document = json.load(handle)
        self.assertTrue(document['passes'], 'the fixture should have produced an aligned pass')
        self.assertEqual(R.validate_document(document, R.AB_SCHEMA), [])

    def test_the_ab_schema_notices_a_member_that_went_missing(self):
        a = self.bundle('a', events=[event(1, marker='P')])
        b = self.bundle('b', events=[event(1, marker='P')])
        out = self.path('diff')
        R.cmd_replaydiff(a, b, ['--out', out])
        with open(os.path.join(out, 'replaydiff.json'), encoding='utf-8') as handle:
            document = json.load(handle)
        del document['sameCapture']
        problems = R.validate_document(document, R.AB_SCHEMA)
        self.assertTrue(problems, 'a missing member is not a valid document')
        self.assertTrue(any('sameCapture' in problem for problem in problems))

    def test_the_ab_schema_notices_a_field_that_changed_type(self):
        """The flag and a side's count are both called `withImages`; the schema keeps them apart."""
        a = self.bundle('a', events=[event(1, marker='P')])
        b = self.bundle('b', events=[event(1, marker='P')])
        out = self.path('diff')
        R.cmd_replaydiff(a, b, ['--out', out])
        with open(os.path.join(out, 'replaydiff.json'), encoding='utf-8') as handle:
            document = json.load(handle)
        document['withImages'] = 3          # the *side*'s type, in the flag's place
        problems = R.validate_document(document, R.AB_SCHEMA)
        self.assertTrue(any('withImages' in problem for problem in problems))

    def test_the_same_capture_on_both_sides_is_named_as_a_tools_ab(self):
        event_row = [event(1, marker='P')]
        a = self.bundle('a', events=event_row)
        b = self.bundle('b', events=event_row)
        out = self.path('diff')
        R.cmd_replaydiff(a, b, ['--out', out])
        with open(os.path.join(out, 'replaydiff.json'), encoding='utf-8') as handle:
            document = json.load(handle)
        self.assertTrue(document['sameCapture'])     # both fixtures carry the writer's sha256
        self.assertIn('tools A/B', capture_text(R.cmd_replaydiff, a, b, ['--out', out]))

    def test_a_bad_option_is_refused_with_the_usage_and_exit_2(self):
        a = self.bundle('a', events=[event(1, marker='P')])
        out = capture_text(R.cmd_replaydiff, a, a, ['--nonsense'])
        self.assertIn('usage:', out)
        self.assertEqual(R.cmd_replaydiff(a, a, ['--nonsense']), 2)
        self.assertEqual(R.cmd_replaydiff(a, a, ['--image-detail', 'many']), 2)

    def test_a_bundle_that_cannot_be_read_is_an_error_and_exit_1(self):
        a = self.bundle('a', events=[event(1, marker='P')])
        missing = self.path('not-a-bundle')
        out = capture_text(R.cmd_replaydiff, a, missing, ['--out', self.path('diff')])
        self.assertIn('no such bundle directory', out)

    def test_the_threshold_counts_the_small_changes_instead_of_listing_them(self):
        a = self.bundle('a', events=[event(1, marker='P')])
        self.write_image(a, 1, 0, self.image((0, 0, 0, 255)))
        b = self.bundle('b', events=[event(1, marker='P')])
        self.write_image(b, 1, 0, rdc_image.Image(2, 2, rgba((4, 0, 0, 255), (0, 0, 0, 255),
                                                             (0, 0, 0, 255), (0, 0, 0, 255))))
        low = self.path('low')
        R.cmd_replaydiff(a, b, ['--out', low, '--with-images', '--threshold', '90'])
        with open(os.path.join(low, 'replaydiff.md'), encoding='utf-8') as handle:
            markdown = handle.read()
        self.assertIn('below the 90.000% threshold', markdown)

    def test_the_rendering_is_deterministic_and_carries_every_member_the_json_has(self):
        event_row = [event(1, marker='Scene > BasePass',
                           targets=['11 64x64x1 R8G8B8A8_UNORM'])]
        a = self.bundle('a', events=event_row, resources=[resource('11', name='SceneColour', first=1)],
                        cbuffers={'1_ps_0.json': cbuffer(1, variables=['float X = 1'])})
        b = self.bundle('b', events=event_row, resources=[resource('11', name='SceneColour', first=1)],
                        cbuffers={'1_ps_0.json': cbuffer(1, variables=['float X = 2'])})
        out = self.path('diff')
        R.cmd_replaydiff(a, b, ['--out', out])
        with open(os.path.join(out, 'replaydiff.json'), encoding='utf-8') as handle:
            document = json.load(handle)
        with open(os.path.join(out, 'replaydiff.md'), encoding='utf-8') as handle:
            markdown = handle.read()
        self.assertEqual(markdown, R.render_replaydiff(document))
        self.assertIn('float X', markdown)
        self.assertIn('1', json.dumps(document))


if __name__ == '__main__':
    unittest.main()
