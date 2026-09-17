"""The detectors that read the bundle alone: debug messages, all-zero constant blocks, and allocations nothing uses."""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403
from rdc_detect_common import *  # noqa: F401,F403

import re

from typing import Any, Dict, List, Tuple

def detect_messages(bundle: BundleData) -> List[RedFlag]:
    """The API's own complaints (`debug`), grouped by severity and text (certain).

    Rows are what the driver writes (`eid <n>  <severity>  <text>`), and the grouping key is the text with
    the eid taken out -- the same complaint at forty events is one finding with an eid range, not forty.
    """
    groups: Dict[Tuple[str, str], List[int]] = {}
    for message in bundle['messages']:
        if isinstance(message, dict):
            severity = str(message.get('severityText', message.get('severity', '?')))
            text = str(message.get('text', ''))
            eid = int(message.get('eid', 0) or 0)
        else:
            parts = str(message).split(None, 3)
            severity = parts[2] if len(parts) >= 3 else '?'
            text = parts[3] if len(parts) >= 4 else str(message)
            eid = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        groups.setdefault((severity, text), []).append(eid)

    flags: List[RedFlag] = []
    for key in sorted(groups):
        eids = sorted(groups[key])
        flags.append({
            'detector': 'debug-message',
            'what': '%s: %s' % (key[0], _md(key[1])),
            'evidence': ['%d message(s), eid %d..%d' % (len(eids), eids[0], eids[-1])],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags

def detect_zero_constant_blocks(bundle: BundleData) -> List[RedFlag]:
    """A constant block whose every numeric value is zero (certain).

    The *fact* is certain: the block the engine read holds zeros. What it means is not -- a feature switched
    off looks exactly the same as a buffer that was never filled -- so the wording is the observation and the
    meaning is left open. Occurrences of one block are grouped by (stage, slot, buffer) and the evidence says
    how many of the events it was dumped at were all-zero, because "zero at 120" and "zero everywhere" are
    different findings. Rows with no number in them (a struct's opening brace, a string) are not evidence
    either way.
    """
    number = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
    blocks: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for name in sorted(bundle['cbuffers']):
        document = bundle['cbuffers'][name]
        values: List[float] = []
        for row in document.get('variables', []):
            text = str(row)
            if '=' in text:                          # the value side only: a name can hold a digit
                values.extend(float(token) for token in number.findall(text.split('=', 1)[1]))
        key = (str(document.get('stage', '?')), str(document.get('slot', '?')),
               str(document.get('buffer', '?')))
        entry = blocks.setdefault(key, {'zero': [], 'all': [], 'values': 0})
        entry['all'].append(int(document.get('eid', 0) or 0))
        if values and all(value == 0.0 for value in values):
            entry['zero'].append(int(document.get('eid', 0) or 0))
            entry['values'] = max(entry['values'], len(values))

    flags: List[RedFlag] = []
    for key in sorted(blocks, key=lambda k: (k[0], str(k[1]), k[2])):
        entry = blocks[key]
        if not entry['zero']:
            continue
        zero, every = sorted(entry['zero']), sorted(entry['all'])
        # The driver says so itself when nothing is bound (`buffer` is then a note, not a resource), and that
        # is a different and stronger finding than "a buffer happens to read as zeros": the shader reads a
        # block that no descriptor reaches. Measured on the Android capture, where most hits are this one.
        unbound = 'none bound' in key[2]
        what = ('no root descriptor is bound for this block, so its %d value(s) read as zero -- a shader '
                'reading a block nothing reaches' % entry['values'] if unbound else
                'every value in this block is zero: %d value(s), zero at %d of the %d event(s) it was dumped '
                'at -- never filled in reads the same way as switched off'
                % (entry['values'], len(zero), len(every)))
        flags.append({
            'detector': 'all-zero-constant-block',
            'what': what,
            'evidence': ['%s stage, slot %s, %s%s' % (
                key[0], key[1],
                'no root descriptor bound' if unbound else 'buffer res%s' % _res_id(key[2]),
                ', eid %d..%d' % (zero[0], zero[-1]) if len(zero) > 1 else ', eid %d' % zero[0])],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags

#: How many unused resources a report names before it stops and counts the rest. The point of the finding is
#: usually the bytes, so the biggest come first; a capture with two hundred dead allocations should not bury
#: every other finding in the report.
DEAD_ALLOCATION_LIMIT = 20

def detect_dead_allocations(bundle: BundleData) -> List[RedFlag]:
    """A texture or buffer no call in the frame uses (certain).

    "Uses" is the engine's own usage list: a resource whose every record is usage 0 was created and never
    reached a call. Resources of kind `other` -- fences, queues, the descriptor heaps -- are not allocations
    the application made, and flagging them would bury the real ones. Needs the usage lists, so a bundle
    written with `--no-usage` skips this detector rather than reporting it clean.
    """
    dead: List[BundleResource] = []
    for resource in bundle['resources']:
        if str(resource.get('kind')) not in ('texture', 'buffer'):
            continue
        if any(int(record.get('usage', 0) or 0) != 0 for record in resource.get('usage', [])):
            continue
        dead.append(resource)

    flags: List[RedFlag] = []
    for resource in sorted(dead, key=lambda r: (-int(r.get('bytes', 0) or 0),
                                                int(r.get('resource', '0') or 0)))[:DEAD_ALLOCATION_LIMIT]:
        size = int(resource.get('bytes', 0) or 0)
        name = ' '.join(str(resource.get('name', '')).split())
        if str(resource.get('kind')) == 'texture':
            detail = '%dx%dx%d %s' % (int(resource.get('width', 0) or 0), int(resource.get('height', 0) or 0),
                                      int(resource.get('depth', 0) or 0), str(resource.get('format', '?')))
        else:
            detail = 'buffer'
        flags.append({
            'detector': 'dead-allocation',
            'what': 'created and never used by any call: %.2f MB' % (size / 1048576.0),
            'evidence': ['res%s%s (%s, %s)' % (_res_id(str(resource.get('resource', ''))),
                                               ' "%s"' % name if name else '', resource.get('kind'), detail)],
            'certainty': 'certain',
            'unproven': True,
        })
    if len(dead) > DEAD_ALLOCATION_LIMIT:
        flags.append({
            'detector': 'dead-allocation',
            'what': '%d smaller unused resource(s) are not listed' % (len(dead) - DEAD_ALLOCATION_LIMIT),
            'evidence': ['%d unused in total' % len(dead)],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags

__all__ = [
    'DEAD_ALLOCATION_LIMIT',
    'detect_dead_allocations',
    'detect_messages',
    'detect_zero_constant_blocks',
]
