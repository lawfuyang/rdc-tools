"""Tests for `validate` and for the checked-in schemas in `schema/`.

The schemas are written by the driver (`replay_dump schema --out schema`) and checked in, so these tests
need no driver, no capture and no GPU: they read the same files a consumer reads. Two things are being
pinned here -- that the validator has teeth (a document that breaks a schema is *reported*), and that the
schemas are usable at all (each one is satisfiable, uses only implemented keywords, and declares the version
the driver stamps).

Run directly, via unittest, or through the tool itself:

    python tests/test_rdc_validate.py
    python rdc_analysis.py selftest -k Validate
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Callable, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_analysis as R          # noqa: E402

SCHEMA_DIR = os.path.join(ROOT, 'schema')


def capture_text(func: Callable[..., object], *args: Any, **kwargs: Any) -> str:
    """Run `func` and return everything it printed on stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


def line_with(text: str, needle: str) -> str:
    """The one line of `text` that mentions `needle` -- the output is column-aligned, so a whole-line
    comparison would be a test of the padding."""
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError('no line containing %r in:\n%s' % (needle, text))


def minimal(schema: Dict[str, Any]) -> Any:
    """The smallest document a schema accepts, built from the schema itself.

    It says nothing about a real capture -- it says the schema can be satisfied, that every required member
    has a declared type, and that the validator accepts a document it should. A schema whose required
    member is missing from `properties`, or whose type is misspelled, fails here rather than in the field.
    """
    if 'const' in schema:
        return schema['const']
    if 'enum' in schema:
        return schema['enum'][0]
    kind = schema.get('type')
    if kind == 'object':
        properties = schema.get('properties', {})
        return {key: minimal(properties.get(key, {})) for key in schema.get('required', [])}
    if kind == 'array':
        items = schema.get('items')
        return [minimal(items)] if isinstance(items, dict) else []
    if kind == 'integer':
        return 0
    if kind == 'number':
        return 0.0
    if kind == 'boolean':
        return False
    if kind == 'null':
        return None
    if kind == 'string':
        return 'x'
    return {}


def walk(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every sub-schema, so a keyword check is not a check of the top level only."""
    found = [schema]
    if isinstance(schema.get('items'), dict):
        found.extend(walk(schema['items']))
    for child in schema.get('properties', {}).values():
        if isinstance(child, dict):
            found.extend(walk(child))
    if isinstance(schema.get('additionalProperties'), dict):
        found.extend(walk(schema['additionalProperties']))
    return found


class SchemaCase(unittest.TestCase):
    schemas: Dict[str, Dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        with open(os.path.join(SCHEMA_DIR, 'state.schema.json'), encoding='utf-8') as fh:
            first = json.load(fh)
        assert isinstance(first, dict)
        cls.schemas = R.load_schemas(SCHEMA_DIR)


# =========================================================================== the checked-in schemas
class TestSchemas(SchemaCase):
    def test_every_schema_is_readable_and_declares_the_driver_version(self):
        self.assertTrue(self.schemas, 'no schemas found in %s' % SCHEMA_DIR)
        for kind, schema in self.schemas.items():
            self.assertEqual(schema['title'], kind, '%s: title does not match the file name' % kind)
            self.assertIn('description', schema, '%s: no description' % kind)
            self.assertEqual(schema['additionalProperties'], False,
                             '%s: an unlisted member must be a failure' % kind)
            self.assertEqual(schema['properties'].get('schemaVersion'), {'const': 1},
                             '%s: schemaVersion is not pinned to 1' % kind)

    def test_every_schema_stays_inside_the_implemented_keyword_subset(self):
        for kind, schema in self.schemas.items():
            for sub in walk(schema):
                unknown = sorted(set(sub) - R.SCHEMA_KEYWORDS)
                self.assertEqual(unknown, [], '%s: unimplemented keyword(s) %s' % (kind, unknown))

    def test_every_schema_accepts_a_minimal_document_built_from_itself(self):
        for kind, schema in self.schemas.items():
            errors = R.validate_document(minimal(schema), schema)
            self.assertEqual(errors, [], '%s: %s' % (kind, '; '.join(errors)))

    def test_the_bundle_kinds_all_have_a_schema(self):
        for kind in ('manifest', 'capture', 'events', 'resources', 'messages', 'state', 'shaders'):
            self.assertIn(kind, self.schemas)

    def test_a_file_name_identifies_its_document(self):
        self.assertEqual(R.schema_for_file('manifest.json'), 'manifest')
        self.assertEqual(R.schema_for_file('capture.json'), 'capture')
        self.assertEqual(R.schema_for_file(os.path.join('states', '119.state.json')), 'state')
        self.assertEqual(R.schema_for_file(os.path.join('states', '119.shaders.json')), 'shaders')
        self.assertEqual(R.schema_for_file('rt/96.png'), None)
        self.assertEqual(R.schema_for_file(os.path.join('cbuffers', '119_ps_0.json')), None)


# =========================================================================== the validator
class TestValidator(unittest.TestCase):
    def errors(self, doc: Any, schema: Dict[str, Any]) -> List[str]:
        with self.subTest(doc=doc):
            return R.validate_document(doc, schema)

    def test_a_missing_required_member_is_named(self):
        schema: Dict[str, Any] = {'type': 'object', 'required': ['a', 'b'], 'properties': {}}
        self.assertEqual(self.errors({'a': 1}, schema), ["$: required member 'b' is missing"])

    def test_an_unknown_member_is_named_with_what_is_allowed(self):
        schema: Dict[str, Any] = {'type': 'object', 'properties': {'a': {'type': 'integer'}},
                                  'additionalProperties': False}
        errors = self.errors({'a': 1, 'z': 2}, schema)
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown member 'z'", errors[0])
        self.assertIn('the schema lists: a', errors[0])

    def test_a_wrong_type_is_reported_and_stops_there(self):
        schema: Dict[str, Any] = {'type': 'object', 'required': ['a'],
                                  'properties': {'a': {'type': 'integer', 'enum': [1, 2]}}}
        errors = self.errors({'a': 'x'}, schema)
        self.assertEqual(errors, ["$.a: str, expected integer"],
                         'a wrong type should be reported once, and not also as its own enum')

    def test_true_is_not_an_integer(self):
        schema: Dict[str, Any] = {'type': 'integer'}
        self.assertEqual(self.errors(1, schema), [])
        self.assertEqual(len(self.errors(True, schema)), 1)

    def test_nested_paths_are_reported(self):
        item: Dict[str, Any] = {'type': 'object', 'required': ['eid'], 'properties': {'eid': {'type': 'integer'}}}
        schema: Dict[str, Any] = {'type': 'object', 'required': ['events'],
                                  'properties': {'events': {'type': 'array', 'items': item}}}
        errors = self.errors({'events': [{'eid': 1}, {}]}, schema)
        self.assertEqual(errors, ["$.events[1]: required member 'eid' is missing"])

    def test_a_member_may_be_one_of_several_types(self):
        """`resources.json`'s `usage` is an array normally and a note string with `--no-usage`: the schema
        says so rather than lying about one of the two shapes."""
        schema: Dict[str, Any] = {'type': 'object', 'properties': {'u': {'type': ['array', 'string']}},
                                  'required': ['u']}
        self.assertEqual(self.errors({'u': []}, schema), [])
        self.assertEqual(self.errors({'u': 'not collected'}, schema), [])
        errors = self.errors({'u': 1}, schema)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected ['array', 'string']", errors[0])

    def test_an_unimplemented_keyword_is_a_failure_not_a_shrug(self):
        schema: Dict[str, Any] = {'type': 'object', 'maxProperties': 3}
        errors = self.errors({}, schema)
        self.assertEqual(len(errors), 1)
        self.assertIn('maxProperties', errors[0])
        self.assertIn('does not implement', errors[0])


# =========================================================================== the command
class TestValidateCommand(unittest.TestCase):
    tmp: str

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='rdc_validate_')
        self.addCleanup(self._remove_tmp)

    def _remove_tmp(self) -> None:
        for dirpath, _dirs, files in os.walk(self.tmp, topdown=False):
            for name in files:
                os.remove(os.path.join(dirpath, name))
            os.rmdir(dirpath)

    def schemas(self) -> Dict[str, Dict[str, Any]]:
        return R.load_schemas(SCHEMA_DIR)

    def validate(self, path: str, schema_dir: str, name: str = '') -> Any:
        """Run the command once, returning `(stdout, exit code)` -- never printing into the test log."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = R.cmd_validate(path, schema_dir, name or None)
        return buf.getvalue(), code

    def write(self, relative: str, doc: Any) -> str:
        path = os.path.join(self.tmp, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)
        return path

    def test_a_whole_bundle_of_valid_documents_passes(self):
        schemas = self.schemas()
        for name, kind in (('manifest.json', 'manifest'), ('capture.json', 'capture'),
                           ('events.json', 'events'), ('resources.json', 'resources'),
                           ('messages.json', 'messages')):
            self.write(name, minimal(schemas[kind]))
        self.write(os.path.join('states', '96.state.json'), minimal(schemas['state']))
        self.write(os.path.join('states', '96.shaders.json'), minimal(schemas['shaders']))
        self.write(os.path.join('cbuffers', '96_ps_0.json'), {'not': 'a document'})
        with open(os.path.join(self.tmp, 'rt.png'), 'wb') as fh:
            fh.write(b'png')

        out, code = self.validate(self.tmp, SCHEMA_DIR)
        self.assertTrue(line_with(out, 'manifest.json').startswith('ok'))
        self.assertTrue(line_with(out, 'states/96.state.json').startswith('ok'), out)
        self.assertIn('7 document(s) checked, 0 failed, 1 JSON file(s) not a document', out)
        self.assertEqual(code, 0)

    def test_a_damaged_document_fails_with_its_path_and_exit_code(self):
        schema = minimal(self.schemas()['manifest'])
        self.assertNotIsInstance(schema, list)
        del schema['bundleVersion']
        self.write('manifest.json', schema)
        out, code = self.validate(self.tmp, SCHEMA_DIR)
        self.assertTrue(line_with(out, 'manifest.json').startswith('FAIL'))
        self.assertIn("required member 'bundleVersion' is missing", out)
        self.assertEqual(code, 1)

    def test_a_file_needs_a_kind_and_the_error_says_so(self):
        path = self.write('t.json', {})
        out, code = self.validate(path, SCHEMA_DIR)
        self.assertIn('needs a schema kind', out)
        self.assertIn('the kinds are:', out)
        self.assertEqual(code, 2)
        out, code = self.validate(path, SCHEMA_DIR, 'textures')
        self.assertTrue(line_with(out, 't.json').startswith('FAIL'))
        self.assertIn('textures', line_with(out, 't.json'))
        self.assertEqual(code, 1)

    def test_a_missing_schema_directory_is_reported_not_ignored(self):
        out, code = self.validate(self.tmp, os.path.join(self.tmp, 'nowhere'))
        self.assertIn('no *.schema.json files', out)
        self.assertIn('replay_dump schema --out', out)
        self.assertEqual(code, 2)

    def test_a_single_schema_file_can_be_used(self):
        path = self.write('v.json', minimal(self.schemas()['capture']))
        out, code = self.validate(path, os.path.join(SCHEMA_DIR, 'capture.schema.json'), 'capture')
        self.assertTrue(line_with(out, 'v.json').startswith('ok'), out)
        self.assertEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
