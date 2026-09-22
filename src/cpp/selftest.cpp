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

  // A file in `dir` the table does not know: walked with an iterator and the `error_code` overload,
  // so a folder that cannot be read is "no extras" rather than an exception out of the self-check.
  std::error_code ec;
  std::filesystem::directory_iterator it(dir, ec);
  const std::filesystem::directory_iterator end;
  for(; it != end; it.increment(ec))
  {
    if(ec)
      break;
    const std::string name = it->path().filename().string();
    const size_t cut = name.rfind(".schema.json");
    if(cut == std::string::npos)
      continue;    // not one of ours: the table's own files are the only shape this looks for
    if(FindSchema(name.substr(0, cut).c_str()) == NULL)
    {
      if(bReport)
        printf("extra   %s (no document kind of that name any more)\n", name.c_str());
      differences++;
    }
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
    FILE *f = FileOpen(path, "wb");
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
    RemoveQuiet(path);
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
    MakeDir(dir);

    t.Check(WriteSchemasTo(dir, NULL, false) == 0, "schema-check-writes",
            "the selftest could not write the schemas to a folder of its own");
    t.Check(CheckSchemasAgainstDir(dir, false) == 0, "schema-check-clean",
            "a folder written from this driver's own table does not check clean");

    FILE *f = FileOpen(stale, "wb");
    if(f != NULL)
    {
      fputs("{}", f);
      fclose(f);
    }
    t.Check(CheckSchemasAgainstDir(dir, false) > 0, "schema-check-detects-stale",
            "a file that disagrees with the table was reported as matching");

    RemoveQuiet(stale);
    t.Check(CheckSchemasAgainstDir(dir, false) > 0, "schema-check-detects-missing",
            "a missing file was reported as matching");

    f = FileOpen(extra, "wb");
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
    RemoveQuiet(extra);
    RemoveQuiet(dir);
    t.Check(!ExistsQuiet(dir), "schema-check-cleanup",
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
    FILE *f = FileOpen(notBmp, "wb");
    if(f != NULL)
    {
      fputs("this is not a bitmap", f);
      fclose(f);
    }
    ImageData rejected;
    t.Check(!ReadBMPImage(notBmp.c_str(), rejected, why), "image-rejects-non-bmp",
            "a text file was read as an image");

    RemoveQuiet(bmp);
    RemoveQuiet(oddBmp);
    RemoveQuiet(notBmp);
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

  // ------------------------------------------------ the file times behind the staleness warning
  {
    // `NewestSourceTime` is what the driver asks itself ("am I older than the sources I was built
    // from?"), and like the rest of the file-time plumbing it needs no device -- so the comparison
    // that decides whether a run's answers are the previous build's is checked here rather than
    // found in the field. Two details the check has to get right: a suffix decides what counts as a
    // source (`.txt` next to the sources is not one), and the *newest* wins rather than the first
    // or the last one seen.
    const std::filesystem::path dir(DefaultLogStem() + ".srcdir");
    MakeDir(dir);
    const std::filesystem::path older = dir / "zz-old.cpp";
    const std::filesystem::path newer = dir / "aa-new.h";
    const std::filesystem::path notes = dir / "notes.txt";

    const char *const kSuffixes[] = {".cpp", ".h"};
    std::filesystem::path name;
    t.Check(!NewestSourceTime(dir, kSuffixes, 2, name).has_value(),
            "newest-source-of-an-empty-dir-is-none", "an empty folder answered with a file");

    for(const std::filesystem::path *path : {&older, &newer, &notes})
    {
      FILE *f = FileOpen(*path, "wb");
      if(f == NULL)
        return Fail(1, "cannot write %s for the selftest", path->string().c_str());
      fputs("x", f);
      fclose(f);
      // The clock interrupt this machine's file times are taken from ticks every ~15 ms, so two
      // writes in a row can carry the *same* time and an ordering test would then pass or fail by
      // chance. 50 ms is more than a tick, and it is the difference between the two files that the
      // check below is about.
      Sleep(50);
    }

    const std::optional<FileTime> when = NewestSourceTime(dir, kSuffixes, 2, name);
    t.Check(when.has_value() && name.filename() == "aa-new.h", "newest-source-is-the-newest-source",
            "the file that won is not the newest one the suffixes name");
    // `notes.txt` was written last and is newer than every source: a checker that ignored the
    // suffixes would have answered with it.
    const std::optional<FileTime> notesWhen = FileWriteTime(notes);
    t.Check(notesWhen && when && *notesWhen > *when, "newest-source-ignores-what-is-not-a-source",
            "a file the suffixes do not name was counted as a source");
    t.Equal(name.filename().string(), std::string("aa-new.h"),
            "newest-source-is-not-the-newest-file");
    const std::optional<FileTime> olderWhen = FileWriteTime(older);
    t.Check(olderWhen && when && *olderWhen < *when, "file-write-time-orders-two-files",
            "two files written 50 ms apart do not compare");
    t.Check(!FileWriteTime(dir / "nope.cpp").has_value(),
            "file-write-time-of-a-missing-file-is-none",
            "a file that is not there answered with a time");
    t.Check(!NewestSourceTime(dir / "nope", kSuffixes, 2, name).has_value(),
            "newest-source-of-a-missing-dir-is-none", "a missing folder answered with a file");

    std::error_code cleanupEc;
    std::filesystem::remove(older, cleanupEc);
    std::filesystem::remove(newer, cleanupEc);
    std::filesystem::remove(notes, cleanupEc);
    std::filesystem::remove(dir, cleanupEc);
  }

  // -------------------------------------------------------- `MakeDir` makes the parents (bundle.cpp)
  {
    // `CreateDirectoryA` makes a single level, and the destinations a caller *names* are several:
    // `dump cap.rdc out/frames/cap1`, `sheet cap.rdc shots/frame12`, `patch ... out/tries/fix1`.
    // Refusing those until the caller makes the parents is what `dump` used to do ("cannot create the
    // bundle directory") -- for a path the tool can make itself, and while the Python sweep made the
    // same chain with `os.makedirs`, so one half of the tool was making a destination the other refused.
    const std::filesystem::path outer(DefaultLogStem() + ".makedir");
    const std::filesystem::path deep = outer / "one" / "two" / "three";
    t.Check(MakeDir(deep), "makedir-creates-parents",
            "a destination three levels down was not created");
    std::error_code ec;
    t.Check(std::filesystem::is_directory(deep, ec) && !ec, "makedir-created-a-directory",
            "the deepest component is not a directory");
    // Every caller may be looking at a folder it made on an earlier run (`dump --overwrite`), so
    // the second call is the normal case rather than the exception.
    t.Check(MakeDir(deep), "makedir-existing-is-success",
            "a folder that is already there was reported as a failure");
    std::filesystem::path trailing = deep;
    trailing +=
        "\\";    // a caller may spell a destination this way, so the path type has to as well
    t.Check(MakeDir(trailing), "makedir-trailing-separator",
            "a folder named with a trailing separator was reported as a failure");

    // A *file* where a component has to be is a real failure, and the callers' own messages
    // ("cannot create the bundle directory") are the ones that should keep being printed for it.
    const std::filesystem::path blocked = outer / "blocked.txt";
    FILE *f = FileOpen(blocked, "wb");
    if(f != NULL)
    {
      fputs("x", f);
      fclose(f);
    }
    t.Check(!MakeDir(blocked / "below"), "makedir-refuses-a-file-in-the-way",
            "a path through a file was reported as created");

    std::filesystem::remove(blocked, ec);
    // Innermost first: `remove` takes an empty folder and nothing else, as `RemoveDirectory` did.
    std::filesystem::remove(deep, ec);
    std::filesystem::remove(outer / "one" / "two", ec);
    std::filesystem::remove(outer / "one", ec);
    std::filesystem::remove(outer, ec);
    t.Check(!std::filesystem::exists(outer, ec), "makedir-cleanup",
            "the selftest left its scratch folder behind");
  }

  // -------------------------------------------------------- the version guard (capture.cpp)
  {
    // Two pure functions over strings and one that reads 32 bytes of a file, so all of it is
    // checked here rather than in the field: this decides whether a run is refused, and the one
    // interesting case beyond "equal" is that the compare is *numeric* -- "1.9" is older than
    // "1.10", and a string compare answers the opposite.
    int major = 0, minor = 0;
    t.Check(ParseMajorMinor("1.46", major, minor) && major == 1 && minor == 46,
            "version-parse-plain", "1.46 did not parse as 1.46");
    t.Check(ParseMajorMinor("1.46 e4bd23", major, minor) && major == 1 && minor == 46,
            "version-parse-with-commit", "the header's `version commit` form did not parse");
    t.Check(ParseMajorMinor("v1.9.2", major, minor) && major == 1 && minor == 9,
            "version-parse-tag", "a release tag did not parse");
    t.Check(!ParseMajorMinor("1", major, minor), "version-parse-refuses-a-bare-major",
            "'1' was accepted as a version");
    t.Check(!ParseMajorMinor("unknown", major, minor),
            "version-parse-refuses-what-is-not-a-version", "'unknown' was accepted as a version");

    bool known = false;
    t.Check(CompareMajorMinor("1.46", "1.46 e4bd23", known) == 0 && known, "version-equal",
            "the same release compared as a difference");
    t.Check(CompareMajorMinor("1.45", "1.46 e4bd23", known) == -1 && known,
            "version-older-is-older", "an older engine did not compare as older");
    t.Check(CompareMajorMinor("1.47", "1.46 e4bd23", known) == 1 && known, "version-newer-is-newer",
            "a newer engine did not compare as newer");
    t.Check(CompareMajorMinor("1.9", "1.10 x", known) == -1 && known,
            "version-compares-numerically", "1.9 and 1.10 compared as text");
    t.Check(CompareMajorMinor("", "1.46", known) == 0 && !known, "version-unknown-is-not-a-verdict",
            "an unparsable engine version produced a verdict");

    // The header is read rather than asked for -- the replay API has no accessor -- so its offsets
    // are pinned against bytes written the way `rdcfile.cpp` writes them.
    const std::string path = DefaultLogStem() + ".capture";
    unsigned char header[32] = {0};
    memcpy(header, "RDOC", 4);
    header[8] = 0x02;
    header[9] = 0x01;    // logfile version 258, little-endian, as every capture in the corpus has it
    memcpy(header + 16, "1.46 e4bd23", 11);

    FILE *f = FileOpen(path, "wb");
    if(f == NULL)
      return Fail(1, "cannot write %s for the selftest", path.c_str());
    fwrite(header, 1, sizeof(header), f);
    fclose(f);

    CaptureVersion capture;
    t.Check(ReadCaptureVersion(path.c_str(), capture) && capture.logfile == 258 &&
                capture.program == "1.46 e4bd23",
            "capture-version-reads-the-header", "the header was not read as the engine writes it");
    t.Check(!ReadCaptureVersion((path + ".missing").c_str(), capture),
            "capture-version-refuses-a-missing-file", "a file that is not there produced a version");

    FILE *notCapture = FileOpen(path, "wb");
    if(notCapture == NULL)
      return Fail(1, "cannot write %s for the selftest", path.c_str());
    fwrite("RDX!", 1, 4, notCapture);    // 4 of the 32 bytes: neither the magic nor the length
    fclose(notCapture);
    t.Check(!ReadCaptureVersion(path.c_str(), capture), "capture-version-refuses-not-a-capture",
            "a file that is not a container produced a version");
    RemoveQuiet(path);
  }

  // ------------------------------------------------------------------ per-pass folding
  {
    // A pass is a maximal run of consecutive *calls* sharing a marker path. The marker's own row is
    // not a call and must not break the run, and a path that closes and reopens is two passes.
    const struct
    {
      int eid;
      bool bCall;
      const char *path;
    } kRows[] = {
        {10, true, "A > B"}, {11, false, "A > B"},    // a marker's own row: not an event the pass's cost covers
        {12, true, "A > B"}, {13, true, "A"},      {14, true, "A > B"}, {15, true, "A > B"},
    };
    std::vector<ActionNode> rows;
    for(const auto &row : kRows)
    {
      ActionNode node;
      node.m_Eid = row.eid;
      node.m_bCall = row.bCall;
      node.m_Path = row.path;
      rows.push_back(node);
    }

    const std::vector<PassRange> passes = PassesFromActions(rows);
    t.Check(passes.size() == 3, "passes-groups-consecutive-calls",
            "consecutive calls under one marker path are not one pass");
    if(passes.size() == 3)
    {
      t.Check(passes[0].m_First == 10 && passes[0].m_Last == 12, "passes-spans-its-calls",
              "a pass's range is not its first and last call");
      t.Equal(passes[0].m_Name, "A > B", "passes-name-is-the-marker-path");
      t.Check(passes[1].m_First == 13 && passes[1].m_Last == 13, "passes-one-call-is-a-pass",
              "a single call under its own path is not a pass");
      t.Check(passes[2].m_First == 14 && passes[2].m_Last == 15,
              "passes-reopened-path-is-two-passes",
              "a path that closes and reopens is not one pass");
    }

    const std::string path = DefaultLogStem() + ".passes.txt";
    {
      FILE *f = FileOpen(path, "wb");
      if(f == NULL)
        return Fail(1, "cannot write %s for the selftest", path.c_str());
      fprintf(f, "# one pass per line\n");
      fprintf(f, "\n");
      fprintf(f, "100 140 BasePass\n");
      fprintf(f, "141 150   Shadow > DirLight\n");
      fprintf(f, "200 260\n");
      fclose(f);
    }
    std::vector<PassRange> ranges;
    std::string why;
    const bool bRead = ReadPassRanges(path.c_str(), ranges, why);
    RemoveQuiet(path);
    t.Check(bRead, "read-pass-ranges", why.c_str());
    if(bRead)
    {
      t.Check(ranges.size() == 3, "read-pass-ranges-skips-comments-and-blanks",
              "a comment or a blank line was read as a pass");
      if(ranges.size() == 3)
      {
        t.Check(ranges[0].m_First == 100 && ranges[0].m_Last == 140, "read-pass-ranges-ids",
                "the range is not the two ids on the line");
        t.Equal(ranges[0].m_Name, "BasePass", "read-pass-ranges-name");
        t.Equal(ranges[1].m_Name, "Shadow > DirLight", "read-pass-ranges-name-keeps-its-spaces");
        t.Equal(ranges[2].m_Name, "eid 200-260", "read-pass-ranges-unnamed-is-named-by-its-range");
      }
    }
    {
      FILE *f = FileOpen(path, "wb");
      if(f == NULL)
        return Fail(1, "cannot write %s for the selftest", path.c_str());
      fprintf(f, "100\n");
      fclose(f);
    }
    std::vector<PassRange> bad;
    std::string badWhy;
    const bool bBad = ReadPassRanges(path.c_str(), bad, badWhy);
    RemoveQuiet(path);
    t.Check(!bBad, "read-pass-ranges-rejects-a-line-that-is-not-a-range",
            "a line with one id was read as a pass");
    t.Check(badWhy.find("line 1") != std::string::npos, "read-pass-ranges-says-which-line",
            "the error does not name the line that is wrong");
  }

  // ------------------------------------------------------------------ folding a counter over passes
  {
    // The arithmetic of `counters --per-pass` is checked here because it cannot be checked against
    // a capture: this machine's replays publish 16 counters and produce no results for either real
    // one, so a pass's sum, peak and measured count are otherwise never exercised.
    std::vector<PassRange> passes;
    const struct
    {
      int first, last;
      const char *name;
    } kPasses[] = {{10, 12, "A"}, {20, 20, "B"}, {30, 39, "C"}};
    for(const auto &p : kPasses)
    {
      PassRange pass;
      pass.m_First = p.first;
      pass.m_Last = p.last;
      pass.m_Name = p.name;
      passes.push_back(pass);
    }

    // The action tree is the authority for how many calls a pass holds.
    int calls[] = {10, 11, 12, 20, 30, 31};
    std::vector<ActionNode> rows;
    for(size_t i = 0; i < sizeof(calls) / sizeof(calls[0]); i++)
    {
      ActionNode node;    // the check counts calls and nothing else, so there is no path to set
      node.m_Eid = calls[i];
      node.m_bCall = true;
      rows.push_back(node);
    }

    rdcarray<CounterResult> results;
    const struct
    {
      int eid;
      double value;
    } kValues[] = {{11, 2.0}, {10, 1.5}, {30, 4.0}, {99, 7.0}};
    for(size_t i = 0; i < sizeof(kValues) / sizeof(kValues[0]); i++)
    {
      CounterResult result;
      result.eventId = (uint32_t)kValues[i].eid;
      result.counter = GPUCounter::EventGPUDuration;
      result.value.d = kValues[i].value;
      results.push_back(result);
    }

    const std::vector<PassCost> costs =
        FoldPassCosts(results, GPUCounter::EventGPUDuration, CompType::Float, passes, rows);
    t.Check(costs.size() == 3, "fold-one-row-per-pass",
            "the folding did not produce one row a pass");
    if(costs.size() == 3)
    {
      t.Check(
          costs[0].m_Events == 3 && costs[0].m_Measured == 2, "fold-measured-is-not-events",
          "a pass's event count and the events the counter answered for are not the same thing");
      t.Check(costs[0].m_Sum > 3.4999 && costs[0].m_Sum < 3.5001, "fold-sums-the-pass",
              "a pass's cost is not the sum of the counter over its events");
      t.Check(costs[0].m_Max > 1.9999 && costs[0].m_Max < 2.0001, "fold-peak-is-the-largest-event",
              "the peak is not the largest single value in the pass");
      t.Check(costs[0].m_First == 10 && costs[0].m_Last == 12, "fold-keeps-the-range", "");
      t.Equal(costs[0].m_Name, "A", "fold-keeps-the-pass-name");
      // A pass with no answer from the counter is a pass that measured nothing, not a free pass.
      t.Check(costs[1].m_Events == 1 && costs[1].m_Measured == 0 && costs[1].m_Sum == 0.0,
              "fold-unmeasured-is-not-free", "a pass the counter skipped was given a cost");
      // An event outside every pass contributes to nothing, and neither does another counter's.
      t.Check(costs[2].m_Events == 2 && costs[2].m_Measured == 1 && costs[2].m_Sum > 3.9999,
              "fold-ignores-ids-outside-the-pass", "an id outside every pass was folded in");
    }

    // A u64 counter read through `.d` is a different number, which is why the result type decides.
    CounterResult counted;
    counted.eventId = 10;
    counted.counter = GPUCounter::EventGPUDuration;
    counted.value.u64 = 9007199254740993ull;    // 2^53 + 1: exactly representable as u64, not double
    rdcarray<CounterResult> ints;
    ints.push_back(counted);
    const std::vector<PassCost> folded =
        FoldPassCosts(ints, GPUCounter::EventGPUDuration, CompType::UInt, passes, rows);
    t.Check(!folded.empty() && folded[0].m_Sum == 9007199254740993.0, "fold-reads-a-u64-as-a-u64",
            "a u64 counter was read as a double");
  }

  // ------------------------------------------------------------------ the cross-check comparisons
  {
    // `SignatureLinkText` returns empty for "this links", which is the one answer a reader cannot
    // see from the call site -- and a check that says nothing when it passes is easy to get wrong.
    SigParameter written;
    written.semanticName = "TEXCOORD";
    written.semanticIndex = 0;
    written.compCount = 4;
    written.varType = VarType::Float;

    SigParameter read = written;
    t.Check(SignatureLinkText(written, read).empty(), "link-identical-is-silent",
            "the same element on both sides is reported as a mismatch");

    read.compCount = 2;    // reading fewer than the writer produced is the normal case
    t.Check(SignatureLinkText(written, read).empty(), "link-reading-fewer-components-is-silent",
            "reading fewer components than were written is not a mismatch");

    read.compCount = 4;
    read.varType = VarType::Half;    // the same component family as Float
    t.Check(SignatureLinkText(written, read).empty(), "link-float-and-half-are-one-family",
            "float and half are the same component family");

    read.varType = VarType::UInt;
    t.Equal(SignatureLinkText(written, read), "written as float, read as uint",
            "link-type-mismatch");

    read.compCount = 5;    // more than the writer produced: those components were never written
    t.Equal(SignatureLinkText(written, read), "written with 4 component(s), read as 5",
            "link-reading-more-components");

    // A render target's format is compared with what the shader writes by *family*: a float4 into a
    // unorm target is the normal case, and `Typeless` is the capture saying it does not know.
    t.Check(ComponentClass(CompType::UNorm) == CompType::Float, "component-class-unorm-is-float",
            "a unorm target is not the float family a shader writes");
    t.Check(ComponentClass(CompType::UInt) == CompType::UInt, "component-class-uint-is-uint", "");
    t.Check(ComponentClass(CompType::SInt) == CompType::SInt, "component-class-sint-is-sint", "");
    t.Check(ComponentClass(CompType::Typeless) == CompType::Typeless,
            "component-class-keeps-i-do-not-know",
            "typeless was folded into a family the state did not state");

    ResourceFormat fmt;
    fmt.compType = CompType::UNorm;
    fmt.compCount = 4;
    t.Equal(FormatText(fmt), "unorm4", "format-text");
  }

  // ------------------------------------------------------------- the message table (debug --group)
  {
    // What makes `--group` useful is the counting, and what makes it *correct* is that the identity is
    // the engine's own message id *plus* the severity, category and source -- the same text at another
    // severity is another finding. So the checks here are the identity, the fold, and the order.
    const auto makeMessage = [](uint32_t eid, MessageSeverity severity, MessageCategory category,
                                MessageSource source, uint32_t id, const char *text) {
      DebugMessage message;
      message.eventId = eid;
      message.severity = severity;
      message.category = category;
      message.source = source;
      message.messageID = id;
      message.description = text;
      return message;
    };
    const rdcarray<DebugMessage> messages = {
        makeMessage(1, MessageSeverity::High, MessageCategory::State_Setting, MessageSource::API, 7,
                    "a"),
        makeMessage(9, MessageSeverity::High, MessageCategory::State_Setting, MessageSource::API, 7,
                    "a"),
        makeMessage(4, MessageSeverity::Info, MessageCategory::Miscellaneous, MessageSource::API, 9,
                    "b"),
        makeMessage(4, MessageSeverity::High, MessageCategory::Miscellaneous, MessageSource::API, 9,
                    "b"),
        makeMessage(2, MessageSeverity::High, MessageCategory::State_Setting, MessageSource::API, 7,
                    "a"),
    };

    const std::vector<DebugGroup> groups = GroupDebugMessages(messages);
    t.Check(groups.size() == 3, "debug-group-folds-identical-messages",
            "identical messages did not fold into one group");
    t.Check(groups.size() == 3 && groups[0].count == 3 && groups[0].firstEid == 1 &&
                groups[0].lastEid == 9,
            "debug-group-counts-and-the-eid-range", "a group's count or eid range is wrong");
    t.Check(!groups.empty() && groups[0].severity == MessageSeverity::High,
            "debug-group-most-severe-first", "the worst severity is not the first row");
    // The fourth message has the *same* id and text as the third and differs only in severity: folding
    // on the id alone would have produced two groups where the engine reported two findings.
    t.Check(groups.size() == 3 && groups[1].severity == MessageSeverity::High &&
                groups[2].severity == MessageSeverity::Info,
            "debug-group-keeps-a-severity-apart",
            "the same id at two severities was folded together");
    t.Check(!groups.empty() && groups[0].text == "a", "debug-group-keeps-the-text",
            "a group lost the message text");
    t.Check(GroupDebugMessages(rdcarray<DebugMessage>()).empty(), "debug-group-of-nothing-is-empty",
            "an empty message list produced a group");

    MessageSeverity severity = MessageSeverity::Info;
    t.Check(SeverityFromName("high", severity) && severity == MessageSeverity::High,
            "severity-from-name-high", "`high` did not name the High severity");
    t.Check(SeverityFromName("info", severity) && severity == MessageSeverity::Info,
            "severity-from-name-info", "`info` did not name the Info severity");
    t.Check(!SeverityFromName("High", severity) && !SeverityFromName("error", severity) &&
                !SeverityFromName(NULL, severity),
            "severity-from-name-refuses-the-rest",
            "a name that is not one of the four was accepted");
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
      RemoveQuiet(path);
    }
    t.Check(usage.find("schema") != std::string::npos, "usage-lists-schema",
            "the usage text omits schema");
    t.Check(usage.find("selftest") != std::string::npos, "usage-lists-selftest",
            "the usage text omits selftest");
    t.Check(usage.find("dump") != std::string::npos, "usage-lists-dump",
            "the usage text omits dump");
    t.Check(usage.find("pixelhistory") != std::string::npos, "usage-lists-pixelhistory",
            "the usage text omits pixelhistory");
    t.Check(usage.find("crosscheck") != std::string::npos, "usage-lists-crosscheck",
            "the usage text omits crosscheck");
    t.Check(usage.find("--per-pass") != std::string::npos, "usage-lists-per-pass",
            "the usage text omits counters --per-pass");

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

  // ------------------------------------------------------------------ the sweep's rules
  //
  // No device, no capture: the sweep's stop rules are its contract -- when it stops, what it
  // collected up to the stopping id, and which cap did it -- and they are pure logic, so they are
  // pinned here on shapes made to trip each rule. (A parallel sweep was built on these rules once
  // and removed: bundle.cpp's "why the sweep is not parallel" has the measurements.)
  {
    // The rules, on a shape made to trip each of them: a stateless prefix that must NOT stop the
    // sweep (nothing found yet), a stateful id, then the empty run that does.
    {
      SweepRules rules(0, 100000);
      SweepRules::Step step = SweepRules::Continue;
      for(int i = 0; i < 400; i++)
        step = rules.Feed(false, i + 1);
      t.Check(step == SweepRules::Continue && rules.m_LastEid == 0, "rules-prefix-does-not-stop",
              "a stateless prefix stopped the sweep before anything was found");
      step = rules.Feed(true, 401);
      t.Check(step == SweepRules::Continue && rules.m_LastEid == 401 && rules.m_Count == 1,
              "rules-first-find-is-recorded", "the first with-state id was not recorded");
      for(int i = 0; i < 255; i++)
        step = rules.Feed(false, 402 + i);
      t.Check(step == SweepRules::Continue, "rules-empty-run-counts-to-one-less",
              "the empty run stopped one id early");
      step = rules.Feed(false, 657);
      t.Check(step == SweepRules::StopEmptyRun, "rules-empty-run-stops",
              "256 consecutive stateless ids after a find did not stop the sweep");
      // A with-state id resets the run, and the id that trips a cap is collected first.
      SweepRules caps(3, 100000);
      caps.Feed(true, 10);
      caps.Feed(false, 11);
      caps.Feed(true, 12);
      const SweepRules::Step third = caps.Feed(true, 13);
      t.Check(third == SweepRules::StopMaxEvents && caps.m_Count == 3 && caps.m_LastEid == 13,
              "rules-max-events-collects-the-last-id",
              "the id that tripped --max-events was not collected");
      SweepRules budget(0, 2);
      budget.Feed(true, 7);
      const SweepRules::Step second = budget.Feed(true, 8);
      t.Check(second == SweepRules::StopIdBudget && budget.m_LastEid == 8,
              "rules-budget-stops-at-the-bound", "the id budget did not stop the sweep");
    }
  }

  // ------------------------------------------------------------------ watch's name rules
  //
  // Device-free, and both rules are the kind that go wrong *quietly*: a substring match watches the
  // wrong variable (`intensityScale` for `intensity`), and a case-sensitive one misses the variable
  // the reader typed. The reflection tree is built by hand for the same reason -- a rule that only
  // a real capture can exercise is a rule nobody can falsify.
  {
    const auto leaf = [](const char *name) {
      ShaderVariable v;
      v.name = rdcstr(name);
      v.type = VarType::Float;
      v.rows = 1;
      v.columns = 1;
      return v;
    };

    ShaderVariable light = leaf("Light");
    light.members.push_back(leaf("intensity"));
    light.members.push_back(leaf("intensityScale"));
    rdcarray<ShaderVariable> vars;
    vars.push_back(light);
    vars.push_back(leaf("Scene"));

    const std::vector<std::string> full = WatchPaths(vars, "Light.intensity");
    t.Check(full.size() == 1 && full[0] == "Light.intensity", "watch-name-full-path",
            "a dotted member path did not match itself");
    const std::vector<std::string> bare = WatchPaths(vars, "intensity");
    t.Check(bare.size() == 1 && bare[0] == "Light.intensity", "watch-name-bare-member",
            "a bare member name did not match the member it names");
    t.Check(WatchPaths(vars, "Intensity").size() == 1, "watch-name-ignores-case",
            "a name typed in another case did not match");
    t.Check(WatchPaths(vars, "intensityScale").size() == 1, "watch-name-is-not-a-substring",
            "a substring of a longer member name matched it");

    // A request for a *struct* is a request for its leaves: a struct's own value text is `-`, which
    // is not an answer to "watch Light".
    const std::vector<std::string> whole = WatchPaths(vars, "Light");
    t.Check(whole.size() == 2 && whole[0] == "Light.intensity" && whole[1] == "Light.intensityScale",
            "watch-name-struct-reports-its-members",
            "watching a struct did not report the members underneath it");

    // The whole path is the whole path: `Light` is not a prefix match for `Light.intensity` unless
    // the request is a bare member *name*, which this one is not.
    t.Check(!WatchNameMatches("Light.intensity", "Light.foo"), "watch-name-no-prefix-match",
            "a path matched a different member path");
    t.Check(WatchPaths(vars, "Nothing").empty(), "watch-name-nothing-matches-nothing",
            "a name that is not in the tree matched something");
  }

  // ------------------------------------------------------------------ the probe's range and cache
  //
  // Also no device: `ProbeUntil` is arithmetic over two numbers, and the cache is a text file. What is
  // being pinned is the pair of decisions a reader cannot check from a command line -- which ids a
  // `probe` scans (the frame's own end, not a guess: a capped range on a five-figure frame answers
  // "nothing has state", which is a fact about the range) and which cached answers are trusted
  // (another capture's, another engine's, another bound's and a truncated one are all "sweep again").
  {
    t.Check(ProbeUntil(0, 1186) == 1186, "probe-until-no-cap-is-the-frame",
            "a cap of 0 did not mean the frame's own last event");
    t.Check(ProbeUntil(2000, 1186) == 1186, "probe-until-cap-past-the-frame-is-the-frame",
            "a cap past the frame's end scanned past the frame");
    t.Check(ProbeUntil(500, 1186) == 500, "probe-until-cap-below-the-frame-is-the-cap",
            "a cap inside the frame was not honoured");
    t.Check(ProbeUntil(500, 0) == 500, "probe-until-no-derivable-bound-falls-back-to-the-cap",
            "a frame with no action list lost its cap");
    t.Check(ProbeUntil(0, 0) == 0, "probe-until-nothing-to-scan", "nothing scanned as something");

    const std::string cachePath = DefaultLogStem() + ".probe";
    const std::string capturePath = DefaultLogStem() + ".probe.rdc";
    FILE *captureFile = FileOpen(capturePath, "wb");
    if(captureFile == NULL)
      return Fail(1, "cannot write %s for the selftest", capturePath.c_str());
    fwrite("RDOC", 1, 4, captureFile);
    fclose(captureFile);

    ProbeRow one;
    one.m_Eid = 12;
    one.m_Shaders = 2;
    one.m_RootSig = "1060";
    one.m_Params = 3;
    ProbeRow two;
    two.m_Eid = 700;
    two.m_Shaders = 1;
    two.m_RootSig = "0";
    two.m_Params = 0;
    ProbeCache written;
    written.m_Scanned = 900;
    written.m_Rows.push_back(one);
    written.m_Rows.push_back(two);
    WriteProbeCache(cachePath, capturePath.c_str(), 1186, written);

    ProbeCache read;
    t.Check(ReadProbeCache(cachePath, capturePath.c_str(), 1186, read) && read.m_Scanned == 900 &&
                read.m_Rows.size() == 2 && read.m_Rows[0].m_Eid == 12 &&
                read.m_Rows[0].m_RootSig == "1060" && read.m_Rows[1].m_Eid == 700,
            "probe-cache-round-trips", "the probe cache did not read back what was written");

    ProbeCache other;
    t.Check(!ReadProbeCache(cachePath, (capturePath + ".other").c_str(), 1186, other),
            "probe-cache-refuses-another-capture", "another capture's path was accepted");
    t.Check(!ReadProbeCache(cachePath, capturePath.c_str(), 1187, other),
            "probe-cache-refuses-another-bound", "a different frame bound was accepted");
    t.Check(!ReadProbeCache(cachePath + ".missing", capturePath.c_str(), 1186, other),
            "probe-cache-refuses-a-missing-file", "a missing file produced an answer");

    // A file cut short: the header is there, the rows are not. Refused rather than half-believed.
    {
      FILE *f = FileOpen(cachePath, "rb");
      std::string text;
      char line[1024];
      while(f != NULL && fgets(line, sizeof(line), f) != NULL)
        text += line;
      if(f != NULL)
        fclose(f);
      const size_t cut = text.size() / 2;
      FILE *out = FileOpen(cachePath, "wb");
      if(out == NULL)
        return Fail(1, "cannot rewrite %s for the selftest", cachePath.c_str());
      fwrite(text.data(), 1, cut, out);
      fclose(out);
      t.Check(!ReadProbeCache(cachePath, capturePath.c_str(), 1186, other),
              "probe-cache-refuses-a-truncated-file", "a truncated cache was believed");
    }

    RemoveQuiet(cachePath);
    RemoveQuiet(capturePath);
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
