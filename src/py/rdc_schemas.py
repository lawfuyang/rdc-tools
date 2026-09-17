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
            with open(full, encoding='utf-8-sig') as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
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
