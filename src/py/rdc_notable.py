"""Notability: which passes and which resources are worth looking at first, by rules the report prints.

The report's notable lists (REFERENCE §4.11). The value of this module is not the ranking -- it is that the ranking is *stated*: every input it
used is in `PASS_INPUTS`/`RESOURCE_INPUTS` with the way it is measured, every input it could not use is there
too with the reason, and every rule that lists something regardless of its rank is in `PASS_ODDITIES`/
`RESOURCE_SPECIALS`. A reader who disagrees with a line can say so about that line, and one who wants an input
the ranking did not have (a draw's vertex count, a counter the bundle was not asked for) can see that it is
missing from *this* frame rather than from the report's idea of a good frame.

Nothing here invents a fact about the frame: each notable row carries the pass or the resource it is about, the
eids or the resId to go and look at, and the numbers the rule read, so the order can be checked rather than
taken on faith.
"""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403
from rdc_detect_common import *  # noqa: F401,F403

import re
from typing import Dict, List, Sequence, Tuple

#: How many passes and resources the ranking lists. Stated rather than tuned: a report whose notable list is
#: longer than this is one nobody reads to the end, and the oddity rules below list every match anyway.
NOTABLE_LIMIT = 5

#: How many passes an oddity rule may list before the rest are rolled up into a note. "Also listed whenever
#: they are odd" cannot mean a wall of forty rows for one unmarked capture, or the pass worth looking at is
#: lost inside it.
ODDITY_LIMIT = 15

#: The ranking key for passes, most significant first: the report prints this table, then the rows it produced.
#: `primitives` is first because it is the input a reader will reach for first -- and because a ranking that quietly skipped it would be the misleading kind of quiet.h
#: for, so a ranking that quietly skipped it would be the misleading kind of quiet.
PASS_INPUTS: List[NotableInput] = [
    {'input': 'primitives (vertices, triangles, threads)',
     'how': "the draw's or dispatch's own argument, from the capture's action tree",
     'available': False,
     'why': 'a bundle carries no action list -- the replay API exposes none, so no capture argument is in one '
            '(ROADMAP §1). Every count below is therefore a count of *calls*, not of the work inside them.'},
    {'input': 'calls',
     'how': "the pass's own events of its kind: a graphics pass is a run of draws with the same targets, so its "
            'graphics-event count is its draw count, and a compute pass is a run of dispatches',
     'available': True, 'why': ''},
    {'input': 'render-target footprint',
     'how': 'the pixels of the colour and depth targets it writes (width x height x depth x samples, from the '
            'resource table)',
     'available': True,
     'why': 'a bundle records a texture\'s dimensions and format but not its byte count -- that depends on the '
            'format\'s packing, the mip chain, the array size and the samples, and the driver does not compute '
            'it -- so a target is measured in the pixels the table does record'},

    {'input': 'resource churn',
     'how': "how many resources this pass is the first in the frame to use (the resource table's firstEvent)",
     'available': True, 'why': ''},
    {'input': 'counter cost',
     'how': "the sum of the counter results the bundle holds for this pass's events",
     'available': False,
     'why': 'counters are collected only by a bundle written with --with-counters, and they are '
            'driver-dependent and slow to fetch (ROADMAP §3)'},
]

#: Passes that are listed whatever their rank, and what "odd" means for each.
PASS_ODDITIES: List[str] = [
    'depth only (no colour target) -- a shadow map or a depth prepass, by structure alone',
    'a single event: one call, so whatever it does, it does once',
    'no marker: the engine put no name on it, so nothing in the capture says what it is for',
    'dead: the colour and depth targets it writes have no read anywhere after it in the usage chain (which '
    'stops at the end of this capture, so a read by the next frame is indistinguishable from none)',
    'the only pass touching a resource: every usage row of that resource falls inside this pass',
]

#: The ranking key for resources, most significant first.
RESOURCE_INPUTS: List[NotableInput] = [
    {'input': 'size',
     'how': "a buffer's own byte count, and a texture's pixels x 4 -- one number for both kinds, so a 3 "
            'megapixel target sorts with a 12 MB buffer',
     'available': True,
     'why': 'the table records a buffer\'s bytes but not a texture\'s, so a texture is counted at 4 bytes per '
            'pixel, which is what a byte count would be for an 8-bit-per-channel target. The row prints both '
            'figures, and the `~` on a texture\'s byte estimate is there because it is one.'},
    {'input': 'passes reading it',
     'how': "the read rows of the engine's usage chain for it, each mapped to the pass whose eid range holds "
            'that event; a row says separately when the chain is empty or holds only the engine\'s `Unused` '
            'marker, because those two are not "nobody reads it"',
     'available': False,
     'why': 'the usage lists are missing from a bundle written with --no-usage, and then nothing here knows '
            'who reads what (the detector runs and the caveats say so)'},
]

#: Resources that are listed whatever their rank.
RESOURCE_SPECIALS: List[str] = [
    'never read at all: a resource the engine tracked and no row of its chain reads -- a write-only target, a '
    'staging buffer nobody sampled, or a read that happens outside this capture. A UAV row does not say whether '
    'it was a read, so a resource whose only other rows are UAV or write rows is listed here too.',
    "a render target: it appears in some pass's targets, so it is what a pass writes rather than what one reads",
    'a format whose bytes do not mean what a naive read of them assumes: the row says which family it is and '
    'why that matters',
    'a texture the report could not decode: the resource table names no format for it, which is stated here '
    'rather than skipped. A buffer has no format to name, so this rule is about textures only -- calling every '
    'buffer undecodable would be a claim about the tool, and a wrong one.',
]

#: Formats whose bytes do not mean what a naive read of them assumes, with the reason. Matched as substrings of
#: the format name, case-insensitively: the engine's names carry the family (`..._Float`, `BC7_UNORM`), and a
#: name that matches none of these is left alone rather than called fine.
FORMAT_NOTES: List[Tuple[str, str]] = [
    ('float', 'a float format: its values are not in 0..1, so a reader that treats them as colour misreads '
              'the range'),
    ('snorm', 'a signed-normalised format: its range is -1..1, not 0..1'),
    ('bc', 'a compressed format: the bytes are blocks, not texels, and this report does not decode them'),
    ('srgb', 'an sRGB view: the same bytes mean different numbers depending on what the view declares'),
    ('packed', 'a packed format: more than one channel shares a word, so arithmetic on the word is not '
               'arithmetic on a channel'),
]

def _per_bundle(entry: NotableInput, available: bool) -> NotableInput:
    """A rule row with its availability corrected for this bundle.

    Two of the inputs are a fact about the *bundle* rather than about the tool -- whether it was written with
    counters, and whether its usage lists were collected -- so the table is built from the static rule and then
    told what this frame could answer. The reason stays when the input is still unavailable, and is cleared when
    it is not, because a row that says "not available, because --with-counters was missing" while the ranking
    above used the counters would be the kind of contradiction this report exists to avoid.
    """
    return NotableInput(input=entry['input'], how=entry['how'], available=available,
                        why='' if available else entry['why'])

#: What a texture is counted at when one figure has to compare it with a buffer, in the ranking's own words
#: (`RESOURCE_INPUTS`): the table records a buffer's bytes but not a texture's, and 4 bytes per pixel is what an
#: 8-bit-per-channel target would be. It is an estimate and the report says so where it prints it.
BYTES_PER_PIXEL = 4

def _pixels(resource: BundleResource) -> int:
    """A texture's dimensions as one count: width x height x depth x samples.

    Samples are in it because a multisampled target costs memory per sample, and depth because an array or a
    volume is that many slices. It is *pixels*, not bytes: see `PASS_INPUTS` for why a bundle has no byte count
    for a texture.
    """
    width = int(resource.get('width', 0) or 0)
    height = int(resource.get('height', 0) or 0)
    depth = max(1, int(resource.get('depth', 0) or 0))
    samples = max(1, int(resource.get('samples', 0) or 0))
    return width * height * depth * samples if width and height else 0

def _weight(resource: BundleResource) -> int:
    """One size for both kinds: bytes for a buffer, pixels x `BYTES_PER_PIXEL` for a texture."""
    if str(resource.get('kind')) == 'texture':
        return _pixels(resource) * BYTES_PER_PIXEL
    return int(resource.get('bytes', 0) or 0)

def _target_ids(entry: ReportPass) -> List[str]:
    """The resources a pass writes: its colour targets and, for a graphics pass, its depth target."""
    ids = [_res_id(str(target).split()[0]) for target in entry['targets']]
    if entry['kind'] != 'compute' and _is_resource(entry['depth']):
        ids.append(_res_id(str(entry['depth'])))
    return [idtext for idtext in ids if idtext not in ('', '0')]

def _read_eids(resource: BundleResource) -> List[int]:
    """The events at which the engine recorded a *read* of this resource, from its usage chain."""
    return [eid for eid, names in _usage_chain(resource) if names & USAGE_READS]

def _rw_eids(resource: BundleResource) -> List[int]:
    """The events where the resource is bound as a UAV, or otherwise in a read-write row.

    The engine's `*_RWResource` usages say the resource was reachable for reading *and* writing and do not say
    which happened, so they are in neither `USAGE_READS` nor `USAGE_WRITES` (that is measured, see
    `rdc_detect_common`). Counting them as reads would be a claim the row does not support, and ignoring them
    entirely -- "read by 0 passes" for a buffer a compute shader sampled through a UAV -- would be a false
    claim, so they are counted separately and named as the ambiguity they are.
    """
    return [eid for eid, names in _usage_chain(resource)
            if any(name.endswith('RWResource') for name in names)]

def _touched_after(resource: BundleResource, eid: int) -> bool:
    """Whether anything reads -- or *may* read -- this resource later than an event, which is what "nothing
    consumes what this pass wrote" means. A definite read counts, and so does a UAV row, because "the row does
    not say" is not the same as "it did not"."""
    return any(read > eid for read in _read_eids(resource) + _rw_eids(resource))

def _counter_cost(bundle: BundleData, first: int, last: int) -> Tuple[float, int]:
    """The counters a bundle holds for one pass's events: their sum, and how many rows that was.

    A row is the driver's own text (`eid 12  <name> = <value>`, and a counter can report a vector), so what is
    summed is the numbers to the right of the `=`. A bundle with no counters returns `(0.0, 0)` and the ranking
    table says the input was not there, rather than reporting the cost as zero.
    """
    total = 0.0
    rows = 0
    number = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
    for row in bundle['counters']:
        text = str(row)
        match = re.search(r'\beid\s+(\d+)', text)
        if not match or '=' not in text:
            continue
        eid = int(match.group(1))
        if not first <= eid <= last:
            continue
        total += sum(float(token) for token in number.findall(text.split('=', 1)[1]))
        rows += 1
    return total, rows

def notable_passes(bundle: BundleData, passes: Sequence[ReportPass]) -> Tuple[List[NotablePass], List[str]]:
    """The passes worth looking at first: the top of the ranking, then every pass an oddity rule lists.

    Returns the rows and the roll-up notes (what the oddity cap left out).
    """
    have_counters = bool(bundle['counters'])
    resources = {_res_id(str(r.get('resource', ''))): r for r in bundle['resources']}

    def calls(entry: ReportPass) -> int:
        return entry['graphics'] if entry['kind'] != 'compute' else entry['compute']

    def footprint(entry: ReportPass) -> int:
        return sum(_pixels(resources[idtext]) for idtext in _target_ids(entry) if idtext in resources)

    # The key is the input table's order, most significant first, and the pass index breaks ties: a report that
    # reshuffles between runs of the same bundle is not one a reader can diff.
    ranked = sorted(passes, key=lambda e: (-calls(e), -footprint(e), -len(e['firstTouched']),
                                           -_counter_cost(bundle, e['firstEid'], e['lastEid'])[0], e['index']))
    rows: Dict[int, NotablePass] = {}
    for rank, entry in enumerate(ranked[:NOTABLE_LIMIT], 1):
        values = ['%d call(s)' % calls(entry), '%.1f Mpixel of targets' % (footprint(entry) / 1000000.0),
                  '%d resource(s) first used here' % len(entry['firstTouched'])]
        if have_counters:
            total, count = _counter_cost(bundle, entry['firstEid'], entry['lastEid'])
            values.append('counter cost %.3f over %d row(s)' % (total, count))
        rows[entry['index']] = NotablePass(passIndex=entry['index'], firstEid=entry['firstEid'],
                                           lastEid=entry['lastEid'], rank=rank,
                                           why=['listed by the ranking: #%d of %d pass(es)'
                                                % (rank, len(passes))], values=values)

    # The oddity rules, in the order the module states them, one line per pass per rule.
    odd: Dict[int, List[str]] = {}
    for entry in sorted(passes, key=lambda e: e['index']):
        targets = _target_ids(entry)

        def odd_here(reason: str) -> None:
            if reason not in odd.setdefault(entry['index'], []):
                odd[entry['index']].append(reason)

        if entry['structure'] == 'depth only (no colour target)':
            odd_here(PASS_ODDITIES[0])
        if entry['kind'] != 'compute' and calls(entry) == 1:
            odd_here(PASS_ODDITIES[1])
        if not entry.get('marker'):
            odd_here(PASS_ODDITIES[2])
        known = [idtext for idtext in targets if idtext in resources]
        if known and all(not _touched_after(resources[idtext], entry['lastEid']) for idtext in known):
            odd_here('%s: res%s' % (PASS_ODDITIES[3], ', res'.join(known)))
        only = sorted(idtext for idtext in known if _only_this_pass(resources[idtext], entry, passes))
        if only:
            odd_here('%s: res%s' % (PASS_ODDITIES[4], ', res'.join(only)))

    notes: List[str] = []
    unlisted = [index for index in sorted(odd) if index not in rows]
    if len(unlisted) > ODDITY_LIMIT:
        notes.append('%d pass(es) matched an oddity rule and are not listed below; the pipeline map above has '
                     'them all' % (len(unlisted) - ODDITY_LIMIT))
        unlisted = unlisted[:ODDITY_LIMIT]
    for index in unlisted + [i for i in sorted(odd) if i in rows]:
        entry = next(e for e in passes if e['index'] == index)
        row = rows.get(index)
        if row is None:
            row = NotablePass(passIndex=index, firstEid=entry['firstEid'], lastEid=entry['lastEid'], rank=0,
                              why=[], values=['%d call(s)' % calls(entry), '%d event(s)' % entry['events']])
            rows[index] = row
        row['why'].extend(reason for reason in odd[index] if reason not in row['why'])

    listed = sorted(rows.values(), key=lambda r: (r['rank'] == 0, r['rank'] or r['passIndex']))
    return listed, notes

def _only_this_pass(resource: BundleResource, entry: ReportPass, passes: Sequence[ReportPass]) -> bool:
    """Whether every usage row of a resource falls inside one pass -- and there is more than one row to it."""
    chain = [eid for eid, _names in _usage_chain(resource)]
    if len(chain) < 2:
        return False
    elsewhere = [other for other in passes if other['index'] != entry['index']
                 and any(other['firstEid'] <= eid <= other['lastEid'] for eid in chain)]
    mine = any(entry['firstEid'] <= eid <= entry['lastEid'] for eid in chain)
    return mine and not elsewhere

def _usage_text(resource: BundleResource, readers: List[int], rw: List[int]) -> str:
    """What the usage chain says about a resource -- including the two ways it can say nothing.

    "Read by 0 passes" is only true of a resource the engine tracked and never saw read. A resource with an
    *empty* chain was not recorded at all, and one whose only row is `eid 0, Unused` is the documented marker for
    "not tracked"; printing either as "read by 0 passes" would turn "I do not know" into "nothing reads it",
    which is the one claim this whole report is built to avoid making.
    """
    chain = _usage_chain(resource)
    if not chain:
        return 'the engine recorded no usage row for it at all, so nothing here knows who touches it'
    if len(chain) == 1 and chain[0][1] == frozenset(['Unused']):
        return 'the engine did not track it: its only row is the documented `eid 0, Unused` marker'
    text = 'read by %d pass(es)' % len(readers)
    if readers:
        text += ': %s' % ', '.join('pass %d' % index for index in readers)
    if rw:
        text += '; bound read-write in %d pass(es) (%s), where the engine does not say whether the access ' \
                'was a read' % (len(rw), ', '.join('pass %d' % index for index in rw))
    return text

def _size_text(resource: BundleResource) -> str:
    """A resource's size in the units the table actually recorded, and the estimate where it has to be one."""
    if str(resource.get('kind')) == 'texture':
        return ('%.1f Mpixel (~%.1f MB at %d bytes/pixel)'
                % (_pixels(resource) / 1000000.0, _weight(resource) / 1048576.0, BYTES_PER_PIXEL))
    return '%.2f MB' % (int(resource.get('bytes', 0) or 0) / 1048576.0)

def _resource_row(resource: BundleResource, rank: int, values: List[str]) -> NotableResource:
    """One notable resource, with the same fields the driver prints for a resource everywhere else."""
    name = ' '.join(str(resource.get('name', '')).split())
    kind = str(resource.get('kind', 'other'))
    if kind == 'texture':
        detail = '%dx%dx%d %s' % (int(resource.get('width', 0) or 0), int(resource.get('height', 0) or 0),
                                  int(resource.get('depth', 0) or 0), str(resource.get('format', '?')))
    else:
        detail = '%.2f MB' % (int(resource.get('bytes', 0) or 0) / 1048576.0)
    return NotableResource(resource='res%s' % _res_id(str(resource.get('resource', ''))), name=name, kind=kind,
                           detail=detail, rank=rank, why=[], values=values)

def notable_resources(bundle: BundleData, passes: Sequence[ReportPass]) -> Tuple[List[NotableResource], List[str]]:
    """The resources worth looking at first: the top of the ranking, then every resource a rule lists."""
    have_usage = str(bundle['manifest'].get('resourceUsage', '')) == 'collected'
    targets = {idtext for entry in passes for idtext in _target_ids(entry)}

    def read_passes(resource: BundleResource) -> List[int]:
        if not have_usage:
            return []
        eids = _read_eids(resource)
        return [entry['index'] for entry in passes
                if any(entry['firstEid'] <= eid <= entry['lastEid'] for eid in eids)]

    def rw_passes(resource: BundleResource) -> List[int]:
        """The passes that bind it read-write: a UAV read and a UAV write look the same in the row."""
        if not have_usage:
            return []
        eids = _rw_eids(resource)
        return [entry['index'] for entry in passes
                if any(entry['firstEid'] <= eid <= entry['lastEid'] for eid in eids)]

    judged = [r for r in bundle['resources'] if str(r.get('kind')) in ('texture', 'buffer')]
    ranked = sorted(judged, key=lambda r: (-_weight(r), -len(read_passes(r)),
                                           int(r.get('resource', '0') or 0)))
    rows: Dict[str, NotableResource] = {}
    for rank, resource in enumerate(ranked[:NOTABLE_LIMIT], 1):
        idtext = 'res%s' % _res_id(str(resource.get('resource', '')))
        values = [_size_text(resource)]
        if have_usage:
            values.append(_usage_text(resource, read_passes(resource), rw_passes(resource)))
        rows[idtext] = _resource_row(resource, rank, values)
        rows[idtext]['why'].append('listed by the ranking: #%d of %d resource(s)' % (rank, len(ranked)))

    for resource in sorted(judged, key=lambda r: int(r.get('resource', '0') or 0)):
        idtext = 'res%s' % _res_id(str(resource.get('resource', '')))
        reasons: List[str] = []
        if (have_usage and not _read_eids(resource) and not _rw_eids(resource)
                and _usage_judged(resource) is not None):
            reasons.append(RESOURCE_SPECIALS[0])
        if idtext in targets:
            reasons.append(RESOURCE_SPECIALS[1])
        format_name = str(resource.get('format', '')).strip()
        if str(resource.get('kind')) == 'texture' and (not format_name or format_name in ('?', 'unknown')):
            reasons.append(RESOURCE_SPECIALS[3])
        elif format_name:
            reasons.extend('%s: `%s`' % (RESOURCE_SPECIALS[2], note) for token, note in FORMAT_NOTES
                           if token in format_name.lower())
        if not reasons:
            continue
        row = rows.get(idtext)
        if row is None:
            row = _resource_row(resource, 0, [_size_text(resource)])
            rows[idtext] = row
        row['why'].extend(reason for reason in reasons if reason not in row['why'])

    notes: List[str] = []
    listed = sorted(rows.values(), key=lambda r: (r['rank'] == 0, r['rank'] or 0, r['resource']))
    # The ranked five are never dropped; only the rule-listed rows can overflow the cap.
    if len(listed) > NOTABLE_LIMIT + ODDITY_LIMIT:
        notes.append('%d resource(s) matched a special rule and are not listed below'
                     % (len(listed) - NOTABLE_LIMIT - ODDITY_LIMIT))
        listed = listed[:NOTABLE_LIMIT + ODDITY_LIMIT]
    return listed, notes

def notables(bundle: BundleData, passes: Sequence[ReportPass]) -> Notables:
    """Both notable lists and the rules they were built by: the whole content of the report's section."""
    pass_rows, pass_notes = notable_passes(bundle, passes)
    resource_rows, resource_notes = notable_resources(bundle, passes)
    # The two inputs whose availability is a fact about *this* bundle rather than about the tool: the table says
    # which this frame could answer, so a reader can see what the order above was computed from.
    per_bundle = {'counter cost': bool(bundle['counters']),
                  'passes reading it': str(bundle['manifest'].get('resourceUsage', '')) == 'collected'}
    inputs = [_per_bundle(entry, per_bundle.get(entry['input'], entry['available']))
              for entry in PASS_INPUTS]
    resource_inputs = [_per_bundle(entry, per_bundle.get(entry['input'], entry['available']))
                       for entry in RESOURCE_INPUTS]
    return Notables(limit=NOTABLE_LIMIT, oddityLimit=ODDITY_LIMIT, passInputs=inputs,
                    oddities=list(PASS_ODDITIES), passes=pass_rows, resourceInputs=resource_inputs,
                    resourceSpecials=list(RESOURCE_SPECIALS), resources=resource_rows,
                    notes=pass_notes + resource_notes)

__all__ = [
    'FORMAT_NOTES',
    'NOTABLE_LIMIT',
    'ODDITY_LIMIT',
    'PASS_INPUTS',
    'PASS_ODDITIES',
    'RESOURCE_INPUTS',
    'RESOURCE_SPECIALS',
    'notable_passes',
    'notable_resources',
    'notables',
]
