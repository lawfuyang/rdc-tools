"""Which shaders a frame actually uses: one row per `(stage, hash)`, counted over the frame's own events.

The question this answers is a feature study's first one -- "how many permutations does this frame really
contain, and is the one I am editing even in it?" -- and both halves of it were already in the tree:

* A bundle's `states/<eid>.shaders.json` gives each bound stage's `hash` (`sha256` of the shader's bytes), its
  entry point and its reflection summary. Those documents are written per state *change* rather than per event
  (`bundle.cpp`), so they are where the *identity* of a shader comes from, not where its events are counted.
* `events.json` names the shader *resource* every event binds (`vs=2348 ps=2349`), for every event.

So the table is a join of the two: `(stage, resource) -> hash` is read off the state documents, and each event's
own resource is looked up in it. A shader whose state document was never written cannot exist here -- the
driver writes one at the first event with bound state and at every change -- which is what makes the count
complete rather than a sample.

**The file side joins on a measured key.** A bundle's `hash` is `sha256` of the shader's bytes, which is the
third of the three identities `psos` keeps per container (`ContainerRow['sha256']`, REFERENCE §4.22); the two
other identities a container carries (its header hash and the DXIL `HASH` part, which names a PDB) are *not*
what a bundle holds. With `--capture` the rows gain the container each hash lives at and whether it carries its
own debug data (`ILDB`), which is the `psos` half of the same question.
"""
from __future__ import annotations

import os
import sys

from rdc_bundle import *  # noqa: F401,F403  (BundleData and the loader are this command's input)

import rdc_cache       # called qualified: the stream and its sidecar path are the cache's own answers
import rdc_psos       # called qualified: the container index is that module's answer, not a copy of it
import rdc_table      # called qualified: the format the entry point parsed and checked

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypedDict

#: What one shader is, in one row: its stage, its identity, how much of the frame binds it and what it needs.
#:
#: `resources` is every shader resource id seen bound for this hash, in frame order: the same shader can be
#: uploaded twice (a reload, a second entry point), and two ids for one hash is a fact worth seeing rather
#: than a duplicate to fold away. The container fields stay empty unless the capture was given (`--capture`):
#: a bundle does not hold them, and a path in the manifest is this machine's state rather than a fact.
class PermutationRow(TypedDict):
    stage: str
    hash: str
    entry: str
    events: int
    firstEid: int
    lastEid: int
    resources: List[str]
    cbuffers: int
    srvs: int
    uavs: int
    container: int
    size: int
    ilbd: Optional[bool]

__all__ = [
    'PermutationRow',
    'bound_shaders',
    'cmd_permutations',
    'join_containers',
    'permutations',
]

def _res_id(text: str) -> str:
    """A resource id as the bundle's rows spell it, with any `res` prefix taken off."""
    text = text.strip()
    return text[3:] if text.startswith('res') else text

def bound_shaders(event: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """`[(stage, resource id)]` for one `events.json` row: the driver writes `vs=2348 ps=2349`."""
    out: List[Tuple[str, str]] = []
    for token in str(event.get('shaders', '')).split():
        stage, _sep, resource = token.partition('=')
        if stage and resource:
            out.append((stage, _res_id(resource)))
    return out

def shader_identities(bundle: BundleData) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """`(stage, resource) -> the shaders document's stage object`, read off the state documents.

    Every event with bound state has a state document, and one is written at every change, so a resource that
    no document names was never bound. Two documents that name one resource with two hashes are the same
    shader reloaded; the later one wins, and `--hash` answers about hashes rather than about history.
    """
    known: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for _eid, documents in bundle['states'].items():
        shaders = documents.get('shaders')
        if not isinstance(shaders, dict):
            continue
        for stage in shaders.get('stages', []):
            if not isinstance(stage, dict) or not stage.get('hash'):
                continue
            known[(str(stage.get('stage', '?')), _res_id(str(stage.get('resource', ''))))] = stage
    return known

def permutations(bundle: BundleData) -> List[PermutationRow]:
    """The frame's shaders: one row per `(stage, hash)`, counted over the events that bind it."""
    known = shader_identities(bundle)
    rows: Dict[Tuple[str, str], PermutationRow] = {}
    for event in bundle['events']:
        eid = int(event['eid'])
        for stage, resource in bound_shaders(event):
            document = known.get((stage, resource))
            if document is None:
                continue    # a stage no document describes: counted nowhere rather than guessed at
            key = (stage, str(document['hash']))
            row = rows.get(key)
            if row is None:
                row = rows[key] = PermutationRow(
                    stage=stage, hash=str(document['hash']), entry=str(document.get('entry', '')),
                    events=0, firstEid=eid, lastEid=eid, resources=[],
                    cbuffers=len(document.get('constantBlocks', []) or []),
                    srvs=len(document.get('readOnlyResources', []) or []),
                    uavs=len(document.get('readWriteResources', []) or []),
                    container=0, size=0, ilbd=None)
            row['events'] += 1
            row['firstEid'] = min(row['firstEid'], eid)
            row['lastEid'] = max(row['lastEid'], eid)
            if resource not in row['resources']:
                row['resources'].append(resource)
    return sorted(rows.values(), key=lambda row: (row['stage'], -row['events'], row['hash']))

def join_containers(rows: Sequence[PermutationRow], index: Any) -> int:
    """Add the container each hash lives in, from a `psos` index; returns how many rows joined.

    The key is the container's `sha256` -- the digest of the container's *bytes*, which is what a bundle's
    `hash` is -- and not the two other identities a container carries: its header hash (what `dxbc` prints) and
    its `HASH` part's digest (the name a PDB takes). A hash that joins nothing is left empty rather than
    guessed at, and the count is reported.
    """
    by_sha256: Dict[str, Any] = {}
    for container in index['containers']:
        by_sha256.setdefault(container['sha256'], container)
    joined = 0
    for row in rows:
        container = by_sha256.get(row['hash'])
        if container is None:
            continue
        row['container'] = int(container['offset'])
        row['size'] = int(container['size'])
        row['ilbd'] = bool(container['ilbd'])
        joined += 1
    return joined

def _reflection_text(row: PermutationRow) -> str:
    """The reflection summary of one row, compactly: what a shader needs, not what it declares."""
    return 'cb%d srv%d uav%d' % (row['cbuffers'], row['srvs'], row['uavs'])

def _container_text(row: PermutationRow) -> str:
    if not row['container']:
        return '-'
    return '@%d, %d B%s' % (row['container'], row['size'], ', ILDB' if row['ilbd'] else '')

def cmd_permutations(path: str, limit: int = 40, fmt: str = 'table',
                     want_hash: Optional[str] = None, capture: str = '') -> int:
    """`permutations <bundle> [maxRows] [--hash <prefix>] [--capture <rdc>] [--format ...]`.

    Exit 1 when `--hash` matches nothing, the same rule `psos --hash` follows: a filter that found nothing is a
    failed question, not an empty table.
    """
    bundle = load_bundle(path)
    rows = permutations(bundle)
    joined = 0
    if capture:
        info, stream, _how = rdc_cache.load_stream(capture)
        joined = join_containers(rows, rdc_psos.shader_index(stream, rdc_cache.stream_source(capture, info)))

    if want_hash:
        want = want_hash.lower()
        matches = [row for row in rows if row['hash'].startswith(want)]
        if not matches:
            print('hash %s: not bound by any event of this frame (%d shader(s) known)'
                  % (want_hash, len(rows)), file=sys.stderr)
            return 1
        for row in matches:
            print('hash %s (%s, %d event(s) eid %d-%d, entry `%s`)' % (row['hash'], row['stage'],
                                                                       row['events'], row['firstEid'],
                                                                       row['lastEid'], row['entry']))
            print('    reflection: %s' % _reflection_text(row))
            print('    bound as  : %s' % ', '.join('res%s' % r for r in row['resources']))
            if capture:
                print('    container : %s' % (('@%d, %d bytes, debug data %s'
                                               % (row['container'], row['size'],
                                                  'embedded (ILDB)' if row['ilbd'] else
                                                  'not embedded (a DX11/DXIL shader is stepped through its '
                                                  'PDB)'))
                                              if row['container'] else 'not in the capture given'))
        return 0

    stages: Dict[str, int] = {}
    for row in rows:
        stages[row['stage']] = stages.get(row['stage'], 0) + 1
    events_bound = sum(row['events'] for row in rows)

    table: List[Sequence[object]] = []
    for row in rows[:limit]:
        table.append((row['stage'], row['hash'][:12], row['events'],
                      '%d-%d' % (row['firstEid'], row['lastEid']), row['entry'][:28] or '-',
                      _reflection_text(row), _container_text(row)))

    notes: List[str] = []
    notes.append('permutations: %d shader(s) bound by %d event(s) over %d state-document(s); %s'
                 % (len(rows), events_bound, len(bundle['states']),
                    ', '.join('%s %d' % (stage, count) for stage, count in sorted(stages.items()))))
    if capture:
        notes.append('containers: %d of %d hash(es) found in %s (the join is the container\'s sha256, the '
                     'same identity a bundle holds); %d carry ILDB'
                     % (joined, len(rows), os.path.basename(capture),
                        sum(1 for row in rows if row['ilbd'])))
    else:
        notes.append('containers: not read (pass `--capture <rdc>` to add the container, its size and whether '
                     'it carries debug data)')
    if len(rows) > limit:
        notes.append('shown: the first %d of %d shader(s) (a `maxRows` argument raises it)' % (limit, len(rows)))
    notes.append('hashes are `sha256` of the shader\'s bytes, one per stage: the same identity `replaydiff` '
                 'compares two frames by, and the one `psos` keys a container on (`--hash <prefix>` answers '
                 'one)')
    columns = ('stage', 'hash', 'events', 'eids', 'entry', 'reflection', 'container')
    if fmt != 'table':
        rdc_table.emit(fmt, columns, table, notes)
        return 0
    for note in notes:
        print(note)
    print('%-5s %-12s %6s %-13s %-28s %-14s %s' % columns)
    for row in table:
        print('%-5s %-12s %6d %-13s %-28s %-14s %s' % (row[0], row[1], row[2], row[3], row[4], row[5],
                                                       row[6]))
    return 0
