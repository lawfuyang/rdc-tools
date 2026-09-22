"""The offline format audit (`formats`) and the classifier under it.

Two levels, and the second is the one worth having: the names the tool reads are a *table* of 116 DXGI formats,
so the classifier can be checked against every one of them rather than against the three a test author happened
to think of. A name it does not understand has to come back as `other` with a note -- never as a different
class, and never as an exception -- because that is the case the audit exists to surface.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(HERE)
for _p in (HERE, _ROOT, os.path.join(_ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_chunknames     # noqa: E402
import rdc_fixtures as F  # noqa: E402
import rdc_formats        # noqa: E402
import rdc_resources      # noqa: E402
from rdc_testcase import CmdCase, TempDirCase    # noqa: E402

def run(*args: object) -> str:
    """`cmd_formats`'s stdout, which is where its table goes."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rdc_formats.cmd_formats(*args)    # type: ignore[arg-type]
    return buffer.getvalue()

class TestTheClassifier(TempDirCase):
    """`classify_format`: a reading of the DXGI *name*, which is what a capture's table gives the tool."""

    def test_every_bundled_name_classifies(self) -> None:
        """All 116 of them, because the alternative is a class that only exists for the names a test knew.

        The bundled table is the *floor* of what the tool can encounter (a tree adds more, and a capture can
        name an id neither has), so this is the smallest set worth sweeping -- and it catches the shape of
        failure that matters: a name that raises, or that lands in `other`, would make the audit's "cannot
        read this" line a lie for a format that is perfectly well known.
        """
        for fmt_id, name in sorted(rdc_chunknames.FORMAT_NAMES.items()):
            shape = rdc_resources.classify_format(name.replace('DXGI_FORMAT_', '', 1))
            self.assertIn(shape['class'], rdc_resources.CLASSES, '%s (%d)' % (name, fmt_id))
            if fmt_id != 0:    # `UNKNOWN` is `other` by definition, and says so in its note
                self.assertNotEqual(shape['class'], 'other', '%s is not understood' % name)

    def test_the_classes_that_change_what_a_picture_means(self) -> None:
        """The four the audit's summary counts: a cast, a block, a video layout, and depth."""
        typeless = rdc_resources.classify_format('B8G8R8A8_TYPELESS')
        self.assertEqual((typeless['class'], typeless['components'], typeless['layout']),
                         ('typeless', 4, '8:8:8:8'))
        self.assertIn('--cast', typeless['note'])

        srgb = rdc_resources.classify_format('R8G8B8A8_UNORM_SRGB')
        self.assertEqual(srgb['class'], 'unorm')
        self.assertTrue(srgb['srgb'])    # a *flag* rather than a class: the layout is still UNORM's

        block = rdc_resources.classify_format('BC1_UNORM')
        self.assertEqual(block['class'], 'block')
        self.assertTrue(block['blockCompressed'])
        self.assertEqual(block['components'], 0)    # a block is not a texel: no per-texel width to claim

        self.assertEqual(rdc_resources.classify_format('D32_FLOAT')['class'], 'depth')
        self.assertEqual(rdc_resources.classify_format('D24_UNORM_S8_UINT')['class'], 'depth')
        self.assertEqual(rdc_resources.classify_format('NV12')['class'], 'yuv')
        self.assertEqual(rdc_resources.classify_format('R10G10B10A2_UNORM')['layout'], '10:10:10:2')
        self.assertEqual(rdc_resources.classify_format('R11G11B10_FLOAT')['bits'], 0)  # 11:11:10 differ

    def test_a_name_it_does_not_know_is_other_and_never_an_exception(self) -> None:
        """The audit's whole point: an unknown format is a *row* with a reason, not a crash and not a guess.

        The guess is the tempting one -- `unorm` is what most formats are -- and it is what this caught: a name
        with no type suffix came back as a plain 8-bit format, which would have made the audit's "cannot read
        this" line a lie for exactly the formats it exists to report.
        """
        for name in ('', 'UNKNOWN', 'SOMETHING_NEW_FROM_A_DRIVER', 'SUPERMIPS_2X'):
            shape = rdc_resources.classify_format(name)
            self.assertEqual(shape['class'], 'other', name)
            self.assertTrue(shape['note'], name)

class TestCmdFormats(CmdCase):
    """The command: one row per format the file's own resource table names."""

    def test_the_table_lists_each_format_and_its_shape(self) -> None:
        """Two resources, one format: the table is per format, and id 10's layout is 16:16:16:16 in the fake
        source's table -- the *shape* is read from the name, so what the fixture calls it does not matter."""
        chunks = [
            self.ch('Device_CreateCommittedResource',
                    F.pl_committed_resource(271, F.pl_resource_desc(3, 64, 64, fmt=10))),
            self.ch('Device_CreateCommittedResource',
                    F.pl_committed_resource(272, F.pl_resource_desc(3, 64, 64, fmt=10))),
            self.ch('SetName', F.pl_set_name(271, 'm_Albedo')),
        ]
        text = run(self.cap(*chunks))
        self.assertIn('1 format(s)', text)               # one format, two resources
        self.assertIn('2 resource(s)', text)
        self.assertIn('R16G16B16A16_FLOAT', text)
        self.assertIn('16:16:16:16', text)               # the layout, which the name alone does not spell out
        self.assertIn('float', text)                     # ... and the class the summary counts

    def test_a_format_nothing_can_name_is_a_row_rather_than_a_skip(self) -> None:
        """The sentence the audit exists to be able to say: a resource uses a format no table here has."""
        chunks = [self.ch('Device_CreateCommittedResource',
                          F.pl_committed_resource(271, F.pl_resource_desc(3, 64, 64, fmt=300)))]
        text = run(self.cap(*chunks))
        self.assertIn('id 300', text)
        self.assertIn('0 named, 1 not', text)

    def test_the_payload_coverage_is_reported(self) -> None:
        """What the parse decoded and what it did not, from the ledger `deps` and `memory` print from."""
        chunks = [self.ch('PushMarker', b'Marker\x00'), self.ch('PopMarker')]
        text = run(self.cap(*chunks))
        self.assertIn('payloads:', text)
        self.assertIn('event(s) walked', text)
        self.assertIn('not attributed as uses:', text)
        self.assertIn('descriptor bindings to a slot the capture never wrote:', text)
