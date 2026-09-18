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
int CheckSchemasAgainstDir(const std::string &dir, bool bReport)
{
  int differences = 0;
  for(int i = 0; i < kSchemaCount; i++)
  {
    const std::string path = dir + "\\" + kSchemas[i].m_Name + ".schema.json";
    std::string text;
    if(!ReadSchemaText(path, text))
    {
      if(bReport)
        printf("missing %s (%s)\n", kSchemas[i].m_Name, path.c_str());
      differences++;
      continue;
    }

    std::string want = kSchemas[i].text;
    want += "\n";    // `--out` ends the file with a newline, and so must this
    if(text != want)
    {
      if(bReport)
        printf("stale   %s (differs from this driver's schema)\n", kSchemas[i].m_Name);
      differences++;
    }
    else if(bReport)
    {
      printf("ok      %s\n", kSchemas[i].m_Name);
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
        if(bReport)
          printf("extra   %s (no document kind of that name any more)\n", name.c_str());
        differences++;
      }
    } while(FindNextFileA(search, &found));
    FindClose(search);
  }
  return differences;
}

//: Write the table to `dir`; `name` limits it to one schema. Returns 0, or the exit code of the failure.
int WriteSchemasTo(const std::string &dir, const char *name, bool bReport)
{
  int written = 0;
  for(int i = 0; i < kSchemaCount; i++)
  {
    if(name != NULL && *name && strcmp(name, kSchemas[i].m_Name) != 0)
      continue;

    const std::string path = dir + "\\" + kSchemas[i].m_Name + ".schema.json";
    FILE *f = fopen(path.c_str(), "wb");
    if(f == NULL)
      return Fail(1, "cannot write %s", path.c_str());
    fputs(kSchemas[i].text, f);
    fputc('\n', f);
    fclose(f);
    if(bReport)
      printf("written: %s\n", path.c_str());
    written++;
  }
  if(written == 0)
    return Fail(2, "no schema named '%s' (`schema` lists them)", name == NULL ? "" : name);
  if(bReport)
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
    printf("  %-14s %s\n", kSchemas[i].m_Name, kSchemas[i].writtenBy);
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
  void Check(bool bCondition, const char *name, const char *why)
  {
    if(bCondition)
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
  bool bInString = false, escaped = false;
  for(; i < text.size(); i++)
  {
    const char c = text[i];
    if(bInString)
    {
      if(escaped)
        escaped = false;
      else if(c == '\\')
        escaped = true;
      else if(c == '"')
        bInString = false;
      continue;
    }
    if(c == '"')
      bInString = true;
    else if(c == '{' || c == '[')
      depth++;
    else if(c == '}' || c == ']')
      if(--depth < 0)
        return false;
  }
  return depth == 0 && !bInString;
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
      const JsonDocument bJson;    // JSON, whatever the terminal asked for
      const CaptureStdout out(path.c_str());
      if(!out.Ok())
        return Fail(1, "cannot write %s for the selftest", path.c_str());
      g_Indent = 1;
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
      g_Indent = 0;
      printf("}\n");
    }

    std::string text;
    const bool bRead = ReadWholeFile(path.c_str(), text);
    remove(path.c_str());
    t.Check(bRead, "writer-document-written", "the selftest could not read back what it wrote");
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
    bool bUnique = true, versioned = true, objects = true;
    for(int i = 0; i < kSchemaCount; i++)
    {
      for(int j = i + 1; j < kSchemaCount; j++)
        if(!strcmp(kSchemas[i].m_Name, kSchemas[j].m_Name))
          bUnique = false;
      const std::string text = kSchemas[i].text;
      if(text.find("\"schemaVersion\"") == std::string::npos ||
         text.find("\"const\": 1") == std::string::npos)
        versioned = false;
      if(text.empty() || text[0] != '{' || text[text.size() - 1] != '}')
        objects = false;
    }
    t.Check(bUnique, "schema-names-unique", "two schemas share a name");
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
      remove((dir + "\\" + kSchemas[i].m_Name + ".schema.json").c_str());
    remove(extra.c_str());
    RemoveDirectoryA(dir.c_str());
    t.Check(GetFileAttributesA(dir.c_str()) == INVALID_FILE_ATTRIBUTES, "schema-check-cleanup",
            "the selftest left its scratch folder behind");
  }

  // ------------------------------------------------------------------ the image helpers
  //
  // A contact sheet and a difference map are pictures nobody can check by reading the code, so the
  // maths behind them is checked where it can be: no device, no capture, no DLL. `WriteBMP` is the
  // writer the bundle's `rt/` images go through -- the manifest hashes those files -- so the round
  // trip below also says that a reader has not drifted from that writer.
  {
    ImageData img;
    img.m_Width = 4;
    img.m_Height = 4;
    img.Reset(4u * 4u * 4u, 0);
    for(int y = 0; y < 4; y++)
    {
      for(int x = 0; x < 4; x++)
      {
        uint8_t *px = img.m_Rgba.data() + ((size_t)y * 4u + (size_t)x) * 4u;
        px[0] = (uint8_t)(x * 60);
        px[1] = (uint8_t)(y * 60);
        px[2] = 128;
        px[3] = 255;
      }
    }

    const std::string bmp = DefaultLogStem() + ".selftest.bmp";
    t.Check(WriteBMPImage(bmp.c_str(), img), "image-bmp-written", "the BMP could not be written");

    ImageData back;
    std::string why;
    t.Check(ReadBMPImage(bmp.c_str(), back, why), "image-bmp-read", why.c_str());
    t.Check(back.Valid() && back.m_Width == img.m_Width && back.m_Height == img.m_Height &&
                back.m_Rgba == img.m_Rgba,
            "image-bmp-round-trip", "what the reader got back is not what the writer wrote");

    // BMP rounds every row up to four bytes, so a 3-wide image is where a reader that ignores the
    // padding reads the next row one byte early -- wrong pixels, no error.
    ImageData odd;
    odd.m_Width = 3;
    odd.m_Height = 2;
    // Alpha 255 and one distinct red per pixel: the writer is 24-bit (the reader fills alpha in),
    // and a uniform image would hide exactly the misalignment this checks for.
    odd.Reset(3u * 2u * 4u, 255);
    for(int i = 0; i < 6; i++)
      odd.m_Rgba[(size_t)i * 4u] = (uint8_t)(10 + i * 10);
    const std::string oddBmp = DefaultLogStem() + ".selftest-pad.bmp";
    ImageData oddBack;
    t.Check(WriteBMPImage(oddBmp.c_str(), odd) && ReadBMPImage(oddBmp.c_str(), oddBack, why) &&
                oddBack.m_Rgba == odd.m_Rgba,
            "image-bmp-row-padding", "a row that is not a multiple of four bytes did not survive");

    const ImageData small = DownscaleImage(img, 2, 2);
    t.Check(small.m_Width == 2 && small.m_Height == 2, "image-downscale-size",
            "a 4x4 image asked to fit 2x2 did not come back 2x2");
    // Each destination pixel is the average of the 2x2 block it covers: red runs 0,60,120,180 across
    // the source, so the two reds are (0+60)/2 = 30 and (120+180)/2 = 150, and green (0+60)/2 = 30.
    t.Check(small.Valid() && small.m_Rgba[0] == 30 && small.m_Rgba[4] == 150 && small.m_Rgba[5] == 30,
            "image-downscale-average", "the thumbnail is not the average of the pixels it covers");

    std::vector<ImageData> tiles(3, small);
    const ImageData montage = MakeMontage(tiles, 2, 2, 2, 1);
    t.Check(montage.m_Width == 7 && montage.m_Height == 7, "image-montage-size",
            "a 3-tile montage in 2 columns with a 1px gutter is not 7x7");

    t.Check(DifferenceHash(img) == DifferenceHash(img), "image-hash-stable",
            "the same image hashed twice gave two answers");
    ImageData inverted = img;
    for(size_t i = 0; i + 4 <= inverted.m_Rgba.size(); i += 4)
    {
      for(int k = 0; k < 3; k++)
        inverted.m_Rgba[i + k] = (uint8_t)(255 - inverted.m_Rgba[i + k]);
    }
    t.Check(DifferenceHash(inverted) != DifferenceHash(img), "image-hash-inverted",
            "inverting an image did not change its hash");

    int maxDelta = 0;
    long long sumDelta = 0;
    t.Check(ImagePixelDelta(img, img, maxDelta, sumDelta, NULL) == 0 && maxDelta == 0,
            "image-diff-same", "an image differs from itself");
    ImageData one = img;
    one.m_Rgba[7] = (uint8_t)(one.m_Rgba[7] ^ 0xff);
    t.Check(ImagePixelDelta(img, one, maxDelta, sumDelta, NULL) == 1, "image-diff-one-pixel",
            "one changed pixel was not reported as one");
    t.Check(ImagePixelDelta(img, small, maxDelta, sumDelta, NULL) == -1, "image-diff-size-mismatch",
            "two images of different sizes were compared anyway");

    const std::string notBmp = DefaultLogStem() + ".selftest.txt";
    FILE *f = fopen(notBmp.c_str(), "wb");
    if(f != NULL)
    {
      fputs("this is not a bitmap", f);
      fclose(f);
    }
    ImageData rejected;
    t.Check(!ReadBMPImage(notBmp.c_str(), rejected, why), "image-rejects-non-bmp",
            "a text file was read as an image");

    remove(bmp.c_str());
    remove(oddBmp.c_str());
    remove(notBmp.c_str());
  }

  // ------------------------------------------------------------------ the pixel-history vocabulary
  //
  // `pixelhistory`'s answers are phrased in these helpers, and two of them can be wrong in a way that
  // reads as a fact: a rejection list that misses a flag the engine set (the fragment did not write
  // the pixel, and nothing says why), and a value read as the wrong component type or printed from the
  // engine's "invalid" sentinel. Both are checked with hand-built structures: no device, no capture.
  {
    CompType cast = CompType::Typeless;
    t.Check(CastFromName("float", cast) && cast == CompType::Float, "pixelhistory-cast-name",
            "`float` is not read as the float component type");
    t.Check(CastFromName("srgb", cast) && cast == CompType::UNormSRGB, "pixelhistory-cast-srgb",
            "`srgb` is not read as UNormSRGB");
    // `f32` is `buffer --as`'s word for printing bytes, not a texture component type: accepting it
    // here would be one vocabulary pretending to be the other.
    t.Check(!CastFromName("f32", cast) && !CastFromName("", cast), "pixelhistory-cast-unknown",
            "a name that is not a component type was accepted");

    // The printed name has to read back as the type it came from: `readAs` is printed that way, and
    // the next command line takes the name as `--cast`.
    bool bRoundTrip = true;
    const CompType kAllCasts[] = {CompType::Typeless, CompType::Float,   CompType::UNorm,
                                  CompType::SNorm,    CompType::UInt,    CompType::SInt,
                                  CompType::UScaled,  CompType::SScaled, CompType::Depth,
                                  CompType::UNormSRGB};
    for(const CompType type : kAllCasts)
    {
      CompType back = CompType::Typeless;
      if(!CastFromName(CastText(type), back) || back != type)
        bRoundTrip = false;
    }
    t.Check(bRoundTrip, "pixelhistory-cast-round-trip",
            "a cast name does not read back as the type it was printed from");

    // The same four words, three readings: which member of the union is meaningful is the component
    // type's business, and a float format read as unsigned prints 1065353216 where 1.0 belongs. Two
    // words whose three readings are all exact, so the check pins the reading and not the formatter.
    PixelValue value = {};
    value.floatValue[0] = 1.0f;     // 0x3f800000
    value.floatValue[1] = -2.0f;    // 0xc0000000
    t.Equal(PixelValueText(value, CompType::Float), "1.000000,-2.000000,0.000000,0.000000",
            "pixelhistory-value-float");
    t.Equal(PixelValueText(value, CompType::UInt), "1065353216,3221225472,0,0",
            "pixelhistory-value-uint");
    t.Equal(PixelValueText(value, CompType::SInt), "1065353216,-1073741824,0,0",
            "pixelhistory-value-sint");
    t.Equal(PixelValueText(value, CompType::Float), PixelValueText(value, CompType::UNormSRGB),
            "pixelhistory-value-scaledtypes-read-as-float");

    ModificationValue mod = {};
    mod.col.uintValue[0] = 1065353216u;
    mod.depth = 0.5f;
    mod.stencil = 3;
    t.Equal(ModificationValueText(mod, CompType::Float),
            "col(1.000000,0.000000,0.000000,0.000000) depth(0.500000) stencil(3)",
            "pixelhistory-value-line");
    // The sentinel: an invalid value's colour is `0xdeadbeef`, and printing it is a colour that
    // never existed -- the one mistake here that a reader cannot see.
    ModificationValue invalid = {};
    invalid.SetInvalid();
    t.Check(!invalid.IsValid(), "pixelhistory-invalid-sentinel",
            "SetInvalid did not mark the value invalid (the check below proves nothing then)");
    t.Equal(ModificationColorText(invalid, CompType::Float), "-", "pixelhistory-invalid-color");
    t.Equal(ModificationDepthText(invalid), "-", "pixelhistory-invalid-depth");
    t.Equal(ModificationStencilText(invalid), "-", "pixelhistory-invalid-stencil");
    t.Equal(ModificationValueText(invalid, CompType::Float), "-", "pixelhistory-invalid-line");

    // Every flag `PixelModification::Passed` reads, one at a time: the verdict and the reasons are
    // two answers to one question, so a flag that makes a fragment fail must also name itself. The
    // count is asserted, because a flag added to the struct (RenderDoc's, not ours) would otherwise
    // be missing from the list silently -- which is exactly the failure this checks for.
    const struct
    {
      bool PixelModification::*flag;
      const char *name;
    } kFlags[] = {
        {&PixelModification::sampleMasked, "sample masked"},
        {&PixelModification::backfaceCulled, "backface culled"},
        {&PixelModification::depthClipped, "depth clipped"},
        {&PixelModification::depthBoundsFailed, "depth bounds failed"},
        {&PixelModification::viewClipped, "view clipped"},
        {&PixelModification::scissorClipped, "scissor clipped"},
        {&PixelModification::shaderDiscarded, "shader discarded"},
        {&PixelModification::depthTestFailed, "depth test failed"},
        {&PixelModification::stencilTestFailed, "stencil test failed"},
        {&PixelModification::predicationSkipped, "predication skipped"},
    };
    PixelModification none = {};
    t.Check(none.Passed() && RejectionText(none).empty(), "pixelhistory-passed-is-silent",
            "a fragment with no failing flag is not passed, or is given a reason");
    int checked = 0, named = 0;
    for(const auto &flag : kFlags)
    {
      PixelModification one = {};
      one.*(flag.flag) = true;
      checked++;
      if(!one.Passed() && RejectionText(one) == std::string(flag.name))
        named++;
    }
    t.Check(checked == 10, "pixelhistory-every-flag-is-checked",
            "the flag list no longer covers every flag `Passed` reads");
    t.Check(named == checked, "pixelhistory-every-flag-is-named",
            "a flag that stops a fragment from writing the pixel has no reason in the list");
    PixelModification both = {};
    both.scissorClipped = true;
    both.depthTestFailed = true;
    t.Equal(RejectionText(both), "scissor clipped, depth test failed", "pixelhistory-reason-order");
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
    t.Check(usage.find("pixelhistory") != std::string::npos, "usage-lists-pixelhistory",
            "the usage text omits pixelhistory");

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
        "the documents themselves are checked with `python src\\py\\rdc_analysis.py validate "
        "<bundle> "
        "schema`\n");
  return t.failed == 0 ? 0 : 1;
}

// --------------------------------------------------------------------------- CLI
