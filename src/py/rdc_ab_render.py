"""The `replaydiff` comparison as Markdown: one deterministic layout over `rdc_ab`'s document.

Split from `rdc_ab` the way `rdc_report_render` is split from `rdc_report`: the comparison decides what
is true and this decides where it sits. Every line here comes out of the document -- no counting, no
measuring, no reading of the bundles -- so a number in the Markdown is always a number in the JSON, and a
reader who disagrees with the layout can read the document instead.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

from rdc_bundle import _md   # the table-cell escape every document renderer shares

# The document's types are `rdc_ab`'s, and `rdc_ab` imports this module to render with it: a runtime
# import back would be a cycle that breaks at `from rdc_ab import *` (the partially initialised module
# exports only what it has defined so far, and the document types are defined below its imports). With
# `from __future__ import annotations` none of these names is evaluated at runtime, so the type-only
# import is all this module needs -- and the renderer stays a pure function of the document it is handed.
if TYPE_CHECKING:
    from rdc_ab import (AbCbufferMember, AbImageMember, AbPassMember, AbShaderMember,
                        ReplayDiffDocument)

#: The biggest number of changed passes rendered in detail. Everything is in the JSON; a Markdown that
#: runs for a thousand sections is not a document anyone reads, so the rest are counted.
DETAIL_LIMIT = 40


def render_replaydiff(document: ReplayDiffDocument, threshold: float = 0.0) -> str:
    """The comparison as Markdown. `threshold` is the percentage below which an image is counted, not
    listed: the same number `cmd_replaydiff` took, applied here rather than in the comparison so the
    document keeps every pair."""
    lines: List[str] = []
    lines.append('# replaydiff — two bundles, one frame')
    lines.append('')
    lines.extend(_sides(document))
    lines.append('')
    lines.extend(_pass_tables(document))
    lines.append('')
    lines.extend(_details(document, threshold))
    lines.extend(_images(document, threshold))
    lines.append('')
    lines.extend(_summary(document, threshold))
    lines.append('## What this cannot say')
    lines.append('')
    for caveat in document['caveats']:
        lines.append('* %s' % caveat)
    lines.append('')
    return '\n'.join(lines) + '\n'


def _sides(document: ReplayDiffDocument) -> List[str]:
    """The two frames' shape, side by side, and whether they are the same capture at all."""
    left, right = document['a'], document['b']
    rows = (('capture', left['capture'], right['capture']),
            ('bundle', left['bundle'], right['bundle']),
            ('sha256', left['captureSha256'][:16], right['captureSha256'][:16]),
            ('api', left['api'], right['api']),
            ('events', str(left['events']), str(right['events'])),
            ('passes', str(left['passes']), str(right['passes'])),
            ('resources', str(left['resources']), str(right['resources'])),
            ('messages', str(left['messages']), str(right['messages'])),
            ('renderdoc', left['renderdoc'], right['renderdoc']),
            ('withImages', str(left['withImages']), str(right['withImages'])))
    out = ['| | A | B |', '|---|---|---|']
    for name, one, other in rows:
        out.append('| %s | %s | %s |' % (name, _md(one), _md(other)))
    out.append('')
    out.append('**The same capture on both sides** (one sha256): this is a tools A/B, not a capture A/B.'
               if document['sameCapture'] else
               'Two different captures: everything below is a difference between them, and a difference '
               'can come from the frame, from the engine and from the machine that replayed it.')
    return out


def _pass_tables(document: ReplayDiffDocument) -> List[str]:
    """The pass list: what the two sides aligned, and the passes only one of them has."""
    passes = document['passes']
    out = ['## Passes', '', '| # | status | pass | A | B | note |', '|---|---|---|---|---|---|']
    for index, entry in enumerate(passes, 1):
        out.append('| %d | %s | %s | %s | %s | %s |'
                   % (index, entry['status'], _md(_path(entry)), _md(_structure(entry, 'a')),
                      _md(_structure(entry, 'b')), _md(entry['note']) if entry['note'] else ''))
    return out


def _structure(entry: AbPassMember, side: str) -> str:
    """One side's structure column: its own line when the pass is one-sided, else the shared comparison."""
    if entry['status'] == 'same':
        halves = entry['structure'].split('  |  ')
        return halves[0] if side == 'a' else (halves[1] if len(halves) > 1 else '')
    if (side == 'a') != (entry['status'] == 'removed'):
        return '—'
    return entry['structure']


def _path(entry: AbPassMember) -> str:
    """A pass's name for the table, plus its eid ranges when it is on both sides."""
    if entry['aFirstEid'] and entry['bFirstEid']:
        return '%s (eids %d / %d)' % (entry['path'], entry['aFirstEid'], entry['bFirstEid'])
    if entry['aFirstEid']:
        return '%s (eid %d)' % (entry['path'], entry['aFirstEid'])
    return entry['path']


def _details(document: ReplayDiffDocument, threshold: float) -> List[str]:
    """One section per pass that has something to say, in frame order."""
    out = ['## What differs, pass by pass', '']
    detailed = 0
    changed = 0
    for entry in document['passes']:
        interesting = bool(entry['changes'] or entry['stateRemoved'] or entry['stateAdded']
                           or entry['shaders'] or entry['cbuffers'] or entry['images'])
        if not interesting:
            continue
        changed += 1
        if detailed >= DETAIL_LIMIT:
            continue
        detailed += 1
        out.append('### %s' % _md(_path(entry)))
        out.append('')
        for change in entry['changes']:
            out.append('* structure `%s`: `%s` → `%s`' % (change['field'], _md(change['a']),
                                                          _md(change['b'])))
        for row in entry['stateRemoved']:
            out.append('* state only in A: `%s`' % _md(row))
        for row in entry['stateAdded']:
            out.append('* state only in B: `%s`' % _md(row))
        for shader in entry['shaders']:
            out.extend(_shader_lines(shader))
        for block in entry['cbuffers']:
            out.extend(_cbuffer_lines(block))
        out.extend(_image_lines(entry, threshold))
        out.append('')
    if changed > detailed:
        out.append('%d further pass(es) differ; every one of them is in `replaydiff.json`.'
                   % (changed - detailed))
        out.append('')
    return out


def _shader_lines(shader: AbShaderMember) -> List[str]:
    """One bound stage of a pass: its verdict, and the reflection rows that moved."""
    stage = shader['stage']
    verdict = shader['verdict']
    if verdict == 'only-in-a':
        return ['* shader `%s`: bound in A only' % stage]
    if verdict == 'only-in-b':
        return ['* shader `%s`: bound in B only' % stage]
    a_hash, b_hash = shader['aHash'], shader['bHash']
    if verdict == 'different':
        head = ('* shader `%s`: **a different shader** — `%s` (%d byte(s)) against `%s` (%d byte(s))'
                % (stage, a_hash[:16], shader['aBytes'], b_hash[:16], shader['bBytes']))
    elif verdict == 'same':
        head = '* shader `%s`: the same bytes (hash `%s`)' % (stage, a_hash[:16])
    else:
        head = ('* shader `%s`: %d byte(s) against %d, and **no hash in one of these bundles** (written '
                'by a driver from before the hash existed), so "the same" is evidence from the reflection '
                'rather than a fact' % (stage, shader['aBytes'], shader['bBytes']))
    lines = [head]
    for change in shader['changes']:
        lines.append('    * `%s`: `%s` → `%s`' % (change['field'], _md(change['a']), _md(change['b'])))
    return lines


def _cbuffer_lines(block: AbCbufferMember) -> List[str]:
    """One constant block of a pass: who has it, and the members whose values moved."""
    label = 'constants `%s` slot %d (%s)' % (block['stage'], block['slot'], block['block'])
    lines = ['* %s: %s' % (label, '; '.join(block['notes'])) if block['notes'] else '* %s' % label]
    for value in block['values']:
        lines.append('    * `%s`: `%s` → `%s`' % (_md(str(value['member'])), _md(str(value['a'])),
                                                  _md(str(value['b']))))
    return lines


def _image_lines(entry: AbPassMember, threshold: float) -> List[str]:
    """The pass's readbacks: the ones that changed, and a count of the ones that did not."""
    lines: List[str] = []
    identical = 0
    below = 0
    for image in entry['images']:
        if image['verdict'] == 'identical':
            identical += 1
            continue
        if _percent(image) < threshold and image['verdict'] == 'different':
            below += 1
            continue
        lines.append('* image slot %d: %s' % (image['slot'], _image_text(image)))
    if identical:
        lines.append('* %d image slot(s) identical (byte for byte, which for one encoder is pixel for '
                     'pixel)' % identical)
    if below:
        lines.append('* %d image slot(s) differ by less than the %.3f%% threshold' % (below, threshold))
    return lines


def _image_text(image: AbImageMember) -> str:
    """One image row as a sentence: what it is, and what the comparison found."""
    verdict = image['verdict']
    if verdict == 'only-in-a':
        return 'only in A (`%s`): %s' % (image['aFile'], image['note'])
    if verdict == 'only-in-b':
        return 'only in B (`%s`): %s' % (image['bFile'], image['note'])
    if verdict == 'identical':
        return 'identical'
    if verdict in ('unreadable', 'sizes-differ'):
        return '%s: %s' % (verdict, image['note'])
    if verdict == 'different':
        text = ('**changed** (taken at eids %d / %d): %s%% of the %s pixel(s) differ, mean delta %s, '
                'worst %d, hash distance %d'
                % (image['aEid'], image['bEid'], image['percentDiffering'],
                   image['width'] * image['height'], image['meanDelta'], image['maxDelta'],
                   image['hashDistance']))
        if image['note']:
            text += ' (%s)' % image['note']
        if image['heatMap']:
            text += ' — heat map `%s`' % image['heatMap']
        return text
    return verdict


def _percent(image: AbImageMember) -> float:
    """A row's `percentDiffering` as a number (the document carries it as a string: see AGENTS.md)."""
    try:
        return float(image['percentDiffering'])
    except ValueError:
        return 0.0


def _images(document: ReplayDiffDocument, threshold: float) -> List[str]:
    """Every changed image pair in one table, biggest change first -- the frame's answer at a glance."""
    pairs: List[Tuple[float, str, AbImageMember]] = []
    identical = below = not_compared = 0
    for entry in document['passes']:
        for image in entry['images']:
            if image['verdict'] == 'identical':
                identical += 1
            elif image['verdict'] == 'different':
                if _percent(image) < threshold:
                    below += 1
                else:
                    pairs.append((_percent(image), entry['path'], image))
            elif image['note'].startswith('not compared'):
                not_compared += 1
    out = ['', '## Images', '']
    if not document['withImages']:
        out.append('Not compared: the bundles were read without `--with-images`, so no pass has a '
                   'readback to compare. Write both bundles with `dump --with-images` and pass '
                   '`--with-images` here.')
        return out
    if not document['imagesFound']:
        out.append('No bundle here has a readback for any aligned pass: was `dump --with-images` used '
                   'for both sides?')
        return out
    if pairs:
        out.append('| change | pass | slot | eids | pixels differ | mean delta | worst | hash distance | '
                   'file |')
        out.append('|---|---|---|---|---|---|---|---|---|')
        for percent, path, image in sorted(pairs, key=lambda row: (-row[0], row[1], row[2]['slot'])):
            out.append('| %.3f%% | %s | %d | %d / %d | %s | %s | %d | %d | %s |'
                       % (percent, _md(path), image['slot'], image['aEid'], image['bEid'],
                          image['differing'], image['meanDelta'], image['maxDelta'],
                          image['hashDistance'], _md(image['heatMap'] or image['aFile'])))
    else:
        out.append('No image pair differs by %.3f%% or more.' % threshold)
    out.append('')
    out.append('%d identical pair(s), %d below the %.3f%% threshold, %d not compared '
               '(--image-detail: %d).'
               % (identical, below, threshold, not_compared, document['imageDetail']))
    return out


def _summary(document: ReplayDiffDocument, threshold: float) -> List[str]:
    """The counts, so a reader can tell "nothing differs" from "nothing was compared"."""
    summary = document['summary']
    return ['## Summary', '',
            '| | |', '|---|---|',
            '| passes in both | %d |' % summary['same'],
            '| passes only in A | %d |' % summary['removed'],
            '| passes only in B | %d |' % summary['added'],
            '| passes with a structure change | %d |' % summary['structureChanges'],
            '| passes with a state change | %d |' % summary['stateChanges'],
            '| constant blocks whose values moved | %d |' % summary['constantsChanged'],
            '| shaders with a different hash | %d |' % summary['shadersDifferent'],
            '| image pairs compared | %d |' % summary['imagesCompared'],
            '| image pairs left to identity | %d |' % summary['imagesNotCompared'],
            '| image threshold | %.3f%% |' % threshold,
            '']


__all__ = [
    'DETAIL_LIMIT',
    'render_replaydiff',
]
