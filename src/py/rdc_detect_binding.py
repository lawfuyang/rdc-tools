"""The binding detectors: a register nothing serves, a slot whose declaration and heap disagree, and a signature the pair of stages does not match."""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403
from rdc_detect_common import *  # noqa: F401,F403


import re

from typing import Any, Dict, List, Optional, Tuple

CONSTANT_BLOCK_ROW = re.compile(r'\bb(?P<reg>\d+)\s+s(?P<space>\d+)\b')

#: A root parameter as `state` writes it: `rp2   reg=0 space=0 vis=ps res1907`,
#: `rp0   reg=0 space=0 vis=cs heap298+0x21cde` for a table, or -- for one that is *not set to anything* --
#: `rp1   reg=0 space=0 vis=ps` and nothing more. `vis=` is optional on purpose: a bundle written by an
#: earlier driver has no visibility, and its rows are then taken as serving every stage, which is what the
#: tool assumed before the token existed. The visibility is not decoration -- measured on `PC Renderer.rdc`
#: at eid 640, the vertex and pixel shaders both declare `t0`..`t4` and each is served by its own table, so
#: a row without it cannot be matched against the reflection.
ROOT_PARAMETER_ROW = re.compile(r'^rp(?P<param>\d+)\s+reg=(?P<reg>\d+)\s+space=(?P<space>\d+)'
                                r'(?:\s+vis=(?P<vis>[a-z+?]+))?(?:\s+(?P<target>\S+))?\s*$')

#: A resolved table slot as `state` writes it since the driver resolves tables:
#: `rp0   t3  s0   cat(3) type(4) res2233` — the parameter it came from, the register letter and number the
#: range maps it to, the space, the range's *category* (`cat`) and the heap slot's own `DescriptorType`
#: (`type`); `none` is an empty slot. The letter is the category, so the reflection can be matched by
#: letter; `type` is what the heap actually holds, which is what the mismatch rule compares `cat` against.
#: `type(N)` is optional: a bundle written by the driver before that token existed parses, and its rows are
#: not compared (the comparison needs both numbers).
TABLE_SLOT_ROW = re.compile(r'^rp(?P<param>\d+)\s+(?P<letter>[btsu])(?P<reg>\d+)\s+s(?P<space>\d+)\s+'
                            r'cat\((?P<category>\d+)\)(?:\s+type\((?P<type>\d+)\))?\s+'
                            r'(?P<resource>none|res\d+)\s*$')

#: `DescriptorType` -> `DescriptorCategory`, mirroring the engine's own `CategoryForDescriptorType`
#: (`renderdoc-src/renderdoc/api/replay/replay_enums.h`): a heap slot's type and the range's declared
#: category are the two sides of the mismatch rule, and the engine's mapping is what relates them.
CATEGORY_FOR_TYPE = {
    0: 0,        # Unknown
    1: 1,        # ConstantBuffer
    2: 2,        # Sampler
    3: 3,        # ImageSampler
    4: 3,        # Image
    5: 3,        # Buffer
    6: 3,        # TypedBuffer
    7: 4,        # ReadWriteImage
    8: 4,        # ReadWriteTypedBuffer
    9: 4,        # ReadWriteBuffer
    10: 3,       # AccelerationStructure
}

#: A resource binding as the reflection writes it: `TransmittanceLutTexture t0 s0 n1`,
#: `SkyAtmosphere.SkyViewLut u3 s0 n1`.
RESOURCE_BINDING_ROW = re.compile(r'^\s*(?P<name>\S+)\s+(?P<letter>[btu])(?P<reg>\d+)\s+s(?P<space>\d+)\s+n\d+\s*$')

#: What a register letter means in a finding's text (with the article, so sentences read as sentences).
REGISTER_KINDS = {'b': 'a constant block', 's': 'a sampler', 't': 'a read-only resource',
                  'u': 'a read-write resource'}

#: The `DescriptorCategory` a register letter stands for, mirroring the driver's `RegisterLetter`.
CATEGORY_FOR_LETTER = {'b': 1, 's': 2, 't': 3, 'u': 4}

#: What a `DescriptorType` number means on a slot row, for finding text (the names come from the enum in
#: `api/replay/replay_enums.h`; the engine's own stringiser is not reachable from either tool).
DESCRIPTOR_TYPES = {0: 'nothing', 1: 'a constant buffer view', 2: 'a sampler',
                    3: 'a combined image sampler', 4: 'an image', 5: 'a buffer', 6: 'a typed buffer',
                    7: 'a read-write image', 8: 'a read-write typed buffer', 9: 'a read-write buffer',
                    10: 'an acceleration structure'}

def _table_bindings(state: Any) -> Tuple[Dict[int, Tuple[str, int, int, str]],
                                         Dict[Tuple[int, int], List[Tuple[str, int, Optional[int], str, str]]]]:
    """A state document's root parameters and its resolved table slots.

    `parameters[param]` is `(visibility, reg, space, row)` -- visibility is the `vis=` text, or `'all'` when
    the bundle predates the token. `slots[(reg, space)]` is every resolved slot row at that register, as
    `(letter, param, type, resource, row)`, across *all* letters: the register locates a row, the letter is
    what the reflection is matched against, and the type is what the mismatch rule compares the range's
    category with (`None` when the bundle's rows predate the token).
    """
    parameters: Dict[int, Tuple[str, int, int, str]] = {}
    slots: Dict[Tuple[int, int], List[Tuple[str, int, Optional[int], str, str]]] = {}
    for row in state.get('rootParameters', []):
        text = str(row)
        slot = TABLE_SLOT_ROW.match(text)
        if slot:
            where = (int(slot.group('reg')), int(slot.group('space')))
            kind = slot.group('type')
            slots.setdefault(where, []).append((slot.group('letter'), int(slot.group('param')),
                                                int(kind) if kind is not None else None,
                                                slot.group('resource'), text))
            continue
        parameter = ROOT_PARAMETER_ROW.search(text)
        if parameter:
            parameters[int(parameter.group('param'))] = (parameter.group('vis') or 'all',
                                                         int(parameter.group('reg')),
                                                         int(parameter.group('space')), text)
    return parameters, slots

def _declared_bindings(stage: Any) -> List[Tuple[str, int, int, str]]:
    """`(letter, reg, space, name)` for what a stage's reflection declares: a constant block reads `bR sS`
    and is named `cbuffer[0] $Globals ...`, a resource reads `NAME tR sS nN` or `NAME uR sS nN`."""
    declared: List[Tuple[str, int, int, str]] = []
    for row in stage.get('constantBlocks', []):
        block = CONSTANT_BLOCK_ROW.search(str(row))
        if block:
            parts = str(row).split()
            declared.append(('b', int(block.group('reg')), int(block.group('space')),
                             parts[1] if len(parts) > 1 else '?'))
    for key in ('readOnlyResources', 'readWriteResources'):
        for row in stage.get(key, []):
            match = RESOURCE_BINDING_ROW.match(str(row))
            if match:
                declared.append((match.group('letter'), int(match.group('reg')),
                                 int(match.group('space')), match.group('name')))
    return declared

def _slots_visible_to(parameters: Dict[int, Tuple[str, int, int, str]],
                      slots: Dict[Tuple[int, int], List[Tuple[str, int, Optional[int], str, str]]],
                      stage_name: str, reg: int, space: int) -> List[Tuple[str, str, str]]:
    """The resolved slot rows at `(reg, space)` whose parameter is visible to this stage, as
    `(letter, resource, row)`. `all` -- and a bundle with no `vis=` at all -- serves every stage."""
    visible: List[Tuple[str, str, str]] = []
    for letter, param, _kind, resource, row in slots.get((reg, space), []):
        visibility = parameters.get(param, ('all', reg, space, ''))[0]
        if visibility != 'all' and stage_name not in visibility.split('+'):
            continue
        visible.append((letter, resource, row))
    return visible

def detect_unbound_root_parameters(bundle: BundleData) -> List[RedFlag]:
    """A root parameter that is not set to anything (the root-descriptor half).

    `shaders` says the stage reads a block at `bR sS`, and `state` says what the root parameter at
    `reg=R space=S` holds -- nothing, when the row ends at the register. Measured on the Android capture,
    where the driver's own note ("none bound as a root descriptor") says the same thing from the other side,
    so this is a detector that agrees with the engine rather than guessing at it.

    `question`, not `certain`, for one reason: a shader register can also be served by *root constants*,
    which are bound by value and print with no resource either. The root signature would say which it is, and
    a bundle does not carry the signature's contents -- so the finding is the observation, and the other
    possibility is named in it. A register with *no* root parameter row at all is not reported: a descriptor
    table whose range covers that register looks exactly the same from here (and the table half below is what
    covers that case).
    """
    flags: List[RedFlag] = []
    groups: Dict[Tuple[str, int, int], List[int]] = {}
    for key in sorted(bundle['states']):
        documents = bundle['states'][key]
        state, shaders = documents.get('state'), documents.get('shaders')
        if not isinstance(state, dict) or not isinstance(shaders, dict):
            continue

        unset: Dict[Tuple[int, int], bool] = {}
        for row in state.get('rootParameters', []):
            match = ROOT_PARAMETER_ROW.search(str(row))
            if match:
                unset[(int(match.group('reg')), int(match.group('space')))] = match.group('target') is None

        for stage in shaders.get('stages', []):
            for row in stage.get('constantBlocks', []):
                block = CONSTANT_BLOCK_ROW.search(str(row))
                if not block:
                    continue
                where = (int(block.group('reg')), int(block.group('space')))
                if unset.get(where) is True:
                    eid = int(state.get('eid', 0) or 0)
                    groups.setdefault((str(stage.get('stage', '?')), where[0], where[1]), []).append(eid)

    for key in sorted(groups):
        eids = sorted(groups[key])
        flags.append({
            'detector': 'unbound-root-parameter',
            'what': 'the shader reads a constant block at b%d s%d, and the root parameter there is not set to '
                    'a resource -- root constants are the other way that register can be served, and the '
                    'bundle does not say which' % (key[1], key[2]),
            'evidence': ['%s stage, eid %d..%d' % (key[0], eids[0], eids[-1])],
            'certainty': 'question',
            'unproven': True,
        })
    return flags

def detect_unbound_table_slots(bundle: BundleData) -> List[RedFlag]:
    """A descriptor table that resolves a register the shader reads to nothing (the table half,
    `certain`).

    The driver resolves every *set* table's slots through the engine (`GetDescriptors`), so a row like
    `rp0   t3  s0   cat(3) none` is the engine saying what is in that slot: nothing. A null descriptor reads
    as zeros, so the stage reads zeros rather than data -- and unlike the root-descriptor half above there is
    no second explanation to weigh, which is why this one is certain.

    Only rows from a parameter *visible* to the reading stage are matched: measured at `PC Renderer.rdc`
    eid 640, the vertex and pixel shaders both declare `t0`..`t4`, each served by its own table. A table that
    was never set prints no slot rows at all, so this rule stays silent there and the half above keeps its
    own, weaker finding instead.
    """
    flags: List[RedFlag] = []
    empty: Dict[Tuple[str, str, int, int], Dict[str, Any]] = {}
    for key in sorted(bundle['states']):
        documents = bundle['states'][key]
        state, shaders = documents.get('state'), documents.get('shaders')
        if not isinstance(state, dict) or not isinstance(shaders, dict):
            continue
        parameters, slots = _table_bindings(state)
        if not slots:
            continue
        for stage in shaders.get('stages', []):
            stage_name = str(stage.get('stage', '?'))
            eid = int(state.get('eid', 0) or 0)
            for letter, reg, space, name in _declared_bindings(stage):
                rows = _slots_visible_to(parameters, slots, stage_name, reg, space)
                same = [one for one in rows if one[0] == letter]
                if not same or any(one[1] != 'none' for one in same):
                    continue
                key2 = (stage_name, letter, reg, space)
                entry = empty.setdefault(key2, {'eids': [], 'name': name, 'rows': []})
                entry['eids'].append(eid)
                entry['rows'] = sorted({one[2] for one in same})

    for key in sorted(empty):
        entry = empty[key]
        eids = sorted(entry['eids'])
        letter, reg, space = key[1], key[2], key[3]
        flags.append({
            'detector': 'unbound-table-slot',
            'what': 'the %s stage reads %s at %s%d s%d and the descriptor table bound there resolves the '
                    'slot to nothing -- a null descriptor, so the read yields zeros rather than data'
                    % (key[0], entry['name'], letter, reg, space),
            'evidence': ['eid %d..%d' % (eids[0], eids[-1])] + entry['rows'],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags

def detect_binding_kind_mismatch(bundle: BundleData) -> List[RedFlag]:
    """The root signature and the descriptor heap disagree about a slot (certain).

    Every resolved slot row carries two numbers, and they are different statements: `cat(N)` is the
    *range's* category -- what the root signature declares at that register -- and `type(N)` is the heap
    slot's own `DescriptorType`, what was actually written there. `CategoryForDescriptorType` relates them,
    so a slot the signature declares a constant buffer and the heap holds an image in is decidable from the
    row alone, with the engine as the source of both sides.

    One reading of the roadmap row needs a fact a bundle does not carry: whether the shader declared a
    texture or a buffer (the reflection rows give a name, a register and a space, and nothing else), so a
    "resource of the wrong type in the right register space" is *not* reported. And a `t` register is never
    served by a `b` range -- D3D12 numbers those spaces separately -- so matching the reflection's letter
    against a *different* letter's row is not a mismatch at all: that was the first draft of this rule, and
    a real capture's 60-odd false positives killed it. What is compared here is range against heap, for the
    registers the reflection actually reads (a mismatch where nothing is read is not this report's business).
    """
    flags: List[RedFlag] = []
    groups: Dict[Tuple[str, str, int, int, int, int], Dict[str, Any]] = {}
    for key in sorted(bundle['states']):
        documents = bundle['states'][key]
        state, shaders = documents.get('state'), documents.get('shaders')
        if not isinstance(state, dict) or not isinstance(shaders, dict):
            continue
        parameters, slots = _table_bindings(state)
        for stage in shaders.get('stages', []):
            stage_name = str(stage.get('stage', '?'))
            eid = int(state.get('eid', 0) or 0)
            for letter, reg, space, name in _declared_bindings(stage):
                for row_letter, param, kind, resource, row in slots.get((reg, space), []):
                    if row_letter != letter or kind is None or resource == 'none':
                        continue                    # another register space, no type token, or an empty slot
                    visibility = parameters.get(param, ('all', reg, space, ''))[0]
                    if visibility != 'all' and stage_name not in visibility.split('+'):
                        continue
                    declared, held = CATEGORY_FOR_LETTER[letter], CATEGORY_FOR_TYPE.get(kind, 0)
                    if held == 0 or held == declared:
                        continue                    # nothing there, or the two agree
                    key2 = (stage_name, letter, reg, space, declared, kind)
                    entry = groups.setdefault(key2, {'eids': [], 'name': name, 'rows': set()})
                    entry['eids'].append(eid)
                    entry['rows'].add(row)

    for key in sorted(groups):
        entry = groups[key]
        eids = sorted(entry['eids'])
        stage_name, letter, reg, space, declared, kind = key
        flags.append({
            'detector': 'binding-kind-mismatch',
            'what': 'the %s stage reads %s at %s%d s%d, and the range bound there is declared %s while the '
                    'heap holds %s -- the root signature and the descriptor disagree'
                    % (stage_name, entry['name'], letter, reg, space,
                       REGISTER_KINDS.get(letter, letter), DESCRIPTOR_TYPES.get(kind, str(kind))),
            'evidence': ['eid %d..%d' % (eids[0], eids[-1])] + sorted(entry['rows']),
            'certainty': 'certain',
            'unproven': True,
        })
    return flags

#: A signature row as the reflection writes it: `<SEMANTIC><index> reg<N> [c<M>] [<type>]` —
#: `SV_Position0 reg4`, `TEXCOORD9 reg3 c3`, `TEXCOORD10_centroid0 reg0 c4 float`. The `cN` is the component
#: count the engine reports (`SigParameter::compCount`) and the type is the engine's `VarType` name; both are
#: *optional*, because a bundle written by an older driver has neither and such a row is compared for name
#: and index only rather than guessed at. The semantic carries its index, and HLSL's interpolation modifier
#: (`_centroid`, `_linear`, `_nointerpolation`) can be on either side or on both, which is why the comparison
#: below tries the name with and without it rather than keeping a list of modifiers to strip.
SIGNATURE_ROW = re.compile(r'^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*?)(?P<index>\d+)\s+reg\d+'
                           r'(?:\s+c(?P<count>\d+))?(?:\s+(?P<type>[A-Za-z_][A-Za-z0-9_]*))?\s*$')

def _semantic(row: Any) -> Optional[Tuple[str, int, Optional[int], str]]:
    """`(name, index, components, type)` for a signature row, or None for a row this rule does not read. The
    count is None when the row does not carry one (not the same as a count of zero) and the type is `''`.
    The type is a *component* type -- `float`, `uint` -- and it is what the format rule needs; it is not a
    format, and the row is still the evidence either way."""
    match = SIGNATURE_ROW.match(str(row))
    if match is None:
        return None
    count = match.group('count')
    return (match.group('name'), int(match.group('index')),
            int(count) if count is not None else None, match.group('type') or '')

def _pair(semantic: Tuple[str, int, Optional[int], str]) -> Tuple[str, int]:
    """Name and index, which is what "the same semantic" means here. The width is a separate question and
    is compared separately, because reading *fewer* components than the producer writes is legal."""
    return semantic[0], semantic[1]

def _semantic_matches(produced: Tuple[str, int], consumed: Tuple[str, int]) -> bool:
    """Whether one side's semantic serves the other's, ignoring an interpolation suffix on either.

    `TEXCOORD10_centroid0` and `TEXCOORD10` are the same semantic -- measured on `PC Renderer.rdc` at eid 700,
    where the vertex shader emits `TEXCOORD10_centroid0` and the pixel shader reads exactly that, so the
    suffix is not what distinguishes them. The suffix is only removed when the base name matches the other
    side, so this can never invent a match that is not there.
    """
    if produced == consumed:
        return True
    for side, other in ((produced, consumed), (consumed, produced)):
        base, _, _suffix = side[0].rpartition('_')
        if base and (base, side[1]) == other:
            return True
    return False

def detect_shader_io_mismatch(bundle: BundleData) -> List[RedFlag]:
    """A pixel shader input the vertex shader does not provide (certain).

    Both reflections are in the same document, so this needs no capture, and it answers both halves of the
    row *VS out is not PS in*:

    * **A semantic the vertex shader does not emit at all.** D3D12 requires every PS input to be produced by
      the preceding stage, and the only exceptions are the `SV_` system values, which the rasteriser supplies
      (`SV_IsFrontFace` in the measured case -- the one input of six that the vertex shader does not emit, and
      the reason the rule ignores `SV_` on both sides). A vertex shader emitting *more* than the pixel shader
      reads is legal and never reported.
    * **The same semantic at a greater width.** The driver's rows carry the engine's component count
      (`c4`, `c3`, `c1`; measured on `PC Renderer.rdc`, where `TEXCOORD9` is c3 and `SV_Position0` is c4), and
      an input that reads *more* components of a semantic than its producer writes cannot be satisfied at
      pipeline creation. Reading *fewer* is a legal prefix subset and stays silent. The component *type*
      (float against uint) is not in the row, so a type-level mismatch is not reported: the row says what it
      says, and the row is the evidence.

    A geometry, hull or domain shader between them can change the signature, so an event that has one is
    skipped rather than reported: the rule compares adjacent stages, and those are not adjacent.
    """
    flags: List[RedFlag] = []
    missing: Dict[Tuple[str, str, str], List[int]] = {}
    narrow: Dict[Tuple[str, str, str, int, int], Dict[str, Any]] = {}
    for key in sorted(bundle['states']):
        shaders = bundle['states'][key].get('shaders')
        if not isinstance(shaders, dict):
            continue
        stages = {str(stage.get('stage')): stage for stage in shaders.get('stages', [])}
        if 'vs' not in stages or 'ps' not in stages:
            continue
        if any(other in stages for other in ('gs', 'hs', 'ds')):
            continue
        eid = int(shaders.get('eid', 0) or 0)
        vs_entry = str(stages['vs'].get('entry', '?'))
        ps_entry = str(stages['ps'].get('entry', '?'))

        produced = [(row, raw) for row, raw in
                    ((_semantic(r), str(r)) for r in stages['vs'].get('outputSignature', []))
                    if row is not None and not row[0].startswith('SV_')]
        for row in stages['ps'].get('inputSignature', []):
            consumed = _semantic(row)
            if consumed is None or consumed[0].startswith('SV_'):
                continue
            # An exact name match wins over a suffix match: if the vertex shader writes both `TEXCOORD9`
            # and `TEXCOORD9_centroid`, the plain one is what a plain `TEXCOORD9` input is compared with.
            matches = [one for one in produced if _semantic_matches(_pair(one[0]), _pair(consumed))]
            exact = [one for one in matches if one[0][0] == consumed[0]]
            match = exact[0] if exact else (matches[0] if matches else None)
            if match is None:
                missing.setdefault((vs_entry, ps_entry, '%s%d' % (consumed[0], consumed[1])),
                                   []).append(eid)
                continue
            if match[0][2] is not None and consumed[2] is not None and consumed[2] > match[0][2]:
                key2 = (vs_entry, ps_entry, '%s%d' % (consumed[0], consumed[1]),
                        match[0][2], consumed[2])
                entry = narrow.setdefault(key2, {'eids': [], 'vs': match[1], 'ps': str(row)})
                entry['eids'].append(eid)

    for key in sorted(missing):
        eids = sorted(missing[key])
        flags.append({
            'detector': 'shader-io-mismatch',
            'what': 'the pixel shader reads %s and the vertex shader does not emit it -- D3D12 has no other '
                    'source for it than the preceding stage' % key[2],
            'evidence': ['vs %s -> ps %s, eid %d..%d' % (key[0], key[1], eids[0], eids[-1])],
            'certainty': 'certain',
            'unproven': True,
        })
    for key in sorted(narrow):
        entry = narrow[key]
        eids = sorted(entry['eids'])
        flags.append({
            'detector': 'shader-io-mismatch',
            'what': 'the pixel shader reads %s at width c%d and the vertex shader writes it at c%d: an input '
                    'may use fewer components than its producer writes, never more' % (key[2], key[4], key[3]),
            'evidence': ['vs %s -> ps %s, eid %d..%d' % (key[0], key[1], eids[0], eids[-1]),
                         'vs writes: %s' % entry['vs'], 'ps reads: %s' % entry['ps']],
            'certainty': 'certain',
            'unproven': True,
        })
    return flags

__all__ = [
    'CATEGORY_FOR_LETTER',
    'CATEGORY_FOR_TYPE',
    'CONSTANT_BLOCK_ROW',
    'DESCRIPTOR_TYPES',
    'REGISTER_KINDS',
    'RESOURCE_BINDING_ROW',
    'ROOT_PARAMETER_ROW',
    'SIGNATURE_ROW',
    'TABLE_SLOT_ROW',
    '_declared_bindings',
    '_pair',
    '_semantic',
    '_semantic_matches',
    '_slots_visible_to',
    '_table_bindings',
    'detect_binding_kind_mismatch',
    'detect_shader_io_mismatch',
    'detect_unbound_root_parameters',
    'detect_unbound_table_slots',
]
