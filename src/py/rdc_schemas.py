"""The JSON contract: the validator for the driver's schemas, and `validate`.

The driver publishes the schema for each `--json` document (`replay_dump schema --out schema`,
checked in as `schema/`); this module is the other half -- reading real documents and checking them
against those files -- and it is the half that keeps a schema honest. Split out of `rdc_analysis.py`
when that file passed 3000 lines; `rdc_analysis.py validate` is still the entry point, and it
re-exports everything here.

The keyword subset is deliberate: `type`, `required`, `properties`, `items`, `enum`, `const` and
`additionalProperties` and nothing else, so an unimplemented keyword is reported rather than
silently skipped.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Schema validation (`validate`).
#
# The driver stamps `schemaVersion` into every `--json` document and publishes the schema for each kind:
# `replay_dump schema --out schema` writes the 15 files in `schema/`, which are checked in. This is the
# other half of that contract -- reading real documents and checking them against those files -- and it is
# the half that keeps a schema honest, because a schema that has drifted from its writer fails a run here
# instead of misleading a reader later.
#
# The validator implements the subset the schemas are written in -- type, required, properties, items,
# enum, const, additionalProperties -- and *nothing else*. That is deliberate: a keyword it skipped
# silently would make "validated" mean less than it sounds, so an unimplemented keyword is reported as a
# failure rather than ignored, and the driver's schema table stays inside this subset.
SCHEMA_KEYWORDS = frozenset(('title', 'description', 'type', 'required', 'properties', 'items', 'enum',
                             'const', 'additionalProperties'))

def _obj(properties: Dict[str, Any], required: Optional[List[str]] = None,
         extra: bool = False) -> Dict[str, Any]:
    """An object schema: its members, which of them are required, and whether strangers are allowed.

    `extra` is False everywhere in this module on purpose -- the driver's schemas make the same choice, and it
    is the one that catches a member that was renamed or dropped on one side of a change.
    """
    return {'type': 'object', 'properties': properties, 'additionalProperties': extra,
            'required': required if required is not None else sorted(properties)}

def _arr(items: Dict[str, Any]) -> Dict[str, Any]:
    """An array schema; `items` may be an object schema or a bare type."""
    return {'type': 'array', 'items': items}

def _text() -> Dict[str, Any]:
    return {'type': 'string'}

def _num() -> Dict[str, Any]:
    return {'type': 'number'}

#: The **report's** own schema. It is here rather than in `schema/` because that folder is what the driver
#: publishes (`schema --out`, checked by `schema --check`) and the driver does not write this document -- the
#: offline tool does. `validate <report.json> <schemaDir> report` uses it, and so does a bundle-wide
#: `validate <bundleDir> <schemaDir>`, which is what keeps the report's JSON twin honest: a member that a
#: change dropped or renamed fails a run instead of going unnoticed. `bundle` is left open on purpose: it is
#: the bundle's manifest, which has its own schema (`manifest.schema.json`) and is checked there.
REPORT_SCHEMA: Dict[str, Any] = _obj({
    'schemaVersion': {'const': 1},
    'reportVersion': {'type': 'integer'},
    'capture': _text(),
    'captureSha256': _text(),
    'bundleDir': _text(),
    'bundle': {'type': 'object'},
    'frame': _obj({
        'events': {'type': 'integer'},
        'graphicsEvents': {'type': 'integer'},
        'computeEvents': {'type': 'integer'},
        'resources': {'type': 'integer'},
        'resourcesByKind': {'type': 'object'},
        'textureBytes': {'type': 'integer'},
        'bufferBytes': {'type': 'integer'},
        'targetsSeen': _arr(_text()),
        'formatsSeen': _arr(_text()),
        'messages': {'type': 'integer'},
        'messagesBySeverity': {'type': 'object'},
        'chunks': {'type': 'integer'},
        'stateDocuments': {'type': 'integer'},
        'pipelineType': {'type': 'integer'},
        'api': _text(),
        'localRenderer': {'type': 'integer'},
        'vendor': {'type': 'integer'},
        'shaderDebugging': {'type': 'integer'},
        'pixelHistory': {'type': 'integer'},
    }),
    'passes': _arr(_obj({
        'index': {'type': 'integer'},
        'firstEid': {'type': 'integer'},
        'lastEid': {'type': 'integer'},
        'marker': _text(),
        'kind': _text(),
        'reason': _text(),
        'events': {'type': 'integer'},
        'graphics': {'type': 'integer'},
        'compute': {'type': 'integer'},
        'targets': _arr(_text()),
        'depth': _text(),
        'structure': _text(),
        'shaders': _arr(_text()),
        'otherShaders': _arr(_text()),
        'blocks': _arr(_text()),
        'firstTouched': _arr(_text()),
    })),
    'engine': _obj({
        'engine': _text(),
        'schema': _text(),
        'basis': _text(),
        'detected': _arr(_obj({'kind': _text(), 'name': _text(), 'where': _text()})),
        'concepts': _arr(_obj({
            'concept': _text(),
            'kind': _text(),
            'evidence': _arr(_obj({'kind': _text(), 'name': _text(), 'where': _text()})),
            'note': _text(),
            'passIndex': {'type': 'integer'},
            'firstEid': {'type': 'integer'},
        })),
        'questions': _arr(_obj({
            'id': _text(),
            'title': _text(),
            'ask': _text(),
            'conceptRows': _arr(_obj({
                'concept': _text(),
                'kind': _text(),
                'evidence': _arr(_obj({'kind': _text(), 'name': _text(), 'where': _text()})),
                'note': _text(),
                'passIndex': {'type': 'integer'},
                'firstEid': {'type': 'integer'},
            })),
            'values': _arr(_obj({
                'concept': _text(),
                'block': _text(),
                'member': _text(),
                'value': _text(),
                'firstEid': {'type': 'integer'},
                'passIndex': {'type': 'integer'},
                'bound': {'type': 'boolean'},
            })),
        })),
        'notInterpreted': _arr(_text()),
    }),
    # The notable lists (REFERENCE §4.11): the rule -- its inputs, its oddity rules, its cap -- then the rows.
    # The rule is part of the document rather than of the renderer because it *is* the answer to "why is this
    # pass here", and a consumer that wants to disagree with the ranking needs it.
    'notables': _obj({
        'limit': {'type': 'integer'},
        'oddityLimit': {'type': 'integer'},
        'passInputs': _arr(_obj({'input': _text(), 'how': _text(), 'available': {'type': 'boolean'},
                                 'why': _text()})),
        'oddities': _arr(_text()),
        'passes': _arr(_obj({
            'passIndex': {'type': 'integer'},
            'firstEid': {'type': 'integer'},
            'lastEid': {'type': 'integer'},
            'rank': {'type': 'integer'},
            'why': _arr(_text()),
            'values': _arr(_text()),
        })),
        'resourceInputs': _arr(_obj({'input': _text(), 'how': _text(), 'available': {'type': 'boolean'},
                                     'why': _text()})),
        'resourceSpecials': _arr(_text()),
        'resources': _arr(_obj({
            'resource': _text(),
            'name': _text(),
            'kind': _text(),
            'detail': _text(),
            'rank': {'type': 'integer'},
            'why': _arr(_text()),
            'values': _arr(_text()),
        })),
        'notes': _arr(_text()),
    }),
    # What to look at first (REFERENCE §4.11): one lead per thing to check, each with the driver command that
    # shows its evidence. `kind` says whether it came from a finding, a notable or a gap.
    'recommendations': _obj({
        'limit': {'type': 'integer'},
        'rows': _arr(_obj({
            'rank': {'type': 'integer'},
            'kind': _text(),
            'severity': _text(),
            'do': _text(),
            'why': _text(),
            'command': _text(),
            'eid': {'type': 'integer'},
            'resource': _text(),
        })),
        'notes': _arr(_text()),
    }),
    # The rule the findings are grouped by: what each severity means, and why each detector is in its group.
    'severityTable': _arr(_obj({
        'severity': _text(),
        'means': _text(),
        'members': _arr(_obj({'detector': _text(), 'why': _text()})),
    })),
    # `cause` and `verdict` are optional, and only a finding the corpus's `known` list explains has them:
    # the schema has to allow both, because "no cause established" is a state a finding is in rather than a
    # member with a placeholder value in it (`rdc_report.apply_known`).
    'flags': _arr(_obj({
        'detector': _text(),
        'what': _text(),
        'evidence': _arr(_text()),
        'certainty': _text(),
        'unproven': {'type': 'boolean'},
        'cause': _text(),
        'verdict': _text(),
    }, required=['detector', 'what', 'evidence', 'certainty', 'unproven'])),
    'detectors': _arr(_obj({'detector': _text(), 'ran': {'type': 'boolean'}, 'why': _text()})),
    'caveats': _arr(_text()),
    'appendix': _arr(_text()),
})
REPORT_SCHEMA['title'] = 'report'
REPORT_SCHEMA['description'] = ('The frame report the offline tool writes: the frame, its passes, the engine\'s '
                                'vocabulary, the red flags and the gaps. Written by `report <rdc> <bundleDir>`; '
                                'validated by `validate <bundleDir> <schemaDir>`.')

def _ab_side() -> Dict[str, Any]:
    """One side of the A/B, as `replaydiff.json` states it (a function: the two sides are two objects)."""
    return _obj({
        'bundle': _text(),
        'capture': _text(),
        'captureSha256': _text(),
        'api': _text(),
        'events': {'type': 'integer'},
        'passes': {'type': 'integer'},
        'resources': {'type': 'integer'},
        'messages': {'type': 'integer'},
        'withImages': {'type': 'integer'},    # how many images the bundle carries, not the flag
        'renderdoc': _text(),
    })

#: The **A/B's** own schema, for the same reason as the report's: the document is written by the offline tool
#: (`replaydiff <bundleA> <bundleB>`), so `schema/` -- which is what the *driver* publishes -- cannot hold it.
#: Until this existed, `replaydiff.json` carried a `schemaVersion` and no shape at all: a reader could tell
#: which version of the document it was holding and nothing else, and `validate` could not answer "is this
#: one this tool wrote?". Every member is required and none may be added, so a field a change dropped or
#: renamed fails a run instead of reaching a consumer as a missing column -- which is what the report's own
#: schema has been doing since it landed.
#:
#: `withImages` appears twice with two types, deliberately and correctly: the top-level one is *the flag the
#: run was given* (a boolean) and a side's is *how many images that bundle holds* (a count). The schema is
#: also what pins that distinction.
AB_SCHEMA: Dict[str, Any] = _obj({
    'schemaVersion': {'const': 1},
    'a': _ab_side(),
    'b': _ab_side(),
    'sameCapture': {'type': 'boolean'},
    'withImages': {'type': 'boolean'},
    'imagesFound': {'type': 'boolean'},
    'imageDetail': {'type': 'integer'},
    'passes': _arr(_obj({
        'path': _text(),
        'status': _text(),
        'note': _text(),
        'aFirstEid': {'type': 'integer'},
        'bFirstEid': {'type': 'integer'},
        'structure': _text(),
        'changes': _arr(_obj({'field': _text(), 'a': _text(), 'b': _text()})),
        'stateRemoved': _arr(_text()),
        'stateAdded': _arr(_text()),
        'shaders': _arr(_obj({
            'stage': _text(),
            'verdict': _text(),
            'aHash': _text(),
            'bHash': _text(),
            'aBytes': {'type': 'integer'},
            'bBytes': {'type': 'integer'},
            'changes': _arr(_obj({'field': _text(), 'a': _text(), 'b': _text()})),
        })),
        'cbuffers': _arr(_obj({
            'block': _text(),
            'stage': _text(),
            'slot': {'type': 'integer'},
            'aFile': _text(),
            'bFile': _text(),
            'notes': _arr(_text()),
            'values': _arr(_obj({'member': _text(), 'a': _text(), 'b': _text()})),
        })),
        'images': _arr(_obj({
            'slot': {'type': 'integer'},
            'verdict': _text(),
            'aEid': {'type': 'integer'},
            'bEid': {'type': 'integer'},
            'aFile': _text(),
            'bFile': _text(),
            'width': {'type': 'integer'},
            'height': {'type': 'integer'},
            'differing': {'type': 'integer'},
            # The three statistics are *formatted* in the document ("12.4%"): they are printed as they
            # are rendered in the Markdown, so a consumer reads the same text the report shows.
            'percentDiffering': _text(),
            'meanDelta': _text(),
            'maxDelta': {'type': 'integer'},
            'hashDistance': {'type': 'integer'},
            'heatMap': _text(),
            'note': _text(),
        })),
    })),
    'summary': _obj({
        'same': {'type': 'integer'},
        'added': {'type': 'integer'},
        'removed': {'type': 'integer'},
        'structureChanges': {'type': 'integer'},
        'stateChanges': {'type': 'integer'},
        'constantsChanged': {'type': 'integer'},
        'shadersDifferent': {'type': 'integer'},
        'imagesCompared': {'type': 'integer'},
        'imagesNotCompared': {'type': 'integer'},
    }),
    'caveats': _arr(_text()),
})
AB_SCHEMA['title'] = 'replaydiff'
AB_SCHEMA['description'] = ('The A/B of two bundles the offline tool writes: each side\'s frame, the aligned '
                            'passes with what differs inside them, and the counts the summary line prints. '
                            'Written by `replaydiff <bundleA> <bundleB>`; validated by '
                            '`validate <replaydiff.json> <schemaDir> replaydiff`.')

class SchemaError(Exception):
    """A schema file that cannot be used: unreadable, not JSON, or not an object."""

def _one_type_ok(value: Any, name: str) -> bool:
    """Whether `value` has the single JSON type `name`.

    `bool` is a subclass of `int` in Python and is not one in JSON, so the integer and number checks exclude
    it -- otherwise `true` would validate as a count.
    """
    if name == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if name == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == 'boolean':
        return isinstance(value, bool)
    if name == 'null':
        return value is None
    if name == 'object':
        return isinstance(value, dict)
    if name == 'array':
        return isinstance(value, list)
    if name == 'string':
        return isinstance(value, str)
    return False

def _type_ok(value: Any, name: Any) -> bool:
    """Whether `value` has JSON type `name`, where `name` is a string or a list of them (JSON Schema allows
    either, and a member whose shape depends on how the file was written -- `resources.json`'s `usage` with
    and without `--no-usage` -- needs the list form rather than a schema that lies about one of the two)."""
    if isinstance(name, list):
        return any(_one_type_ok(value, str(one)) for one in name)
    return _one_type_ok(value, str(name))

def validate_document(doc: Any, schema: Dict[str, Any], path: str = '$') -> List[str]:
    """Every way `doc` fails `schema`, as `'<json path>: <what>'`; empty means it validates."""
    unknown = sorted(set(schema) - SCHEMA_KEYWORDS)
    if unknown:
        return ['%s: the schema uses keyword(s) this validator does not implement: %s'
                % (path, ', '.join(unknown))]

    errors: List[str] = []
    if 'type' in schema and not _type_ok(doc, schema['type']):
        # The type is checked first because a wrong type makes every other check about this node noise:
        # "`x` is not one of 1, 2" on a string that is also not an integer says the same thing twice.
        errors.append('%s: %s, expected %s' % (path, type(doc).__name__, schema['type']))
        return errors
    if 'const' in schema and doc != schema['const']:
        errors.append('%s: %r, expected %r' % (path, doc, schema['const']))
    if 'enum' in schema and doc not in schema['enum']:
        errors.append('%s: %r is not one of %s'
                      % (path, doc, ', '.join(repr(value) for value in schema['enum'])))

    if isinstance(doc, dict):
        for key in schema.get('required', []):
            if key not in doc:
                errors.append('%s: required member %r is missing' % (path, key))
        properties: Dict[str, Any] = schema.get('properties', {})
        additional = schema.get('additionalProperties', True)
        for key in sorted(doc):
            child = '%s.%s' % (path, key)
            if key in properties:
                errors.extend(validate_document(doc[key], properties[key], child))
            elif additional is False:
                errors.append('%s: unknown member %r (the schema lists: %s)'
                              % (path, key, ', '.join(sorted(properties))))
            elif isinstance(additional, dict):
                errors.extend(validate_document(doc[key], additional, child))

    if isinstance(doc, list) and isinstance(schema.get('items'), dict):
        for index, item in enumerate(doc):
            errors.extend(validate_document(item, schema['items'], '%s[%d]' % (path, index)))
    return errors

def no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """A `json` object hook that refuses an object with a repeated key.

    A plain `json.load` keeps the *last* value of a repeated key and says nothing, which is how a dropped
    vertex-shader block went unnoticed once (two `"vs"` members, the second winning the first's place).
    The hook is what makes a document's own redundancy a validation failure rather than a silent choice --
    and it is the one check a *schema* cannot make, because by the time the schema sees the object the
    repetition is already gone.
    """
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError('the key %r appears twice in one object (a JSON reader keeps the last one)'
                             % key)
        seen[key] = value
    return seen


def load_document(path: str) -> Any:
    """One JSON document, read so a repeated key is a *failure* (`no_duplicate_keys`)."""
    with open(path, encoding='utf-8-sig') as fh:
        return json.load(fh, object_pairs_hook=no_duplicate_keys)


def load_schemas(schema_dir: str) -> Dict[str, Dict[str, Any]]:
    """The schemas in a directory (or a single schema file), keyed by kind.

    `replay_dump schema --out <dir>` writes `<kind>.schema.json`, which is where the checked-in `schema/`
    folder comes from; a name that is not a schema of this shape is reported rather than skipped.
    """
    names: List[str] = []
    if os.path.isfile(schema_dir):
        names = [schema_dir]
    elif os.path.isdir(schema_dir):
        names = sorted(os.path.join(schema_dir, name) for name in os.listdir(schema_dir)
                       if name.endswith('.schema.json'))
    if not names:
        raise SchemaError('no *.schema.json files in %s (write them with `replay_dump schema --out %s`)'
                          % (schema_dir, schema_dir))

    schemas: Dict[str, Dict[str, Any]] = {}
    for name in names:
        base = os.path.basename(name)
        kind = base[:-len('.schema.json')] if base.endswith('.schema.json') else os.path.splitext(base)[0]
        try:
            with open(name, encoding='utf-8-sig') as fh:
                schema = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise SchemaError('cannot read %s: %s' % (name, exc)) from exc
        if not isinstance(schema, dict):
            raise SchemaError('%s is not a JSON object' % name)
        schemas[kind] = schema
    # Two of the documents are this module's own data (see `REPORT_SCHEMA` and `AB_SCHEMA`), not files the
    # driver publishes: the offline tool writes both. A folder that carries a `report.schema.json` of its own
    # wins, so a consumer can pin a different revision without a code change -- the same for `replaydiff`.
    schemas.setdefault('report', REPORT_SCHEMA)
    schemas.setdefault('replaydiff', AB_SCHEMA)
    return schemas

#: Which schema a bundle file's *name* identifies. The rest of a bundle (PNGs, `cbuffers/`) is not a
#: document, and `validate` counts those as skipped rather than failing them.
BUNDLE_SCHEMAS = {
    'manifest.json': 'manifest',
    'capture.json': 'capture',
    'events.json': 'events',
    'resources.json': 'resources',
    'messages.json': 'messages',
    'counters.json': 'counters',
}

def schema_for_file(name: str) -> Optional[str]:
    """The schema kind for a file name, or None when the name is not a document's."""
    base = os.path.basename(name).lower()
    if base == 'report.json':
        return 'report'
    if base == 'replaydiff.json':
        return 'replaydiff'    # the A/B's own document, which the offline tool writes
    if base in BUNDLE_SCHEMAS:
        return BUNDLE_SCHEMAS[base]
    if base.endswith('.state.json'):
        return 'state'
    if base.endswith('.shaders.json'):
        return 'shaders'
    return None

def cmd_validate(path: str, schema_dir: str, name: Optional[str] = None) -> int:
    """`validate <file|bundleDir> <schemaDir|one.schema.json> [kind]`.

    With a directory it validates every file whose name identifies a document and reports the others as not
    a document; with a file it needs `kind` (a document saved from a command's stdout has a name that says
    nothing, e.g. `textures --json > t.json`). Exit code 1 when anything fails to validate.
    """
    try:
        schemas = load_schemas(schema_dir)
    except SchemaError as exc:
        print('error: %s' % exc)
        return 2

    targets: List[Tuple[str, str]] = []
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for base in sorted(files):
                full = os.path.join(root, base)
                kind = name if name else schema_for_file(base)
                if kind:
                    targets.append((full, kind))
    elif os.path.isfile(path):
        if not name:
            print('error: %s is a file, so it needs a schema kind: validate <%s> <schemaDir> <kind>'
                  % (path, os.path.basename(path)))
            print('       the kinds are: %s' % ', '.join(sorted(schemas)))
            return 2
        targets.append((path, name))
    else:
        print('error: no such file or directory: %s' % path)
        return 2

    if not targets:
        print('error: nothing to validate in %s (no file name matched a document)' % path)
        return 2

    failed = 0
    for full, kind in targets:
        schema = schemas.get(kind)
        # Forward slashes in what is printed: the name is for a reader, and the same bundle reports the same
        # way on either platform (`states/96.state.json`, not `states\96.state.json`).
        shown = os.path.relpath(full, path) if os.path.isdir(path) else os.path.basename(full)
        shown = shown.replace(os.sep, '/')
        if schema is None:
            print('error: no schema called %r for %s (the schemas are: %s)'
                  % (kind, shown, ', '.join(sorted(schemas))))
            failed += 1
            continue
        try:
            doc = load_document(full)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            # `ValueError` is the duplicate-key hook: a document with a repeated key is not read, because
            # the object a plain parse would hand the schema is not the object the file holds.
            print('FAIL %-40s not readable as JSON: %s' % (shown, exc))
            failed += 1
            continue

        errors = validate_document(doc, schema)
        if errors:
            failed += 1
            print('FAIL %-40s (%s, %d problem(s))' % (shown, kind, len(errors)))
            for error in errors[:20]:
                print('       %s' % error)
            if len(errors) > 20:
                print('       ... and %d more' % (len(errors) - 20))
        else:
            print('ok   %-40s (%s)' % (shown, kind))

    if os.path.isdir(path):
        skipped = sum(len([f for f in files if f.lower().endswith('.json')])
                      for _root, _dirs, files in os.walk(path)) - len(targets)
    else:
        skipped = 0
    print('%d document(s) checked, %d failed, %d JSON file(s) not a document' % (len(targets), failed,
                                                                                skipped))
    return 1 if failed else 0

