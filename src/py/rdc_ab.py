"""`replaydiff`: what two bundles say about the same frame, and where they disagree.

A bundle is what the engine answered about one capture (`replay_dump dump`, `REFERENCE.md` §9), so
comparing two of them is the A/B this project exists for -- mobile against PC, or one capture through two
builds of the tools -- with no device in the loop: each replay already happened when its bundle was
written, and everything here reads files. The driver half is `dump`; this half is testable without a GPU,
a capture or a driver, which is the split the roadmap lays down for anything that spans the two tools.

Two `.rdc` files cannot be replayed in one process (`REFERENCE.md` §9: one replay at a time -- two replay
devices on one GPU is what makes a run look stuck), so this command takes the *engine's answers* for both
sides rather than opening both captures:

    replay_dump dump mobile.rdc  mobile-bundle --with-images
    replay_dump dump pc.rdc      pc-bundle     --with-images
    python rdc_analysis.py replaydiff mobile-bundle pc-bundle --with-images

What it compares, all of it out of the documents a bundle carries:

  * the two frames' shape -- events, passes, resources, API -- and whether the two bundles describe *the
    same capture* (an equal `captureSha256` means a tools A/B rather than a capture A/B);
  * the pass lists, aligned by **marker path** (a name survives a re-capture where an index does not),
    with the structure changes of every aligned pair and the passes only one side has;
  * the effective state at each aligned pass's first state event: the root-parameter rows, the targets and
    the shaders, with resource ids annotated by the name the capture gave them (`res2207[SceneColour]`);
  * the named constant values of each aligned pass, paired by stage and slot, member by member;
  * the shaders' identity: the SHA-256 the driver stamps every stage with, so "the same shader" and "a
    different one" are facts rather than a comparison of two sizes;
  * with `--with-images`, each pass's readback -- every pair by identity, and the pairs that differ pixel
    by pixel up to `--image-detail`, with a heat map per compared pair.

What it cannot say is in the document's `caveats`, and it is not a short list: a bundle holds no shader
source, no triangle counts and no names for anything the capture did not name, and both sides were
replayed on *this* machine's GPU whatever they were captured on.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple, TypedDict

from rdc_bundle import *  # noqa: F401,F403  (the bundle, its types and the shared readers)
from rdc_passes import *  # noqa: F401,F403  (pass reconstruction: one notion of a pass for both commands)
import rdc_ab_render
import rdc_image

#: The document's own format version, the way the report's `schemaVersion` works: a reader can refuse a
#: shape it does not know instead of guessing at a member that moved.
AB_SCHEMA_VERSION = 1

#: How many differing image pairs are decoded and compared pixel by pixel by default. Decoding is pure
#: Python and measured at ~0.15 s per megabyte (`rdc_image`), so an unbounded default would turn a
#: five-minute command into an hour-long one; `--image-detail N` raises it and the rest are named.
IMAGE_DETAIL = 8

#: `res<id>`, as the driver prints a resource. Compiled once: `annotate` runs over every row of every
#: aligned pass, and a fresh pattern per call is a thousand compiles for a frame.
_RES_ID = re.compile(r'res(\d+)')


class AbChange(NamedTuple):
    """One field that differs: its name, and what each side says."""
    field: str
    a: str
    b: str


class AbValueRow(NamedTuple):
    """One constant-buffer member: its name, and the row each side printed (`name = value`)."""
    member: str
    a: str
    b: str


class AbCbufferRow(NamedTuple):
    """One constant block of an aligned pass pair, as the comparison sees it."""
    block: str
    stage: str
    slot: int
    a_file: str
    b_file: str
    notes: List[str]
    values: List[AbValueRow]


class AbShaderRow(NamedTuple):
    """One bound stage of an aligned pass pair: its identity, and the reflection rows that moved."""
    stage: str
    verdict: str
    a_hash: str
    b_hash: str
    a_bytes: int
    b_bytes: int
    changes: List[AbChange]


class AbImageRow(NamedTuple):
    """One render target of an aligned pass pair, as far as it was compared.

    `a_eid`/`b_eid` are the events each side's picture was taken at -- the last one inside the pass that
    wrote that slot -- because the two frames' ids are different numberings and a reader may want to open
    either side at the event the picture came from.
    """
    slot: int
    verdict: str
    a_eid: int
    b_eid: int
    a_file: str
    b_file: str
    width: int
    height: int
    differing: int
    percent_differing: str
    mean_delta: str
    max_delta: int
    hash_distance: int
    heat_map: str
    note: str


class AbPassRow(NamedTuple):
    """One aligned pass pair: the structure of both sides and everything that differs inside."""
    path: str
    status: str
    note: str
    a_first_eid: int
    b_first_eid: int
    structure: str
    changes: List[AbChange]
    state_removed: List[str]
    state_added: List[str]
    shaders: List[AbShaderRow]
    cbuffers: List[AbCbufferRow]
    images: List[AbImageRow]


class AbRow(NamedTuple):
    """One *aligned* pair before anything inside it is compared: which passes the two sides map to."""
    path: str
    status: str
    a: Optional[ReportPass]
    b: Optional[ReportPass]
    note: str


class AbSide(NamedTuple):
    """One bundle being compared: where it is, what it says, and its two lookup tables."""
    directory: str
    bundle: BundleData
    passes: List[ReportPass]
    #: `res<id>` -> the name the capture gave that resource, for annotating ids in rows.
    names: Dict[str, str]


# ---------------------------------------------------------------------------
# The document itself. Written out member by member rather than derived from the records above, because
# this is the artefact a reader (and `replaydiff.json`'s consumer) keeps: a member that moved has to be a
# deliberate change to this shape, not something a `_asdict()` happened to rename.
#
# camelCase to match the driver's documents and the report's (`firstEid`, `percentDiffering`), so a rule
# written against one of them reads the same way against this one.

class AbChangeMember(TypedDict):
    """One field that differs, in the document."""
    field: str
    a: str
    b: str


class AbValueMember(TypedDict):
    """One constant-buffer member, in the document."""
    member: str
    a: str
    b: str


class AbCbufferMember(TypedDict):
    """One constant block of an aligned pass pair, in the document."""
    block: str
    stage: str
    slot: int
    aFile: str
    bFile: str
    notes: List[str]
    values: List[AbValueMember]


class AbShaderMember(TypedDict):
    """One bound stage of an aligned pass pair, in the document."""
    stage: str
    verdict: str
    aHash: str
    bHash: str
    aBytes: int
    bBytes: int
    changes: List[AbChangeMember]


class AbImageMember(TypedDict):
    """One render target of an aligned pass pair, in the document."""
    slot: int
    verdict: str
    aEid: int
    bEid: int
    aFile: str
    bFile: str
    width: int
    height: int
    differing: int
    percentDiffering: str
    meanDelta: str
    maxDelta: int
    hashDistance: int
    heatMap: str
    note: str


class AbPassMember(TypedDict):
    """One aligned pass pair, with everything that differs inside it."""
    path: str
    status: str
    note: str
    aFirstEid: int
    bFirstEid: int
    structure: str
    changes: List[AbChangeMember]
    stateRemoved: List[str]
    stateAdded: List[str]
    shaders: List[AbShaderMember]
    cbuffers: List[AbCbufferMember]
    images: List[AbImageMember]


class AbSideMember(TypedDict):
    """One side's frame, as the document states it."""
    bundle: str
    capture: str
    captureSha256: str
    api: str
    events: int
    passes: int
    resources: int
    messages: int
    withImages: int
    renderdoc: str


class AbSummaryMember(TypedDict):
    """The counts the document's own summary line prints."""
    same: int
    added: int
    removed: int
    structureChanges: int
    stateChanges: int
    constantsChanged: int
    shadersDifferent: int
    imagesCompared: int
    imagesNotCompared: int


class ReplayDiffDocument(TypedDict):
    """The whole comparison: what `replaydiff.json` holds and what the Markdown is rendered from."""
    schemaVersion: int
    a: AbSideMember
    b: AbSideMember
    sameCapture: bool
    withImages: bool
    imagesFound: bool
    imageDetail: int
    passes: List[AbPassMember]
    summary: AbSummaryMember
    caveats: List[str]


def load_side(bundle_dir: str) -> AbSide:
    """Read one bundle and reconstruct its passes, raising `BundleError` if it cannot be read.

    The resource names come from `resources.json` and are only ever used to *annotate* a row: an id is
    capture-local (the engine numbers resources per capture), so an id is never compared across sides --
    the name and the row's own text are what survive, and the annotation is what makes the rows readable.
    """
    bundle = load_bundle(bundle_dir)
    passes = reconstruct_passes(bundle['events'], bundle['resources'])
    names: Dict[str, str] = {}
    for resource in bundle['resources']:
        name = ' '.join(str(resource.get('name', '')).split())
        if name:
            names['res%s' % _res_id(str(resource.get('resource', '')))] = name
    return AbSide(bundle_dir, bundle, passes, names)


def side_summary(side: AbSide) -> AbSideMember:
    """The frame's shape as one side of the document: what the bundle says, counted and named."""
    facts = frame_facts(side.bundle)
    manifest = side.bundle['manifest']
    return {
        'bundle': side.directory,
        'capture': str(manifest.get('capture', '')),
        'captureSha256': str(manifest.get('captureSha256', '')),
        'api': str(facts['api']),
        'events': int(facts['events']),
        'passes': len(side.passes),
        'resources': int(facts['resources']),
        'messages': int(facts['messages']),
        'withImages': int(manifest.get('withImages', 0) or 0),
        'renderdoc': str(manifest.get('renderdoc', '')),
    }


def pass_markers(side: AbSide) -> Dict[int, str]:
    """`pass index -> the marker path of the first event inside it that the engine put in one`.

    `reconstruct_passes` labels a pass with the marker of its *first* event, and that is empty more often
    than it looks: the ids between two command lists carry state -- replay-history ghosts, `REFERENCE.md`
    §9 -- while sitting in no marker, so a pass that begins on one of those would have no name to align
    on. The first *marked* event is what a reader means by "which pass is this"; a pass with no marked
    event at all keeps no key, which is why the other rules exist.
    """
    markers: Dict[int, str] = {}
    index = 0
    for event in side.bundle['events']:
        eid = int(event['eid'])
        while index < len(side.passes) and eid > int(side.passes[index]['lastEid']):
            index += 1
        if index >= len(side.passes):
            break
        if index in markers:
            continue
        if int(side.passes[index]['firstEid']) <= eid <= int(side.passes[index]['lastEid']):
            path = str(event.get('marker', ''))
            if path:
                markers[index] = path
    return markers


def align(a: AbSide, b: AbSide) -> List[AbRow]:
    """Line the two pass lists up, strongest evidence first, and say which rule matched each pair.

    Three rules, in the roadmap's order, each applied to what the previous one left unpaired:

     1. the **marker path** (`Scene > BasePass`), which is what a re-capture preserves;
     2. the **innermost marker name**, because a path carries dynamic text no table could list -- a real
        pair of captures has `CullLights 22x14x8 NumLights 0` against `CullLights 32x20x8 NumLights 0`,
        the same pass with a different light grid -- and this is the same "the name inside the path" rule
        `--at-marker` resolves by (REFERENCE §9);
     3. the **resource names both passes write**, for a pass whose markers were renamed on one side;
     4. **call order** for whatever is left, which is all an unnamed pass on either side can be matched by.

    Every rule pairs by *occurrence*: a name used twice in a frame pairs with its own occurrence on the
    other side, so the second `Scene > Shadow` pairs with the second, and the ranges stay about one pass.
    Each fallback writes itself into the row's note, so a reader can disagree with a match instead of
    having to guess how it was made.
    """
    paired: Dict[int, int] = {}
    how: Dict[int, str] = {}
    taken: List[bool] = [False] * len(b.passes)

    def match(keys_a: Dict[int, str], keys_b: Dict[int, str], explain: str) -> None:
        """Pair what is still unpaired by equal keys, in occurrence order ('' means "no key at all")."""
        queues: Dict[str, List[int]] = {}
        for index in range(len(a.passes)):
            key = keys_a.get(index, '')
            if index not in paired and key:
                queues.setdefault(key, []).append(index)
        for index in range(len(b.passes)):
            key = keys_b.get(index, '')
            if taken[index] or not key:
                continue
            queue = queues.get(key)
            if queue:
                other = queue.pop(0)
                paired[other] = index
                taken[index] = True
                how[other] = explain % key if '%s' in explain else explain

    paths_a, paths_b = pass_markers(a), pass_markers(b)
    match(paths_a, paths_b, '')
    match({index: _leaf(path) for index, path in paths_a.items()},
          {index: _leaf(path) for index, path in paths_b.items()},
          'aligned by the innermost marker name `%s`: the full paths differ')
    match({index: _target_key(a, entry) for index, entry in enumerate(a.passes)},
          {index: _target_key(b, entry) for index, entry in enumerate(b.passes)},
          'aligned by the resources both passes write (%s)')

    # Call order last, and only for passes with nothing else to go on: it is the weakest rule here, and
    # applying it to a named pass would pair two passes that merely happen to sit at the same place.
    loose_b = [index for index in range(len(b.passes))
               if not taken[index] and not paths_b.get(index)]
    for index, entry in enumerate(a.passes):
        if index in paired or paths_a.get(index):
            continue
        for other in list(loose_b):
            if str(b.passes[other]['kind']) != str(entry['kind']):
                continue      # a graphics pass is not the same pass as a dispatch, however they are ordered
            loose_b.remove(other)
            paired[index] = other
            taken[other] = True
            how[index] = ('aligned by call order (pass %d of each) and the call kind: neither pass sits '
                          'in a marker' % (index + 1))
            break

    rows: List[AbRow] = []
    for index, entry in enumerate(a.passes):
        other_index = paired.get(index)
        path = paths_a.get(index) or 'pass %d of A' % (index + 1)
        if other_index is None:
            rows.append(AbRow(path, 'removed', entry, None, ''))
            continue
        other = b.passes[other_index]
        notes = [how.get(index, '')]
        if other_index != index:
            notes.append('moved: pass %d of A is pass %d of B' % (index + 1, other_index + 1))
        rows.append(AbRow(path, 'same', entry, other, '; '.join(note for note in notes if note)))

    for index, entry in enumerate(b.passes):
        if not taken[index]:
            rows.append(AbRow(paths_b.get(index) or 'pass %d of B' % (index + 1), 'added', None, entry,
                              ''))
    return rows


def _leaf(path: str) -> str:
    """The innermost component of a marker path (`A > B` -> `B`), which is what survives dynamic text."""
    return path.rsplit(' > ', 1)[-1].strip() if path else ''


def _target_key(side: AbSide, entry: ReportPass) -> str:
    """A pass's identity by the resources it writes: its kind and target names, or '' when it has none.

    A compute pass has no targets (a dispatch does not set the output merger, `_targets_text` says so) and
    an unnamed resource contributes nothing, so a pass whose targets the capture did not name has no key
    here and is left to the other rules -- never matched on an empty set, which would pair every unnamed
    pass with every other.
    """
    if str(entry['kind']) == 'compute':
        return ''
    names = sorted({side.names['res%s' % _res_id(str(target).split()[0])]
                    for target in entry['targets'] if str(target).split()
                    and 'res%s' % _res_id(str(target).split()[0]) in side.names})
    if not names:
        return ''
    return '%s: %s' % (entry['kind'], ', '.join(names))


def annotate(text: str, names: Dict[str, str]) -> str:
    """Every `res<id>` in `text` gains `[Name]` when the bundle knows one -- and nothing else changes.

    A row that already carries a name, or an id the capture never named, is returned as it was: this is a
    reader's annotation, not a rewrite, so two rows that were equal before are equal after.
    """
    if not names or 'res' not in text:
        return text
    return _RES_ID.sub(lambda match: '%s[%s]' % (match.group(0), names[match.group(0)])
                       if match.group(0) in names else match.group(0), text)


def annotate_id(text: str, names: Dict[str, str]) -> str:
    """A bare resource id as `res<id>[Name]`, for the members that carry an id and no `res` prefix.

    `events.json` and the state document write the depth target as a number (`"0"`, `"2207"`), while the
    rows the state document's arrays carry spell it `res2207`. Reading them the same way is what lets a
    depth change be compared as a resource rather than as a pair of unrelated numbers -- and `0` stays
    `0`: it is the engine's null resource, not something called `res0`.
    """
    token = text.strip()
    if token.isdigit() and token != '0':
        return annotate('res' + token, names)
    return annotate(text, names)


def structure_changes(a: ReportPass, b: ReportPass, names_a: Dict[str, str],
                      names_b: Dict[str, str]) -> List[AbChange]:
    """What the two sides of an aligned pass disagree about, field by field.

    Resource ids in the targets are annotated with the name each side's capture gave them, because the
    ids themselves are capture-local and a bare `res2207` on the left against `res3312` on the right is
    exactly the shape of "different" that is not one. The eid *ranges* are carried by the row rather than
    compared: the two frames' numberings are unrelated.
    """
    fields = (('kind', str(a['kind']), str(b['kind'])),
              ('structure', str(a['structure']), str(b['structure'])),
              ('events', str(a['events']), str(b['events'])),
              ('graphics', str(a['graphics']), str(b['graphics'])),
              ('compute', str(a['compute']), str(b['compute'])),
              ('depth', annotate_id(str(a['depth']), names_a), annotate_id(str(b['depth']), names_b)),
              ('targets', annotate(', '.join(str(t) for t in a['targets']), names_a) or 'none',
               annotate(', '.join(str(t) for t in b['targets']), names_b) or 'none'))
    return [AbChange(field, left, right) for field, left, right in fields if left != right]


def _state_document(side: AbSide, eid: int) -> Optional[Dict[str, object]]:
    """The state document the bundle wrote for `eid`, or None when it wrote none.

    None is not "no state": a bundle written with `--since`/`--max-events` has state documents for the
    events it walked, and the caller says so rather than reporting an empty state as a difference.
    """
    documents = side.bundle['states'].get(str(eid))
    if not documents:
        return None
    state = documents.get('state')
    return state if isinstance(state, dict) else None


def _shaders_document(side: AbSide, eid: int) -> Optional[Dict[str, object]]:
    """The reflection document the bundle wrote for `eid`, or None when it wrote none."""
    documents = side.bundle['states'].get(str(eid))
    if not documents:
        return None
    shaders = documents.get('shaders')
    return shaders if isinstance(shaders, dict) else None


def state_rows(side: AbSide, eid: int, kind: str) -> List[str]:
    """One state document's rows of `kind` (`rootParameters`, `shaders`, `renderTargets`), annotated.

    The rows are the driver's own text (`rp1 reg=0 space=0 vis=vs|ps res2207`, `vs res2348`), which is
    what makes a difference readable: both sides print the same field names because they are the same
    command's output, so a row that moved, appeared or went missing is visible as itself.
    """
    document = _state_document(side, eid)
    if document is None:
        return []
    rows = document.get(kind)
    if not isinstance(rows, list):
        return []
    return [annotate(str(row), side.names) for row in rows]


def depth_row(side: AbSide, eid: int) -> str:
    """`depthTarget` as one annotated row, or '' when the event has no state document."""
    document = _state_document(side, eid)
    if document is None:
        return ''
    return 'depth %s' % annotate_id(str(document.get('depthTarget', '0')), side.names)


def constant_blocks(side: AbSide, eid: int) -> Dict[Tuple[str, int], Tuple[str, Dict[str, str]]]:
    """The constant-buffer documents the bundle wrote for `eid`, keyed by `(stage, slot)`.

    The driver's file name is `<eid>_<stage>_<slot>.json`, which is where the pairing key lives; the
    document's own `stage`/`slot` members are read and used, because a document whose name says one thing
    and whose contents say another is a damaged bundle rather than a value to compare.
    """
    found: Dict[Tuple[str, int], Tuple[str, Dict[str, str]]] = {}
    prefix = '%d_' % eid
    for name, document in side.bundle['cbuffers'].items():
        if not name.startswith(prefix) or not name.endswith('.json'):
            continue
        stage = str(document.get('stage', ''))
        # `_int_of` and not `int(... or -1)`: slot 0 is a real slot, and the `or` idiom turns it into the
        # "no slot" the line below throws away -- which is how every cbuffer of every pass went missing.
        slot = _int_of(document.get('slot'), -1)
        if not stage or slot < 0:
            continue
        found[(stage, slot)] = (name, _member_rows(document))
    return found


def _member_rows(document: Dict[str, object]) -> Dict[str, str]:
    """One cbuffer document's `variables` as `key -> row text`, the key carrying the nesting depth.

    The driver prints one member per line, indented by twice its depth in the reflection's tree, and an
    array element repeats its parent's name -- so the key is the indentation plus the name, and a repeat
    gets its ordinal appended. Nothing here parses a type or a value: the whole row is compared as text,
    which is what makes a value change a string difference rather than a numeric comparison this tool
    would have to interpret (and get wrong for a struct, a half or a bitfield).
    """
    rows: Dict[str, str] = {}
    counted: Dict[str, int] = {}
    for text in _rows_of(document.get('variables')):
        indent = len(text) - len(text.lstrip())
        body = text.strip()
        name = body.split(' = ', 1)[0] if ' = ' in body else body
        key = '%s%s' % (' ' * indent, name)
        counted[key] = counted.get(key, 0) + 1
        if counted[key] > 1:
            key = '%s#%d' % (key, counted[key])
        rows[key] = body
    return rows


def _block_label(file_name: str) -> str:
    """The `cbuffers/<eid>_<stage>_<slot>.json` name without the eid and the extension, for a row label."""
    stem = file_name[:-len('.json')] if file_name.endswith('.json') else file_name
    parts = stem.split('_')
    return '_'.join(parts[1:]) if len(parts) > 1 else stem


def compare_cbuffers(a: AbSide, b: AbSide, pass_a: ReportPass, pass_b: ReportPass) -> List[AbCbufferRow]:
    """Every constant block of the two passes' first state event, paired by `(stage, slot)`.

    A block both sides have is compared member by member; a block only one side has is reported with its
    members, because "this block is not bound over here" is the answer to a great many A/B questions and
    an empty comparison would hide it. The pass's *first* state event is the scope: a pass whose later
    events rebind its cbuffers is not compared further, and the caveats say so.
    """
    left = constant_blocks(a, int(pass_a['firstEid']))
    right = constant_blocks(b, int(pass_b['firstEid']))
    rows: List[AbCbufferRow] = []
    for stage, slot in sorted(set(left) | set(right)):
        key = (stage, slot)
        a_file, a_rows = left.get(key, ('', {}))
        b_file, b_rows = right.get(key, ('', {}))
        notes: List[str] = []
        values: List[AbValueRow] = []
        if a_file and not b_file:
            notes.append('only in A: nothing is bound to this block over here')
        elif b_file and not a_file:
            notes.append('only in B: nothing is bound to this block over here')
        for member in sorted(set(a_rows) | set(b_rows)):
            row_a, row_b = a_rows.get(member, ''), b_rows.get(member, '')
            if row_a != row_b:
                values.append(AbValueRow(member.strip(), row_a or '(not present)',
                                         row_b or '(not present)'))
        if a_file and b_file and not values:
            notes.append('every member the two documents share has the same value')
        rows.append(AbCbufferRow(_block_label(a_file or b_file), stage, slot, a_file, b_file, notes,
                                 values))
    return rows


def compare_shaders(a: AbSide, b: AbSide, pass_a: ReportPass, pass_b: ReportPass) -> List[AbShaderRow]:
    """The bound stages of the two passes' first state event, by identity and by reflection.

    The identity is the `hash` member the driver stamps every stage with (SHA-256 of the shader's own
    bytes), so "the same shader" is a fact. Where a bundle predates that member, the comparison falls back
    to the reflection rows and says which of the two it used: a hash mismatch is proof of a different
    shader, while equal reflection with no hash is only evidence.
    """
    left = _stages(a, int(pass_a['firstEid']))
    right = _stages(b, int(pass_b['firstEid']))
    rows: List[AbShaderRow] = []
    for stage in sorted(set(left) | set(right)):
        one, other = left.get(stage), right.get(stage)
        if one is None or other is None:
            rows.append(AbShaderRow(stage, 'only-in-a' if one else 'only-in-b', '', '', 0, 0, []))
            continue
        changes: List[AbChange] = []
        for field in ('entry', 'encoding', 'bytes'):
            value_a, value_b = str(one.get(field, '')), str(other.get(field, ''))
            if value_a != value_b:
                changes.append(AbChange(field, value_a, value_b))
        for field in ('constantBlocks', 'readOnlyResources', 'readWriteResources', 'inputSignature',
                      'outputSignature'):
            rows_a, rows_b = _rows_of(one.get(field)), _rows_of(other.get(field))
            for gone in [row for row in rows_a if row not in rows_b]:
                changes.append(AbChange(field, gone, '(not present)'))
            for new in [row for row in rows_b if row not in rows_a]:
                changes.append(AbChange(field, '(not present)', new))
        hash_a, hash_b = str(one.get('hash', '')), str(other.get('hash', ''))
        if hash_a and hash_b:
            verdict = 'same' if hash_a == hash_b else 'different'
        else:
            verdict = 'no-hash-in-this-bundle'
        rows.append(AbShaderRow(stage, verdict, hash_a, hash_b, _int_of(one.get('bytes')),
                                _int_of(other.get('bytes')), changes))
    return rows


def _rows_of(value: object) -> List[str]:
    """A document's array member as text rows: anything that is not a list is no rows at all."""
    return [str(row) for row in value] if isinstance(value, list) else []


def _int_of(value: object, default: int = 0) -> int:
    """An integer member of a document, or `default` when it does not hold one.

    A member read as an integer this way rather than through `int(...)`: the document is JSON, so a value
    that arrived as a string, a bool or a missing member must give the default instead of raising on the
    one hand or counting `True` as 1 on the other.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _stages(side: AbSide, eid: int) -> Dict[str, Dict[str, object]]:
    """The reflection document's stages at `eid`, keyed by stage name (`vs`, `ps`, `cs`, ...)."""
    document = _shaders_document(side, eid)
    if document is None:
        return {}
    stages = document.get('stages')
    if not isinstance(stages, list):
        return {}
    return {str(entry.get('stage', '')): entry for entry in stages if isinstance(entry, dict)}


def rt_files(side: AbSide) -> List[Tuple[int, int, str]]:
    """Every readback the bundle holds, as `(eid, slot, path)` sorted by eid -- `rt/<eid>_<slot>.png`.

    The directory is listed rather than probed slot by slot: `dump --with-images` writes a file per bound
    render target per state event, so the set is sparse, and asking "is there a slot 3 here" eight times
    per pass answers nothing that one listing does not. A name that is not `<eid>_<slot>.png` is not a
    readback this tool wrote, and is skipped rather than guessed at.
    """
    directory = os.path.join(side.directory, 'rt')
    found: List[Tuple[int, int, str]] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for name in names:
        if not name.endswith('.png'):
            continue
        eid_text, _, slot_text = name[:-4].partition('_')
        if eid_text.isdigit() and slot_text.isdigit():
            found.append((int(eid_text), int(slot_text), os.path.join(directory, name)))
    return sorted(found)


def last_readbacks(files: Sequence[Tuple[int, int, str]], first_eid: int,
                   last_eid: int) -> Dict[int, Tuple[int, str]]:
    """The last readback of each slot inside `[first_eid, last_eid]`: what the pass produced.

    The last one and not the first, because a pass's *first* state event is often one with no target
    bound at all (a compute dispatch, a barrier, or the same target re-set) -- the picture of the pass is
    at its last event that wrote one, which is the rule `sheet` uses for its own tiles.
    """
    out: Dict[int, Tuple[int, str]] = {}
    for eid, slot, path in files:
        if first_eid <= eid <= last_eid:
            out[slot] = (eid, path)     # sorted by eid, so the last one wins
    return out


def compare_images(a: AbSide, b: AbSide, files_a: Sequence[Tuple[int, int, str]],
                   files_b: Sequence[Tuple[int, int, str]], pass_a: ReportPass, pass_b: ReportPass,
                   path: str, out_dir: str, with_images: bool,
                   detail_left: List[int]) -> List[AbImageRow]:
    """The two passes' readbacks, slot by slot: the last one each side wrote inside its own range.

    Every pair is compared by the file's bytes first: both bundles were written by one encoder, so equal
    bytes *are* equal pixels, and that costs nothing. A pair that differs is decoded and compared pixel by
    pixel while `detail_left` lasts -- decoding is ~0.15 s per megabyte in Python, so the caller caps how
    many -- and the rest are named with the reason they were not compared. An image nobody looked at must
    never read as an image that did not change.

    **Measured: the decode is the cost, and it is not one a process pool helps with.** On this machine
    `replaydiff --with-images` is 9.6 s against 0.6 s at `--image-detail 0`, i.e. *one* 1080p pair is ~9 s;
    decoding one of its PNGs is ~6 s (198-452 KB of file for ~8 MB decoded, so 0.25-0.4 s per decoded
    megabyte, against the 0.15 s/MB the constant above was written from). A pool over the pairs was tried
    and removed: the default compares a single pair, so there was nothing to split, and at eight workers a
    multi-pair run measured *slower* (11.4 s against 9.9 s) -- the workers are memory-bandwidth bound and
    the spawn is not free. What would pay is moving the unfiltering into the C helpers the tool already
    ships (`bin/rdc_lz4.dll`, the way `_lz4_c_function` is used); that is a new kernel, and this comment is
    the measurement a reader needs to decide whether to write it.
    """
    if not with_images:
        return []
    left = last_readbacks(files_a, int(pass_a['firstEid']), int(pass_a['lastEid']))
    right = last_readbacks(files_b, int(pass_b['firstEid']), int(pass_b['lastEid']))
    rows: List[AbImageRow] = []
    for slot in sorted(set(left) | set(right)):
        eid_a, file_a = left.get(slot, (0, ''))
        eid_b, file_b = right.get(slot, (0, ''))
        row = AbImageRow(slot, '', eid_a, eid_b, rel(a.directory, file_a), rel(b.directory, file_b), 0, 0,
                         0, '0.000', '0.000', 0, 0, '', '')
        if not file_a or not file_b:
            rows.append(row._replace(
                verdict='only-in-a' if file_a else 'only-in-b',
                note='this pass wrote a readback on one side only (the other side has no target in this '
                     'slot, or its bundle was written without --with-images)'))
            continue
        if _same_bytes(file_a, file_b):
            rows.append(row._replace(verdict='identical'))
            continue
        if detail_left[0] <= 0:
            rows.append(row._replace(
                verdict='different',
                note='not compared pixel by pixel: --image-detail is used up (raise it to compare)'))
            continue
        detail_left[0] -= 1
        rows.append(_compare_pixels(row, file_a, file_b, out_dir, path, slot))
    return rows


def _compare_pixels(row: AbImageRow, file_a: str, file_b: str, out_dir: str, path: str,
                    slot: int) -> AbImageRow:
    """Decode two PNGs and return the row's difference numbers, plus a heat map when one was asked for."""
    try:
        image_a = rdc_image.read_png(file_a)
        image_b = rdc_image.read_png(file_b)
    except rdc_image.PngError as exc:
        return row._replace(verdict='unreadable', note=str(exc))
    if image_a.width != image_b.width or image_a.height != image_b.height:
        return row._replace(
            verdict='sizes-differ', width=image_a.width, height=image_a.height,
            note='a %dx%d image against a %dx%d one: a pixel difference is not defined across sizes'
                 % (image_a.width, image_a.height, image_b.width, image_b.height))
    compared = rdc_image.image_delta(image_a, image_b, heat=bool(out_dir))
    if compared is None:
        return row._replace(verdict='unreadable', note='the two images could not be compared')
    delta, heat = compared
    pixels = delta.pixels
    heat_map = ''
    note = ''
    if heat is not None:
        target = os.path.join(out_dir, 'heat', '%s_%d.png' % (slug(path), slot))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            rdc_image.write_png(target, heat)
            # Relative to the output directory, like the readbacks are to their bundles: an absolute path
            # in the prose is what makes two runs of the same command undiffable.
            heat_map = rel(out_dir, target)
        except (OSError, rdc_image.PngError) as exc:
            note = 'the heat map could not be written (%s)' % exc
    return row._replace(
        verdict='different', width=image_a.width, height=image_a.height, differing=delta.differing,
        percent_differing='%.3f' % (100.0 * delta.differing / pixels if pixels else 0.0),
        mean_delta='%.3f' % (delta.sum_delta / delta.differing if delta.differing else 0.0),
        max_delta=delta.max_delta,
        hash_distance=rdc_image.hash_distance(rdc_image.difference_hash(image_a),
                                              rdc_image.difference_hash(image_b)),
        heat_map=heat_map, note=note)


def _same_bytes(one: str, other: str) -> bool:
    """Whether two files hold the same bytes, read in blocks so a 40 MB image is not held twice."""
    try:
        if os.path.getsize(one) != os.path.getsize(other):
            return False
        with open(one, 'rb') as left, open(other, 'rb') as right:
            while True:
                block = left.read(1 << 16)
                if block != right.read(1 << 16):
                    return False
                if not block:
                    return True
    except OSError:
        return False


def rel(root: str, path: str) -> str:
    """`path` relative to `root` when it is inside it, else `path` as it is (never a wrong relation)."""
    try:
        inside = os.path.relpath(path, root)
    except ValueError:
        return path
    return os.path.join(os.path.basename(root), inside) if not inside.startswith('..') else path


def slug(text: str) -> str:
    """A file name for a pass: everything Windows refuses (`: < > " / \\ | ? *`), and whitespace, to `-`.

    The rule the driver's `sheet` uses for its own file names, so a pass's heat map and its tile in a
    contact sheet are recognisably the same pass. An empty or all-punctuation name becomes `pass`.
    """
    out = ''
    for char in text[:48]:
        out += '-' if (char.isspace() or char in ':< >"\\/|?*') else char
    out = out.rstrip('-. ')
    return out or 'pass'


def build_document(a: AbSide, b: AbSide, out_dir: str, with_images: bool,
                   image_detail: int) -> ReplayDiffDocument:
    """The whole comparison as one document: the two sides, the aligned passes and what differs in each.

    Deterministic by construction -- every list is sorted or in frame order, no timestamps, and every path
    relative to the bundle or the output directory it belongs to -- so two runs over the same bundles are
    byte-identical and can be diffed. That is what makes `--out` worth keeping: a tools A/B becomes a
    reviewable artefact rather than a transcript.
    """
    rows: List[AbPassMember] = []
    detail_left = [max(0, image_detail)]
    images_expected = False
    # The two bundles' readbacks, listed once: a pass is compared against its own range, so the same file
    # set is asked about 47 times and listing it per pass would be 47 directory walks for one answer.
    files_a, files_b = rt_files(a), rt_files(b)
    for aligned in align(a, b):
        if aligned.a is None or aligned.b is None:
            only = aligned.a if aligned.a is not None else aligned.b
            assert only is not None
            rows.append({
                'path': aligned.path, 'status': aligned.status, 'note': aligned.note, 'aFirstEid': 0,
                'bFirstEid': 0, 'structure': _structure_of(only), 'changes': [], 'stateRemoved': [],
                'stateAdded': [], 'shaders': [], 'cbuffers': [], 'images': []})
            continue
        pass_a, pass_b = aligned.a, aligned.b
        eid_a, eid_b = int(pass_a['firstEid']), int(pass_b['firstEid'])
        removed: List[str] = []
        added: List[str] = []
        for kind in ('rootParameters', 'shaders', 'renderTargets'):
            left, right = state_rows(a, eid_a, kind), state_rows(b, eid_b, kind)
            removed += ['%s: %s' % (kind, row) for row in left if row not in right]
            added += ['%s: %s' % (kind, row) for row in right if row not in left]
        depth_a, depth_b = depth_row(a, eid_a), depth_row(b, eid_b)
        if depth_a and depth_b and depth_a != depth_b:
            removed.append(depth_a)
            added.append(depth_b)
        images = compare_images(a, b, files_a, files_b, pass_a, pass_b, aligned.path, out_dir,
                                with_images, detail_left)
        images_expected = images_expected or bool(images)
        rows.append({
            'path': aligned.path, 'status': aligned.status, 'note': aligned.note, 'aFirstEid': eid_a,
            'bFirstEid': eid_b,
            'structure': '%s  |  %s' % (_structure_of(pass_a), _structure_of(pass_b)),
            'changes': [_change_member(change)
                        for change in structure_changes(pass_a, pass_b, a.names, b.names)],
            'stateRemoved': removed, 'stateAdded': added,
            'shaders': [_shader_member(row) for row in compare_shaders(a, b, pass_a, pass_b)],
            'cbuffers': [_cbuffer_member(row) for row in compare_cbuffers(a, b, pass_a, pass_b)],
            'images': [_image_member(row) for row in images]})

    summary: AbSummaryMember = {
        'same': sum(1 for row in rows if row['status'] == 'same'),
        'added': sum(1 for row in rows if row['status'] == 'added'),
        'removed': sum(1 for row in rows if row['status'] == 'removed'),
        'structureChanges': sum(1 for row in rows if row['changes']),
        'stateChanges': sum(1 for row in rows if row['stateRemoved'] or row['stateAdded']),
        'constantsChanged': sum(1 for row in rows for block in row['cbuffers'] if block['values']),
        'shadersDifferent': sum(1 for row in rows for shader in row['shaders']
                                if shader['verdict'] == 'different'),
        'imagesCompared': sum(1 for row in rows for image in row['images']
                              if image['verdict'] in ('identical', 'different')),
        'imagesNotCompared': sum(1 for row in rows for image in row['images']
                                 if image['note'].startswith('not compared')),
    }
    left_side, right_side = side_summary(a), side_summary(b)
    return {
        'schemaVersion': AB_SCHEMA_VERSION,
        'a': left_side,
        'b': right_side,
        'sameCapture': bool(left_side['captureSha256']
                            and left_side['captureSha256'] == right_side['captureSha256']),
        'withImages': with_images,
        'imagesFound': images_expected,
        'imageDetail': image_detail,
        'passes': rows,
        'summary': summary,
        'caveats': caveats(with_images, image_detail),
    }


def _change_member(change: AbChange) -> AbChangeMember:
    """One `AbChange` as the document's member."""
    return {'field': change.field, 'a': change.a, 'b': change.b}


def _shader_member(row: AbShaderRow) -> AbShaderMember:
    """One `AbShaderRow` as the document's member."""
    return {'stage': row.stage, 'verdict': row.verdict, 'aHash': row.a_hash, 'bHash': row.b_hash,
            'aBytes': row.a_bytes, 'bBytes': row.b_bytes,
            'changes': [_change_member(change) for change in row.changes]}


def _cbuffer_member(row: AbCbufferRow) -> AbCbufferMember:
    """One `AbCbufferRow` as the document's member."""
    return {'block': row.block, 'stage': row.stage, 'slot': row.slot, 'aFile': row.a_file,
            'bFile': row.b_file, 'notes': row.notes,
            'values': [{'member': value.member, 'a': value.a, 'b': value.b} for value in row.values]}


def _image_member(row: AbImageRow) -> AbImageMember:
    """One `AbImageRow` as the document's member."""
    return {'slot': row.slot, 'verdict': row.verdict, 'aEid': row.a_eid, 'bEid': row.b_eid,
            'aFile': row.a_file, 'bFile': row.b_file,
            'width': row.width, 'height': row.height, 'differing': row.differing,
            'percentDiffering': row.percent_differing, 'meanDelta': row.mean_delta,
            'maxDelta': row.max_delta, 'hashDistance': row.hash_distance, 'heatMap': row.heat_map,
            'note': row.note}


def _structure_of(pass_entry: ReportPass) -> str:
    """One pass's shape as one line: kind, structure, events, its own eid range."""
    return '%s, %s, %d event(s), eids %d..%d' % (pass_entry['kind'], pass_entry['structure'],
                                                 pass_entry['events'], pass_entry['firstEid'],
                                                 pass_entry['lastEid'])


def caveats(with_images: bool, image_detail: int) -> List[str]:
    """What this comparison cannot say, in the document rather than in a reader's head.

    The contract the report's own caveats follow: a document that lists only findings cannot be told
    apart from one that did not look, so what was not looked at is written down.
    """
    lines = [
        'Both frames were replayed on the machine that wrote the bundles, whatever they were captured '
        "on: a mobile capture replayed on a desktop GPU answers with that GPU's behaviour.",
        "A resource id is capture-local: the two sides' ids are unrelated numberings, so rows are "
        "compared as text with each side's own names attached, never by id.",
        'A bundle holds no shader source: a shader that differs only in its body is caught by its hash, '
        'but nothing here can say what it does.',
        "Passes are compared at their first state event: a constant buffer rebound later inside a pass "
        'is not compared (the bundle writes one cbuffer document per state event, and this reads the '
        "pass's first).",
        'A pass only one side has is reported as added or removed, not as changed: two frames need not '
        'contain the same passes at all.',
    ]
    if with_images:
        lines.append('Only the first %d differing image pairs are decoded and compared pixel by pixel; '
                     'the rest are named as differing, which is exact but does not say by how much.'
                     % image_detail)
    else:
        lines.append('The renders themselves are not compared: pass --with-images (and write the '
                     "bundles with it) to compare each pass's readback.")
    return lines


def cmd_replaydiff(bundle_a: str, bundle_b: str, args: Sequence[str] = ()) -> int:
    """`replaydiff <bundleA> <bundleB> [--out <dir>] [--with-images] [--image-detail N] [--threshold N]`.

    Two bundles in, `replaydiff.md` and `replaydiff.json` out (`--out` defaults to `replaydiff`), plus a
    summary on stdout. `--threshold` is a percentage: images that differ by less are counted in the
    summary rather than listed row by row, which is what keeps "which passes changed" readable on a frame
    where every pass changed a little.

    Exit codes: 0 = compared (differences and all), 1 = a bundle could not be read or written, 2 = a bad
    option.
    """
    out_dir = 'replaydiff'
    with_images = False
    image_detail = IMAGE_DETAIL
    threshold = 0.0
    index = 0
    while index < len(args):
        option = args[index]
        if option == '--out' and index + 1 < len(args):
            index += 1
            out_dir = args[index]
        elif option == '--with-images':
            with_images = True
        elif option == '--image-detail' and index + 1 < len(args):
            index += 1
            try:
                image_detail = int(args[index])
            except ValueError:
                print('replaydiff: --image-detail takes a number, not %r' % args[index])
                return 2
        elif option == '--threshold' and index + 1 < len(args):
            index += 1
            try:
                threshold = float(args[index])
            except ValueError:
                print('replaydiff: --threshold takes a percentage, not %r' % args[index])
                return 2
        else:
            print('usage: rdc_analysis.py replaydiff <bundleA> <bundleB> [--out <dir>] [--with-images] '
                  '[--image-detail N] [--threshold N]')
            return 2
        index += 1

    try:
        side_a, side_b = load_side(bundle_a), load_side(bundle_b)
    except BundleError as exc:
        print('error: %s' % exc)
        return 1

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        print('error: cannot write into %s (%s)' % (out_dir, exc))
        return 1

    document = build_document(side_a, side_b, out_dir, with_images, image_detail)
    markdown = rdc_ab_render.render_replaydiff(document, threshold)
    markdown_path = os.path.join(out_dir, 'replaydiff.md')
    json_path = os.path.join(out_dir, 'replaydiff.json')
    try:
        with open(markdown_path, 'w', encoding='utf-8', newline='') as handle:
            handle.write(markdown)
        with open(json_path, 'w', encoding='utf-8', newline='') as handle:
            json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write('\n')
    except OSError as exc:
        print('error: cannot write the documents into %s (%s)' % (out_dir, exc))
        return 1

    summary = document['summary']
    for side, label in ((side_a, 'A'), (side_b, 'B')):
        print('%-10s: %s (%s, %d event(s), %d pass(es), %d resource(s))'
              % (label, side.directory, side_summary(side)['capture'], len(side.bundle['events']),
                 len(side.passes), side_summary(side)['resources']))
    if document['sameCapture']:
        print('capture   : the same capture on both sides (one sha256): this is a tools A/B')
    print('passes    : %s in both, %s only in A, %s only in B; %s structure / %s state / %s constant-'
          'block / %s shader change(s)'
          % (summary['same'], summary['removed'], summary['added'], summary['structureChanges'],
             summary['stateChanges'], summary['constantsChanged'], summary['shadersDifferent']))
    if with_images:
        print('images    : %s pair(s) compared, %s left to identity (raise --image-detail to decode more)'
              % (summary['imagesCompared'] - summary['imagesNotCompared'], summary['imagesNotCompared']))
    print('written   : %s' % markdown_path)
    print('written   : %s' % json_path)
    return 0


__all__ = [
    'AB_SCHEMA_VERSION',
    'IMAGE_DETAIL',
    'AbCbufferMember',
    'AbCbufferRow',
    'AbChange',
    'AbChangeMember',
    'AbImageMember',
    'AbImageRow',
    'AbPassMember',
    'AbPassRow',
    'AbRow',
    'AbShaderMember',
    'AbShaderRow',
    'AbSide',
    'AbSideMember',
    'AbSummaryMember',
    'AbValueMember',
    'AbValueRow',
    'ReplayDiffDocument',
    'align',
    'annotate',
    'annotate_id',
    'build_document',
    'caveats',
    'cmd_replaydiff',
    'compare_cbuffers',
    'compare_images',
    'compare_shaders',
    'constant_blocks',
    'depth_row',
    'last_readbacks',
    'load_side',
    'pass_markers',
    'rel',
    'rt_files',
    'side_summary',
    'slug',
    'state_rows',
    'structure_changes',
]
