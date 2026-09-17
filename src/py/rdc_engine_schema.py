"""The engine schema table: a known engine's names, and what a frame's names mean in its vocabulary.

A name like `MobileBasePass` is Unreal's, and a report can only speak that vocabulary if the mapping from name
to concept is written down somewhere a reader can check, extend or disagree with -- that is
`engine-schemas/*.json` at the repository root (not the driver's `schema/`, which is the contract for the
driver's documents, REFERENCE §4.12). The report renders what this module computes (REFERENCE §4.11).

Three rules, and they are the whole design:

- **Name-based, and nothing else.** A concept is claimed because a *name the engine itself wrote into the
  capture* matches an entry in the table -- a constant-block name, a shader entry point, a resource name, a
  marker name, or a pass structure string. Nothing is inferred from values, from timing, or from a name that
  merely looks similar.
- **Every claim carries its evidence.** Each row names the bundle document it was read from and the exact
  string that matched, so a reader can open the file and see it.
- **No match, no claim.** An engine with no table (or a bundle whose names do not match) gets no interpretation
  at all: the report says so rather than guessing, and the concepts it could not check (today: marker names,
  which no bundle carries yet) are listed as not interpreted.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, TypedDict

from rdc_bundle import *  # noqa: F401,F403  (the shapes, the helpers, the report document)

from typing import Iterable

#: The folder the tables live in, looked for beside this module and in every folder above it (the same walk
#: `renderdoc-src` uses, so the repository root is found from `src/py/`).
SCHEMA_DIR_NAME = 'engine-schemas'

#: A table with fewer than this many matches is not an identification. One shared name proves nothing -- `View`
#: and `MainVS` are not Unreal's alone -- and the whole point of the table is that a claim is worth making only
#: when the evidence is.
MIN_DETECT_MATCHES = 2

#: Where a bundle's names come from, by kind. A marker is listed by its own name -- every element of a
#: `A > B > C` path counts -- because that is how a table names one (`BasePass`, not
#: `Scene > BasePass > ...`), and the engine's paths carry dynamic text (`CullLights 22x14x8 NumLights 0`)
#: that no table could list. `markers` is empty for a bundle written by a driver from before 2026-09-17,
#: which did not write them at all -- and then the report says so rather than pretending nothing matched.
class EngineNames(TypedDict):
    constantBlocks: Dict[str, int]    # name -> the smallest eid it was seen at
    shaderEntries: Dict[str, int]
    resources: Dict[str, int]
    markers: Dict[str, int]


class EngineTable(TypedDict):
    """One parsed `engine-schemas/*.json`: what it is called, how to recognise it, and what its names mean."""
    schemaVersion: int
    engine: str
    aliases: List[str]
    notes: List[str]
    detect: Dict[str, List[str]]
    concepts: List[Dict[str, Any]]
    questions: List[Dict[str, Any]]
    #: The file it was read from, filled in by `load_engine_schemas` rather than written in the JSON.
    path: str


def schemas_dir() -> str:
    """The engine-schema folder: `$RDC_ENGINE_SCHEMAS`, else the nearest `engine-schemas` above this module."""
    override = os.environ.get('RDC_ENGINE_SCHEMAS')
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    while True:
        candidate = os.path.join(here, SCHEMA_DIR_NAME)
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return candidate
        here = parent


def load_engine_schemas(directory: str = '', warn: bool = True) -> Tuple[List[EngineTable], List[str]]:
    """Every readable table in the folder, and one problem string per file that is not one.

    A table that cannot be read is a *problem*, not an absence: a mistyped JSON file would otherwise turn into
    "this engine is unknown", which is a different and much more confident statement than "I could not read the
    table". The same goes for a table whose shape is wrong -- a missing `engine`, a `concepts` list with no
    `match` in it -- because a half-read table would match names against nothing.
    """
    folder = directory or schemas_dir()
    problems: List[str] = []
    tables: List[EngineTable] = []
    if not os.path.isdir(folder):
        problems.append('no engine-schema folder at %s (set RDC_ENGINE_SCHEMAS or keep one at the '
                        'repository root)' % folder)
        return tables, problems
    for name in sorted(os.listdir(folder)):
        if not name.endswith('.json'):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding='utf-8-sig') as fh:
                document = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            problems.append('%s could not be read: %s' % (name, exc))
            continue
        if not isinstance(document, dict):
            problems.append('%s is not an object' % name)
            continue
        bad = _table_problem(document)
        if bad:
            problems.append('%s: %s' % (name, bad))
            continue
        table = EngineTable(schemaVersion=int(document.get('schemaVersion', 0)),
                            engine=str(document['engine']),
                            aliases=[str(a) for a in document.get('aliases', [])],
                            notes=[str(n) for n in document.get('notes', [])],
                            detect={str(k): [str(x) for x in v] for k, v in document.get('detect', {}).items()},
                            concepts=list(document['concepts']),
                            questions=list(document.get('questions', [])),
                            path=path)
        tables.append(table)
    if not tables and not problems and warn:
        problems.append('no engine schema table in %s: no frame can be interpreted by name' % folder)
    return tables, problems


def _table_problem(document: Dict[str, Any]) -> str:
    """Why this JSON is not a table, or '' when it is one. One rule per way a table would half-work."""
    if int(document.get('schemaVersion', 0)) != 1:
        return 'schemaVersion %s, this tool reads 1' % document.get('schemaVersion')
    if not str(document.get('engine', '')).strip():
        return 'no engine name'
    concepts = document.get('concepts')
    if not isinstance(concepts, list) or not concepts:
        return 'no concepts list'
    for index, concept in enumerate(concepts):
        if not isinstance(concept, dict) or not str(concept.get('concept', '')).strip():
            return 'concept %d has no name' % index
        match = concept.get('match')
        if not isinstance(match, dict) or not match:
            return 'concept %s has no match' % concept.get('concept')
        for key in match:
            if key not in ('constantBlocks', 'shaderEntries', 'resources', 'markers', 'members', 'structure'):
                return 'concept %s matches on an unknown kind %r' % (concept.get('concept'), key)
    return ''


def bundle_names(bundle: BundleData) -> EngineNames:
    """The names a bundle carries, by kind, each with the smallest eid it was seen at.

    The state documents are keyed by eid, so the evidence for "this frame declares `MobileBasePass`" is the
    eid of the event whose shaders document says so -- which is the same eid the report's pass table uses.
    """
    blocks: Dict[str, int] = {}
    entries: Dict[str, int] = {}
    for eid_text in sorted(bundle['states'], key=lambda t: int(t)):
        documents = bundle['states'][eid_text]
        shaders = documents.get('shaders')
        if not isinstance(shaders, dict):
            continue
        eid = int(eid_text)
        for stage in shaders.get('stages', []):
            entry = str(stage.get('entry', '')).strip()
            if entry and (entry not in entries or eid < entries[entry]):
                entries[entry] = eid
            for row in stage.get('constantBlocks', []):
                name = _block_name(str(row))
                if name and (name not in blocks or eid < blocks[name]):
                    blocks[name] = eid
    resources: Dict[str, int] = {}
    for resource in bundle['resources']:
        name = ' '.join(str(resource.get('name', '')).split())
        if name and name not in resources:
            resources[name] = int(resource.get('firstEvent', 0) or 0)
    markers: Dict[str, int] = {}
    for event in bundle['events']:
        eid = int(event['eid'])
        for segment in _marker_segments(str(event.get('marker', ''))):
            if segment not in markers or eid < markers[segment]:
                markers[segment] = eid
    return EngineNames(constantBlocks=blocks, shaderEntries=entries, resources=resources, markers=markers)


def _marker_segments(path: str) -> List[str]:
    """The names inside a marker path.

    `A > B` yields `A` and `B`: a table names a marker by what the engine called it, and the path is only how
    the engine nests them. A path that is empty yields nothing, so an event outside every marker contributes
    no name -- which is the truth, not a missing measurement.
    """
    return [part.strip() for part in path.split(' > ') if part.strip()]


def _block_name(row: str) -> str:
    """The name out of a `states/*.shaders.json` constant-block row.

    The driver writes `cbuffer[N] <name padded to 28> b<R> s<S> <bytes> bytes, <n> variables`, so the name is
    what follows the bracket up to the padding. A row that does not look like that yields '' rather than a
    guess -- the caller then has no name, which is the truth.
    """
    marker = 'cbuffer['
    if marker not in row:
        return ''
    rest = row.split(marker, 1)[1]
    if ']' not in rest:
        return ''
    return rest.split(']', 1)[1].strip().split('  ')[0].strip()


def detect_engine(names: EngineNames, tables: Sequence[EngineTable]) -> Tuple[Optional[EngineTable], List[EngineEvidence]]:
    """The table the frame's names identify, with the matches behind it; `(None, [])` when none does.

    Counting is over *distinct* matched names, so a block that appears at forty events counts once: the
    question is whether the frame speaks this engine's vocabulary, not how loudly.
    """
    best: Optional[EngineTable] = None
    best_evidence: List[EngineEvidence] = []
    for table in tables:
        evidence: List[EngineEvidence] = []
        for kind, seen in (('constant-block', names['constantBlocks']), ('shader-entry', names['shaderEntries']),
                           ('resource', names['resources']), ('marker', names['markers'])):
            detect_key = {'constant-block': 'constantBlocks', 'shader-entry': 'shaderEntries',
                          'resource': 'resources', 'marker': 'markers'}[kind]
            for wanted in table['detect'].get(detect_key, []):
                if wanted in seen:
                    evidence.append(EngineEvidence(kind=kind, name=wanted,
                                                   where='seen at eid %d' % seen[wanted]))
        if len(evidence) >= MIN_DETECT_MATCHES and len(evidence) > len(best_evidence):
            best, best_evidence = table, evidence
    return best, best_evidence


def _pass_evidence(bundle: BundleData, entry: ReportPass) -> Tuple[Dict[str, Set[str]], List[str]]:
    """What can be said about one pass by name: its structure, its entry points and its constant blocks.

    The pass roll-up already carries the *block rows* of the pass's first event, but the entry points are only
    in the state document, so the names are read there, once, from the file the report's own roll-up reads.
    """
    names: Dict[str, Set[str]] = {'structure': set(), 'shaderEntries': set(), 'constantBlocks': set(),
                                  'markers': set(_marker_segments(str(entry.get('marker', '')))), 'members': set()}
    names['structure'].add(str(entry['structure']))
    documents = bundle['states'].get(str(entry['firstEid']), {})
    shaders = documents.get('shaders')
    if isinstance(shaders, dict):
        for stage in shaders.get('stages', []):
            name = str(stage.get('entry', '')).strip()
            if name:
                names['shaderEntries'].add(name)
            for row in stage.get('constantBlocks', []):
                block = _block_name(str(row))
                if block:
                    names['constantBlocks'].add(block)
    return names, []


def _match_concept(concept: Dict[str, Any], names: Dict[str, Set[str]]) -> List[EngineEvidence]:
    """The evidence for one concept at one pass (or, for a block-only concept, for the frame).

    **Every kind the concept asks about must match at least once**, and the returned evidence is all the names
    that did. A match is therefore a conjunction -- "this pass runs one of these entry points *and* has one of
    these blocks bound" -- which is what lets the table say something narrower than "any of these names": a
    block that is merely *bound* in a pass (a leftover from an earlier call, which happens on every frame)
    cannot name the pass on its own, and a depth-only structure cannot either. Within one kind the names are
    alternatives, because two shaders of one concept set are one concept.

    `where` is filled in by the caller, which knows whether it is looking at a pass or at the whole frame.
    """
    match = {key: value for key, value in concept.get('match', {}).items() if value}
    found: List[EngineEvidence] = []
    for key, kind in (('shaderEntries', 'shader-entry'), ('constantBlocks', 'constant-block'),
                      ('structure', 'structure'), ('markers', 'marker')):
        if key not in match:
            continue
        hits = [wanted for wanted in match[key] if wanted in names.get(key, set())]
        if not hits:
            return []
        for wanted in hits:
            found.append(EngineEvidence(kind=kind, name=wanted, where=''))
    return found


def interpret_frame(bundle: BundleData, passes: Sequence[ReportPass],
                    tables: Optional[Sequence[EngineTable]] = None,
                    problems: Optional[List[str]] = None) -> EngineInterpretation:
    """The frame in the vocabulary of the engine the bundle names, or an honest "no interpretation".

    `passes` is the report's own pass list, because a concept of kind `pass` is claimed per pass and cites the
    pass it was claimed for. A concept whose match is only ever a *structure* string (a depth-only draw) needs
    the entry point too: state alone says what a pass looks like, and only a name says what it is.
    """
    folder = schemas_dir()
    loaded_problems: List[str] = []
    if tables is None:
        tables, loaded_problems = load_engine_schemas(folder)
    not_interpreted: List[str] = list(loaded_problems) + list(problems or [])

    names = bundle_names(bundle)
    table, detected = detect_engine(names, tables)
    if table is None:
        if tables:
            not_interpreted.append('the names in this bundle match none of the %d engine table(s) in %s: this '
                                   'frame is not interpreted by name' % (len(tables), folder))
        return EngineInterpretation(engine='', schema='', basis=_BASIS, detected=[], concepts=[],
                                    questions=[], notInterpreted=_sorted_unique(not_interpreted))

    if not names['markers']:
        not_interpreted.append('markers: this bundle carries no marker names, so a concept that matches only '
                               'on one cannot be claimed (a bundle from before 2026-09-17 was written by a '
                               'driver that did not record them; write it again with the current one)')
    marker_concepts = [str(concept['concept']) for concept in table['concepts']
                       if 'markers' in concept.get('match', {})]
    if marker_concepts and not names['markers']:
        not_interpreted.append('marker-based concept(s) %s need a bundle with marker names'
                               % ', '.join(sorted(marker_concepts)))

    rows: List[EngineConceptRow] = []
    for concept in table['concepts']:
        title = str(concept['concept'])
        kind = str(concept.get('kind', 'constant-block'))
        if kind == 'pass':
            for entry in passes:
                per_pass, _ = _pass_evidence(bundle, entry)
                found = _match_concept(concept, per_pass)
                if found:
                    rows.append(_concept_row(title, kind, found, entry, concept))
        else:
            frame = {'constantBlocks': set(names['constantBlocks']), 'shaderEntries': set(names['shaderEntries']),
                     'structure': set(), 'markers': set(names['markers']),
                     'members': _members_present(bundle, table, concept)}
            found = _match_concept(concept, frame)
            if found:
                rows.append(_concept_row(title, kind, found, None, concept))

    rows.sort(key=lambda row: (row['concept'], row['firstEid'], row['kind']))
    values = _values(bundle, table, rows, passes)
    questions = _questions(table, rows, values)
    return EngineInterpretation(engine=table['engine'], schema=os.path.basename(table['path']), basis=_BASIS,
                                detected=detected, concepts=rows, questions=questions,
                                notInterpreted=_sorted_unique(not_interpreted))


#: The one sentence that has to appear wherever the interpretation does: what it rests on, and what it is not.
_BASIS = ('name-based: a concept is claimed because the capture itself contains a name the table lists, and '
          'nothing here is inferred from values, order or timing')


def _concept_row(title: str, kind: str, found: List[EngineEvidence], entry: Optional[ReportPass],
                 concept: Dict[str, Any]) -> EngineConceptRow:
    """One concept row, with its evidence pointing at the document or the pass it came from."""
    where = 'pass %d (eid %d)' % (entry['index'], entry['firstEid']) if entry is not None else 'the frame'
    evidence = [EngineEvidence(kind=item['kind'], name=item['name'],
                               where=where if item['kind'] != 'structure' else where + ', structure')
                for item in found]
    return EngineConceptRow(concept=title, kind=kind, evidence=evidence, note=str(concept.get('note', '')),
                            passIndex=entry['index'] if entry is not None else 0,
                            firstEid=entry['firstEid'] if entry is not None else 0)


def _members_present(bundle: BundleData, table: EngineTable, concept: Dict[str, Any]) -> Set[str]:
    """The members this concept's block carries, so a `semantic` concept can be matched on a member name."""
    wanted = [str(m) for m in concept.get('match', {}).get('members', [])]
    if not wanted:
        return set()
    blocks = [str(b) for b in concept.get('match', {}).get('constantBlocks', [])]
    present: Set[str] = set()
    for member, _block, _value, _eid, _bound in _member_rows(bundle, blocks):
        for tag in wanted:
            if member == tag or member.startswith(tag + '[') or member.startswith(tag + '_'):
                present.add(tag)
    return present


def _member_rows(bundle: BundleData, blocks: Sequence[str]) -> List[Tuple[str, str, str, int, bool]]:
    """`(member, block, value, eid, bound)` for every leaf row of every document whose block is in `blocks`.

    A cbuffer document is `cbuffers/<eid>_<stage>_<slot>.json` and the block's *name* is the `cbuffer[N]` row
    at the same index in the state document -- so the join is on (eid, stage, slot), and a document with no
    name (the same slot in a shader whose reflection names nothing) is skipped rather than guessed at.
    Padding members are dropped: they are the layout's holes, not the frame's data.

    `bound` comes from the document's own `buffer` field: when nothing was bound, the engine's read returns
    every member as its default and the driver says so in that field. Passing those zeros off as values would
    turn "this block was not bound at this event" into "this block is zero", which is a different claim.
    """
    wanted = set(blocks)
    rows: List[Tuple[str, str, str, int, bool]] = []
    for key in sorted(bundle['cbuffers']):
        document = bundle['cbuffers'][key]
        stem = os.path.basename(key)[:-len('.json')] if key.endswith('.json') else os.path.basename(key)
        parts = stem.split('_')
        if len(parts) != 3 or not parts[0].isdigit() or not parts[2].isdigit():
            continue
        eid, stage, slot = int(parts[0]), parts[1], int(parts[2])
        block = _block_at(bundle, eid, stage, slot)
        if block not in wanted:
            continue
        bound = not str(document.get('buffer', '')).startswith('(')
        for row in document.get('variables', []):
            member, value = _leaf(str(row))
            if member and not member.startswith('Padding'):
                rows.append((member, block, value, eid, bound))
    rows.sort(key=lambda row: (row[1], row[0], row[3]))
    return rows


def _block_at(bundle: BundleData, eid: int, stage: str, slot: int) -> str:
    """The name of the block a cbuffer document belongs to, from the state document of the same event."""
    documents = bundle['states'].get(str(eid), {})
    shaders = documents.get('shaders')
    if not isinstance(shaders, dict):
        return ''
    for entry in shaders.get('stages', []):
        if str(entry.get('stage', '')) != stage:
            continue
        rows = entry.get('constantBlocks', [])
        if 0 <= slot < len(rows):
            return _block_name(str(rows[slot]))
    return ''


def _leaf(row: str) -> Tuple[str, str]:
    """`name = value` for a *leaf* row (a struct row also holds its members, and is not a value of its own)."""
    if '=' not in row or '{' in row or row.count('=') != 1:
        return '', ''
    name, value = row.split('=', 1)
    name, value = name.strip(), value.strip()
    if not name or not value:
        return '', ''
    return name, value


def _values(bundle: BundleData, table: EngineTable, rows: Sequence[EngineConceptRow],
            passes: Sequence[ReportPass]) -> List[EngineValue]:
    """The tagged members of every block a claimed concept names -- the "which value" half of a claim.

    Only members the table *tags* are read: a block can hold two thousand variables and the report's job is to
    point at the ones that carry the concept, not to dump the buffer.

    Each row keeps the event its document was written at and the report's pass that event falls in, because a
    value without a place is not evidence of anything. Rows that agree on all of that except the eid collapse
    to the first one: the same block holding the same value at three state changes inside one pass is one fact,
    and printing it three times buries the next member.
    """
    by_block: Dict[str, List[str]] = {}
    for concept in table['concepts']:
        members = [str(m) for m in concept.get('members', [])]
        for block in concept.get('match', {}).get('constantBlocks', []):
            by_block.setdefault(str(block), [])
            for member in members:
                if member not in by_block[str(block)]:
                    by_block[str(block)].append(member)
    if not by_block:
        return []

    # A block can be claimed by more than one concept -- `Scene` is both "the base pass runs with it" and a
    # concept of its own -- and its values belong to *each* of them: a figure attached to only one of the two
    # would be missing from the other's question, which is exactly where a reader looks for it.
    claimed: Dict[str, List[str]] = {}
    for row in rows:
        for item in row['evidence']:
            if item['kind'] == 'constant-block':
                claimed.setdefault(item['name'], [])
                if row['concept'] not in claimed[item['name']]:
                    claimed[item['name']].append(row['concept'])

    values: List[EngineValue] = []
    seen: List[Tuple[str, str, str, str, int, bool]] = []
    for member, block, value, eid, bound in _member_rows(bundle, sorted(by_block)):
        concepts = claimed.get(block, [])
        if not concepts:
            continue
        tags = by_block[block]
        if not any(member == tag or member.startswith(tag + '[') or member.startswith(tag + '_') for tag in tags):
            continue
        index = _pass_index(passes, eid)
        for concept in concepts:
            key = (concept, block, member, value, index, bound)
            if key in seen:
                continue
            seen.append(key)
            values.append(EngineValue(concept=concept, block=block, member=member, value=value, firstEid=eid,
                                      passIndex=index, bound=bound))
    values.sort(key=lambda item: (item['concept'], item['block'], item['member'], item['firstEid']))
    return values


def _pass_index(passes: Sequence[ReportPass], eid: int) -> int:
    """The pass an event falls in, or 0 when it falls in none (the pass table is not a partition of every id)."""
    for entry in passes:
        if entry['firstEid'] <= eid <= entry['lastEid']:
            return int(entry['index'])
    return 0


def _questions(table: EngineTable, rows: Sequence[EngineConceptRow],
               values: Sequence[EngineValue]) -> List[EngineQuestion]:
    """The table's own questions, answered with the rows and values that belong to the group it names.

    A group is how a question is phrased in the table ("global illumination"), so the answer is assembled from
    the concepts tagged with it -- no question-specific code. A question whose group has nothing in this frame
    is still returned, with empty lists: "nothing here speaks to it" is an answer, and a missing question would
    read as "not asked".
    """
    groups = {str(c.get('concept')): str(c.get('group', '')) for c in table['concepts']}
    out: List[EngineQuestion] = []
    for question in table['questions']:
        group = str(question.get('group', ''))
        in_group = [row['concept'] for row in rows if groups.get(row['concept']) == group]
        out.append(EngineQuestion(id=str(question['id']), title=str(question['title']),
                                  ask=str(question.get('ask', '')),
                                  conceptRows=[row for row in rows if row['concept'] in in_group],
                                  values=[item for item in values if item['concept'] in in_group]))
    return out


def _sorted_unique(items: Iterable[str]) -> List[str]:
    """Stable order and no duplicates, so two runs over one bundle produce the same bytes."""
    out: List[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


__all__ = [
    'EngineNames',
    'EngineTable',
    'MIN_DETECT_MATCHES',
    'SCHEMA_DIR_NAME',
    'bundle_names',
    'detect_engine',
    'interpret_frame',
    'load_engine_schemas',
    'schemas_dir',
]
