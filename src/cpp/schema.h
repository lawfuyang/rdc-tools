#pragma once

//: The JSON contract for every `--json` document: one schema per document kind, and the version
//: they all declare. Split out of replay_dump.cpp because it is *data* -- the biggest single block
//: in that file -- while the logic that prints, writes and checks it stays with the tool.
//
// Every `--json` document needs a contract that is not "read the writer and infer it": the frame
// report and the offline tool read these documents, and a shape change has already gone unnoticed
// once (a stage's members were written flat into one object, so the repeated keys silently dropped
// the vertex shader). Documents therefore carry `schemaVersion`, and this table is the schema for
// each kind.
//
// The schemas are hand-written because the writers are hand-written: there is no descriptor to
// generate both from, and inventing one for fifteen documents is more machinery than it saves. They
// stay honest by being *used*: `schema --out <dir>` writes them, the copy in `schema/` is checked
// in, and `python src\py\rdc_analysis.py validate <bundle> schema` validates real documents against that
// copy -- so a schema that has drifted from its writer fails a run instead of misleading a reader.
// Writing them found a defect immediately: `bundle-verify` wrote `"files"` twice in one object (the
// per-file rows, then their count), which every parser resolves to the count.
//
// The keyword subset is exactly what the offline validator implements -- type, required,
// properties, items, enum, const, description, additionalProperties. `additionalProperties` is
// `false` throughout, so an unlisted member is a validation failure rather than something a
// consumer discovers later.

#include <string_view>

//: Stamped into every document, so a consumer can refuse a shape it does not understand.
//: `constexpr` rather than an extern: an int, one copy per translation unit, no linkage puzzle.
constexpr int kSchemaVersion = 1;

struct SchemaDoc
{
  const char *name;         // `schema <name>`; what validate matches a file by
  const char *writtenBy;    // which command writes it, for the index
  const char *text;
};

extern const SchemaDoc kSchemas[];
extern const int kSchemaCount;

//: The schema for a document kind, or NULL. A `string_view` parameter because the callers name kinds
//: from substrings (`state.schema.json` -> `state`), and building a `std::string` for that is noise.
const SchemaDoc *FindSchema(std::string_view name);
