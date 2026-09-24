// z.commands_image — the frame's pictures: a contact sheet of the passes, and a difference of two images
//
// Part of replay_dump; the internal API is declared in common.h.
//
// The visual half of the analysis: `sheet` answers "what did each pass produce" in one look, and
// `imgdiff` answers "did this change the picture" with a number rather than an opinion. Together with
// `patch` (commands_patch.cpp) they are the differential-replay pair: change one thing, render again,
// and read the difference.

#include "common.h"

namespace
{
//: One pass the sheet can show: the marker, and the last call inside it -- the event whose render
//: target holds everything that pass drew.
struct SheetPass
{
  int m_MarkerEid = 0;
  int m_ImageEid = 0;    // the last call inside it, replaced by the event actually rendered
  int m_Depth = 0;
  int m_Calls = 0;
  std::string m_Name;
  std::string m_Path;    // the full `A > B` path, so a row can be pasted into `--at-marker`
  std::vector<int> m_CallEids;    // every call inside it, in order: the sheet walks back from the end
};

//: Every marker of the frame that contains at least one call, in event order, with the last call
//: inside it. A marker that only contains other markers is a heading rather than a pass; a marker
//: that contains calls is what a reader means by one.
std::vector<SheetPass> PassList(IReplayController *ctrl)
{
  int ignored = 0;
  bool bTruncated = false;
  const std::vector<ActionNode> rows = ActionTree(ctrl, ignored, bTruncated);

  std::vector<SheetPass> passes;
  for(size_t i = 0; i < rows.size(); i++)
  {
    if(!rows[i].m_bMarker)
      continue;
    const int depth = rows[i].m_Depth;
    int lastCall = 0, seen = 0;
    std::vector<int> callEids;
    for(size_t j = i + 1; j < rows.size() && rows[j].m_Depth > depth; j++)
    {
      if(rows[j].m_bCall)
      {
        lastCall = rows[j].m_Eid;
        callEids.push_back(lastCall);
        seen++;
      }
    }
    if(lastCall == 0)
      continue;

    SheetPass pass;
    pass.m_MarkerEid = rows[i].m_Eid;
    pass.m_ImageEid = lastCall;
    pass.m_Depth = depth;
    pass.m_Calls = seen;
    pass.m_CallEids = callEids;
    pass.m_Name = std::string(rows[i].m_Name.c_str());
    pass.m_Path = std::string(rows[i].m_Path.c_str());
    if(!pass.m_Path.empty())
      pass.m_Path += " > ";
    pass.m_Path += pass.m_Name;
    passes.push_back(pass);
  }
  return passes;
}

//: A file name for a pass: its name with everything a file name cannot hold turned into `-`.
//: Windows refuses `: < > " / \ | ? *` and a trailing dot, and a pass name is engine-supplied text
//: -- so this is the difference between a usable sheet and a write failure in the middle of one.
std::string SlugOf(const std::string &name)
{
  std::string out;
  for(size_t i = 0; i < name.size() && out.size() < 48; i++)
  {
    const char c = name[i];
    const bool bBad = c == ':' || c == '<' || c == '>' || c == '"' || c == '/' || c == '\\' ||
                      c == '|' || c == '?' || c == '*' || c == ' ' || c == '\t';
    out += bBad ? '-' : c;
  }
  while(!out.empty() && (out[out.size() - 1] == '-' || out[out.size() - 1] == '.'))
    out.erase(out.size() - 1);
  return out.empty() ? std::string("pass") : out;
}

std::string HashText(uint64_t hash)
{
  return Fmt("%016llx", (unsigned long long)hash);
}

//: The smallest square that holds `count` tiles, so a sheet of 5 passes is 3x2 rather than 5x1.
int ColumnsFor(int count)
{
  int columns = 1;
  while(columns * columns < count)
    columns++;
  return columns;
}
}    // namespace

//: `sheet <rdc> [--out <dir>] [--every N] [--max N] [--tile N] [--list] [--json]`: one image per
//: pass, a montage of them, and an index that says which pass each tile is.
//:
//: The image is taken at the pass's *last* call, the only event where the target holds everything
//: the pass drew, and it is the first bound render target of that event's state (a depth-only pass
//: shows a later slot rather than nothing). A pass whose target cannot be read is listed with the
//: reason instead of a file -- a sheet with a labelled gap beats a sheet that silently lost a pass.
//:
//: Files land in `<dir>` and existing ones are overwritten: the names are `NN-<pass>.bmp`, and the
//: index lists exactly what this run wrote. `--list` writes nothing and prints what the sheet would
//: contain, which is how you find out how many passes a frame has before rendering any of them.
int CmdSheet(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *outDir,
             int every, int maxPasses, int tileWidth, bool bList)
{
  if(every <= 0)
    every = 1;
  if(maxPasses <= 0)
    maxPasses = 24;
  if(tileWidth <= 0)
    tileWidth = 320;
  const std::string dir = outDir == NULL ? std::string("sheet") : std::string(outDir);

  const std::vector<SheetPass> all = PassList(ctrl);

  // Thin first, then cap: `--every` is "one in N passes", and applying `--max` before it would
  // shift which passes land in the sheet as a frame grows.
  std::vector<SheetPass> chosen;
  for(size_t i = 0; i < all.size() && (int)chosen.size() < maxPasses; i += (size_t)every)
    chosen.push_back(all[i]);

  PrintCaptureHeader(file, path);
  Field("outDir", bList ? std::string() : dir);
  Field("passesInFrame", (long long)all.size());
  Field("every", (long long)every);
  Field("max", (long long)maxPasses);
  Field("tile", (long long)tileWidth);

  if(bList)
  {
    Field("shown", (long long)chosen.size());
    if(g_bJson)
    {
      ArrayOpen("passes");
      for(size_t i = 0; i < chosen.size(); i++)
        ObjectRow(
            Fmt("{\"markerEid\": %d, \"imageEid\": %d, \"depth\": %d, \"calls\": %d, "
                "\"name\": \"%s\", \"path\": \"%s\"}",
                chosen[i].m_MarkerEid, chosen[i].m_ImageEid, chosen[i].m_Depth, chosen[i].m_Calls,
                JsonEscape(chosen[i].m_Name).c_str(), JsonEscape(chosen[i].m_Path).c_str()));
      ArrayClose(true);
      printf("}\n");
      return 0;
    }
    printf("%-4s %-6s %-7s %-5s %-5s %s\n", "#", "marker", "image", "calls", "depth", "pass");
    for(size_t i = 0; i < chosen.size(); i++)
      printf("%-4d %-6d %-7d %-5d %-5d %s\n", (int)i + 1, chosen[i].m_MarkerEid,
             chosen[i].m_ImageEid, chosen[i].m_Calls, chosen[i].m_Depth, chosen[i].m_Path.c_str());
    return 0;
  }

  if(!MakeDir(dir))
    return Fail(1, "cannot create the sheet directory %s", dir.c_str());

  struct Tile
  {
    SheetPass m_Pass;
    std::string m_File;
    int m_Width = 0;
    int m_Height = 0;
    uint64_t m_Hash = 0;
    std::string m_Note;
  };
  std::vector<Tile> tiles;
  std::vector<ImageData> thumbs;

  Progress progress;
  progress.Begin("sheet: passes", (int)chosen.size());
  int shown = 0, skipped = 0;
  for(size_t i = 0; i < chosen.size(); i++)
  {
    // A pass's *last* call is often not the one that shows anything: a barrier or a clear at the
    // end of a pass has no bound state at all (measured: `Clear`'s last call, eid 813, has no
    // render target, no depth target and no root signature). So walk back through the pass's calls
    // and render the first one that has a target -- the event where the pass's output exists. A
    // colour target wins over the depth target, and a pass with neither is reported rather than
    // dropped.
    const int kMaxWalkBack = 12;
    const std::vector<int> &passCalls = chosen[i].m_CallEids;
    int imageEid = chosen[i].m_ImageEid;
    ResourceId target = ResourceId::Null();
    bool bDepthOnly = false;
    for(size_t back = 0; back < passCalls.size() && (int)back < kMaxWalkBack; back++)
    {
      const int eid = passCalls[passCalls.size() - 1 - back];
      MoveToEvent(ctrl, eid);
      const D3D12Pipe::State *st = ctrl->GetD3D12PipelineState();
      const ResourceId rt = FirstRenderTarget(st);
      if(rt != ResourceId::Null())
      {
        target = rt;
        imageEid = eid;
        bDepthOnly = false;
        break;
      }
      if(target == ResourceId::Null() && st != NULL &&
         st->outputMerger.depthTarget.resource != ResourceId::Null())
      {
        target = st->outputMerger.depthTarget.resource;
        imageEid = eid;
        bDepthOnly = true;
      }
    }

    Tile tile;
    tile.m_Pass = chosen[i];
    tile.m_Pass.m_ImageEid = imageEid;
    ImageData img;
    std::string why;
    if(!ReadTargetImage(ctrl, target, PictureOptions(), img, why))
    {
      tile.m_Note = why;
      skipped++;
    }
    else
    {
      tile.m_File = Fmt("%02d-%s.bmp", (int)i + 1, SlugOf(chosen[i].m_Name).c_str());
      const std::string full = (std::filesystem::path(dir) / tile.m_File).string();
      if(!WriteBMPImage(full.c_str(), img))
      {
        tile.m_File.clear();
        tile.m_Note = Fmt("could not write %s", full.c_str());
        skipped++;
      }
      else
      {
        tile.m_Width = img.m_Width;
        tile.m_Height = img.m_Height;
        tile.m_Hash = DifferenceHash(img);
        thumbs.push_back(DownscaleImage(img, tileWidth, tileWidth));
        shown++;
        if(bDepthOnly)
          tile.m_Note = "the depth target: no colour target was bound in this pass";
      }
    }
    tiles.push_back(tile);
    progress.Tick((int)i + 1);
  }
  progress.Done((int)chosen.size());

  // The montage and the index, both written even when a pass or two could not be shown: the index
  // says which is which, and that is the difference between a sheet and a pile of files.
  const std::string montage = (std::filesystem::path(dir) / "sheet.bmp").string();
  const ImageData sheet =
      MakeMontage(thumbs, ColumnsFor((int)thumbs.size()), tileWidth, tileWidth, 6);
  const bool bMontage = sheet.Valid() && WriteBMPImage(montage.c_str(), sheet);

  const std::string indexPath = (std::filesystem::path(dir) / "sheet.md").string();
  FILE *md = FileOpen(indexPath, "wb");
  if(md == NULL)
    return Fail(1, "cannot write the sheet index %s", indexPath.c_str());

  fprintf(md, "# Contact sheet\n\n`%s`\n\n", path);
  fprintf(md,
          "%d pass(es) of %d in the frame, one image per pass, taken at the pass's last call.\n\n",
          (int)chosen.size(), (int)all.size());
  if(bMontage)
    fprintf(md, "![contact sheet](sheet.bmp)\n\n");
  fprintf(md, "| # | pass | marker eid | image eid | calls | depth | file |\n");
  fprintf(md, "|---|---|---|---|---|---|---|\n");
  for(size_t i = 0; i < tiles.size(); i++)
    fprintf(md, "| %d | %s | %d | %d | %d | %d | %s |\n", (int)i + 1,
            tiles[i].m_Pass.m_Name.c_str(), tiles[i].m_Pass.m_MarkerEid, tiles[i].m_Pass.m_ImageEid,
            tiles[i].m_Pass.m_Calls, tiles[i].m_Pass.m_Depth,
            tiles[i].m_File.empty() ? Fmt("- (%s)", tiles[i].m_Note.c_str()).c_str()
                                    : tiles[i].m_File.c_str());
  fclose(md);

  Field("shown", (long long)shown);
  Field("skipped", (long long)skipped);
  Field("montage", bMontage ? montage : std::string());
  Field("index", indexPath);
  if(g_bJson)
  {
    ArrayOpen("passes");
    for(size_t i = 0; i < tiles.size(); i++)
      ObjectRow(
          Fmt("{\"markerEid\": %d, \"imageEid\": %d, \"depth\": %d, \"calls\": %d, "
              "\"name\": \"%s\", \"path\": \"%s\", \"file\": \"%s\", \"width\": %d, "
              "\"height\": %d, \"hash\": \"%s\", \"note\": \"%s\"}",
              tiles[i].m_Pass.m_MarkerEid, tiles[i].m_Pass.m_ImageEid, tiles[i].m_Pass.m_Depth,
              tiles[i].m_Pass.m_Calls, JsonEscape(tiles[i].m_Pass.m_Name).c_str(),
              JsonEscape(tiles[i].m_Pass.m_Path).c_str(), JsonEscape(tiles[i].m_File).c_str(),
              tiles[i].m_Width, tiles[i].m_Height, HashText(tiles[i].m_Hash).c_str(),
              JsonEscape(tiles[i].m_Note).c_str()));
    ArrayClose(true);
    printf("}\n");
  }
  else
  {
    printf("%-4s %-6s %-7s %-8s %s\n", "#", "marker", "image", "hash", "file / why not");
    for(size_t i = 0; i < tiles.size(); i++)
      printf("%-4d %-6d %-7d %-8s %s\n", (int)i + 1, tiles[i].m_Pass.m_MarkerEid,
             tiles[i].m_Pass.m_ImageEid, HashText(tiles[i].m_Hash).substr(0, 8).c_str(),
             tiles[i].m_File.empty() ? tiles[i].m_Note.c_str() : tiles[i].m_File.c_str());
  }

  if(shown == 0)
    return Fail(1, "no pass in this frame produced a readable render target");
  Log("sheet: %d image(s), a montage and an index in %s", shown, dir.c_str());
  return 0;
}

//: `imgdiff <a.bmp> <b.bmp> [--out <heat.bmp>] [--json]`: how two images differ, exactly and
//: perceptually.
//:
//: Two numbers, because one is not enough: *how many pixels changed* is what a person means by "did
//: this change the picture", and the 64-bit difference hash says whether a human would see it -- a
//: re-render on another driver differs in a handful of pixels and in none of the hash's bits, while a
//: shader that stopped writing colour differs in both. The sizes must match: comparing a 1920x1080
//: image with a 1280x720 one pixel by pixel is not a smaller difference, it is a different question.
int CmdImgDiff(ICaptureFile *file, const char *path, const char *aPath, const char *bPath,
               const char *outPath)
{
  ImageData a, b;
  std::string why;
  if(!ReadBMPImage(aPath, a, why))
    return Fail(1, "%s", why.c_str());
  if(!ReadBMPImage(bPath, b, why))
    return Fail(1, "%s", why.c_str());
  if(a.m_Width != b.m_Width || a.m_Height != b.m_Height)
    return Fail(1, "%s is %dx%d and %s is %dx%d: a difference needs two images of one size", aPath,
                a.m_Width, a.m_Height, bPath, b.m_Width, b.m_Height);

  ImageData heat;
  int maxDelta = 0;
  long long sumDelta = 0;
  const long long differing =
      ImagePixelDelta(a, b, maxDelta, sumDelta, outPath == NULL ? NULL : &heat);
  if(differing < 0)
    return Fail(1, "the two images could not be compared");

  const long long pixels = (long long)a.m_Width * (long long)a.m_Height;
  const uint64_t hashA = DifferenceHash(a), hashB = DifferenceHash(b);
  int hamming = 0;
  for(int bit = 0; bit < 64; bit++)
  {
    if(((hashA >> bit) & 1u) != ((hashB >> bit) & 1u))
      hamming++;
  }

  bool bWrote = true;
  if(outPath != NULL)
    bWrote = WriteBMPImage(outPath, heat);

  PrintCaptureHeader(file, path);
  Field("a", std::string(aPath));
  Field("b", std::string(bPath));
  Field("width", (long long)a.m_Width);
  Field("height", (long long)a.m_Height);
  Field("pixels", pixels);
  Field("differing", differing);
  // Strings, because the writer has no fractional field (see `Field` in common.h): three decimals,
  // so two runs are comparable by eye and by a consumer that parses them.
  Field("percentDiffering",
        Fmt("%.3f", pixels == 0 ? 0.0 : 100.0 * (double)differing / (double)pixels));
  Field("meanDelta", Fmt("%.3f", differing == 0 ? 0.0 : (double)sumDelta / (double)differing));
  Field("maxDelta", (long long)maxDelta);
  Field("hashA", HashText(hashA));
  Field("hashB", HashText(hashB));
  Field("hashDistance", (long long)hamming);
  Flag("identical", differing == 0);
  Flag("visuallySame", hamming <= 2);
  Field("heatMap", outPath == NULL ? std::string() : std::string(outPath), true);
  if(g_bJson)
    printf("}\n");

  if(!bWrote)
    Log("imgdiff: the heat map could not be written to %s", outPath);

  // The verdict a caller would script on, in one line and in both formats.
  if(differing == 0)
    Log("imgdiff: the two images are identical");
  else if(hamming <= 2)
    Log("imgdiff: %lld pixel(s) differ (%.3f%%) but the two look the same (hash distance %d of 64)",
        differing, 100.0 * (double)differing / (double)pixels, hamming);
  else
    Log("imgdiff: the images differ -- %lld pixel(s) (%.3f%%), worst channel %d, hash distance %d "
        "of 64",
        differing, 100.0 * (double)differing / (double)pixels, maxDelta, hamming);
  return 0;
}

// --------------------------------------------------------------------------- a cubemap, six ways

//: `cubemap <rdc> <resId|name> [outDir=cross]` (REFERENCE §9): an environment map as six pictures
//: plus the engine's own cruciform.
//:
//: Why a command of its own rather than a switch on `textures --save`: what makes a cubemap
//: reviewable is the *layout* -- six faces named in the order a viewer expects, and the unfolded
//: cross beside them -- and `TextureSliceMapping::cubeCruciform` is the engine drawing that cross.
//: Reassembling one out of six bitmaps by hand is exactly where the rotations go wrong (the `+z`
//: face is not the one a reader assumes), so the layout is the engine's and this command is the
//: names, the summary and the options around it.
bool ChannelFlags(const char *text, rdcfixedarray<bool, 4> &out, std::string &why)
{
  // `rgba` is the default and the letters are the four channels, in any order and any subset: "rgb"
  // is what a person asks for when the alpha of an opaque target would swamp the bars, and "a" when
  // only the alpha matters. A letter that is not one of the four is refused rather than ignored: a
  // flag that is silently dropped is a chart of something other than what was asked. Every member
  // is written on every path, including the default one: the caller hands in an uninitialised
  // array, so a path that returned true without filling it would be four indeterminate booleans
  // deciding what the chart counts.
  out[0] = out[1] = out[2] = out[3] = (text == NULL || text[0] == '\0');
  if(text == NULL || text[0] == '\0')
    return true;
  for(const char *p = text; *p != '\0'; p++)
  {
    const char c = (char)tolower((unsigned char)*p);
    if(c == 'r')
      out[0] = true;
    else if(c == 'g')
      out[1] = true;
    else if(c == 'b')
      out[2] = true;
    else if(c == 'a')
      out[3] = true;
    else
    {
      why = Fmt("'%c' is not a channel (r, g, b, a)", c);
      return false;
    }
  }
  if(!out[0] && !out[1] && !out[2] && !out[3])
  {
    why = "--channels needs at least one of r, g, b, a";
    return false;
  }
  return true;
}

std::vector<double> AggregateBuckets(const rdcarray<uint32_t> &buckets, int rows)
{
  // The engine's own bucket count is not a parameter (`GetHistogram` takes the range, not the
  // count), so the bars are drawn from *its* buckets rather than from a resampled range: adjacent
  // buckets are summed into `rows` groups for the terminal, and `--json` carries the engine's own
  // numbers untouched. A count of buckets below the row limit is drawn as it is, so the bars never
  // claim a resolution the engine did not give.
  std::vector<double> out;
  const int want = rows > 0 ? rows : (int)buckets.size();
  if(buckets.empty() || want >= (int)buckets.size())
  {
    out.reserve(buckets.size());
    for(size_t i = 0; i < buckets.size(); i++)
      out.push_back((double)buckets[i]);
    return out;
  }
  out.assign((size_t)want, 0.0);
  for(size_t i = 0; i < buckets.size(); i++)
  {
    // Integer arithmetic on the index, so the groups partition the buckets exactly: the last one
    // takes the remainder rather than dropping it.
    const size_t group = (size_t)((uint64_t)i * (uint64_t)want / (uint64_t)buckets.size());
    out[group] += (double)buckets[i];
  }
  return out;
}

int CmdHistogram(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *what,
                 int eid, const PictureOptions &opts, int rows, const char *channelText)
{
  // A picture's statistics: the numeric form of "is this target black, blown out, clipping?" -- the
  // three questions a person answers today by opening a PNG and looking at it. `GetMinMax` and
  // `GetHistogram` are the engine's own answers, and the range the buckets cover is the one the
  // *caller* passes, so the min/max is asked for first and the histogram is asked over exactly it
  // (REFERENCE §9).
  std::string name;
  std::string why;
  ResourceId id;
  if(eid != 0)
  {
    MoveToEvent(ctrl, eid);
    const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
    id = d3d12 && !d3d12->outputMerger.renderTargets.empty()
             ? d3d12->outputMerger.renderTargets[0].resource
             : ResourceId::Null();
    if(id == ResourceId::Null())
      return Fail(1, "nothing is bound to render target 0 at eid %d", eid);
  }
  else
  {
    id = ResolveResourceArg(ctrl, what, name, why);
    if(id == ResourceId::Null())
      return Fail(1, "%s", why.c_str());
  }

  const TextureDescription *found = NULL;
  for(const TextureDescription &texture : ctrl->GetTextures())
  {
    if(texture.resourceId == id)
    {
      found = &texture;
      break;
    }
  }
  if(found == NULL)
    return Fail(1, "res%s is not a texture this capture created", IdText(id).c_str());

  rdcfixedarray<bool, 4> channels;
  if(!ChannelFlags(channelText, channels, why))
    return Fail(2, "%s", why.c_str());

  // What the values are *read as*: the cast when one was asked for, and the resource's own
  // component type otherwise. `Typeless` is a request ("use the target's type"), not a class -- and
  // the two callers below need a class to know which member of `PixelValue`'s union holds the
  // number, so passing `Typeless` straight through is how a float target's min/max comes back as
  // the bit patterns of its floats (measured: a `R11G11B10_FLOAT` target answered `1055653888` for
  // a maximum, which is a float read as a `uint`).
  const CompType readAs = opts.m_bCastGiven ? opts.m_Cast : found->format.compType;
  const rdcpair<PixelValue, PixelValue> minmax = ctrl->GetMinMax(id, opts.m_Sub, readAs);
  const float lo = MinMaxComponent(minmax.first, readAs, 0);
  const float hi = MinMaxComponent(minmax.second, readAs, 0);
  const rdcarray<uint32_t> buckets = ctrl->GetHistogram(id, opts.m_Sub, readAs, lo, hi, channels);

  const std::vector<double> bars = AggregateBuckets(buckets, rows);
  double widest = 0.0;
  double counted = 0.0;
  for(size_t i = 0; i < buckets.size(); i++)
    counted += (double)buckets[i];
  for(size_t i = 0; i < bars.size(); i++)
    widest = std::max(widest, bars[i]);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  if(!name.empty())
    Field("name", name);
  Field("resource", Fmt("res%s", IdText(id).c_str()));
  Field("description",
        Fmt("%ux%ux%u %s", found->width, found->height, found->depth, found->format.Name().c_str()));
  Field("mip", (long long)opts.m_Sub.mip);
  Field("slice", (long long)opts.m_Sub.slice);
  Field("sample", (long long)opts.m_Sub.sample);
  Field("cast", std::string(CastText(readAs)));
  Field("channels", std::string(channelText == NULL ? "rgba" : channelText));

  if(g_bJson)
  {
    // The min and the max as the engine's own text for them (`PixelValueText`), the same string the
    // terminal prints: the values are per-component and of whichever class the cast asked for, and
    // the writer has no fractional field (`counters --per-pass` writes its costs as text for the
    // same reason).
    Field("min", PixelValueText(minmax.first, readAs));
    Field("max", PixelValueText(minmax.second, readAs));
    Field("bucketCount", (long long)buckets.size());
    Field("low", Fmt("%g", lo));
    Field("high", Fmt("%g", hi));
    ArrayOpen("buckets");
    for(size_t i = 0; i < buckets.size(); i++)
      Row(Fmt("%u", (unsigned)buckets[i]));
    ArrayClose(false);
    Field("counted", (long long)counted, true);
  }
  else
  {
    Row(Fmt("values    : min %s  max %s", PixelValueText(minmax.first, readAs).c_str(),
            PixelValueText(minmax.second, readAs).c_str()));
    Row(Fmt("buckets   : %d over [%g, %g], channels %s, %lld value(s) counted", (int)buckets.size(),
            lo, hi, channelText == NULL ? "rgba" : channelText, (long long)counted));
    if(bars.empty())
      Row(std::string("          no value fell inside the range the engine reports"));
    for(size_t i = 0; i < bars.size(); i++)
    {
      const int width = widest > 0.0 ? (int)(bars[i] / widest * 40.0 + 0.5) : 0;
      Row(Fmt("%4d %-10s %s %.4g", (int)i, BucketRangeText((int)i, (int)bars.size(), lo, hi).c_str(),
              std::string((size_t)width, '#').c_str(), bars[i]));
    }
  }
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return 0;
}

std::string BucketRangeText(int index, int count, float low, float high)
{
  // The range a *display* row covers: the engine's buckets are grouped for the terminal, so the text says
  // the span of the group rather than of one bucket, and `--json` carries the engine's own array.
  const double span = (double)(high - low) / (double)(count > 0 ? count : 1);
  const double from = low + span * (double)index;
  const double to = from + span;
  return Fmt("%.4g..%.4g", from, to);
}

float MinMaxComponent(const PixelValue &value, CompType type, int channel)
{
  // Which member of the union is meaningful is the counter's own business here too (`PixelValue` is
  // one union and the cast says which member to read): reading `floatValue` for a `uint` target is
  // the same mistake a counter's `.d` was, and it is what a cast of `uint` exists to avoid.
  if(channel < 0 || channel > 3)
    return 0.0f;
  switch(ComponentClass(type))
  {
    case CompType::UInt: return (float)value.uintValue[(size_t)channel];
    case CompType::SInt: return (float)value.intValue[(size_t)channel];
    default: return value.floatValue[(size_t)channel];
  }
}

int CmdCubemap(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *what,
               const char *outDir, const PictureOptions &opts)
{
  // D3D's own face order for a cube array -- +X, -X, +Y, -Y, +Z, -Z -- which is also the order a
  // viewer reads a `px nx py ny pz nz` set in, so no mapping table is needed at either end.
  static const char *const kFaces[] = {"px", "nx", "py", "ny", "pz", "nz"};

  std::string name, why;
  const ResourceId id = ResolveResourceArg(ctrl, what, name, why);
  if(id == ResourceId::Null())
    return Fail(1, "%s", why.c_str());

  const rdcarray<TextureDescription> &texs = ctrl->GetTextures();
  const TextureDescription *found = NULL;
  for(size_t i = 0; i < texs.size() && found == NULL; i++)
  {
    if(texs[i].resourceId == id)
      found = &texs[i];
  }
  if(found == NULL)
    return Fail(1, "res%s is not a texture this engine can describe, so its faces cannot be named",
                IdText(id).c_str());
  // `cubemap` is the engine's own flag for it, and the slice count is the second half of the same
  // fact: a cube is an array of six, and the cruciform below needs all six to be there.
  if(!found->cubemap || found->arraysize < 6)
    return Fail(
        1,
        "res%s is not a cubemap (%u slice(s)): `textures --save <dir> --slice N` writes one "
        "subresource of it",
        IdText(id).c_str(), (unsigned)found->arraysize);

  const std::string dir = outDir == NULL ? std::string("cross") : std::string(outDir);
  if(!MakeDir(dir))
    return Fail(1, "cannot create the cubemap directory %s", dir.c_str());

  int written = 0;
  for(int face = 0; face < 6; face++)
  {
    PictureOptions faceOpts = opts;
    faceOpts.m_Sub.slice =
        (uint32_t)face;    // a face is one slice of the array, 0..5 in D3D's order
    TextureSave save;
    save.resourceId = id;
    save.destType = FileType::PNG;
    ApplySaveOptions(save, faceOpts);
    const std::string out = (std::filesystem::path(dir) / Fmt("%s.png", kFaces[face])).string();
    const ResultDetails res = ctrl->SaveTexture(save, rdcstr(out.c_str()));
    if(res.OK())
    {
      written++;
      Log("cubemap: %s -> %s", kFaces[face], out.c_str());
    }
    else
    {
      fprintf(stderr, "  warning: could not save face %s of res%s: %s\n", kFaces[face],
              IdText(id).c_str(), ResultText(res).c_str());
    }
  }

  // The cruciform: one save with every slice and `cubeCruciform`, which is the engine laying the
  // six faces out as the unfolded cross and filling the gaps with transparent black. The
  // `--mip`/`--cast`/`--range` options still apply -- a low mip is what a reflection probe actually
  // samples.
  std::string cross;
  {
    TextureSave save;
    save.resourceId = id;
    save.destType = FileType::PNG;
    ApplySaveOptions(save, opts);
    save.slice.sliceIndex = -1;    // every slice: the six faces
    save.slice.cubeCruciform = true;
    cross = (std::filesystem::path(dir) / "cross.png").string();
    const ResultDetails res = ctrl->SaveTexture(save, rdcstr(cross.c_str()));
    if(res.OK())
    {
      Log("cubemap: cruciform -> %s", cross.c_str());
    }
    else
    {
      fprintf(stderr, "  warning: could not write the cruciform: %s\n", ResultText(res).c_str());
      cross.clear();
    }
  }

  PrintCaptureHeader(file, path);
  Field("resource", IdText(id));
  Field("name", name);
  Field("directory", dir);
  Field("faceSize", (long long)found->width);
  Field("mips", (long long)found->mips);
  Field("mip", (long long)opts.m_Sub.mip);
  Field("format", std::string(found->format.Name().c_str()));
  Field("faces", (long long)written);
  Field("cross", cross, true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");

  // Exit 1 on a partial set: five faces and no cross is a directory a caller would otherwise take
  // for a finished job, and the warning above is the reason.
  if(written < 6)
    return Fail(1, "%d of 6 face(s) and %s could be written", written,
                cross.empty() ? "no cross" : "a cross");
  return cross.empty() ? 1 : 0;
}
