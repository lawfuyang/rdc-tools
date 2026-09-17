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

# ---------------------------------------------------------------------------
# The detectors' own metadata: how much a finding matters, and how to see it again.
#
# The report groups its findings by severity (REFERENCE §4.11), and a severity is an *opinion* -- so it is
# declared here, one line per detector, printed in the report as a table, and grouped by it there. A reader who
# disagrees with a group can see exactly which judgement put a finding in it, which is the whole point of
# declaring it rather than sorting by a number in the renderer. The `recipe` is the other half of the same
# contract: every finding says how to look at its own evidence with the driver alone, so nothing the report
# claims is only checkable *inside* the report.
#
# Severity is not certainty. `certain` (see `RedFlag`) is what the *bundle* proves; severity is what the
# finding would mean *if it is real*. A certain finding about an empty scissor is a high-severity finding; a
# certain finding that a draw sits outside every marker is a low-severity one.
SEVERITY_ORDER = ('high', 'medium', 'low')

#: What each group means for a reader, in the report's own words.
SEVERITY_MEANS = {
    'high': 'if it is real, the frame is not rendering what it means to: a binding, a state or a call '
            'argument contradicts itself',
    'medium': 'if it is real, something is doing less than it looks like it is doing',
    'low': 'a structure observation: nothing here is necessarily wrong with the rendering, but the frame is '
           'not organised the way the rest of the capture is',
}

#: Severity per detector, and the sentence that justifies it. Written as a table so a reader can disagree with
#: one line instead of with the sort order.
DETECTOR_SEVERITY: Dict[str, Tuple[str, str]] = {
    'all-zero-constant-block': (
        'high', 'a block the shader reads holds zeros, where its own reflection says it has members'),
    'binding-kind-mismatch': (
        'high', 'the root signature and the heap disagree about a register: the wrong kind of descriptor is '
                'bound there'),
    'unbound-root-parameter': (
        'high', 'a block is bound to a root parameter the signature does not declare, so the shader reads a '
                'register nothing fills'),
    'unbound-table-slot': (
        'high', 'a descriptor table is bound with a slot in it empty, so a binding the shader reads reaches '
                'nothing'),
    'shader-io-mismatch': (
        'high', 'the pixel stage reads an input the vertex stage never writes'),
    'depth-logic': (
        'high', 'depth is written through a test that is off, or tested against a target that is not bound'),
    'empty-scissor': (
        'high', 'the rectangle cannot rasterise, so the call draws nothing'),
    'zero-work': (
        'high', "the call's own arguments make it do no work"),
    'mismatched-msaa': (
        'medium', 'a multisampled target with no resolve anywhere in the capture'),
    'dead-allocation': (
        'medium', 'memory is allocated for a resource no call in the capture uses'),
    'read-before-write': (
        'medium', 'contents are read before anything in the capture wrote them'),
    'write-never-read': (
        'medium', 'a write nothing in the capture reads'),
    'load-instead-of-clear': (
        'medium', 'a target is loaded rather than cleared where it is first written'),
    'dead-compute': (
        'medium', 'a dispatch whose outputs nothing in the capture reads'),
    'stencil-without-writer': (
        'medium', 'the stencil test is used with nothing writing stencil first'),
    'blend-in-opaque-pass': (
        'medium', 'blending on a target whose name says it holds data that is not colour'),
    'format-units-suspicion': (
        'medium', 'a float output into a narrow or non-linear target, where the units may not be what the '
                  'shader means'),
    'debug-message': (
        'medium', "the application's own complaint: its own severity is in the finding"),
    'marker-imbalance': (
        'low', 'the marker stack does not close inside the capture, so part of it is unlabelled'),
    'unattributed-draws': (
        'low', 'work outside any marker, which is how a capture loses its structure'),
}

#: The command that shows a finding's own evidence with the driver alone. Placeholders are `{rdc}`, `{eid}`
#: (the first event the finding's evidence names), `{resId}`, `{stage}` and `{slot}`; when the evidence does not
#: name one a row needs, the command falls back to `RECIPE_FALLBACK`, which needs nothing but the capture.
DETECTOR_RECIPE: Dict[str, str] = {
    'debug-message': "replay_dump debug '{rdc}'",
    'all-zero-constant-block': "replay_dump cb '{rdc}' {eid} {stage} {slot}",
    'unbound-root-parameter': "replay_dump state '{rdc}' {eid}",
    'unbound-table-slot': "replay_dump state '{rdc}' {eid}",
    'binding-kind-mismatch': "replay_dump state '{rdc}' {eid}",
    'shader-io-mismatch': "replay_dump shaders '{rdc}' {eid}",
    'dead-allocation': "replay_dump usage '{rdc}' {resId}",
    'read-before-write': "replay_dump usage '{rdc}' {resId}",
    'write-never-read': "replay_dump usage '{rdc}' {resId}",
    'load-instead-of-clear': "replay_dump usage '{rdc}' {resId}",
    'dead-compute': "replay_dump usage '{rdc}' {resId}",
    'mismatched-msaa': "replay_dump usage '{rdc}' {resId}",
    'depth-logic': "replay_dump state '{rdc}' {eid}",
    'empty-scissor': "replay_dump state '{rdc}' {eid}",
    'stencil-without-writer': "replay_dump state '{rdc}' {eid}",
    'blend-in-opaque-pass': "replay_dump state '{rdc}' {eid}",
    'format-units-suspicion': "replay_dump state '{rdc}' {eid}",
    'marker-imbalance': "replay_dump draws '{rdc}'",
    'unattributed-draws': "replay_dump draws '{rdc}'",
    'zero-work': "replay_dump draws '{rdc}'",
}

#: Used when a recipe needs a value the finding's evidence does not name: the action tree is in every capture
#: and names every event, so it is always a way to find the one the row is about.
RECIPE_FALLBACK = "replay_dump draws '{rdc}'"

def severity_of(detector: str) -> str:
    """A detector's declared severity. A detector that is not in the table is a bug the tests catch; until then
    it is grouped as `medium`, because dropping it or inventing `high` would both be worse."""
    return DETECTOR_SEVERITY.get(detector, ('medium', ''))[0]

def severity_table() -> List[SeverityRow]:
    """The severity groups, as rows: strongest first, each with its meaning and the detectors in it.

    Every detector in the suite appears exactly once, which is what lets the report group its findings by a
    join on `detector` alone -- and what a test asserts, so a detector added later cannot quietly fall out of
    the report's ordering.
    """
    rows: List[SeverityRow] = []
    for severity in SEVERITY_ORDER:
        # Built as `SeverityMember`s rather than as literals: a dict literal of two strings widens to
        # `dict[str, str]`, which is not the row the document promises.
        members = [SeverityMember(detector=detector, why=DETECTOR_SEVERITY[detector][1])
                   for detector in sorted(DETECTOR_SEVERITY) if DETECTOR_SEVERITY[detector][0] == severity]
        rows.append(SeverityRow(severity=severity, means=SEVERITY_MEANS[severity], members=members))
    return rows

__all__ = [
    'DETECTOR_RECIPE',
    'DETECTOR_SEVERITY',
    'RECIPE_FALLBACK',
    'SEVERITY_MEANS',
    'SEVERITY_ORDER',
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
    'severity_of',
    'severity_table',
    'usage_name',
]
