"""`rootsig-check`: the root signature the *file* declares against the bindings the *stream* shows.

A root signature and the bindings a frame makes are two records of one fact, written by two different
parts of the engine: `Device_CreateRootSignature` carries the layout (parameter types, registers, ranges
and their counts) and the `List_Set*Root*` payloads carry what each draw actually bound. This command
reads both out of the file and reports where they disagree -- the same idea the project uses instead of
trust everywhere else: two independent paths to one fact, compared.

It also takes an optional **bundle** (`replay_dump dump`, REFERENCE §9) and then compares the file's
answer with the *engine's* answer for the same frame: the signature id the engine reports, the parameter
rows it prints (class, register, space, visibility) and its resolved table slots (`cat(N) type(N)`),
against those same facts read out of the chunk stream. That is the strongest form of the check, because
the two sides then share nothing but the capture. What it still cannot do is align an engine *event id*
with a file *chunk index* -- a mapping no file read produces (`Upstream`, ROADMAP §3) -- so a slot is
compared as *"the engine saw a kind the file never wrote there"*, never slot-for-slot at one event.

Findings carry the `certain` / `question` split the report's detectors use: `certain` is something the
file proves (a slot the capture never wrote, an index the signature does not declare), `question` is
something a reader has to weigh (a resource created before the capture, a parameter the frame does not
use). Exit code: 1 when a `certain` finding exists, 0 otherwise -- the `verify` convention, so this can
gate a script.
"""
from __future__ import annotations

from rdc_types import *  # noqa: F401,F403
from rdc_chunkmap import *  # noqa: F401,F403
from rdc_stream import *  # noqa: F401,F403
from rdc_cache import *  # noqa: F401,F403
from rdc_payloads import *  # noqa: F401,F403
from rdc_resources import *  # noqa: F401,F403
from rdc_detect_binding import _table_bindings as _engine_rows
from rdc_bundle import BundleError as BundleError, load_bundle as load_bundle
import rdc_cache  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_chunkmap  # noqa: F401  (used qualified: the loader is called from inside functions)
import rdc_profile
import rdc_table  # noqa: F401  (the csv/markdown shapes)

from typing import Dict, List, NamedTuple, Optional, Set, Tuple

#: How many findings of one group are printed before the rest are counted. Every check's *count* is
#: always complete: a truncated list is a shorter answer, a truncated count would be a wrong one.
FINDING_LIMIT = 20

#: The register letter a range kind serves, which is how a slot fact and an engine slot row are matched
#: to the same range without an event-to-chunk mapping.
LETTER_FOR_RANGE: Dict[str, str] = {'cbv': 'b', 'srv': 't', 'uav': 'u', 'sampler': 's'}


class SigFinding(NamedTuple):
    """One disagreement or unanswered question, in the shape both the terminal and `--format` print."""

    check: str
    certainty: str        # 'certain' | 'question'
    what: str
    evidence: str


class SlotFact(NamedTuple):
    """One descriptor-table slot a binding resolved to, as the *file* sees it.

    `declared` is the range's own kind (what the signature says the slot must hold), `kind` is what the
    capture actually wrote there (`''` when it never wrote that slot at all), and `letter`/`reg`/`space`
    are where the range maps the slot -- the same triple the engine prints on its slot rows, which is
    what makes the two sides comparable without an event-to-chunk mapping. `heap` is carried so the
    coverage accounting can say *which* heap a binding resolved into.
    """

    heap: int
    letter: str
    reg: int
    space: int
    slot: int
    declared: str
    kind: str
    resource: int


class ParamUse(NamedTuple):
    """What one `(signature, parameter)` pair was bound to over the whole frame.

    `kind` is the parameter's own class from the file's decode (`descriptor` for a root CBV/SRV/UAV,
    `table`, `32bit`, or `undeclared` for an index the signature does not have), `sets` how many calls
    set it, `calls` up to three call names that did (evidence), `slots` every descriptor slot a table
    binding resolved to, and `bases` the `(heap, base index)` of every table binding. Several sets of
    one parameter are *one* record: the file cannot say which set an engine event saw, so the bundle
    half compares the frame's whole answer with the engine's -- which is exactly why `bases` is a list
    of facts and not a single value.
    """

    kind: str
    sets: int
    calls: List[str]
    slots: List[SlotFact]
    resources: List[int]
    bases: List[Tuple[int, int]]


class SigCheck(NamedTuple):
    """The file's whole answer for one capture: the signatures, what was set, and what could not be read."""

    signatures: Dict[int, RootSignature]
    uses: Dict[Tuple[int, int], ParamUse]            # (signature id, parameter) -> its use
    resource_ids: Set[int]                           # every id the capture's resource table has
    heap_writes: Dict[int, int]                      # heap id -> slots this capture wrote in it
    unbounded: int                                   # bindings covering a range with no count
    unset: Dict[int, List[int]]                      # signature -> declared parameters no call set
    no_signature: int                                # bindings set with no signature on that list
    unknown_sig: Dict[int, int]                      # signature id -> calls that bound it, undecoded


def _table_slots(param: RootParam, heap: int, base: int,
                 heaps: Dict[int, Dict[int, DescriptorInfo]]) -> List[Optional[SlotFact]]:
    """Every slot a table's ranges cover, or None for a range whose count is unbounded.

    A range says which registers it serves (`base`..`base + count - 1`) and where its slots start in the
    table, so a slot's position in the heap is `binding base + range offset + k` -- and what comes back
    is what the capture *wrote* there, not what the signature hopes for.

    The offset is `D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND` (`0xffffffff`) in every signature this project
    has seen, and it is not a number: it means "after the previous range", so the ranges are walked *in
    declaration order* and each append takes the running end. Treating the sentinel as an offset is how
    a first attempt reported four billion as a slot index. An unbounded range (`count == 0xffffffff`)
    has no end to walk, so it comes back as None and is counted as not checked rather than guessed at.
    """
    out: List[Optional[SlotFact]] = []
    running = 0
    for r in param['ranges']:
        offset = running if r['offset'] == 0xffffffff else r['offset']
        running = offset + (0 if r['count'] == 0xffffffff else r['count'])
        if r['count'] == 0xffffffff:
            out.append(None)
            continue
        for k in range(r['count']):
            index = base + offset + k
            entry = heaps.get(heap, {}).get(index)
            out.append(SlotFact(heap=heap, letter=LETTER_FOR_RANGE.get(r['kind'], '?'),
                                reg=r['base'] + k, space=r['space'], slot=index,
                                declared=r['kind'],
                                kind=entry['kind'] if entry is not None else '',
                                resource=entry['resource'] if entry is not None else 0))
    return out


@rdc_profile.timed('rootsig check')
def file_check(path: str) -> Optional[SigCheck]:
    """Walk one capture's stream and record every root binding against the signature it names.

    None when no chunk can be named at all (`README.md` §1.1): then "the signature declares no such
    parameter" would be a statement about the tool rather than about the capture -- the answer
    `rdc_passdiff.marker_passes` and `rdc_filediff.call_records` give for the same reason.

    The walk mirrors `draws`: state chunks are applied per command list, and at each draw the *graphics*
    or *compute* namespace is read for its current signature. What it adds is the resolution of every
    descriptor-table binding into the slots its ranges cover, which is the only way to ask "is what the
    signature declares what the heap holds".
    """
    _info, stream, _how = rdc_cache.load_stream(path)
    names = rdc_chunkmap.load_chunk_names()
    if not names:
        return None
    resources = parse_resource_table(stream, names)
    sigs = parse_root_signatures(stream, names)
    # The heaps are built *here*, chunk by chunk, rather than by `parse_descriptor_heaps`: a table
    # binding has to resolve against what the slot held when the binding happened, and that function
    # returns the state at the end of the frame. UE re-uses descriptor memory -- one slot can be an SRV
    # early and a UAV later -- so the final-state answer reports every such re-use as a mismatch.
    heaps: Dict[int, Dict[int, DescriptorInfo]] = {}

    uses: Dict[Tuple[int, int], ParamUse] = {}
    counter = {'unbounded': 0, 'no_signature': 0}
    unknown_sig: Dict[int, int] = {}
    states: Dict[int, DrawState] = {}

    def add(signature: Optional[int], rp: int, call: str, pair: Tuple[int, int],
            is_table: bool) -> None:
        """Record one set of `(signature, rp)`, resolving a table into the slots it covers.

        `pair` is the two `u64`/`u32` values at +12 of the payload, which is where a root descriptor
        carries `(resource, offset)` and a descriptor table `(heap, base index)` -- the same place, so
        one call records either.
        """
        if signature is None:
            counter['no_signature'] += 1
            return
        sig = sigs.get(signature)
        if sig is None:
            unknown_sig[signature] = unknown_sig.get(signature, 0) + 1
            return
        previous = uses.get((signature, rp))
        calls = list(previous.calls) if previous is not None else []
        if call not in calls and len(calls) < 3:
            calls.append(call)
        if rp >= len(sig['params']):
            # The index itself is the finding; the record exists so the finding can name who set it.
            uses[(signature, rp)] = ParamUse(kind='undeclared',
                                             sets=(previous.sets if previous is not None else 0) + 1,
                                             calls=calls, slots=[], resources=[],
                                             bases=[])
            return
        param = sig['params'][rp]
        slots = list(previous.slots) if previous is not None else []
        named = list(previous.resources) if previous is not None else []
        bases = list(previous.bases) if previous is not None else []
        if is_table:
            if pair not in bases:
                bases.append(pair)
            for fact in _table_slots(param, pair[0], pair[1], heaps):
                if fact is None:
                    counter['unbounded'] += 1
                    continue
                slots.append(fact)
                if fact.resource:
                    named.append(fact.resource)
        else:
            named.append(pair[0])
        uses[(signature, rp)] = ParamUse(kind=param['kind'], sets=(previous.sets if previous
                                                                  is not None else 0) + 1,
                                         calls=calls, slots=slots, resources=named, bases=bases)

    for chunk in iter_chunks(stream):
        name = names.get(chunk['id'], '')
        readable = (name in DRAW_CHUNKS or name in STATE_CHUNKS or name in DESCRIPTOR_KINDS
                    or name in DESCRIPTOR_COPY_CHUNKS)
        blob = chunk_payload(stream, chunk) if readable else b''
        if name in DESCRIPTOR_KINDS or name in DESCRIPTOR_COPY_CHUNKS:
            apply_descriptor_chunk(name, blob, heaps)
        elif name in DRAW_CHUNKS:
            state = states.get(u64(blob, 0) if len(blob) >= 8 else 0)
            if state is None:
                continue
            compute = name in COMPUTE_CHUNKS
            signature = state['compSig'] if compute else state['gfxSig']
            call = name.replace('List_', '')
            for rp, (heap, index) in sorted((state['compTable'] if compute
                                             else state['gfxTable']).items()):
                add(signature, rp, call, (heap, index), True)
            for roots in ((state['compCbv'], state['compSrv'], state['compUav']) if compute
                          else (state['gfxCbv'], state['gfxSrv'], state['gfxUav'])):
                for rp, pair in sorted(roots.items()):
                    add(signature, rp, call, pair, False)
        elif _apply_state_chunk(name, blob, states):
            pass

    # A declared parameter that no call in this frame sets is a question, not a defect: a shader the
    # frame never runs, a parameter the pipeline does not read, or a signature bound before the frame.
    unset: Dict[int, List[int]] = {}
    for signature in sorted({sig for sig, _rp in uses}):
        sig = sigs.get(signature)
        if sig is None:
            continue
        missing = [rp for rp in range(len(sig['params'])) if (signature, rp) not in uses]
        if missing:
            unset[signature] = missing
    return SigCheck(signatures=sigs, uses=uses, resource_ids=set(resources),
                    heap_writes={heap: len(slots) for heap, slots in heaps.items()},
                    unbounded=counter['unbounded'], unset=unset,
                    no_signature=counter['no_signature'], unknown_sig=unknown_sig)


def _resource_id(text: str) -> Optional[int]:
    """The integer in an `IdText`, or None when there is none to read.

    The driver's `IdText` is the *number* and its callers add the `res` prefix themselves (`Fmt("res%s",
    IdText(...))` in `commands_state.cpp`), so the state document spells the same id two ways: `res2233`
    on every binding row and a bare `2233` in its own `rootSignature` field, which passes `IdText`
    straight through. Both are read here, because the field is the one this command needs.

    A null binding is `0` -- and the ids a capture hands out start at 1 -- so zero is "no signature"
    rather than a signature numbered zero, which is also what the driver's rows mean by an empty
    parameter detail.
    """
    number = text[3:] if text.startswith('res') else text
    if not number.isdigit():
        return None
    value = int(number)
    return value or None


def _engine_table(row: str) -> Tuple[Optional[int], int]:
    """`(heap id, descriptor index)` of an engine table row, or `(None, 0)`.

    The row's detail is `heapH+0xO`, where the offset is D3D12's own `OffsetInDescriptorsFromTableStart`
    in *descriptors* -- the field is spelled `heapByteOffset` and is not bytes (`commands_state.cpp`
    says so where the slot rows are built) -- so it is directly comparable with the index a
    `List_SetGraphicsRootDescriptorTable` payload carries.
    """
    detail = row.split()[-1] if row.split() else ''
    if not detail.startswith('heap') or '+' not in detail:
        return None, 0
    heap_text, _plus, offset_text = detail[4:].partition('+')
    if not heap_text.isdigit() or not offset_text.startswith('0x'):
        return None, 0
    return int(heap_text), int(offset_text, 16)


def _engine_class(row: str) -> str:
    """Which class an engine parameter row describes: `table`, `32bit`, `descriptor` or `unset`.

    The row is `rpN reg=R space=S vis=V <detail>` (`commands_state.cpp`), and the detail is the only
    place the class shows: `heapH+0xO` is a descriptor table, `N words` are root constants, `resR` is a
    root descriptor, and nothing at all is a parameter the event had not set. The engine's own
    `DescriptorType` is not on this row, so a root CBV, SRV and UAV cannot be told apart here -- which is
    why the file's kind is compared against the *class* and the register, never a guessed kind word.
    """
    fields = row.split()
    detail = fields[-1] if fields else ''
    if detail.startswith('heap'):
        return 'table'
    if detail.endswith('words'):
        return '32bit'
    if detail.startswith('res'):
        return 'descriptor'
    return 'unset'


def engine_signatures(bundle_dir: str
                      ) -> Tuple[Dict[int, Dict[int, Tuple[str, int, int, str]]], List[str]]:
    """What the engine says about the frame's signatures, per bundle (REFERENCE §9).

    Returns `(parameters, notes)`: `parameters[signature][rp]` is `(visibility, register, space, row)`
    from the engine's own parameter rows. Several state documents may name the same signature, and the
    rows are merged: every one of them is a statement about the *signature*, which does not change
    between events, so the merge loses nothing.

    The rows are parsed by `rdc_detect_binding._table_bindings`, which is the project's one parser of
    the driver's two row shapes -- a second one would be a second thing to keep in step.
    """
    bundle = load_bundle(bundle_dir)
    parameters: Dict[int, Dict[int, Tuple[str, int, int, str]]] = {}
    events = 0
    for eid in sorted(bundle['states'], key=lambda text: int(text)):
        state = bundle['states'][eid].get('state')
        if not isinstance(state, dict) or 'rootParameters' not in state:
            continue
        signature = _resource_id(str(state.get('rootSignature', '')))
        if signature is None:
            continue
        events += 1
        rows, _resolved = _engine_rows(state)
        parameters.setdefault(signature, {}).update(rows)
    notes = ['bundle    : %d state document(s) with root parameters, %d signature id(s) reported'
             % (events, len(parameters))]
    return parameters, notes


def bundle_findings(check: SigCheck, bundle_dir: str) -> Tuple[List[SigFinding], List[str]]:
    """Compare the file's decode with the engine's own answer for the same frame.

    Four comparisons, each between facts the two sides produce independently: the signature ids the
    engine reports against the ids the file decoded; the parameter class, register, space and
    visibility on the engine's rows against the decoded signature; and, for every slot the engine
    resolved, the `DescriptorType` it names against the descriptor kind the capture wrote there.
    """
    parameters, notes = engine_signatures(bundle_dir)
    findings: List[SigFinding] = []
    param_rows = sum(len(rows) for rows in parameters.values())

    def compare(signature: int, rp: int, engine: Tuple[str, int, int, str]) -> None:
        """One parameter row against the file's decode of that signature."""
        sig = check.signatures.get(signature)
        if sig is None or rp >= len(sig['params']):
            return
        visibility, reg, space, row = engine
        param = sig['params'][rp]
        ours = ('table' if param['kind'] == 'table' else
                '32bit' if param['kind'] == '32bit' else 'descriptor')
        if _engine_class(row) == 'unset' or reg < 0:
            return              # the event had not set it: the file's declaration is not contradicted
        if (_engine_class(row) != ours or reg != param['register'] or space != param['space']
                or (visibility != 'all' and visibility != param['visibility'])):
            findings.append(SigFinding(
                'parameter-shape', 'certain',
                'signature res%d rp%d is %s %s (register %d, space %d) in the file and `%s` on the '
                'engine\'s row' % (signature, rp, param['kind'], param['visibility'],
                                   param['register'], param['space'], row),
                'the signature bytes on one side and the engine\'s own root signature object on the '
                'other disagree about one parameter'))

    for signature in sorted(parameters):
        rows = parameters[signature]
        sig = check.signatures.get(signature)
        if sig is None:
            findings.append(SigFinding(
                'signature-not-in-file', 'question',
                'the engine reports root signature res%d as bound, and the file creates no such '
                'signature' % signature,
                'a signature created before the capture started, or a creation chunk this reader does '
                'not decode'))
            continue
        if len(rows) != len(sig['params']):
            findings.append(SigFinding(
                'parameter-count', 'certain',
                'signature res%d has %d parameter(s) in the file and %d on the engine\'s rows'
                % (signature, len(sig['params']), len(rows)),
                '; '.join(rows[rp][3] for rp in sorted(rows)[:4])))
        for rp in sorted(rows):
            compare(signature, rp, rows[rp])

    # What the engine resolved *inside* a table (its `cat(N) type(N)` rows) is deliberately not
    # compared slot by slot: the engine's rows are per event and the file's writes cannot be aligned to
    # an event -- no event-to-chunk mapping exists (`Upstream`, ROADMAP §3) -- so the two sets cover
    # different moments of the frame and a difference between them would not be a disagreement. What
    # *can* be compared exactly is the binding itself: every `(heap, offset)` the engine reports must be
    # one the file recorded for that parameter, because both are the same frame's own payloads.
    bindings = 0
    for signature in sorted(parameters):
        for rp in sorted(parameters[signature]):
            known = check.uses.get((signature, rp))
            row = parameters[signature][rp][3]
            if _engine_class(row) != 'table' or known is None:
                continue
            heap, index = _engine_table(row)
            if heap is None:
                continue
            bindings += 1
            if (heap, index) not in known.bases:
                findings.append(SigFinding(
                    'table-binding', 'certain',
                    'res%d rp%d is bound to heap%d[%d] on the engine\'s row, and the file records no '
                    'such binding for that parameter (it records %s)'
                    % (signature, rp, heap, index,
                       ', '.join('heap%d[%d]' % base for base in known.bases[:4]) or 'none'),
                    'the engine resolved the table through its own pipe state; the file reads the '
                    'binding payloads of the same frame'))
    notes.append('compare   : %d signature(s), %d parameter row(s) and %d table binding(s) compared, '
                 '%d disagreement(s)' % (len(parameters), param_rows, bindings, len(findings)))
    return findings, notes


def file_findings(check: SigCheck) -> Tuple[List[SigFinding], List[str]]:
    """Every disagreement and question the *file* alone answers, and the coverage notes behind them.

    A slot a binding covers that the capture never wrote is **not** a finding by itself: UE writes its
    descriptor heaps once at startup and the frame only *references* those slots, so a heap with no
    write in this stream is the normal case, and saying "unwritten" about every slot of it would be
    3,520 rows of noise about the frame's first event (`deps` says the same thing as its `unresolved`
    line). What *is* worth a question is a heap the frame writes *and* binds a slot in that it never
    wrote -- one of the two is then unexplained -- and the coverage figures say which heaps those are.
    """
    findings: List[SigFinding] = []
    coverage: Dict[int, List[int]] = {}          # heap -> [slots bound, of those never written]
    # Mismatches are aggregated per *class* -- one parameter, one register letter and space, one
    # declared kind against one written kind -- because a UE range covers 64 slots and a per-slot row
    # would be 64 rows of the same sentence. The count and the first few slots are the evidence.
    mismatches: Dict[Tuple[int, int, str, int, str, str], List[Tuple[int, int]]] = {}
    unknown: Dict[Tuple[int, int, int], int] = {}
    for (signature, rp), use in sorted(check.uses.items()):
        sig = check.signatures.get(signature)
        if use.kind == 'undeclared':
            declared = len(sig['params']) if sig is not None else 0
            findings.append(SigFinding(
                'undeclared-parameter', 'certain',
                'the stream sets root parameter rp%d of signature res%d, which declares %d parameter(s)'
                % (rp, signature, declared),
                'set by %s -- an index the signature does not have is a binding the pipeline cannot '
                'receive' % ', '.join(use.calls)))
            continue
        for fact in use.slots:
            counted = coverage.setdefault(fact.heap, [0, 0])
            counted[0] += 1
            if not fact.kind:
                counted[1] += 1
            elif fact.kind != fact.declared:
                key = (signature, rp, fact.letter, fact.space, fact.declared, fact.kind)
                mismatches.setdefault(key, []).append((fact.reg, fact.slot))
        for resource in sorted(set(use.resources)):
            if resource and resource not in check.resource_ids:
                unknown[(signature, rp, resource)] = unknown.get((signature, rp, resource), 0) + 1
    for (signature, rp, letter, space, declared, written), facts in sorted(mismatches.items()):
        findings.append(SigFinding(
            'range-kind-mismatch', 'certain',
            'res%d rp%d: %d slot(s) of its %s range in space %d (%s%d..%s%d) hold a %s descriptor while '
            'the range declares %s'
            % (signature, rp, len(facts), declared, space, letter, min(reg for reg, _i in facts),
               letter, max(reg for reg, _i in facts), written, declared),
            'first slot(s) %s, resolved against the heap as it stood when the table was bound'
            % ', '.join('heap[%d]=%s%d' % (index, letter, reg) for reg, index in facts[:4])))
    for (signature, rp, resource), _count in sorted(unknown.items()):
        findings.append(SigFinding(
            'unknown-resource', 'question',
            'res%d rp%d names resource res%d, which this capture never creates'
            % (signature, rp, resource),
            'the resource table has no descriptor for it: created before the capture, or only named'))
    external: List[str] = []
    partial: List[str] = []
    for heap in sorted(coverage):
        bound, unwritten = coverage[heap]
        writes = check.heap_writes.get(heap, 0)
        if not unwritten:
            continue
        if not writes:
            external.append('heap%d (%d slot(s) bound, none written)' % (heap, bound))
        else:
            partial.append('heap%d (%d of %d bound slot(s) never written, %d written in this frame)'
                           % (heap, unwritten, bound, writes))
    for entry in partial:
        findings.append(SigFinding(
            'partial-heap', 'question',
            'this frame writes descriptors into a heap it also binds slots in that it never wrote: %s'
            % entry,
            'written before the frame, by another command list, or by a write chunk this reader does '
            'not decode'))
    for signature, missing in sorted(check.unset.items()):
        findings.append(SigFinding(
            'never-set-parameter', 'question',
            'signature res%d declares %d parameter(s) that no call in this frame sets (%s)'
            % (signature, len(missing), ', '.join('rp%d' % rp for rp in missing[:8])),
            'a shader this frame does not run, a parameter the pipeline does not read, or a binding '
            'made in a command list this reader never sees'))
    if check.no_signature:
        findings.append(SigFinding(
            'no-signature', 'question',
            '%d root binding(s) are set on a command list whose signature this stream does not have'
            % check.no_signature,
            'without a signature the indexes have no declaration to be checked against'))
    for signature, count in sorted(check.unknown_sig.items()):
        findings.append(SigFinding(
            'unknown-signature', 'question',
            '%d call(s) bind signature res%d, which this capture never creates' % (count, signature),
            'a signature created before the capture starts'))
    notes: List[str] = []
    if external:
        notes.append('heaps this frame binds and never writes (populated outside it, which is the '
                     'normal case for a renderer that fills its descriptors at startup): %s'
                     % ', '.join(external))
    bound_total = sum(counted[0] for counted in coverage.values())
    written_total = bound_total - sum(counted[1] for counted in coverage.values())
    if bound_total:
        notes.append('%d descriptor slot(s) bound in this frame, %d of them written by it'
                     % (bound_total, written_total))
    return findings, notes


def cmd_rootsig_check(path: str, bundle_dir: Optional[str] = None, fmt: str = 'table') -> int:
    """`rootsig-check <rdc> [bundleDir]` -- the signature's declaration against the stream's bindings.

    Without a bundle this is the file's own cross-check: every root parameter a draw sets is looked up
    in the signature it was bound with, every table binding is resolved into the slots its ranges cover,
    and each of those slots is looked up in the heaps the capture wrote. With one it also compares the
    file's answer with the engine's, which is where a disagreement means two readers of one capture do
    not agree -- the strongest signal this tool produces, and the reason the bundle half exists.

    Returns 1 when a `certain` finding exists, 0 otherwise, so it can gate a script like `verify`.
    """
    try:
        check = file_check(path)
    except FrameError as exc:
        print('error: %s' % exc)
        return 1
    if check is None:
        print('error: no chunk-name map: the RenderDoc source tree was not found (README §1.1), so no '
              'chunk can be named and no signature can be read')
        return 1

    findings, notes = file_findings(check)
    notes.append("chunk indices are the file's numbering, not the engine's event ids (REFERENCE §9)")
    if check.unbounded:
        notes.append('%d binding(s) cover a range with an unbounded count: its slots cannot be walked '
                     'and were not checked' % check.unbounded)
    if bundle_dir:
        try:
            extra, bundle_notes = bundle_findings(check, bundle_dir)
        except BundleError as exc:
            print('error: %s' % exc)
            return 1
        findings += extra
        notes = bundle_notes + notes
    certain = [finding for finding in findings if finding.certainty == 'certain']
    questions = [finding for finding in findings if finding.certainty != 'certain']
    rows = [(finding.certainty, finding.check, finding.what, finding.evidence)
            for finding in findings]
    summary = [
        'signatures: %d created here, %d bound by a call in this frame'
        % (len(check.signatures), len({sig for sig, _rp in check.uses})),
        'parameters: %d set, %d declared and never set'
        % (sum(use.sets for use in check.uses.values()),
           sum(len(missing) for missing in check.unset.values())),
        'findings  : %d certain, %d question' % (len(certain), len(questions)),
    ]
    if fmt != 'table':
        rdc_table.emit(fmt, ('certainty', 'check', 'what', 'evidence'), rows, summary + notes)
        return 1 if certain else 0
    for line in summary:
        print(line)
    for group, label in ((certain, 'certain'), (questions, 'question')):
        if not group:
            continue
        print('--- %s (%d) ---' % (label, len(group)))
        for finding in group[:FINDING_LIMIT]:
            print('%-22s %s' % (finding.check, finding.what))
            print('%22s %s' % ('', finding.evidence))
        if len(group) > FINDING_LIMIT:
            print('  ... %d more (--format csv or markdown prints every row)'
                  % (len(group) - FINDING_LIMIT))
    for note in notes:
        print('note      : %s' % note)
    return 1 if certain else 0


__all__ = [
    'FINDING_LIMIT',
    'LETTER_FOR_RANGE',
    'ParamUse',
    'SigCheck',
    'SigFinding',
    'SlotFact',
    '_engine_class',
    '_engine_table',
    '_resource_id',
    '_table_slots',
    'bundle_findings',
    'cmd_rootsig_check',
    'engine_signatures',
    'file_check',
    'file_findings',
]
