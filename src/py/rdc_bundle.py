"""The report's input: the bundle of engine answers, its types, and the helpers every later module shares (ids, markdown escaping, target text)."""

from __future__ import annotations

import json
import os

import rdc_profile

from typing import Any, Dict, List, TypedDict, Union

BUNDLE_VERSION = 1
REPORT_VERSION = 1
#: The report *document's* format version, which every document the tool writes carries as `schemaVersion`
#: (`REPORT_SCHEMA` pins it with `const`). It is not `REPORT_VERSION`: that one tracks what the report says,
#: this one the shape it says it in, and the two can move independently.
REPORT_SCHEMA_VERSION = 1

class BundleError(Exception):
    """A directory that cannot be read as a bundle: a missing file, a wrong version, invalid JSON."""

class BundleEvent(TypedDict):
    eid: int
    #: The engine's marker path for this event (`A > B`), which a driver from 2026-09-17 on writes and an
    #: older one does not: read it with `.get('marker', '')` rather than by index, because a bundle outlives
    #: the driver that wrote it.
    marker: str
    pso: str
    psoKind: str
    shaders: str
    targets: List[str]
    depth: str
    rootParameters: int
    state: str
    #: What the *call* asked the GPU to do, taken from the engine's action list by a driver from 2026-09-22 on:
    #: a draw carries `vertices`/`instances`/`triangles` (`triangles` is 0 when the topology does not fix one,
    #: and `vertices` is the index count when the draw is indexed), a dispatch carries
    #: `groups`/`threadsPerGroup`/`threads`. Absent for an event that is not a call -- a state setter, a
    #: marker, a barrier -- and from a bundle written before that driver, so read it with
    #: `.get('volume')`: an empty result means "not measured here", which is not the same as "asked for
    #: nothing".
    volume: Dict[str, Any]

class BundleUsage(TypedDict):
    eid: int
    usage: int

class BundleResource(TypedDict, total=False):
    resource: str
    name: str
    kind: str
    format: str
    dimension: int
    width: int
    height: int
    depth: int
    mips: int
    arraySize: int
    samples: int
    bytes: int
    usage: List[BundleUsage]
    usageCount: int
    firstEvent: int
    lastEvent: int

class BundleMessage(TypedDict, total=False):
    eid: int
    severity: int
    severityText: str
    text: str

class BundleCounter(TypedDict):
    #: The event the counter was measured at.
    eid: int
    #: The engine's `GPUCounter` enum value: RenderDoc's names for them live in its own (unexported)
    #: `stringise.cpp`, so a bundle carries the number and `costCounterName` is the one name it can give.
    counter: int
    #: The value, read by the driver through that counter's own result type (a `u64` counter read as a
    #: double is a number nobody can use).
    value: float

class BundleData(TypedDict):
    manifest: Dict[str, Any]
    capture: Dict[str, Any]
    events: List[BundleEvent]
    resources: List[BundleResource]
    #: The driver's counter results, one row per event per counter, empty unless the bundle was written with
    #: `--with-counters`. They are the only per-event *cost* a bundle carries, which is why the notable
    #: ranking and the pass table read them when they are there and say so when they are not.
    counters: List[BundleCounter]
    #: Which counter *is* the cost, and its unit: the same choice the engine's own fold makes
    #: (`CostCounterOf`, `EventGPUDuration` when the replay produced one). A pass's cost is that counter
    #: summed over the pass -- summing every row instead would add a duration to a byte count.
    costCounter: int
    costCounterName: str
    counterUnit: str
    #: The engine's own fold over its passes (`counters-passes.json`, the document `counters --per-pass`
    #: prints), or `{}` when the bundle carries no counters. The pass table does not need it -- it sums the
    #: per-event rows over *its own* ranges -- but a reader comparing against `counters --per-pass`, or
    #: asking which pass the engine itself called dearest, wants the engine's view of its own passes.
    counterPasses: Dict[str, Any]
    #: The driver writes messages as *rows* (`eid 12  error  <text>`), which is what the schema describes; an
    #: object with the same facts is accepted too, because a consumer should not have to know the spelling.
    messages: List[Union[BundleMessage, str]]
    states: Dict[str, Dict[str, Any]]
    cbuffers: Dict[str, Dict[str, Any]]

class ReportPass(TypedDict):
    index: int
    firstEid: int
    lastEid: int
    #: The marker path of the pass's first event, empty when that event is inside no marker (or when the
    #: bundle came from a driver that did not write them).
    marker: str
    kind: str
    reason: str
    events: int
    graphics: int
    compute: int
    targets: List[str]
    depth: str
    structure: str
    shaders: List[str]
    otherShaders: List[str]
    blocks: List[str]
    firstTouched: List[str]
    #: Work summed over the pass's calls, from the event rows' `volume`: `vertices`/`instances`/`triangles` for
    #: a graphics pass, `threads` for a compute pass (a pass is one kind or the other -- the kind changing
    #: starts a new pass -- so one unit is always zero). `volumeCalls` is how many of the pass's events carried
    #: a volume at all: zero means this bundle has none, which the ranking reports as an unavailable input
    #: rather than ranking every pass by a hard zero.
    vertices: int
    instances: int
    triangles: int
    threads: int
    volumeCalls: int
    #: The frame's *time* in this pass, when the bundle carries counters (`--with-counters`): the counter the
    #: bundle names as the cost summed over the pass's events, the fraction of the frame's total that is
    #: (`share`, against every event with a value), how many events produced one (`costRows`, so a pass the
    #: counter skipped reads as unmeasured rather than free) and the dearest single event in it
    #: (`dearestEid`, 0 when none). All four are 0 in a bundle with no counters, and `costRows` 0 is what
    #: tells that apart from a pass that cost nothing.
    cost: float
    share: float
    costRows: int
    dearestEid: int
    #: What the pass *writes*, for the kind of pass whose outputs are not render targets: the state
    #: document's `uavs` rows (`cs u0 res6979`), verbatim, from the pass's first event. Empty for a graphics
    #: pass (its outputs are `targets`/`depth`) and empty for a compute pass in a bundle whose state
    #: documents predate the array -- which the renderer says as `n/a` rather than as "writes nothing".
    writes: List[str]

#: One red flag's required keys. `what` is the *observation*; what it means is the reader's, because a
#: bundle can prove what the engine held, not what the frame intended. `certainty` is what the detector
#: could prove (`certain` -- the bundle shows it; `question` -- the observation is real, its meaning
#: depends on what the frame was for). `unproven` is the corpus's cause gate (REFERENCE §4.17): a detector that has never
#: been checked against a capture whose bugs are known has not earned a verdict, and
#: `rdc_report.apply_known` is what opens it.
#:
#: A flag does *not* carry a severity: that is a property of the detector, not of the finding, and it lives
#: in `severityTable` (one row per group, one member per detector, with the line that justifies it). The
#: report's grouping is therefore a join on `detector`, and there is one place to disagree with -- rather
#: than a value copied into every finding that a later change could leave behind.
class RedFlagBase(TypedDict):
    detector: str
    what: str
    evidence: List[str]
    certainty: str
    unproven: bool

#: A red flag, plus the two keys a **known cause** fills in -- and only those findings have them, which is
#: why they are optional here rather than empty everywhere (`total=False` on the subclass is what says so,
#: and it is 3.8-compatible where `typing.NotRequired` is not). `rdc_report.apply_known` sets both when the
#: corpus's `known` list has an entry matching this finding, and that is the one path by which `unproven`
#: is turned off: the gate is the *cause being written down*, not a detector being trusted.
#:
#: `verdict` is `confirmed` (the finding is real and the cause says why) or `not a defect` (the observation
#: is real, its cause is not a frame bug) -- the two answers following a finding to its cause produces.
class RedFlag(RedFlagBase, total=False):
    cause: str
    verdict: str

#: One detector inside a severity group: its name, and why its findings belong in that group.
class SeverityMember(TypedDict):
    detector: str
    why: str

#: One severity group of the report's findings: the label, what it means for a reader, and the detectors whose
#: findings land in it. The table is part of the document rather than of the renderer because it *is* the rule
#: the grouping follows -- a reader who disagrees with a group should be able to see who put it there.
class SeverityRow(TypedDict):
    severity: str
    means: str
    members: List[SeverityMember]

#: One input of a notable list's ranking: what it is, how it is measured from a bundle, and -- when a bundle
#: cannot answer it at all -- why not. A ranking that hides an input it never had is the shape of claim this
#: tool exists to avoid, so an unavailable input stays in the table with `available` false.
class NotableInput(TypedDict):
    input: str
    how: str
    available: bool
    why: str

#: One pass the notable list carries. `rank` is its position in the ranking (0 for a pass listed only by an
#: oddity rule, which is not a rank), `why` the rules that listed it in the rules' own words, and `values` the
#: measurements those rules read -- printed, so the order can be checked rather than taken.
class NotablePass(TypedDict):
    passIndex: int
    firstEid: int
    lastEid: int
    rank: int
    why: List[str]
    values: List[str]

#: One resource the notable list carries: the same shape, keyed by the resource instead of the pass.
class NotableResource(TypedDict):
    resource: str
    name: str
    kind: str
    detail: str
    rank: int
    why: List[str]
    values: List[str]

#: A ranked notable list: the rule (inputs, oddity rules, the cap), the rows it produced, and the roll-ups of
#: whatever the cap left out -- a cap nobody is told about reads as "this was all there was".
class Notables(TypedDict):
    limit: int
    #: How many rows the *rule*-listed half may add before the rest are rolled up: a separate number from
    #: `limit`, because the ranked list is meant to be short and the rules are meant to be complete.
    oddityLimit: int
    passInputs: List[NotableInput]
    oddities: List[str]
    passes: List[NotablePass]
    resourceInputs: List[NotableInput]
    resourceSpecials: List[str]
    resources: List[NotableResource]
    notes: List[str]

#: One recommendation: what to do (`do`), why in one line with the evidence it rests on (`why`), how to see it
#: for yourself (`command`), and where it came from (`kind`: a finding, a notable, or a gap the report could
#: not close). `eid` and `resource` are 0/'' when the recommendation is not about one event or one resource.
class Recommendation(TypedDict):
    rank: int
    kind: str
    severity: str
    do: str
    why: str
    command: str
    eid: int
    resource: str

#: The ranked recommendations and the roll-up of what the cap left out. One row per thing to look at -- per
#: detector, per oddity rule, per gap -- rather than per finding, because a list of twenty-one dead allocations
#: is not a way to decide where to start.
class Recommendations(TypedDict):
    limit: int
    rows: List[Recommendation]
    notes: List[str]

#: What a detector did. `why` is empty for one that ran and the reason for one that could not -- a report
#: that lists only findings cannot tell "clean" from "not looked at", and that difference matters.
class DetectorRun(TypedDict):
    detector: str
    ran: bool
    why: str

#: One name a concept rests on, and where it was read. `kind` is what sort of name it is -- a constant block, a
#: shader entry point, a resource, a marker, a pass structure string or a member of a block -- because "this
#: pass is a mobile base pass" and "this block is the indirect lighting cache" are claims of different shapes.
class EngineEvidence(TypedDict):
    kind: str
    name: str
    where: str

#: One concept the table's vocabulary recognises in this frame, with the evidence that claimed it. `passIndex`
#: and `firstEid` are 0 for a concept claimed for the whole frame rather than for one pass -- a real value is
#: never 0, because an event id starts at 1 -- so "which pass" is readable without an optional key.
class EngineConceptRow(TypedDict):
    concept: str
    kind: str
    evidence: List[EngineEvidence]
    note: str
    passIndex: int
    firstEid: int

#: One tagged member of one block, at one event: the "which pass, which cbuffer, which value" half of an
#: interpretation. `firstEid` is the event the document was written at, which is what makes a value checkable
#: (`replay_dump cb <capture> <eid> <stage> <slot>`); `passIndex` is the report's pass that event falls in.
#: `bound` is false when nothing was bound to the block at that event -- and then every member reads zero,
#: which is a fact about the *binding*, not about the value, so a reader has to be told which it is.
class EngineValue(TypedDict):
    concept: str
    block: str
    member: str
    value: str
    firstEid: int
    passIndex: int
    bound: bool

#: A question the table asks of a frame, answered with the concepts and values its group claimed.
class EngineQuestion(TypedDict):
    id: str
    title: str
    ask: str
    conceptRows: List[EngineConceptRow]
    values: List[EngineValue]

#: The whole interpretation. Empty `engine` means no table claimed the frame's names, and then there is nothing
#: in `concepts` or `questions` -- the report says why in `notInterpreted` rather than guessing an engine.
class EngineInterpretation(TypedDict):
    engine: str
    schema: str
    basis: str
    detected: List[EngineEvidence]
    concepts: List[EngineConceptRow]
    questions: List[EngineQuestion]
    notInterpreted: List[str]

class ReportDocument(TypedDict):
    schemaVersion: int
    reportVersion: int
    capture: str
    captureSha256: str
    bundleDir: str
    bundle: Dict[str, Any]
    frame: Dict[str, Any]
    passes: List[ReportPass]
    engine: EngineInterpretation
    notables: Notables
    recommendations: Recommendations
    severityTable: List[SeverityRow]
    flags: List[RedFlag]
    detectors: List[DetectorRun]
    caveats: List[str]
    appendix: List[str]

def _res_id(text: str) -> str:
    """The id inside a resource reference as the driver prints it.

    The engine's own documents are not uniform about this: `events.json` writes bare numbers
    (`"2207"`), while `state`/`shaders` write `res2207`. Both are read here, so the report parses what
    the driver actually writes rather than what one of its commands happens to look like.
    """
    token = text.strip()
    return token[3:] if token.startswith('res') else token

def _is_resource(idtext: str) -> bool:
    """Whether an id names something: `0` is the engine's null resource, not a resource called zero."""
    return _res_id(idtext) not in ('', '0')

def _targets_text(entry: ReportPass, short: bool = False) -> str:
    """The targets a pass writes, or why that question does not apply to it.

    At a compute event the engine still reports the output-merge state, but a dispatch did not set it:
    printing it as this pass's targets would be a claim about the frame that the event does not
    support, so it says so instead. (The reported values stay in the JSON document.)

    What a dispatch *does* write is its bound UAVs, which the state document carries (`writes`): where they
    are there they are the answer, and where they are not -- a bundle whose state documents predate that
    array -- the old sentence stands, because "no UAVs recorded" and "writes nothing" are different claims.
    """
    if entry['kind'] == 'compute':
        if entry['writes']:
            # `cs u0 res6979` -> `res6979 (cs u0)`: the resource first, because that is what a reader
            # compares against the rest of the document, with the binding after it for a second look. The id
            # keeps the `res` its row carries -- `_res_id` strips it, and a target spelled differently from
            # every other id in the document is a target a reader has to translate.
            return ', '.join('%s (%s u%s)' % (row.split()[-1], row.split()[0], row.split()[1][1:])
                             for row in entry['writes'])
        # The map column is narrow and the reason is the same for every dispatch: `n/a` there, the
        # sentence in the pass section and in the caveats.
        return 'n/a' if short else 'n/a — a dispatch does not set the output merger'
    if short:
        return ', '.join('res%s' % _res_id(t.split()[0]) for t in entry['targets']) or 'none'
    return ', '.join(entry['targets']) or 'none'

def _md(text: str) -> str:
    """One line of Markdown-safe text: a table cell or a heading fragment cannot contain a pipe or a
    newline, and a resource name comes from the application."""
    return ' '.join(str(text).split()).replace('|', '\\|')

def _bundle_file(bundle_dir: str, name: str, required: bool = True) -> Any:
    path = os.path.join(bundle_dir, name)
    try:
        with open(path, encoding='utf-8-sig') as fh:
            return json.load(fh)
    except FileNotFoundError:
        if required:
            raise BundleError(
                '%s is missing: write the bundle with `replay_dump dump <rdc> %s`' % (name, bundle_dir))
        return None
    except json.JSONDecodeError as exc:
        raise BundleError('%s is not valid JSON (%s): the bundle is damaged, write it again'
                          % (name, exc)) from exc

@rdc_profile.timed('report: bundle read')
def load_bundle(bundle_dir: str) -> BundleData:
    """Read a bundle: the files `replay_dump dump` writes (REFERENCE §9).

    `manifest.json`, `capture.json`, `events.json` and `resources.json` are required, because a report
    without them would have to invent something; `messages.json` and the per-event `states/` documents
    are optional and are reported as absent. A bundle written by a newer driver is refused by version
    rather than half-read.
    """
    if not os.path.isdir(bundle_dir):
        raise BundleError('no such bundle directory: %s' % bundle_dir)

    manifest: Dict[str, Any] = _bundle_file(bundle_dir, 'manifest.json')
    version = manifest.get('bundleVersion')
    if version != BUNDLE_VERSION:
        raise BundleError('bundle version %s, this tool reads %d (write the bundle again with the '
                          'current replay_dump)' % (version, BUNDLE_VERSION))

    capture: Dict[str, Any] = _bundle_file(bundle_dir, 'capture.json')
    events: List[BundleEvent] = _bundle_file(bundle_dir, 'events.json').get('events', [])
    resources: List[BundleResource] = _bundle_file(bundle_dir, 'resources.json').get('resources', [])
    messages: List[Union[BundleMessage, str]] = (_bundle_file(bundle_dir, 'messages.json', False) or {}).get(
        'messages', [])
    # Optional like `messages.json`: a bundle written without `--with-counters` has no counter results, and
    # that absence is reported (the notable ranking names the input as unavailable) rather than assumed.
    counter_doc: Dict[str, Any] = _bundle_file(bundle_dir, 'counters.json', False) or {}
    counters: List[BundleCounter] = counter_doc.get('counters', [])
    # The engine's own fold, written by the same dump from the same fetch: `{}` when there are no counters
    # at all, which is a different answer from a fold that ran and found no pass to fold over (that one has
    # `available` 0 and a `note` saying so).
    counter_passes: Dict[str, Any] = _bundle_file(bundle_dir, 'counters-passes.json', False) or {}

    # The per-event documents that exist, keyed by eid: the pass roll-up names the shaders and the
    # constant blocks of the pass's first event, which is exactly the event the bundle writes a state
    # file for (its state hash includes the render targets, so a pass boundary is always a change).
    states: Dict[str, Dict[str, Any]] = {}
    states_dir = os.path.join(bundle_dir, 'states')
    if os.path.isdir(states_dir):
        for event in events:
            eid = int(event['eid'])
            state = _bundle_file(bundle_dir, os.path.join('states', '%d.state.json' % eid), False)
            shaders = _bundle_file(bundle_dir, os.path.join('states', '%d.shaders.json' % eid), False)
            states[str(eid)] = {'state': state, 'shaders': shaders}

    # The constant-block dumps, keyed by their file name. They are core, not optional: a bundle without
    # `--with-images` still has them, and the all-zero detector is the one that needs them.
    cbuffers: Dict[str, Dict[str, Any]] = {}
    cbuffer_dir = os.path.join(bundle_dir, 'cbuffers')
    if os.path.isdir(cbuffer_dir):
        for name in sorted(os.listdir(cbuffer_dir)):
            if name.endswith('.json'):
                document = _bundle_file(bundle_dir, os.path.join('cbuffers', name), False)
                if isinstance(document, dict):
                    cbuffers[name] = document

    # Built as a `BundleData` rather than as a dict literal: the members have different types, and a literal
    # widens to a union of them, which is no longer the type this function promises to return.
    return BundleData(manifest=manifest, capture=capture, events=events, resources=resources,
                      messages=messages, counters=counters,
                      costCounter=int(counter_doc.get('costCounter', 0)),
                      costCounterName=str(counter_doc.get('costCounterName', '')),
                      counterUnit=str(counter_doc.get('unit', '')),
                      counterPasses=counter_passes, states=states, cbuffers=cbuffers)

def compact_count(count: int) -> str:
    """A count as a reader compares them at a glance: `1204` -> `1.2 K`, `1234567` -> `1.2 M`.

    The report's other measurements are already scaled this way (`%.1f Mpixel of targets`), and this one is the
    largest number in the document by orders of magnitude: a frame's triangle count runs to millions.
    """
    for limit, suffix in ((1000000000, 'G'), (1000000, 'M'), (1000, 'K')):
        if count >= limit:
            return '%.1f %s' % (count / float(limit), suffix)
    return '%d' % count

def work_text(entry: ReportPass) -> str:
    """What a pass's calls asked for, in the unit they asked in -- or an empty string when this bundle carries
    no volume at all (a driver from before 2026-09-22, or a frame with no calls in it).

    Empty rather than a line of zeros on purpose: the notable-inputs table already says the input is
    unavailable, and `0 triangle(s)` on a ranking row reads as a measurement.

    One unit, not both: a pass is a graphics run or a compute run (`reconstruct_passes` starts a new pass when
    the call kind changes), so a pass with triangles has no threads and the other way round.
    """
    if not int(entry.get('volumeCalls', 0) or 0):
        return ''
    if entry['kind'] == 'compute':
        return '%s thread(s) over %d call(s)' % (compact_count(int(entry['threads'])), entry['volumeCalls'])
    return '%s triangle(s) over %d call(s)' % (compact_count(int(entry['triangles'])), entry['volumeCalls'])

__all__ = [
    'BUNDLE_VERSION',
    'BundleCounter',
    'BundleData',
    'BundleError',
    'BundleEvent',
    'BundleMessage',
    'BundleResource',
    'BundleUsage',
    'DetectorRun',
    'EngineConceptRow',
    'EngineEvidence',
    'EngineInterpretation',
    'EngineQuestion',
    'EngineValue',
    'NotableInput',
    'NotablePass',
    'NotableResource',
    'Notables',
    'REPORT_SCHEMA_VERSION',
    'REPORT_VERSION',
    'Recommendation',
    'Recommendations',
    'RedFlag',
    'ReportDocument',
    'ReportPass',
    'SeverityMember',
    'SeverityRow',
    '_bundle_file',
    '_is_resource',
    '_md',
    '_res_id',
    '_targets_text',
    'compact_count',
    'load_bundle',
    'work_text',
]
