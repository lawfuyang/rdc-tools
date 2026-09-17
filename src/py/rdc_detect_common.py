"""What the detector families share: the usage vocabulary the engine's rows are named with, the labels a finding prints, and the grouped-and-capped finding."""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403

from typing import Dict, FrozenSet, List, Optional, Tuple

_USAGE_STAGES = ('VS', 'HS', 'DS', 'GS', 'PS', 'CS', 'TS', 'MS')

#: Which usages *read* the resource's contents and which *write* them. `Resolve` and `Copy` are the
#: source-and-destination forms and are in both; `Barrier` and `Unused` are in neither. The RW family is in
#: both on purpose: a UAV may be read and written by the same dispatch and the row does not say which
#: happened, so a rule that needs "definitely nothing writes here" must not see a read in an RW row.
USAGE_READS = frozenset(
    {'VertexBuffer', 'IndexBuffer', 'InputTarget', 'Indirect', 'ResolveSrc', 'CopySrc', 'Resolve', 'Copy',
     'All_Constants', 'All_Resource'}
    | {'%s_Constants' % stage for stage in _USAGE_STAGES}
    | {'%s_Resource' % stage for stage in _USAGE_STAGES})
USAGE_WRITES = frozenset(
    {'ColorTarget', 'DepthStencilTarget', 'Clear', 'Discard', 'GenMips', 'StreamOut', 'ResolveDst',
     'CopyDst', 'CPUWrite', 'Resolve', 'Copy', 'All_RWResource'}
    | {'%s_RWResource' % stage for stage in _USAGE_STAGES})

#: The usages that make a resource a render target (the load-instead-of-clear rule keys off these).
USAGE_TARGETS = frozenset({'ColorTarget', 'DepthStencilTarget'})

#: How many resources a usage finding names before it counts the rest, for the same reason as
#: DEAD_ALLOCATION_LIMIT: the finding is the group, not a wall of resource ids.
USAGE_LIST_LIMIT = 8

# ---------------------------------------------------------------------------
# The usage chain: read-before-write, write-never-read, load-instead-of-clear.
#
# `GetUsage` answers, per resource, the events it was used at and *how*. The decode below is measured rather
# than inferred, and two of its facts are the kind that would be wrong if assumed: the value is *one usage,
# not a bitmask* (the API's own example says one entry per usage, and the real bundle shows one buffer with
# eight `VertexBuffer` rows at one eid -- one per binding slot), and the numbering is the enum's declaration
# order in `renderdoc-src/renderdoc/api/replay/replay_enums.h`. All 17 distinct values in the real bundle
# land where their semantics say they must: 32/33 on the two target kinds `events.json` also names (the depth
# texture's own chain proves it), 35/36 on the Clear/Discard rows before them, 42/43 on the copy halves.
#
# A resource whose only row is `(eid 0, Unused)` was not tracked by the engine -- the API documents exactly
# that marker -- and is never judged. What the chain cannot show is named in the findings themselves: the
# list stops at the capture, so a read by the next frame, or by the CPU after it, is indistinguishable from
# nothing ever reading the resource.
USAGE_NAMES: Tuple[str, ...] = (
    'Unused', 'VertexBuffer', 'IndexBuffer',
    'VS_Constants', 'HS_Constants', 'DS_Constants', 'GS_Constants', 'PS_Constants', 'CS_Constants',
    'TS_Constants', 'MS_Constants', 'All_Constants',
    'StreamOut',
    'VS_Resource', 'HS_Resource', 'DS_Resource', 'GS_Resource', 'PS_Resource', 'CS_Resource',
    'TS_Resource', 'MS_Resource', 'All_Resource',
    'VS_RWResource', 'HS_RWResource', 'DS_RWResource', 'GS_RWResource', 'PS_RWResource', 'CS_RWResource',
    'TS_RWResource', 'MS_RWResource', 'All_RWResource',
    'InputTarget', 'ColorTarget', 'DepthStencilTarget',
    'Indirect',
    'Clear', 'Discard', 'GenMips', 'Resolve', 'ResolveSrc', 'ResolveDst', 'Copy', 'CopySrc', 'CopyDst',
    'Barrier', 'CPUWrite')

def usage_name(value: int) -> str:
    """The engine's name for a usage value, or `usage(N)` -- the same fallback the driver prints, for a value
    this table does not know (a newer engine's usage, and naming it would be a guess)."""
    return USAGE_NAMES[value] if 0 <= value < len(USAGE_NAMES) else 'usage(%d)' % value

def _usage_chain(resource: BundleResource) -> List[Tuple[int, FrozenSet[str]]]:
    """A resource's usage as `(eid, {names})` per event, ascending.

    Rows are one usage each, so an eid can carry several and the chain is a *set* per event: deduplicating
    here is what keeps "the same buffer bound to eight slots" from reading as eight different uses. `[]`
    means the list is empty (a resource no call touched) and `[(0, {'Unused'})]` means the engine did not
    track it -- two different facts, and every caller below checks both.
    """
    per_event: Dict[int, set] = {}
    for record in resource.get('usage', []):
        # A bundle written with `--no-usage` puts a *string* here (`"(not collected: --no-usage)"`), which is
        # why the callers are gated on the manifest and this skips it rather than trusting the shape.
        if not isinstance(record, dict):
            continue
        per_event.setdefault(int(record.get('eid', 0) or 0), set()).add(
            usage_name(int(record.get('usage', 0) or 0)))
    return [(eid, frozenset(names)) for eid, names in sorted(per_event.items())]

def _usage_judged(resource: BundleResource) -> Optional[List[Tuple[int, FrozenSet[str]]]]:
    """The chain of a resource this family may judge, or None: textures and buffers only (a heap or a queue
    is not an allocation the application reads and writes), and never a resource the engine did not track."""
    if str(resource.get('kind')) not in ('texture', 'buffer'):
        return None
    chain = _usage_chain(resource)
    if not chain or (len(chain) == 1 and chain[0][1] == frozenset(['Unused'])):
        return None
    return chain

def _resource_label(resource: BundleResource) -> str:
    """`res2207 "BufferedRT" (texture, 3104x3296x1 B10G11R11_UFloatPack32)`: enough to find it in the capture."""
    name = ' '.join(str(resource.get('name', '')).split())
    if str(resource.get('kind')) == 'texture':
        detail = '%dx%dx%d %s' % (int(resource.get('width', 0) or 0), int(resource.get('height', 0) or 0),
                                  int(resource.get('depth', 0) or 0), str(resource.get('format', '?')))
    else:
        detail = '%.2f MB' % (int(resource.get('bytes', 0) or 0) / 1048576.0)
    return 'res%s%s (%s, %s)' % (_res_id(str(resource.get('resource', ''))),
                                 ' "%s"' % name if name else '', str(resource.get('kind')), detail)

def _usage_flag(detector: str, what: str, lines: List[str]) -> RedFlag:
    """One finding from a group of resource lines, capped and rolled up like the dead-allocation list."""
    return {
        'detector': detector,
        'what': what,
        'evidence': lines[:USAGE_LIST_LIMIT] + (
            ['%d more, not listed' % (len(lines) - USAGE_LIST_LIMIT)] if len(lines) > USAGE_LIST_LIMIT else []),
        'certainty': 'question',
        'unproven': True,
    }

__all__ = [
    'USAGE_LIST_LIMIT',
    'USAGE_NAMES',
    'USAGE_READS',
    'USAGE_TARGETS',
    'USAGE_WRITES',
    '_USAGE_STAGES',
    '_resource_label',
    '_usage_chain',
    '_usage_flag',
    '_usage_judged',
    'usage_name',
]
