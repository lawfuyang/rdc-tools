"""What to look at first: the report's ranked recommendations, each with the command that shows it.

The report's recommendations (REFERENCE §4.11). A recommendation is not a finding and not a verdict -- it is a *lead*: the most significant
instance of something the report noticed, one line saying what it would be worth checking, and the command that
puts the evidence on screen with the driver alone. One row per thing to look at, not one per finding: a capture
with twenty-one dead allocations has one recommendation ("the biggest of them, and how many there are"), because
a list of twenty-one is not a way to decide where to start.

The rows are ranked, and the rank is the same kind of stated opinion as the detector severities it uses: the
detector's declared severity first, then findings before structure notes before the gaps the report could not
close. Nothing here re-reads the frame: everything comes from the findings, the notable lists and the detector
runs that were computed before it.
"""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403
from rdc_detect_common import *  # noqa: F401,F403
from rdc_notable import *  # noqa: F401,F403

import re
from typing import Dict, List, Sequence, Tuple

#: How many recommendations the report lists. Stated, and rolled up in a note: a recommendation list longer
#: than this is a second copy of the findings section.
RECOMMENDATION_LIMIT = 12

#: The order two rows of equal severity are listed in: a finding is a lead, a notable is structure worth
#: knowing, and a gap is a statement about the report rather than about the frame.
KIND_ORDER = ('finding', 'notable', 'gap')

#: What to *do* about each oddity rule in `rdc_notable.PASS_ODDITIES`, one line per rule in the same order. The
#: rule says what was seen; this says what it is worth checking, which is the part a report can be wrong about
#: and a reader can therefore disagree with.
PASS_ACTIONS: List[str] = [
    'check whether this depth-only pass is the shadow or depth prepass you expect it to be',
    'check whether a one-call pass is doing the work it was meant to: a full-screen draw is one call, one '
    'object is usually not',
    'check what this unmarked work belongs to -- work with no marker is where "what is this draw for" is '
    'hardest to answer',
    'check whether anything is meant to consume what this pass writes: none of its targets is read anywhere '
    'in this capture',
    'check whether the resource this pass alone touches is its own scratch, or something another stage was '
    'meant to read',
]

#: What to do about each resource special in `rdc_notable.RESOURCE_SPECIALS`, in the same order.
RESOURCE_ACTIONS: List[str] = [
    'check whether this resource is written for a reader outside the capture (the next frame, the CPU, or a '
    'present) -- the usage chain stops at the end of this one',
    'check this pass\'s target: it is read from, so its contents are an input to something as well as an '
    'output of one',
    'read this format with its own units in mind: the note in the notable list says which ones are unusual and '
    'why',
    'check why this resource has no format: the table could not decode it, so nothing here can say what it '
    'holds',
]

#: How to make a gap checkable, keyed by a substring of the reason the detector run gives. A gap is the only
#: kind of recommendation whose fix is usually about the *bundle* rather than the frame, so the command is the
#: one that writes or reads a bundle, and `do` says which flavour of it.
GAP_RECIPES: List[Tuple[str, str, str]] = [
    ('--no-usage', "replay_dump dump '{rdc}' <dir>",
     'write the bundle again without --no-usage, so the usage lists are collected'),
    ('older driver', "replay_dump dump '{rdc}' <dir>",
     'write the bundle again with a driver whose format carries what this rule reads'),
    ('pipeline state', "replay_dump dump '{rdc}' <dir>",
     'write the bundle again with the current driver, which records the pipeline state'),
    ('no capture path', "replay_dump info '<capture.rdc>'",
     'pass the capture as the report\'s first argument, so the three .rdc-side rules can run'),
    ('could not be read', "replay_dump info '{rdc}'",
     'point the report at the capture this bundle was written from'),
    ('chunk-name map', "replay_dump info '{rdc}'",
     'put a copy of the RenderDoc source tree in <root>/renderdoc-src (README §1.1), which names the chunks'),
]

#: The references a finding's evidence spells, so a command can be aimed at the same event or resource the
#: finding is about. The driver writes these forms itself (`eid 12..40`, `res2207`, `ps stage, slot 3`), and a
#: form it does not write simply leaves the placeholder empty -- the row then falls back to a command that needs
#: nothing but the capture.
_EID = re.compile(r'\beid\s+(\d+)')
_RES = re.compile(r'\bres(\d+)')
# The slot is a number in the driver's own format (`ps stage, slot 3, buffer res30, eid 12`), so the digits are
# what it is: taking non-space characters instead would carry the comma into the command.
_STAGE_SLOT = re.compile(r'\b([a-z]{2})\s+stage,\s*slot\s+(\d+)')

def _refs(flag: RedFlag) -> Dict[str, str]:
    """The first event, the first resource and the stage/slot a finding's evidence names."""
    text = ' '.join(flag['evidence']) + ' ' + flag['what']
    refs: Dict[str, str] = {}
    match = _EID.search(text)
    if match:
        refs['eid'] = match.group(1)
    match = _RES.search(text)
    if match:
        refs['resId'] = match.group(1)
    match = _STAGE_SLOT.search(text)
    if match:
        refs['stage'], refs['slot'] = match.group(1), match.group(2)
    return refs

def _fill(template: str, rdc: str) -> str:
    """A command with the capture in it. `str.replace`, not `str.format`: a capture path is application data and
    an unusual one can hold a brace, which `format` would read as its own syntax."""
    return template.replace('{rdc}', rdc)

def _command(detector: str, refs: Dict[str, str], rdc: str) -> str:
    """The command that shows a finding's evidence, or the frame-level one when a needed value is not in it."""
    template = DETECTOR_RECIPE.get(detector, RECIPE_FALLBACK)
    filled = _fill(template, rdc)
    # Only the placeholders the command actually uses have to be known: a usage row needs a resId and has no
    # event, and demanding an eid of it would send every one of those rows to the fallback.
    for placeholder, value in (('{eid}', refs.get('eid', '')), ('{resId}', refs.get('resId', '')),
                               ('{stage}', refs.get('stage', '')), ('{slot}', refs.get('slot', ''))):
        if placeholder in template and value == '':
            return _fill(RECIPE_FALLBACK, rdc)
        filled = filled.replace(placeholder, value)
    return filled

def _finding_rows(flags: Sequence[RedFlag], rdc: str) -> List[Recommendation]:
    """One row per detector that fired: its most significant finding, and how many there are in total.

    The detectors sort their own findings -- "biggest first" is information -- so the first of a group is the
    one to look at first, and the row says how many more there are rather than listing them.
    """
    groups: Dict[str, List[RedFlag]] = {}
    for flag in flags:
        groups.setdefault(flag['detector'], []).append(flag)
    rows: List[Recommendation] = []
    for detector in sorted(groups):
        group = groups[detector]
        first = group[0]
        more = '' if len(group) == 1 else ' (%d finding(s) in all, listed below)' % len(group)
        refs = _refs(first)
        rows.append({
            'rank': 0, 'kind': 'finding', 'severity': severity_of(detector),
            'do': 'check %s: %s%s' % (detector, first['what'], more),
            'why': '%s -- %s' % (first['certainty'], '; '.join(first['evidence'])[:220]),
            'command': _command(detector, refs, rdc),
            'eid': int(refs.get('eid', '0')), 'resource': 'res%s' % refs['resId'] if 'resId' in refs else '',
        })
    return rows

def _pass_rule_index(reason: str) -> int:
    """Which pass oddity rule a notable row's reason came from: a row's reasons *start* with the rule it matched,
    because the module appends the evidence (`...: res1103, res1104`) rather than replacing the rule with it."""
    for index, rule in enumerate(PASS_ODDITIES):
        if reason.startswith(rule):
            return index
    return -1

def _resource_rule_index(reason: str) -> int:
    """The same for the resource specials, which are a shorter list and are matched on their own."""
    for index, rule in enumerate(RESOURCE_SPECIALS):
        if reason.startswith(rule):
            return index
    return -1

def _notable_rows(rows: List[NotablePass], rdc: str) -> List[Recommendation]:
    """One row per oddity rule that matched, with every pass that matched it named in the row."""
    matched: Dict[int, List[NotablePass]] = {}
    for row in rows:
        for reason in row['why']:
            index = _pass_rule_index(reason)
            if index >= 0 and row not in matched.setdefault(index, []):
                matched[index].append(row)
    out: List[Recommendation] = []
    for index in sorted(matched):
        passes = matched[index]
        first = passes[0]
        where = ', '.join('pass %d (eid %d)' % (row['passIndex'], row['firstEid']) for row in passes[:6])
        if len(passes) > 6:
            where += ' and %d more' % (len(passes) - 6)
        out.append({
            'rank': 0, 'kind': 'notable', 'severity': 'low',
            'do': '%s -- %d pass(es): %s' % (PASS_ACTIONS[index], len(passes), where),
            'why': PASS_ODDITIES[index],
            'command': "replay_dump state '%s' %d" % (rdc, first['firstEid']),
            'eid': first['firstEid'], 'resource': '',
        })
    return out

def _resource_rows(rows: List[NotableResource], rdc: str) -> List[Recommendation]:
    """One row per resource special rule that matched, with every resource that matched it named."""
    matched: Dict[int, List[NotableResource]] = {}
    for row in rows:
        for reason in row['why']:
            index = _resource_rule_index(reason)
            if index >= 0 and row not in matched.setdefault(index, []):
                matched[index].append(row)
    out: List[Recommendation] = []
    for index in sorted(matched):
        resources = matched[index]
        first = resources[0]
        where = ', '.join('%s (%s)' % (row['resource'], row['detail']) for row in resources[:4])
        if len(resources) > 4:
            where += ' and %d more' % (len(resources) - 4)
        out.append({
            'rank': 0, 'kind': 'notable', 'severity': 'low',
            'do': '%s -- %d resource(s): %s' % (RESOURCE_ACTIONS[index], len(resources), where),
            'why': RESOURCE_SPECIALS[index],
            'command': "replay_dump usage '%s' %s" % (rdc, first['resource']),
            'eid': 0, 'resource': first['resource'],
        })
    return out

def _gap_rows(runs: Sequence[DetectorRun], bundle: BundleData, rdc: str) -> List[Recommendation]:
    """One row per thing the report could not check, with what would make it checkable.

    A skipped detector is the most useful kind of gap to state, because it is the one a reader would otherwise
    read as clean. The bundle's own empty members (no counters, no shader debugging) are gaps too, and they are
    read from the bundle rather than assumed.
    """
    out: List[Recommendation] = []
    for run in runs:
        if run['ran']:
            continue
        recipe = _fill(RECIPE_FALLBACK, rdc)
        action = 'check why this rule did not run: %s' % run['why']
        for needle, command, text in GAP_RECIPES:
            if needle in run['why']:
                recipe, action = _fill(command, rdc), text
                break
        out.append({
            'rank': 0, 'kind': 'gap', 'severity': 'low',
            'do': 'the report could not check %s: %s. %s' % (run['detector'], run['why'], action),
            'why': run['why'], 'command': recipe, 'eid': 0, 'resource': '',
        })
    if not bundle['counters']:
        out.append({
            'rank': 0, 'kind': 'gap', 'severity': 'low',
            'do': 'no counter results in this bundle, so cost could not rank the passes. Write it with '
                  '--with-counters to rank by what the work cost',
            'why': 'counters are collected only with --with-counters, and they are slow and driver-dependent',
            'command': 'replay_dump dump \'%s\' <dir>' % rdc, 'eid': 0, 'resource': '',
        })
    if not int(bundle['capture'].get('shaderDebugging', 0) or 0):
        out.append({
            'rank': 0, 'kind': 'gap', 'severity': 'low',
            'do': 'this capture has no shader debugging information, so nothing here can say what a shader '
                  'computed from its inputs -- only what it was bound to',
            'why': 'the capture properties report shaderDebugging 0',
            'command': "replay_dump info '%s'" % rdc, 'eid': 0, 'resource': '',
        })
    return out

def recommendations(rdc: str, bundle: BundleData, flags: Sequence[RedFlag], runs: Sequence[DetectorRun],
                    notable_passes_rows: List[NotablePass],
                    notable_resource_rows: List[NotableResource]) -> Recommendations:
    """The ranked list of what to look at first, and the roll-up of what the cap left out."""
    rows = (_finding_rows(flags, rdc) + _notable_rows(notable_passes_rows, rdc)
            + _resource_rows(notable_resource_rows, rdc) + _gap_rows(runs, bundle, rdc))
    rows.sort(key=lambda row: (SEVERITY_ORDER.index(row['severity']), KIND_ORDER.index(row['kind']),
                               row['do']))
    notes: List[str] = []
    if len(rows) > RECOMMENDATION_LIMIT:
        notes.append('%d more, not listed: the rest of the ranking, in the same order' %
                     (len(rows) - RECOMMENDATION_LIMIT))
        rows = rows[:RECOMMENDATION_LIMIT]
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
    return Recommendations(limit=RECOMMENDATION_LIMIT, rows=rows, notes=notes)

__all__ = [
    'GAP_RECIPES',
    'KIND_ORDER',
    'PASS_ACTIONS',
    'RECOMMENDATION_LIMIT',
    'RESOURCE_ACTIONS',
    '_command',
    '_refs',
    'recommendations',
]
