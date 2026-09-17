// z.selftest — the driver checking itself and publishing its schemas
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

bool ReadSchemaText(const std::string &path, std::string &text)
{
  if(!ReadWholeFile(path.c_str(), text))
    return false;

  std::string folded;
  folded.reserve(text.size());
  for(size_t i = 0; i < text.size(); i++)
  {
    if(text[i] == '\r' && i + 1 < text.size() && text[i + 1] == '\n')
      continue;
    folded += text[i];
  }
  text = folded;
  return true;
}

//: How many ways `dir` differs from this driver's table: a schema file that is missing, one whose
//: text differs, and a `<name>.schema.json` for a document kind the table no longer has -- which is
//: worse than useless, because a reader would take it for current. One code path for `schema
//: --check` and for the selftest, so the mode that is checked is the mode that runs.
int CheckSchemasAgainstDir(const std::string &dir, bool report)
{
  int differences = 0;
  for(int i = 0; i < kSchemaCount; i++)
  {
    const std::string path = dir + "\\" + kSchemas[i].name + ".schema.json";
    std::string text;
    if(!ReadSchemaText(path, text))
    {
      if(report)
        printf("missing %s (%s)\n", kSchemas[i].name, path.c_str());
      differences++;
      continue;
    }

    std::string want = kSchemas[i].text;
    want += "\n";    // `--out` ends the file with a newline, and so must this
    if(text != want)
    {
      if(report)
        printf("stale   %s (differs from this driver's schema)\n", kSchemas[i].name);
      differences++;
    }
    else if(report)
    {
      printf("ok      %s\n", kSchemas[i].name);
    }
  }

  WIN32_FIND_DATAA found;
  const std::string pattern = dir + "\\*.schema.json";
  HANDLE search = FindFirstFileA(pattern.c_str(), &found);
  if(search != INVALID_HANDLE_VALUE)
  {
    do
    {
      const std::string name = found.cFileName;
      const size_t cut = name.rfind(".schema.json");
      if(FindSchema(name.substr(0, cut).c_str()) == NULL)
      {
        if(report)
          printf("extra   %s (no document kind of that name any more)\n", name.c_str());
        differences++;
      }
    } while(FindNextFileA(search, &found));
    FindClose(search);
  }
  return differences;
}

//: Write the table to `dir`; `name` limits it to one schema. Returns 0, or the exit code of the failure.
int WriteSchemasTo(const std::string &dir, const char *name, bool report)
{
  int written = 0;
  for(int i = 0; i < kSchemaCount; i++)
  {
    if(name != NULL && *name && strcmp(name, kSchemas[i].name) != 0)
      continue;

    const std::string path = dir + "\\" + kSchemas[i].name + ".schema.json";
    FILE *f = fopen(path.c_str(), "wb");
    if(f == NULL)
      return Fail(1, "cannot write %s", path.c_str());
    fputs(kSchemas[i].text, f);
    fputc('\n', f);
    fclose(f);
    if(report)
      printf("written: %s\n", path.c_str());
    written++;
  }
  if(written == 0)
    return Fail(2, "no schema named '%s' (`schema` lists them)", name == NULL ? "" : name);
  if(report)
    printf("%d schema file(s), schemaVersion %d\n", written, kSchemaVersion);
  return 0;
}

//: `schema [<name>] [--out <dir>] [--check <dir>]`. Without a name it prints the index -- every
//: kind, the command that writes it and the version they all declare. `--out` writes
//: `<dir>/<name>.schema.json` for each, which is how the checked-in `schema/` folder is made;
//: `--check` compares that folder against this driver's table and exits non-zero when they differ.
//: The check is what makes "regenerate after changing a document" enforceable rather than a
//: convention: a generated file that is committed *can* go stale, and the only cure is a command
//: that says so.
int CmdSchema(const char *name, const char *outDir, const char *checkDir)
{
  if(outDir != NULL && *outDir && checkDir != NULL && *checkDir)
    return Fail(2, "`--out` writes the schemas and `--check` verifies them: pass one of the two");

  if(checkDir != NULL && *checkDir)
  {
    const int differences = CheckSchemasAgainstDir(checkDir, true);
    if(differences == 0)
    {
      printf("%d schema file(s) match this driver, schemaVersion %d\n", kSchemaCount, kSchemaVersion);
      return 0;
    }
    printf("%d of %d file(s) differ: regenerate with `replay_dump schema --out %s`\n", differences,
           kSchemaCount, checkDir);
    return 1;
  }

  if(outDir != NULL && *outDir)
    return WriteSchemasTo(outDir, name, true);

  if(name != NULL && *name)
  {
    const SchemaDoc *doc = FindSchema(name);
    if(doc == NULL)
      return Fail(2, "no schema named '%s' (`schema` lists them)", name);
    fputs(doc->text, stdout);
    fputc('\n', stdout);
    return 0;
  }

  printf("schemaVersion %d, %d document kind(s):\n", kSchemaVersion, kSchemaCount);
  for(int i = 0; i < kSchemaCount; i++)
    printf("  %-14s %s\n", kSchemas[i].name, kSchemas[i].writtenBy);
  printf(
      "`schema <name>` prints one; `schema --out <dir>` writes them all where a consumer can read "
      "them, which is how the checked-in schema/ folder is made; `schema --check <dir>` fails when "
      "that "
      "folder and this driver disagree.\n");
  return 0;
}

// --------------------------------------------------------------------------- selftest
//
// What can be checked without a capture is checked without one -- the writer's escaping, its separators and
// whether a document it wrote is balanced -- and what needs the engine is checked against the DLL alone
// (load, version, entry points). A machine without RenderDoc reports those as *skipped*, the same
// convention the offline suite uses, because the hermetic half is still worth running there.
//
// The full check of a document is its schema, and that belongs to the tool with a JSON parser:
// `python rdc_analysis.py validate <bundle> schema` reads these very documents and the schemas below.
// This runs where neither a capture nor a parser is available, so it checks what text can be checked.
void Usage();

struct SelfTest
{
  int passed = 0, failed = 0, skipped = 0;

  void Ok(const char *name)
  {
    printf("ok      %s\n", name);
    passed++;
  }
  void Skipped(const char *name, const char *why)
  {
    printf("skipped %s -- %s\n", name, why);
    skipped++;
  }
  void Failed(const char *name, const char *why)
  {
    printf("FAILED  %s -- %s\n", name, why);
    failed++;
  }
  void Check(bool condition, const char *name, const char *why)
  {
    if(condition)
      Ok(name);
    else
      Failed(name, why);
  }
  void Equal(const std::string &got, const std::string &want, const char *name)
  {
    if(got == want)
    {
      Ok(name);
      return;
    }
    printf("FAILED  %s\n          got      %s\n          expected %s\n", name, got.c_str(),
           want.c_str());
    failed++;
  }
};

//: Whether `text` is one balanced JSON object. Not a parser -- it cannot tell a wrong member from a
//: right one -- but it catches the failure that matters for a document nobody looks at before it
//: ships: a truncated write, or a separator bug that leaves the object open. The writer's output is
//: checked with it here, and the offline validator checks the documents themselves against the
//: schema.
bool JsonBalanced(const std::string &text)
{
  size_t i = 0;
  while(i < text.size() && (text[i] == ' ' || text[i] == '\n' || text[i] == '\r' || text[i] == '\t'))
    i++;
  if(i >= text.size() || text[i] != '{')
    return false;

  int depth = 0;
  bool inString = false, escaped = false;
  for(; i < text.size(); i++)
  {
    const char c = text[i];
    if(inString)
    {
      if(escaped)
        escaped = false;
      else if(c == '\\')
        escaped = true;
      else if(c == '"')
        inString = false;
      continue;
    }
    if(c == '"')
      inString = true;
    else if(c == '{' || c == '[')
      depth++;
    else if(c == '}' || c == ']')
      if(--depth < 0)
        return false;
  }
  return depth == 0 && !inString;
}

int CmdSelftest()
{
  SelfTest t;

  // ------------------------------------------------------------------ the writer's helpers
  t.Equal(JsonEscape("a\"b"), "a\\\"b", "json-escape-quote");
  t.Equal(JsonEscape("a\\b"), "a\\\\b", "json-escape-backslash");
  t.Equal(JsonEscape("a\nb\tc\rd"), "a\\nb\\tc\\rd", "json-escape-controls");
  t.Equal(JsonEscape(std::string("\x01")), "\\u0001", "json-escape-low-byte");
  // A Windows path is the common case and the one that broke documents before escaping existed.
  t.Equal(JsonEscape("renderdoc-src\\Android.rdc"), "renderdoc-src\\\\Android.rdc",
          "json-escape-path");

  // The separator machinery is a state machine, and its two failure modes are opposite: a missing
  // comma is unreadable and a trailing one is too. Writing a small document and looking at the
  // bytes pins both -- reading the writer cannot, which is how a hardcoded trailing comma got into
  // a document once.
  {
    const std::string path = DefaultLogStem() + ".selftest.json";
    {
      const JsonDocument json;    // JSON, whatever the terminal asked for
      const CaptureStdout out(path.c_str());
      if(!out.Ok())
        return Fail(1, "cannot write %s for the selftest", path.c_str());
      g_indent = 1;
      printf("{\n");
      Field("a", 1);
      Field("b", std::string("x"));
      ArrayOpen("rows");
      Row(std::string("one"));
      ObjectOpen();
      Field("k", 2, true);
      ObjectClose();
      Row(std::string("two"));
      ArrayClose(false);
      Field("n", 3, true);
      g_indent = 0;
      printf("}\n");
    }

    std::string text;
    const bool read = ReadWholeFile(path.c_str(), text);
    remove(path.c_str());
    t.Check(read, "writer-document-written", "the selftest could not read back what it wrote");
    t.Check(JsonBalanced(text), "writer-document-balanced", "the writer's document is not balanced");

    // The separators are compared with the whitespace removed, because the writer puts a separator
    // *in front* of the item that needs one (and indents to its own taste): what must be exactly
    // right is the sequence of commas, and pinning the bytes would pin the layout instead.
    std::string flat;
    for(size_t i = 0; i < text.size(); i++)
    {
      const char c = text[i];
      if(c != ' ' && c != '\n' && c != '\r' && c != '\t')
        flat += c;
    }
    t.Check(flat.find("[\"one\",{\"k\":2},\"two\"]") != std::string::npos, "writer-separators",
            "array items are not comma-separated in the right places (the separator goes in front "
            "of an "
            "item, and never in front of the first)");
    t.Check(flat.find(",}") == std::string::npos && flat.find(",]") == std::string::npos,
            "writer-no-trailing-comma", "a trailing comma makes the document unreadable");
    t.Check(flat.find("\"n\":3") != std::string::npos, "writer-last-field",
            "the `last` member is missing");
    t.Check(flat.find("{\"k\":2}") != std::string::npos, "writer-object-row",
            "an object's last member has a comma, or the object was not closed");
    t.Check(!JsonBalanced("{\"a\": 1"), "writer-balance-detects-truncation",
            "a truncated document was reported as balanced");
    t.Check(!JsonBalanced("{\"a\": \"unterminated}"), "writer-balance-detects-unclosed-string",
            "an unclosed string was reported as balanced");
  }

  // ------------------------------------------------------------------ the schema table
  {
    bool unique = true, versioned = true, objects = true;
    for(int i = 0; i < kSchemaCount; i++)
    {
      for(int j = i + 1; j < kSchemaCount; j++)
        if(!strcmp(kSchemas[i].name, kSchemas[j].name))
          unique = false;
      const std::string text = kSchemas[i].text;
      if(text.find("\"schemaVersion\"") == std::string::npos ||
         text.find("\"const\": 1") == std::string::npos)
        versioned = false;
      if(text.empty() || text[0] != '{' || text[text.size() - 1] != '}')
        objects = false;
    }
    t.Check(unique, "schema-names-unique", "two schemas share a name");
    t.Check(versioned, "schema-declares-version",
            "a schema does not describe schemaVersion as a const");
    t.Check(objects, "schema-is-one-object", "a schema is not a single JSON object");
    t.Check(FindSchema("state") != NULL && FindSchema("manifest") != NULL &&
                FindSchema("events") != NULL,
            "schema-covers-the-bundle", "a document the bundle depends on has no schema");
    t.Check(kSchemaVersion == 1, "schema-version-known",
            "the offline validator does not know this version");
  }

  // ------------------------------------------------------------------ `schema --check`
  {
    const std::string dir = DefaultLogStem() + ".schemacheck";
    const std::string stale = dir + "\\state.schema.json";
    const std::string extra = dir + "\\gone.schema.json";
    CreateDirectoryA(dir.c_str(), NULL);

    t.Check(WriteSchemasTo(dir, NULL, false) == 0, "schema-check-writes",
            "the selftest could not write the schemas to a folder of its own");
    t.Check(CheckSchemasAgainstDir(dir, false) == 0, "schema-check-clean",
            "a folder written from this driver's own table does not check clean");

    FILE *f = fopen(stale.c_str(), "wb");
    if(f != NULL)
    {
      fputs("{}", f);
      fclose(f);
    }
    t.Check(CheckSchemasAgainstDir(dir, false) > 0, "schema-check-detects-stale",
            "a file that disagrees with the table was reported as matching");

    remove(stale.c_str());
    t.Check(CheckSchemasAgainstDir(dir, false) > 0, "schema-check-detects-missing",
            "a missing file was reported as matching");

    f = fopen(extra.c_str(), "wb");
    if(f != NULL)
    {
      fputs("{}", f);
      fclose(f);
    }
    t.Check(CheckSchemasAgainstDir(dir, false) > 0, "schema-check-detects-extra",
            "a file with no document kind behind it was reported as matching");

    // Leave the folder as it was found: a leftover file would fail the *next* run's clean check, on
    // another day, in a directory nobody connects to this one.
    for(int i = 0; i < kSchemaCount; i++)
      remove((dir + "\\" + kSchemas[i].name + ".schema.json").c_str());
    remove(extra.c_str());
    RemoveDirectoryA(dir.c_str());
    t.Check(GetFileAttributesA(dir.c_str()) == INVALID_FILE_ATTRIBUTES, "schema-check-cleanup",
            "the selftest left its scratch folder behind");
  }

  // ------------------------------------------------------------------ the help text and the DLL
  {
    std::string usage;
    {
      const std::string path = DefaultLogStem() + ".usage.txt";
      {
        const CaptureStdout out(path.c_str());
        if(!out.Ok())
          return Fail(1, "cannot write %s for the selftest", path.c_str());
        Usage();
      }
      ReadWholeFile(path.c_str(), usage);
      remove(path.c_str());
    }
    t.Check(usage.find("schema") != std::string::npos, "usage-lists-schema",
            "the usage text omits schema");
    t.Check(usage.find("selftest") != std::string::npos, "usage-lists-selftest",
            "the usage text omits selftest");
    t.Check(usage.find("dump") != std::string::npos, "usage-lists-dump",
            "the usage text omits dump");

    HMODULE dll = LoadReplayDLL();
    if(dll == NULL)
    {
      t.Skipped("dll-load", "renderdoc.dll was not found ($RDC_RENDERDOC_DLL overrides the path)");
      t.Skipped("dll-version", "no dll");
      t.Skipped("dll-entry-points", "no dll");
    }
    else
    {
      t.Ok("dll-load");
      const char *version = g_GetVersionString ? g_GetVersionString() : NULL;
      t.Check(version != NULL && *version != '\0', "dll-version",
              "RENDERDOC_GetVersionString returned nothing");
      t.Check(OpenCaptureFile(dll) != NULL, "dll-entry-points",
              "RENDERDOC_OpenCaptureFile is not exported");
    }
  }

  printf("\n%d passed, %d failed, %d skipped\n", t.passed, t.failed, t.skipped);
  if(t.failed == 0)
    printf(
        "the documents themselves are checked with `python src\\py\\rdc_analysis.py validate <bundle> "
        "schema`\n");
  return t.failed == 0 ? 0 : 1;
}

// --------------------------------------------------------------------------- CLI
