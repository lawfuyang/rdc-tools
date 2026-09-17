// z.bundle — the bundle producer and verifier the offline report reads
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

std::string Sha256File(const char *path)
{
  BCRYPT_ALG_HANDLE alg = NULL;
  BCRYPT_HASH_HANDLE hash = NULL;
  std::string hex;

  if(BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0)
  {
    fprintf(stderr, "warning: no SHA-256 provider available; the manifest will have no hashes\n");
    return hex;
  }

  DWORD objectBytes = 0, ignored = 0;
  if(BCryptGetProperty(alg, BCRYPT_OBJECT_LENGTH, (PUCHAR)&objectBytes, sizeof(objectBytes),
                       &ignored, 0) < 0)
    objectBytes = 0;
  std::vector<unsigned char> object(objectBytes);
  unsigned char digest[32];

  FILE *f = fopen(path, "rb");
  bool ok = false;
  if(f != NULL)
  {
    ok = BCryptCreateHash(alg, &hash, object.empty() ? NULL : object.data(), (ULONG)object.size(),
                          NULL, 0, 0) >= 0;
    if(ok)
    {
      std::vector<unsigned char> buf(65536);
      size_t n = 0;
      while((n = fread(buf.data(), 1, buf.size(), f)) > 0)
      {
        if(BCryptHashData(hash, (PUCHAR)buf.data(), (ULONG)n, 0) < 0)
        {
          ok = false;
          break;
        }
      }
      ok = ok && BCryptFinishHash(hash, digest, (ULONG)sizeof(digest), 0) >= 0;
    }
  }

  if(f != NULL)
    fclose(f);
  if(hash != NULL)
    BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(alg, 0);

  if(!ok)
  {
    fprintf(stderr, "warning: cannot hash %s\n", path);
    return std::string();
  }

  char out[65];
  for(int i = 0; i < 32; i++)
    snprintf(out + i * 2, 3, "%02x", digest[i]);
  return std::string(out, 64);
}

bool ReadWholeFile(const char *path, std::string &text)
{
  FILE *f = fopen(path, "rb");
  if(f == NULL)
    return false;
  text.clear();
  char buf[65536];
  size_t n = 0;
  while((n = fread(buf, 1, sizeof(buf), f)) > 0)
    text.append(buf, n);
  fclose(f);
  return true;
}

bool FileBytes(const char *path, unsigned long long &bytes)
{
  WIN32_FILE_ATTRIBUTE_DATA info;
  if(GetFileAttributesExA(path, GetFileExInfoStandard, &info) == 0)
    return false;
  bytes = ((unsigned long long)info.nFileSizeHigh << 32) | (unsigned long long)info.nFileSizeLow;
  return true;
}

bool MakeDir(const std::string &path)
{
  if(CreateDirectoryA(path.c_str(), NULL) != 0)
    return true;
  return GetLastError() == ERROR_ALREADY_EXISTS;
}

bool DirIsEmpty(const std::string &path, bool &empty)
{
  const std::string pattern = path + "\\*";
  WIN32_FIND_DATAA entry;
  HANDLE find = FindFirstFileA(pattern.c_str(), &entry);
  if(find == INVALID_HANDLE_VALUE)
    return false;
  empty = true;
  do
  {
    if(strcmp(entry.cFileName, ".") != 0 && strcmp(entry.cFileName, "..") != 0)
    {
      empty = false;
      break;
    }
  } while(FindNextFileA(find, &entry) != 0);
  FindClose(find);
  return true;
}

//: A path inside the bundle, relative to its root and with forward slashes, so the manifest reads
//: the same whichever way the root was spelled.
std::string BundleRelative(const std::string &root, const std::string &full)
{
  std::string rel = full.size() > root.size() ? full.substr(root.size()) : full;
  for(size_t i = 0; i < rel.size(); i++)
  {
    if(rel[i] == '\\')
      rel[i] = '/';
  }
  while(!rel.empty() && rel[0] == '/')
    rel.erase(0, 1);
  return rel;
}

//: FNV-1a over the state fields this driver read, so "did anything change between these two events"
//: is one string comparison. It is our hash of our own fields -- the engine exposes no state hash
//: -- and the manifest names exactly which fields go into it.
std::string StateHash(const std::string &text)
{
  uint64_t h = 1469598103934665603ull;
  for(size_t i = 0; i < text.size(); i++)
  {
    h ^= (unsigned char)text[i];
    h *= 1099511628211ull;
  }
  return Fmt("%016llx", (unsigned long long)h);
}

//: Whether an id has anything bound. `probe` uses this exact test, and it has to be this one: the
//: engine exposes no action list, so "is this id an event" is answered by what is there when you
//: ask -- and the pipeline *object* id is not part of it. A forced non-event carries a leftover
//: non-null `pipelineResourceId` (measured: a sweep that tested it reported every id as an event
//: and ran to the guard), while the shaders and the root signature are the fields that are only
//: there for a real one.
bool HasBoundState(const D3D12Pipe::State *st)
{
  if(st == NULL)
    return false;
  if(st->rootSignature.resourceId != ResourceId::Null())
    return true;
  for(int i = 0; i < (int)ShaderStage::Count; i++)
  {
    const D3D12Pipe::Shader *sh = StageShader(st, (ShaderStage)i);
    if(sh != NULL && sh->resourceId != ResourceId::Null())
      return true;
  }
  return false;
}

//: Writes one render target the way the engine displays it: the display readback as a BMP, and when
//: that comes back empty, the engine's own encoder (a PNG beside the BMP's name). `written`
//: receives whichever path was actually used, because the manifest hashes files rather than
//: intentions. Extracted from `CmdImage` so `image` and the bundle save a target through one code
//: path.
int WriteEventDocuments(IReplayController *ctrl, ICaptureFile *file, const char *path,
                        const D3D12Pipe::State *st, int eid, bool wantDisasm,
                        const std::string &bundle, std::vector<std::string> &written)
{
  const std::string statesDir = bundle + "\\states";
  const std::string cbuffersDir = bundle + "\\cbuffers";
  const std::string stem = Fmt("%s\\%d", statesDir.c_str(), eid);

  {
    const JsonDocument json;
    const std::string target = stem + ".state.json";
    const CaptureStdout out(target.c_str());
    if(!out.Ok())
      return 1;
    CmdState(ctrl, file, path, eid);
    written.push_back(BundleRelative(bundle, target));
  }

  {
    const JsonDocument json;
    const std::string target = stem + ".shaders.json";
    const CaptureStdout out(target.c_str());
    if(!out.Ok())
      return 1;
    CmdShaders(ctrl, file, path, eid, wantDisasm);
    written.push_back(BundleRelative(bundle, target));
  }

  for(int i = 0; i < (int)ShaderStage::Count; i++)
  {
    const ShaderStage stage = (ShaderStage)i;
    const D3D12Pipe::Shader *sh = StageShader(st, stage);
    if(sh == NULL || sh->resourceId == ResourceId::Null())
      continue;

    const ShaderReflection *refl =
        ctrl->GetShader(st->pipelineResourceId, sh->resourceId, ShaderEntryPoint(rdcstr(), stage));
    if(refl == NULL)
      continue;

    for(size_t b = 0; b < refl->constantBlocks.size() && b < 64; b++)
    {
      const JsonDocument json;
      const std::string target =
          Fmt("%s\\%d_%s_%d.json", cbuffersDir.c_str(), eid, StageName(stage), (int)b);
      const CaptureStdout out(target.c_str());
      if(!out.Ok())
        return 1;
      CmdCbuffer(ctrl, file, path, eid, stage, (int)b);
      written.push_back(BundleRelative(bundle, target));
    }
  }

  return 0;
}

//: The `dump` command's options, parsed from its argument list here and passed to the writers as
//: one struct rather than as eight parameters that must stay in step. `forceEvents` are extra ids
//: the caller wants documents for even where the state did not change.
struct DumpOptions
{
  std::string outDir = "bundle";
  int since = 1;
  int until = 0;
  int maxEvents = 0;
  bool withImages = false;
  bool withCounters = false;
  bool withTextures = false;
  bool overwrite = false;
  bool noUsage = false;
  std::vector<int> forceEvents;
};

//: Defined with the CLI helpers further down; `dump`'s options take integers, and `atoi` would turn
//: a typo into a plausible number (`--since` becoming 0) instead of saying that it is not a number.

void ParseEventList(const std::string &text, std::vector<int> &out)
{
  size_t start = 0;
  while(start <= text.size())
  {
    const size_t comma = text.find(',', start);
    const std::string token =
        text.substr(start, comma == std::string::npos ? std::string::npos : comma - start);
    int value = 0;
    if(ParseInt(token.c_str(), value) && value > 0)
      out.push_back(value);
    else if(!token.empty())
      fprintf(stderr, "warning: '--events %s': '%s' is not an event id\n", text.c_str(),
              token.c_str());
    if(comma == std::string::npos)
      break;
    start = comma + 1;
  }
}

//: The bundle producer (ROADMAP §2): one replay session, everything the engine alone can answer
//: written to disk, so the offline half can analyse a frame without a device. It is also the reason
//: a crash is survivable: files are written as they are produced, and the manifest lists what was
//: written, so a partial bundle says so.
int CmdDump(IReplayController *ctrl, ICaptureFile *file, const char *path,
            const std::vector<std::string> &args, bool wantDisasm)
{
  DumpOptions opts;
  for(size_t i = 1; i < args.size(); i++)
  {
    const std::string &a = args[i];
    if(a == "--with-images")
      opts.withImages = true;
    else if(a == "--with-counters")
      opts.withCounters = true;
    else if(a == "--textures")
      opts.withTextures = true;
    else if(a == "--overwrite")
      opts.overwrite = true;
    else if(a == "--no-usage")
      opts.noUsage = true;
    else if(a == "--since" && i + 1 < args.size())
      opts.since = ToInt(args[++i], 1);
    else if(a == "--until" && i + 1 < args.size())
      opts.until = ToInt(args[++i], 0);
    else if(a == "--max-events" && i + 1 < args.size())
      opts.maxEvents = ToInt(args[++i], 0);
    else if(a == "--events" && i + 1 < args.size())
      ParseEventList(args[++i], opts.forceEvents);
    else if(a.size() > 2 && a[0] == '-' && a[1] == '-')
      return Fail(2, "unknown option '%s' for dump", a.c_str());
    else
      opts.outDir = a;
  }

  if(opts.since < 1)
    opts.since = 1;

  // The directory has to be ours: writing a bundle into one that already holds another frame's files
  // would leave a mixture no manifest could describe. `--overwrite` says the old contents may be replaced.
  if(!MakeDir(opts.outDir))
    return Fail(1, "cannot create the bundle directory %s", opts.outDir.c_str());
  bool empty = true;
  if(!DirIsEmpty(opts.outDir, empty))
    return Fail(1, "cannot read the bundle directory %s", opts.outDir.c_str());
  if(!empty && !opts.overwrite)
    return Fail(1, "%s is not empty (pass --overwrite to write into it)", opts.outDir.c_str());

  static const char *kSubDirs[] = {"states", "cbuffers", "rt", "textures"};
  for(size_t i = 0; i < sizeof(kSubDirs) / sizeof(kSubDirs[0]); i++)
  {
    if(!MakeDir(opts.outDir + "\\" + kSubDirs[i]))
      return Fail(1, "cannot create %s\\%s", opts.outDir.c_str(), kSubDirs[i]);
  }

  std::vector<std::string> written;    // bundle-relative paths, hashed into the manifest
  std::vector<std::pair<std::string, std::string>> skipped;    // what was not written, and why

  // ------------------------------------------------------------------ the id sweep (first, always)
  //
  // Which ids are events is asked *before* anything else touches the engine, because it is the one
  // question whose answer stops being true afterwards: `SetFrameEvent(n, true)` on an id that is not an
  // event leaves the last replayed event's state in place, so once anything has replayed a real event
  // every forced non-event looks like it has state -- the sweep then never sees an empty run and never
  // ends. (Measured on this capture: `probe 120` alone finds 25-32 ids, and the same `probe 120` after
  // other commands finds ~120. `CmdProbe` carries the same warning.)
  // The sweep cannot *find* the end of a frame: `SetFrameEvent(n, true)` past the last event clamps to
  // it, so every id beyond the frame reports the last event's state and a "no state any more" test never
  // fires. Measured on this capture: ids 1..120 hold 25-32 events, while `probe 4500` reports 4405 ids
  // with state -- and the structured file has 723 chunks, so those extra ids are clamped, not real.
  //
  // What bounds the sweep instead is the file: every event is a chunk, so the chunk count is an upper
  // bound on how many events the frame has. The empty-run test still ends a sweep early on a sparse
  // capture, and `--until` says it exactly. (Deriving the engine's ids from the file is ROADMAP §3.)
  const int kEmptyRun = 256;    // consecutive ids with nothing bound that end a sweep
  const int kHardCap = 200000;
  const size_t idBudget = ctrl->GetStructuredFile().chunks.size();
  const int until = opts.until > 0 ? opts.until : kHardCap;
  int scanned = 0, emptyRun = 0, lastEid = 0;
  const char *stopped = "the end of the scan range";
  std::vector<int> ids;

  Log("bundle: sweeping ids %d..%d for bound state, at most %d id(s) (the file's chunk count)",
      opts.since, until, (int)idBudget);
  for(int eid = opts.since; eid <= until; eid++)
  {
    ctrl->SetFrameEvent(eid, true);
    scanned++;
    if(!HasBoundState(ctrl->GetD3D12PipelineState()))
    {
      if(lastEid > 0 && ++emptyRun >= kEmptyRun)
      {
        stopped = "a run of ids with nothing bound";
        break;
      }
      continue;
    }
    emptyRun = 0;
    lastEid = eid;
    ids.push_back(eid);
    // `--max-events` stops the *sweep*, not just the writing: the sweep is the expensive part on a big
    // capture (each id is a `SetFrameEvent`, ~12 ms here), and on the 1.4 GB capture the file's chunk
    // count -- the budget -- is 29216, which is minutes of walking before anything is written.
    if(opts.maxEvents > 0 && (int)ids.size() >= opts.maxEvents)
    {
      stopped = "--max-events";
      break;
    }
    if(ids.size() >= idBudget)
    {
      stopped = "the id budget (the file's chunk count)";
      break;
    }
    if(scanned % 2000 == 0)
      Log("bundle: swept %d id(s), %d collected so far", scanned, (int)ids.size());
  }
  Log("bundle: %d id(s) collected out of %d scanned (%s)", (int)ids.size(), scanned, stopped);

  // Formats and dimensions come from the resource list, not from the pipeline state: the state
  // names a target, the description says what it is.
  std::vector<std::pair<std::string, const TextureDescription *>> textures;
  for(size_t i = 0; i < ctrl->GetTextures().size(); i++)
    textures.push_back(
        std::make_pair(IdText(ctrl->GetTextures()[i].resourceId), &ctrl->GetTextures()[i]));

  // ------------------------------------------------------------------ capture.json
  {
    const JsonDocument json;
    const CaptureStdout out((opts.outDir + "\\capture.json").c_str());
    if(!out.Ok())
      return Fail(1, "cannot write capture.json in %s", opts.outDir.c_str());

    PrintCaptureHeader(file, path);
    const APIProperties props = ctrl->GetAPIProperties();
    unsigned long long captureBytes = 0;
    Field("pipelineType", (long long)props.pipelineType);
    Field("localRenderer", (long long)props.localRenderer);
    Field("remoteReplay", (long long)props.remoteReplay);
    Field("vendor", (long long)props.vendor);
    Field("shaderDebugging", (long long)props.shaderDebugging);
    Field("pixelHistory", (long long)props.pixelHistory);
    Field("chunks", (long long)ctrl->GetStructuredFile().chunks.size());
    Field("resources", (long long)ctrl->GetResources().size());
    Field("textures", (long long)ctrl->GetTextures().size());
    Field("buffers", (long long)ctrl->GetBuffers().size());
    Field("debugMessages", (long long)ctrl->GetDebugMessages().size());
    Field("captureBytes",
          FileBytes(AbsolutePath(path).c_str(), captureBytes) ? (long long)captureBytes : 0);
    Field("absPath", AbsolutePath(path),
          true);    // the object's last member: a comma here is not JSON
    g_indent = 0;
    printf("}\n");
    written.push_back("capture.json");
  }
  Log("bundle: capture.json written");

  // ------------------------------------------------------------------ events.json + states/
  //
  // The call kind of every event, worked out once before anything is written: the sweep above established
  // which ids are events, and this says which of those are dispatches (`DispatchByEid` explains why).
  int callCount = 0;
  const std::map<int, bool> dispatchKinds = DispatchByEid(ctrl, callCount);
  Log("bundle: %d call(s) classified from the engine's action flags", callCount);

  int eventsWritten = 0;
  size_t stateFiles = 0;
  {
    const JsonDocument json;
    const CaptureStdout out((opts.outDir + "\\events.json").c_str());
    if(!out.Ok())
      return Fail(1, "cannot write events.json in %s", opts.outDir.c_str());

    PrintCaptureHeader(file, path);
    ArrayOpen("events");

    std::string previousKey;
    // The kind of the last call, for the events the action list does not name (`DispatchByEid`).
    bool lastKindWasDispatch = false;
    for(size_t index = 0; index < ids.size(); index++)
    {
      const int eid = ids[index];
      ctrl->SetFrameEvent(eid, true);
      const D3D12Pipe::State *st = ctrl->GetD3D12PipelineState();

      // Everything the state can be compared and hashed by, so the offline side does not have to
      // guess which fields matter.
      std::string shaderIds;
      bool computeBound = false;
      for(int i = 0; i < (int)ShaderStage::Count; i++)
      {
        const ShaderStage stage = (ShaderStage)i;
        const D3D12Pipe::Shader *sh = StageShader(st, stage);
        if(sh == NULL || sh->resourceId == ResourceId::Null())
          continue;
        shaderIds += Fmt("%s=%s ", StageName(stage), IdText(sh->resourceId).c_str());
        if(stage == ShaderStage::Compute)
          computeBound = true;
      }
      // The call kind is the action's, not the bound shaders': see `DispatchByEid`. An event the
      // action list does not name takes the kind of the call it follows, and `computeBound` --
      // whether a compute shader happens to be bound -- is only for a capture whose action list
      // came back empty.
      const std::map<int, bool>::const_iterator kind = dispatchKinds.find(eid);
      if(kind != dispatchKinds.end())
        lastKindWasDispatch = kind->second;
      const bool compute = dispatchKinds.empty() ? computeBound : lastKindWasDispatch;

      std::string targets;
      for(size_t slot = 0; slot < st->outputMerger.renderTargets.size(); slot++)
      {
        const ResourceId rt = st->outputMerger.renderTargets[slot].resource;
        std::string detail = IdText(rt);
        for(size_t t = 0; t < textures.size(); t++)
        {
          if(textures[t].first == IdText(rt))
          {
            const TextureDescription &td = *textures[t].second;
            detail += Fmt(" %ux%ux%u %s", td.width, td.height, td.depth, td.format.Name().c_str());
            break;
          }
        }
        targets += Fmt("%s\"%s\"", targets.empty() ? "" : ", ", detail.c_str());
      }
      const std::string depth = IdText(st->outputMerger.depthTarget.resource);

      const std::string key =
          Fmt("pso=%s;shaders=%s;targets=%s;depth=%s;rs=%s;rp=%u",
              IdText(st->pipelineResourceId).c_str(), shaderIds.c_str(), targets.c_str(),
              depth.c_str(), IdText(st->rootSignature.resourceId).c_str(),
              (unsigned)st->rootSignature.parameters.size());
      const std::string stateHash = StateHash(key);

      ObjectRow(
          Fmt("{\"eid\": %d, \"pso\": \"%s\", \"psoKind\": \"%s\", \"shaders\": \"%s\","
              " \"targets\": [%s], \"depth\": \"%s\", \"rootParameters\": %u, \"state\": \"%s\"}",
              eid, IdText(st->pipelineResourceId).c_str(), compute ? "compute" : "graphics",
              shaderIds.c_str(), targets.c_str(), depth.c_str(),
              (unsigned)st->rootSignature.parameters.size(), stateHash.c_str()));
      eventsWritten++;

      // A state file per distinct state rather than per event: the documents are kilobytes each and
      // most events repeat the previous one's, but the *first* event and every change are exactly
      // the ones a reader wants. `--events` forces extra ids.
      bool force = false;
      for(size_t i = 0; i < opts.forceEvents.size(); i++)
      {
        if(opts.forceEvents[i] == eid)
          force = true;
      }
      if(previousKey.empty() || key != previousKey || force)
      {
        previousKey = key;
        const int rc =
            WriteEventDocuments(ctrl, file, path, st, eid, wantDisasm, opts.outDir, written);
        if(rc == 0)
          stateFiles++;
        else
          skipped.push_back(std::make_pair(Fmt("states/%d", eid), "could not be written"));

        // Images belong to the same events as the state files, and for the same reason: most events
        // repeat the previous picture. One per event put 715 images (354 MB) in a bundle whose
        // whole point was to be readable; at the state boundaries it is ~30 events' worth.
        if(opts.withImages)
        {
          for(size_t slot = 0; slot < st->outputMerger.renderTargets.size() && slot < 4; slot++)
          {
            const ResourceId rt = st->outputMerger.renderTargets[slot].resource;
            if(rt == ResourceId::Null())
              continue;
            // The engine's own encoder, not the display readback `image` uses: a PNG of the
            // resource is ~10x smaller than the BMP the display path writes (a 1920x1080 target is
            // 6 MB as BMP), and the bundle wants the target as it is, not as a viewer would
            // tone-map it.
            const std::string png = Fmt("%s\\rt\\%d_%d.png", opts.outDir.c_str(), eid, (int)slot);
            TextureSave save;
            save.resourceId = rt;
            save.destType = FileType::PNG;
            const ResultDetails res = ctrl->SaveTexture(save, rdcstr(png.c_str()));
            unsigned long long bytes = 0;
            if(res.OK() && FileBytes(png.c_str(), bytes) && bytes > 0)
              written.push_back(BundleRelative(opts.outDir, png));
            else
              skipped.push_back(std::make_pair(
                  BundleRelative(opts.outDir, png),
                  res.OK() ? std::string("the engine wrote an empty file") : ResultText(res)));
          }
        }
      }
    }

    ArrayClose(false);    // the scan block and the totals below
    g_indent = 1;
    Field("total", (long long)eventsWritten);
    Field("scanned", (long long)scanned);
    Field("scanFrom", (long long)opts.since);
    Field("scanTo", (long long)lastEid);
    Field("scanStopped", std::string(stopped));
    Field("stateFiles", (long long)stateFiles, true);
    g_indent = 0;
    printf("}\n");
    written.push_back("events.json");
  }
  Log("bundle: events.json written (%d id(s) with bound state, %d state file group(s))",
      eventsWritten, (int)stateFiles);

  // ------------------------------------------------------------------ resources.json
  {
    const JsonDocument json;
    const CaptureStdout out((opts.outDir + "\\resources.json").c_str());
    if(!out.Ok())
      return Fail(1, "cannot write resources.json in %s", opts.outDir.c_str());

    PrintCaptureHeader(file, path);
    ArrayOpen("resources");

    const rdcarray<ResourceDescription> &resources = ctrl->GetResources();
    const rdcarray<BufferDescription> &buffers = ctrl->GetBuffers();
    for(size_t i = 0; i < resources.size(); i++)
    {
      const ResourceDescription &r = resources[i];
      const std::string id = IdText(r.resourceId);

      const TextureDescription *tex = NULL;
      for(size_t t = 0; t < textures.size(); t++)
      {
        if(textures[t].first == id)
        {
          tex = textures[t].second;
          break;
        }
      }
      const BufferDescription *buf = NULL;
      for(size_t b = 0; b < buffers.size(); b++)
      {
        if(IdText(buffers[b].resourceId) == id)
        {
          buf = &buffers[b];
          break;
        }
      }

      ObjectOpen();
      Field("resource", id);
      Field("name", std::string(r.name.c_str()));
      Field("kind", tex != NULL ? std::string("texture")
                                : (buf != NULL ? std::string("buffer") : std::string("other")));
      if(tex != NULL)
      {
        Field("format", std::string(tex->format.Name().c_str()));
        Field("dimension", (long long)tex->dimension);
        Field("width", (long long)tex->width);
        Field("height", (long long)tex->height);
        Field("depth", (long long)tex->depth);
        Field("mips", (long long)tex->mips);
        Field("arraySize", (long long)tex->arraysize);
        Field("samples", (long long)tex->msSamp);
      }
      if(buf != NULL)
        Field("bytes", (long long)buf->length);

      // The usage list is what lets the offline side ask "was this ever written, and by whom" without a
      // device. The values are the engine's numeric `ResourceUsage`; the offline tool's `rdc_report.py`
      // carries the name table, taken from the enum's declaration order in RenderDoc's own header.
      if(opts.noUsage)
      {
        Field("usage", std::string("(not collected: --no-usage)"), true);
      }
      else
      {
        const rdcarray<EventUsage> usage = ctrl->GetUsage(r.resourceId);
        uint32_t first = 0, last = 0;
        ArrayOpen("usage");
        for(size_t u = 0; u < usage.size(); u++)
        {
          ObjectRow(Fmt("{\"eid\": %u, \"usage\": %u}", (unsigned)usage[u].eventId,
                        (unsigned)usage[u].usage));
          if(first == 0 || usage[u].eventId < first)
            first = usage[u].eventId;
          if(usage[u].eventId > last)
            last = usage[u].eventId;
        }
        ArrayClose(false);
        Field("usageCount", (long long)usage.size());
        Field("firstEvent", (long long)first);
        Field("lastEvent", (long long)last, true);
      }
      ObjectClose();
    }

    ArrayClose(false);
    g_indent = 1;
    Field("total", (long long)resources.size(), true);
    g_indent = 0;
    printf("}\n");
    written.push_back("resources.json");
  }
  Log("bundle: resources.json written (%d resources)", (int)ctrl->GetResources().size());

  // ------------------------------------------------------------------ messages.json
  {
    const JsonDocument json;
    const CaptureStdout out((opts.outDir + "\\messages.json").c_str());
    if(!out.Ok())
      return Fail(1, "cannot write messages.json in %s", opts.outDir.c_str());

    PrintCaptureHeader(file, path);
    const rdcarray<DebugMessage> &msgs = ctrl->GetDebugMessages();
    ArrayOpen("messages");
    for(size_t i = 0; i < msgs.size(); i++)
    {
      // Structured rather than a `severity text` blob: the offline side groups these by message and ranks
      // them by severity, and a string it would have to re-parse is not a format, it is a hint.
      ObjectRow(Fmt("{\"eid\": %u, \"severity\": %u, \"severityText\": \"%s\", \"text\": \"%s\"}",
                    (unsigned)msgs[i].eventId, (unsigned)msgs[i].severity,
                    SeverityText(msgs[i].severity).c_str(),
                    JsonEscape(msgs[i].description.c_str()).c_str()));
    }
    ArrayClose(false);
    g_indent = 1;
    Field("total", (long long)msgs.size(), true);
    g_indent = 0;
    printf("}\n");
    written.push_back("messages.json");
  }

  // ------------------------------------------------------------------ counters.json (optional)
  if(opts.withCounters)
  {
    Log("bundle: fetching counters (the slow part, when the driver supports them)");
    const JsonDocument json;
    const CaptureStdout out((opts.outDir + "\\counters.json").c_str());
    if(!out.Ok())
      return Fail(1, "cannot write counters.json in %s", opts.outDir.c_str());

    PrintCaptureHeader(file, path);
    const rdcarray<CounterResult> results = ctrl->FetchCounters(rdcarray<GPUCounter>());
    ArrayOpen("counters");
    for(size_t i = 0; i < results.size(); i++)
      ObjectRow(Fmt("{\"eid\": %u, \"counter\": %u, \"value\": %g}", (unsigned)results[i].eventId,
                    (unsigned)results[i].counter, results[i].value.d));
    ArrayClose(false);
    g_indent = 1;
    Field("total", (long long)results.size(), true);
    g_indent = 0;
    printf("}\n");
    written.push_back("counters.json");
  }

  // ------------------------------------------------------------------ textures/ (optional)
  if(opts.withTextures)
  {
    Log("bundle: saving %d texture(s) through the engine's decoder", (int)ctrl->GetTextures().size());
    for(size_t i = 0; i < ctrl->GetTextures().size(); i++)
    {
      const TextureDescription &t = ctrl->GetTextures()[i];
      TextureSave save;
      save.resourceId = t.resourceId;
      save.destType = FileType::PNG;
      const std::string out = opts.outDir + "\\textures\\" + IdText(t.resourceId) + ".png";
      const ResultDetails res = ctrl->SaveTexture(save, rdcstr(out.c_str()));
      // A successful `SaveTexture` can still leave an empty file (measured: one texture in the Android
      // capture), and a 0-byte PNG in the manifest is worse than a line saying it could not be decoded.
      unsigned long long bytes = 0;
      if(res.OK() && FileBytes(out.c_str(), bytes) && bytes > 0)
        written.push_back(BundleRelative(opts.outDir, out));
      else
        skipped.push_back(std::make_pair(
            BundleRelative(opts.outDir, out),
            res.OK() ? std::string("the engine wrote an empty file") : ResultText(res)));
    }
  }

  // ------------------------------------------------------------------ manifest.json
  {
    const JsonDocument json;
    const CaptureStdout out((opts.outDir + "\\manifest.json").c_str());
    if(!out.Ok())
      return Fail(1, "cannot write manifest.json in %s", opts.outDir.c_str());

    unsigned long long captureBytes = 0;
    const std::string captureAbs = AbsolutePath(path);
    printf("{\n");    // this writer builds its own document
    g_indent = 1;
    Field("schemaVersion", (long long)kSchemaVersion);
    Field("bundleVersion", 1);
    Field("driver", std::string("replay_dump"));
    Field("renderdoc", std::string(g_GetVersionString ? g_GetVersionString() : "?"));
    Field("capture", std::string(path));
    Field("captureAbsolute", captureAbs);
    Field("captureBytes", FileBytes(captureAbs.c_str(), captureBytes) ? (long long)captureBytes : 0);
    Field("captureSha256", Sha256File(captureAbs.c_str()));
    Field("since", (long long)opts.since);
    Field("until", (long long)opts.until);
    Field("maxEvents", (long long)opts.maxEvents);
    Field("withImages", (long long)(opts.withImages ? 1 : 0));
    Field("withCounters", (long long)(opts.withCounters ? 1 : 0));
    Field("withTextures", (long long)(opts.withTextures ? 1 : 0));
    Field("resourceUsage", std::string(opts.noUsage ? "not collected" : "collected"));
    Field("stateHashInputs",
          std::string("pso, shader ids, render targets, depth target, root signature"
                      " id, root parameter count"));
    Field("statesRule",
          std::string("a state file for the first event with bound state and for every event"
                      " whose state hash differs from the previous one, plus --events"));

    // By design, not by accident: what this bundle *cannot* contain, said here so a reader does not
    // conclude that the frame has no copies, no markers and no counts.
    ArrayOpen("notInThisBundle");
    ObjectRow(std::string(
        "{\"what\": \"the finer kind of each call (draw against copy, clear or marker)\","
        " \"why\": \"`psoKind` says whether the event is a dispatch and nothing more; the"
        " rest, and the call's own name, are in the action list the engine exposes and this"
        " bundle does not write\"}"));
    ObjectRow(std::string(
        "{\"what\": \"per-event triangle and thread counts\", \"why\": \"those live in the"
        " captured call arguments, not in the pipeline state\"}"));
    ObjectRow(std::string(
        "{\"what\": \"marker and pass names\", \"why\": \"the action list carries them"
        " (`ActionDescription::customName`, reached through `GetRootActions()`) and this"
        " bundle does not write them yet\"}"));
    ObjectRow(
        std::string("{\"what\": \"texture thumbnails\", \"why\": \"the engine decodes textures but"
                    " does not resize them; --textures writes full decodes\"}"));
    ObjectRow(
        std::string("{\"what\": \"an exact end to the id list\", \"why\": \"ids past the frame's"
                    " last event clamp to it, so the sweep is bounded by the file's chunk count"
                    " (ROADMAP §3) and may hold a few trailing repeats\"}"));
    ArrayClose(false);

    ArrayOpen("skipped");
    for(size_t i = 0; i < skipped.size(); i++)
      ObjectRow(Fmt("{\"what\": \"%s\", \"why\": \"%s\"}", JsonEscape(skipped[i].first).c_str(),
                    JsonEscape(skipped[i].second).c_str()));
    ArrayClose(false);

    ArrayOpen("files");
    unsigned long long totalBytes = 0;
    for(size_t i = 0; i < written.size(); i++)
    {
      const std::string full = opts.outDir + "\\" + written[i];
      unsigned long long bytes = 0;
      FileBytes(full.c_str(), bytes);    // a '/' in the path is accepted by the Win32 API
      const std::string hash = Sha256File(full.c_str());
      ObjectRow(Fmt("{\"path\": \"%s\", \"bytes\": %llu, \"sha256\": \"%s\"}", written[i].c_str(),
                    bytes, hash.c_str()));
      totalBytes += bytes;
    }
    ArrayClose(false);

    g_indent = 1;
    Field("fileCount", (long long)written.size());
    Field("fileBytes", (long long)totalBytes, true);
    g_indent = 0;
    printf("}\n");
  }
  Log("bundle: manifest.json written (%d file(s))", (int)written.size());

  PrintCaptureHeader(file, path);
  Field("out", opts.outDir);
  Field("events", (long long)eventsWritten);
  Field("stateFiles", (long long)stateFiles);
  Field("files", (long long)written.size());
  Field("skipped", (long long)skipped.size());
  Field("scanStopped", std::string(stopped), true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

//: `bundle-verify <dir>`: re-hashes what the manifest lists and reports anything missing, resized
//: or changed. No device, no DLL and no capture are involved, which is the point -- a bundle that
//: arrives from another machine can be checked before it is trusted.
//:
//: The manifest is JSON and this is not a JSON parser: the `files` entries are matched against the
//: exact shape this driver writes, and an entry that does not match stops the check rather than
//: passing silently. Full document validation belongs to the offline side (`python -m json.tool`,
//: the README's playbook).
int CmdBundleVerify(const char *dir)
{
  const std::string manifestPath = std::string(dir) + "\\manifest.json";
  std::string text;
  if(!ReadWholeFile(manifestPath.c_str(), text))
    return Fail(1, "cannot read %s", manifestPath.c_str());

  if(g_json)
    printf("{\n");
  g_indent = g_json ? 1 : 0;
  Field("schemaVersion", (long long)kSchemaVersion);
  Field("manifest", manifestPath);

  int checked = 0, bad = 0;
  ArrayOpen("files");
  size_t pos = 0;
  while((pos = text.find("{\"path\":", pos)) != std::string::npos)
  {
    char rel[512] = {0};
    char digest[80] = {0};
    unsigned long long bytes = 0;
    const int fields =
        sscanf(text.c_str() + pos,
               "{\"path\": \"%511[^\"]\", \"bytes\": %llu, \"sha256\": \"%79[0-9a-f]\"}", rel,
               &bytes, digest);
    if(fields != 3)
    {
      Row(std::string("a `files` entry does not have the shape this driver writes"));
      bad++;
      break;
    }

    std::string full = std::string(dir) + "\\" + rel;
    for(size_t i = 0; i < full.size(); i++)
    {
      if(full[i] == '/')
        full[i] = '\\';
    }

    unsigned long long onDisk = 0;
    if(!FileBytes(full.c_str(), onDisk))
    {
      Row(Fmt("%-46s MISSING", rel));
      bad++;
    }
    else if(onDisk != bytes)
    {
      Row(Fmt("%-46s %llu bytes on disk, %llu in the manifest", rel, onDisk, bytes));
      bad++;
    }
    else
    {
      const std::string hash = Sha256File(full.c_str());
      if(hash.empty() || hash != digest)
      {
        Row(Fmt("%-46s sha256 %s, the manifest says %s", rel,
                hash.empty() ? "(unreadable)" : hash.c_str(), digest));
        bad++;
      }
      else
      {
        checked++;
      }
    }
    pos++;
  }
  ArrayClose(false);

  g_indent = g_json ? 1 : 0;
  Field("checked", (long long)checked);
  Field("problems", (long long)bad);
  // `fileCount`, not `files`: the array above already holds that name, and a repeated key in one
  // object is resolved by every parser to the last one -- so the count silently replaced the rows.
  // Writing the document's schema is what surfaced it (the schema cannot describe two members with
  // one name).
  Field("fileCount", (long long)(checked + bad), true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return bad == 0 ? 0 : 1;
}

// --------------------------------------------------------------------------- schema
//: The schemas themselves live in schema.cpp/schema.h, with the reasoning: they are data, and they
//: were the largest single block in this file. What follows prints them, writes them and checks
//: them, and is here because it reports through `Fail` and the run log like every other command.

//: Read one schema file, folding CRLF to LF: a checkout can rewrite a file's line endings, and the contract
//: is the JSON, not the ending. A missing or unreadable file is a difference, not an error.
