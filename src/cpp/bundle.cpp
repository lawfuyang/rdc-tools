// z.bundle — the bundle producer and verifier the offline report reads
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

namespace
{
//: The 32-byte digest as 64 lowercase hex characters -- one spelling, so a hash this tool prints
//: and a hash it compares against are the same string by construction.
std::string DigestText(const unsigned char *digest)
{
  char out[65];
  for(int i = 0; i < 32; i++)
    snprintf(out + i * 2, 3, "%02x", digest[i]);
  return std::string(out, 64);
}
}    // namespace

//: SHA-256 of a buffer in memory, for a document that has to identify *bytes* rather than a file:
//: `shaders` stamps every bound stage with the digest of the shader it read, which is what lets two
//: bundles (two captures, or the same capture through two builds of this tool) be compared by
//: shader identity instead of by size. An empty result means the OS provider was missing, and the
//: field then says so rather than carrying a hash that is not one.
std::string Sha256Bytes(const void *data, size_t size)
{
  if(data == NULL && size != 0)
    return std::string();

  BCRYPT_ALG_HANDLE alg = NULL;
  BCRYPT_HASH_HANDLE hash = NULL;
  if(BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0)
    return std::string();

  DWORD objectBytes = 0, ignored = 0;
  if(BCryptGetProperty(alg, BCRYPT_OBJECT_LENGTH, (PUCHAR)&objectBytes, sizeof(objectBytes),
                       &ignored, 0) < 0)
    objectBytes = 0;
  std::vector<unsigned char> object(objectBytes);
  unsigned char digest[32];

  bool bOk = BCryptCreateHash(alg, &hash, object.empty() ? NULL : object.data(),
                              (ULONG)object.size(), NULL, 0, 0) >= 0;
  if(bOk && size > 0)
    bOk = BCryptHashData(hash, (PUCHAR)data, (ULONG)size, 0) >= 0;
  bOk = bOk && BCryptFinishHash(hash, digest, (ULONG)sizeof(digest), 0) >= 0;

  if(hash != NULL)
    BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(alg, 0);
  return bOk ? DigestText(digest) : std::string();
}

std::string Sha256File(const std::filesystem::path &path)
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

  FILE *f = FileOpen(path, "rb");
  bool bOk = false;
  if(f != NULL)
  {
    bOk = BCryptCreateHash(alg, &hash, object.empty() ? NULL : object.data(), (ULONG)object.size(),
                           NULL, 0, 0) >= 0;
    if(bOk)
    {
      std::vector<unsigned char> buf(65536);
      size_t n = 0;
      while((n = fread(buf.data(), 1, buf.size(), f)) > 0)
      {
        if(BCryptHashData(hash, (PUCHAR)buf.data(), (ULONG)n, 0) < 0)
        {
          bOk = false;
          break;
        }
      }
      bOk = bOk && BCryptFinishHash(hash, digest, (ULONG)sizeof(digest), 0) >= 0;
    }
  }

  if(f != NULL)
    fclose(f);
  if(hash != NULL)
    BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(alg, 0);

  if(!bOk)
  {
    fprintf(stderr, "warning: cannot hash %s\n", path.string().c_str());
    return std::string();
  }

  return DigestText(digest);
}

bool ReadWholeFile(const std::filesystem::path &path, std::string &text)
{
  FILE *f = FileOpen(path, "rb");
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

bool FileBytes(const std::filesystem::path &path, unsigned long long &bytes)
{
  std::error_code ec;
  const std::uintmax_t size = std::filesystem::file_size(path, ec);
  if(ec)
    return false;
  bytes = (unsigned long long)size;
  return true;
}

bool MakeDir(const std::filesystem::path &path)
{
  // One call, and the reason this is `std::filesystem` rather than `CreateDirectoryA` per
  // component: `create_directories` walks the path itself, so `C:\`, a UNC share, a trailing
  // separator, `/` where Windows wants `\` and a `..` in the middle are the library's problem
  // instead of ours. A destination a caller names is allowed to be several levels deep (`dump
  // cap.rdc out/frames/cap1`, `sheet cap.rdc shots/frame12`, `patch ... out/tries/fix1`), and the
  // folder that already exists is the normal case rather than an error: `create_directories`
  // answers false with no error for it. False here therefore means what the callers' `cannot create
  // <what>` messages say -- a file in the way, a drive that is not there, no permission.
  if(path.empty())
    return false;
  std::error_code ec;
  if(std::filesystem::create_directories(path, ec))
    return true;
  if(ec)
    return false;
  return std::filesystem::is_directory(path, ec) && !ec;
}

void RemoveQuiet(const std::filesystem::path &path)
{
  std::error_code ec;
  std::filesystem::remove(path, ec);
}

bool ExistsQuiet(const std::filesystem::path &path)
{
  std::error_code ec;
  return std::filesystem::exists(path, ec) && !ec;
}

bool DirIsEmpty(const std::filesystem::path &path, bool &bEmpty)
{
  // One entry is one thing too many, so the iterator is asked for its first and not walked: the
  // answer is whether the glob `path\*` used to return anything beyond `.` and `..`, which an
  // iterator does not offer in the first place.
  std::error_code ec;
  const std::filesystem::directory_iterator first(path, ec);
  if(ec)
    return false;    // cannot be read at all, which is not the same answer as "empty"
  bEmpty = first == std::filesystem::directory_iterator();
  return true;
}

//: A path inside the bundle, relative to its root and with forward slashes, so the manifest reads
//: the same whichever way the root was spelled.
std::string BundleRelative(const std::filesystem::path &root, const std::filesystem::path &full)
{
  // `lexically_relative` rather than a byte count: the version before this cut `root.size()` bytes off
  // the front of `full`, which is not a relative path at all unless the root is a literal prefix of it
  // (`out` and `outer\a.txt` gave `er/a.txt`). `/` separators because the manifest is read by the
  // offline tool on either platform, which is what `generic_string` writes.
  std::string rel = full.lexically_relative(root).generic_string();
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
                        const D3D12Pipe::State *st, int eid, bool bWantDisasm,
                        const std::string &bundle, std::vector<std::string> &written)
{
  // The names under the bundle are paths, built by the path type rather than by hand: a caller may
  // have named the bundle with a trailing separator, which `+ "\\states"` would have doubled.
  const std::filesystem::path statesDir = std::filesystem::path(bundle) / "states";
  const std::filesystem::path cbuffersDir = std::filesystem::path(bundle) / "cbuffers";
  const std::filesystem::path stem = statesDir / Fmt("%d", eid);

  {
    const ULONGLONG tDoc = Millis();
    const JsonDocument bJson;
    std::filesystem::path target = stem;
    target += ".state.json";
    const CaptureStdout out(target);
    if(!out.Ok())
      return 1;
    CmdState(ctrl, file, path, eid);
    ProfileAdd(kProfileStateDoc, tDoc);
    written.push_back(BundleRelative(bundle, target));
  }

  {
    const ULONGLONG tDoc = Millis();
    const JsonDocument bJson;
    std::filesystem::path target = stem;
    target += ".shaders.json";
    const CaptureStdout out(target);
    if(!out.Ok())
      return 1;
    CmdShaders(ctrl, file, path, eid, bWantDisasm);
    ProfileAdd(kProfileShadersDoc, tDoc);
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
      const ULONGLONG tDoc = Millis();
      const JsonDocument bJson;
      const std::filesystem::path target =
          cbuffersDir / Fmt("%d_%s_%d.json", eid, StageName(stage), (int)b);
      const CaptureStdout out(target);
      if(!out.Ok())
        return 1;
      CmdCbuffer(ctrl, file, path, eid, stage, (int)b);
      ProfileAdd(kProfileCBuffers, tDoc);
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
  std::string m_OutDir = "bundle";
  int m_Since = 1;
  int m_Until = 0;
  int m_MaxEvents = 0;
  //: The id the sweep stops at, derived (not parsed): the frame's last event id from the action
  //: list, or the file's chunk count when no action list came back. Recorded in the sweep cache's
  //: key and header because it changes the answer as much as the range does: a sweep that stopped
  //: at the chunk count collected a clamped tail a sweep bounded by the last event does not.
  int m_Bound = 0;
  bool m_bWithImages = false;
  bool m_bWithCounters = false;
  bool m_bWithTextures = false;
  bool m_bOverwrite = false;
  bool m_bNoUsage = false;
  std::vector<int> m_ForceEvents;
};

// --------------------------------------------------------------------------- the sweep cache
//
// The sweep is the most expensive thing this program does -- 47 ms per id, ~70 s of a 300-event
// dump on the 1.4 GB capture -- and its answer is a pure function of the capture and the scan
// range: which ids have bound state, how many were scanned, and what stopped the scan. So it is
// cached, keyed by everything that answer depends on, in the same cache directory the offline tool
// uses (`$RDC_CACHE_DIR` moves it, `$RDC_NO_CACHE` turns it off, `%LOCALAPPDATA%\rdc-tools\cache`
// is the default).
//
// The format is a header of `# key: value` lines and then one id per line, deliberately *not* JSON:
// a JSON reader is a parser this program does not have, needs nowhere else, and would be a
// liability to add for a cache it can also simply ignore. Every header line is checked before a
// single id is trusted, so a stale or hand-edited file is refused rather than half-believed.
//
// What the cache does *not* claim: that skipping the sweep leaves the engine where the sweep left
// it. The writing pass then starts from an unreplayed engine and its first `SetFrameEvent` is a
// cold jump rather than a backwards one, which is why a cached run's bundle is hashed against a
// cold run's before this is trusted (REFERENCE §9 records that check).
struct SweepCache
{
  std::vector<int> m_Ids;
  int m_Scanned = 0;
  std::string m_Stopped;
};

//: `%LOCALAPPDATA%\rdc-tools\cache` unless `$RDC_CACHE_DIR` says otherwise: the offline tool's
//: directory, so both halves of the tool have one cache to inspect or clear.
std::filesystem::path CacheDir()
{
  const char *override = getenv("RDC_CACHE_DIR");
  if(override != NULL && *override != '\0')
    return std::filesystem::path(override);
  const char *local = getenv("LOCALAPPDATA");
  return std::filesystem::path(local != NULL ? local : ".") / "rdc-tools" / "cache";
}

bool CacheDisabled()
{
  const char *off = getenv("RDC_NO_CACHE");
  return off != NULL && *off != '\0';
}

//: A short, stable key from everything the sweep's answer depends on: the capture's identity (path,
//: size, modification time), the engine that produced the frame, and the scan range. FNV-1a,
//: because this is a cache key and not a signature -- a collision would also have to pass the
//: header check below, which compares the fields themselves.
std::string SweepCacheKey(const DumpOptions &opts, const std::filesystem::path &absolute)
{
  // The capture's size and write time, through the same two helpers the rest of the tool asks with
  // -- this used to read the attributes itself, which is how a cache key and a staleness check can
  // come to disagree about what "the same file" means. The write time is hashed as this
  // filesystem's own count rather than Windows' 100 ns ticks (`file_time_type` fixes no epoch), and
  // a key that changes is a cache *miss* and never a wrong answer: the header lines below compare
  // the fields themselves.
  unsigned long long bytes = 0, written = 0;
  FileBytes(absolute, bytes);
  std::error_code ec;
  const FileTime time = std::filesystem::last_write_time(absolute, ec);
  if(!ec)
    written = (unsigned long long)time.time_since_epoch().count();

  std::string identity = absolute.string();
  for(size_t i = 0; i < identity.size(); i++)
    identity[i] = (char)tolower((unsigned char)identity[i]);
  const std::string material = Fmt("%s|%llu|%llu|%s|%d|%d|%d|%d", identity.c_str(), bytes, written,
                                   g_GetVersionString != NULL ? g_GetVersionString() : "?",
                                   opts.m_Since, opts.m_Until, opts.m_MaxEvents, opts.m_Bound);
  unsigned long long hash = 1469598103934665603ULL;
  for(size_t i = 0; i < material.size(); i++)
  {
    hash ^= (unsigned char)material[i];
    hash *= 1099511628211ULL;
  }
  return Fmt("%016llx", hash);
}

//: The cache file for this capture and range, or an empty path when caching is off.
std::filesystem::path SweepCachePath(const std::filesystem::path &path, const DumpOptions &opts)
{
  if(CacheDisabled())
    return std::filesystem::path();
  return CacheDir() / Fmt("sweep-%s.txt", SweepCacheKey(opts, AbsolutePath(path)).c_str());
}

//: Reads a cache file, refusing it unless every header line says what this run is asking for. A
//: missing file, a stale one, a truncated one and one for another range are all the same answer --
//: sweep -- which is always correct and only slow.
bool ReadSweepCache(const std::filesystem::path &cachePath, const std::filesystem::path &path,
                    const DumpOptions &opts, SweepCache &out)
{
  if(cachePath.empty())
    return false;
  FILE *f = FileOpen(cachePath, "rb");
  if(f == NULL)
    return false;
  char line[1024];
  const std::string wantCapture = Fmt("# capture: %s", AbsolutePath(path).string().c_str());
  const std::string wantRange = Fmt("# range: since %d until %d maxEvents %d bound %d",
                                    opts.m_Since, opts.m_Until, opts.m_MaxEvents, opts.m_Bound);
  const std::string wantEngine =
      Fmt("# engine: %s", g_GetVersionString != NULL ? g_GetVersionString() : "?");
  bool bCapture = false, bRange = false, bEngine = false, bScanned = false;
  while(fgets(line, sizeof(line), f) != NULL)
  {
    std::string text(line);
    while(!text.empty() && (text[text.size() - 1] == '\n' || text[text.size() - 1] == '\r'))
      text.erase(text.size() - 1);
    if(text.compare(0, 2, "# ") == 0)
    {
      if(text == wantCapture)
        bCapture = true;
      else if(text == wantRange)
        bRange = true;
      else if(text == wantEngine)
        bEngine = true;
      else if(text.compare(0, 10, "# scanned:") == 0)
      {
        int scanned = 0;
        char stopped[512];
        stopped[0] = '\0';
        if(sscanf(text.c_str(), "# scanned: %d stopped: %511[^\n]", &scanned, stopped) >= 1)
        {
          out.m_Scanned = scanned;
          out.m_Stopped = stopped[0] != '\0' ? std::string(stopped) : std::string("the cache");
          bScanned = true;
        }
      }
      continue;
    }
    if(text.empty())
      continue;
    int eid = 0;
    if(sscanf(text.c_str(), "%d", &eid) != 1 || eid <= 0)
    {
      fclose(f);
      return false;    // a body this file should not have: refuse all of it
    }
    out.m_Ids.push_back(eid);
  }
  fclose(f);
  const bool bOk = bCapture && bRange && bEngine && bScanned && !out.m_Ids.empty();
  if(!bOk)
  {
    out.m_Ids.clear();
    out.m_Scanned = 0;
    out.m_Stopped.clear();
  }
  return bOk;
}

//: Writes the cache, best effort: one that cannot be written is not a failure, it is a run that
//: sweeps again next time. Written to a temporary name and renamed, so a killed run cannot leave a
//: half file behind.
void WriteSweepCache(const std::filesystem::path &cachePath, const std::filesystem::path &path,
                     const DumpOptions &opts, const std::vector<int> &ids, int scanned,
                     const char *stopped)
{
  if(cachePath.empty())
    return;
  MakeDir(CacheDir());
  // `path +=` rather than building the name as a string: the temporary is the cache's own name with
  // an extension, whatever that name is.
  std::filesystem::path temp = cachePath;
  temp += ".part";
  FILE *f = FileOpen(temp, "wb");
  if(f == NULL)
    return;
  fprintf(f, "# rdc-tools sweep cache v1\n");
  fprintf(f, "# capture: %s\n", AbsolutePath(path).string().c_str());
  fprintf(f, "# engine: %s\n", g_GetVersionString != NULL ? g_GetVersionString() : "?");
  fprintf(f, "# range: since %d until %d maxEvents %d bound %d\n", opts.m_Since, opts.m_Until,
          opts.m_MaxEvents, opts.m_Bound);
  fprintf(f, "# scanned: %d stopped: %s\n", scanned, stopped);
  for(size_t i = 0; i < ids.size(); i++)
    fprintf(f, "%d\n", ids[i]);
  fclose(f);
  // The remove is explicit even though Windows' rename replaces: this is the one thing a cache
  // write does to a name another run may already have open, and an error code is cheaper to reason
  // about than an implementation's replace semantics.
  std::error_code ec;
  std::filesystem::remove(cachePath, ec);
  std::filesystem::rename(temp, cachePath, ec);
}

// --------------------------------------------------------------------------- the probe cache
//
// `probe` sweeps the same ids the bundle's sweep does -- one `SetFrameEvent` per id, 12 ms on the
// `desktop-1` frame and 47 on the 1.4 GB one -- and it had no cache at all: 39 s on `desktop-1`,
// every run, and on a frame whose ids run to five figures the old default range answered "nothing
// has state" after a minute of sweeping. Its answer is a pure function of the capture, the engine
// and the ids scanned, so it is cached beside the sweep's: same directory, same `# key: value`
// header and one row per line, same two environment variables, and no JSON parser anywhere.
//
// Two things differ from the sweep's, and both are about `probe` being a *range* query rather than
// a pass over the frame:
//
//  * the key is the capture and the engine alone. A probe's answer is a prefix -- a file whose
//    `# scanned: N` line says ids 1..N were swept *in order* answers `probe N` and every smaller
//    request -- and a request for more is a fresh sweep from id 1 rather than an extension of what
//    is cached. Extending would mean arriving at 501 by a cold jump instead of through 500, and the
//    ids just after such a jump lose their state: that is what the parallel sweep measured (further
//    down) and why no id is ever skipped here;
//  * it is flushed every `kProbeFlushEvery` ids, so a sweep killed at minute twenty keeps the
//  prefix
//    it had established -- which the paragraph above says is still a correct answer to a smaller
//    request.
//
// Rows are `eid shaders rootSig params`: the four fields the row prints, so the cache reprints the
// line the scan would have. `rootSig` is `IdText`'s decimal, and holds no space.

//: The cache file for this capture, or an empty path when caching is off. Keyed like the sweep's
//: -- path, size, write time and engine -- because a capture edited in place is a different capture
//: whose ids mean something else, and the size and write time are what say so.
std::filesystem::path ProbeCachePath(const std::filesystem::path &path)
{
  if(CacheDisabled())
    return std::filesystem::path();
  const std::filesystem::path identity = AbsolutePath(path);
  std::string lower = identity.string();
  for(size_t i = 0; i < lower.size(); i++)
    lower[i] = (char)tolower((unsigned char)lower[i]);
  unsigned long long bytes = 0, written = 0;
  FileBytes(identity, bytes);
  std::error_code ec;
  const FileTime time = std::filesystem::last_write_time(identity, ec);
  if(!ec)
    written = (unsigned long long)time.time_since_epoch().count();
  const std::string material = Fmt("%s|%llu|%llu|%s", lower.c_str(), bytes, written,
                                   g_GetVersionString != NULL ? g_GetVersionString() : "?");
  unsigned long long hash = 1469598103934665603ULL;
  for(size_t i = 0; i < material.size(); i++)
  {
    hash ^= (unsigned char)material[i];
    hash *= 1099511628211ULL;
  }
  return CacheDir() / Fmt("probe-%016llx.txt", hash);
}

//: Reads the cache, refusing anything that is not this capture's answer. A missing file, another
//: capture's, another engine's, a truncated one and one with no rows are all the same answer -- sweep
//: -- which is always correct and only slow.
bool ReadProbeCache(const std::filesystem::path &cachePath, const std::filesystem::path &path,
                    int lastEvent, ProbeCache &out)
{
  out.m_Rows.clear();
  out.m_Scanned = 0;
  if(cachePath.empty())
    return false;
  FILE *f = FileOpen(cachePath, "rb");
  if(f == NULL)
    return false;
  const std::string wantCapture = Fmt("# capture: %s", AbsolutePath(path).string().c_str());
  const std::string wantEngine =
      Fmt("# engine: %s", g_GetVersionString != NULL ? g_GetVersionString() : "?");
  // The frame's own extent is part of the answer's meaning (`probe` stops at it), so a file swept
  // against a different bound is refused rather than reinterpreted.
  const std::string wantBound = Fmt("# bound: %d", lastEvent);
  bool bCapture = false, bEngine = false, bBound = false, bScanned = false;
  char line[1024];
  while(fgets(line, sizeof(line), f) != NULL)
  {
    std::string text(line);
    while(!text.empty() && (text[text.size() - 1] == '\n' || text[text.size() - 1] == '\r'))
      text.erase(text.size() - 1);
    if(text.compare(0, 2, "# ") == 0)
    {
      if(text == wantCapture)
        bCapture = true;
      else if(text == wantEngine)
        bEngine = true;
      else if(text == wantBound)
        bBound = true;
      else if(text.compare(0, 10, "# scanned:") == 0)
      {
        const char *tail = text.c_str() + 10;
        while(*tail == ' ')    // `ParseInt` takes the whole text, so the space is the reader's job
          tail++;
        int scanned = 0;
        if(ParseInt(tail, scanned) && scanned > 0)
        {
          out.m_Scanned = scanned;
          bScanned = true;
        }
      }
      continue;
    }
    if(text.empty())
      continue;
    ProbeRow row;
    char rootSig[64] = {0};
    if(sscanf(text.c_str(), "%d %d %63s %d", &row.m_Eid, &row.m_Shaders, rootSig, &row.m_Params) != 4)
    {
      fclose(f);
      out.m_Rows.clear();
      out.m_Scanned = 0;
      return false;    // a row that is not a row: refuse the whole file
    }
    row.m_RootSig = rootSig;
    out.m_Rows.push_back(row);
  }
  fclose(f);
  const bool bOk = bCapture && bEngine && bBound && bScanned;
  if(!bOk)
  {
    out.m_Rows.clear();
    out.m_Scanned = 0;
  }
  return bOk;
}

//: Writes the cache, best effort: one that cannot be written is a run that sweeps again next time.
//: Temporary name, then rename, so an interrupted write cannot leave a half file behind.
void WriteProbeCache(const std::filesystem::path &cachePath, const std::filesystem::path &path,
                     int lastEvent, const ProbeCache &cache)
{
  if(cachePath.empty() || cache.m_Scanned <= 0)
    return;
  MakeDir(CacheDir());
  std::filesystem::path temp = cachePath;
  temp += ".part";
  FILE *f = FileOpen(temp, "wb");
  if(f == NULL)
    return;
  fprintf(f, "# rdc-tools probe cache v1\n");
  fprintf(f, "# capture: %s\n", AbsolutePath(path).string().c_str());
  fprintf(f, "# engine: %s\n", g_GetVersionString != NULL ? g_GetVersionString() : "?");
  fprintf(f, "# bound: %d\n", lastEvent);
  fprintf(f, "# scanned: %d\n", cache.m_Scanned);
  for(size_t i = 0; i < cache.m_Rows.size(); i++)
  {
    const ProbeRow &row = cache.m_Rows[i];
    fprintf(f, "%d %d %s %d\n", row.m_Eid, row.m_Shaders, row.m_RootSig.c_str(), row.m_Params);
  }
  fclose(f);
  std::error_code ec;
  std::filesystem::remove(cachePath, ec);
  std::filesystem::rename(temp, cachePath, ec);
}

//: The ids `probe` should scan: the caller's cap, the frame's own last event when that is smaller,
//: and the whole frame when the cap is 0 (`probe <rdc> last`). A frame with no derivable bound
//: scans the cap: fewer ids than the frame is a fact the caller can see in `scanned`.
int ProbeUntil(int cap, int lastEvent)
{
  if(cap <= 0)
    return lastEvent > 0 ? lastEvent : 0;
  if(lastEvent <= 0)
    return cap;
  return cap < lastEvent ? cap : lastEvent;
}

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

//: Walks the ids looking for bound state, and keeps only the ids it found.
//:
//: A function of its own so the cache can skip it: `CmdDump` either reads this answer from a cache
//: file or pays for it here. `--max-events` stops the *sweep*, not just the writing -- on a big
//: capture the sweep is the expensive part (each id is a `SetFrameEvent`, measured at 12 ms on the
//: `desktop-1` and 47 ms on `desktop-2`) and walking 29216 ids before anything is written is
//: minutes of silence.
void SweepForEvents(IReplayController *ctrl, const DumpOptions &opts, int until, size_t idBudget,
                    std::vector<int> &ids, int &scanned, int &lastEid, std::string &stopped)
{
  SweepRules rules(opts.m_MaxEvents, idBudget);
  Progress sweep;
  // The bound a reader can act on: the scan range, or the file's chunk count when that is smaller. With
  // `--max-events` the id count is not knowable up front, and a percentage against the hard cap said
  // "~2 h 33 min left" while the sweep was about to stop at id 1140 of a nominal 200000.
  const int scanBound = (until < (int)idBudget) ? until : (int)idBudget;
  sweep.Begin("bundle: sweep", opts.m_MaxEvents > 0 ? 0 : scanBound - opts.m_Since + 1);
  for(int eid = opts.m_Since; eid <= until; eid++)
  {
    const ULONGLONG tMove = Millis();
    MoveToEvent(ctrl, eid);
    ProfileAdd(kProfileSetFrameEvent, tMove);
    const ULONGLONG tState = Millis();
    const D3D12Pipe::State *st = ctrl->GetD3D12PipelineState();
    ProfileAdd(kProfilePipelineState, tState);
    scanned++;
    // Before the state test, not after it: the ids *without* state `continue` past the rest of the
    // body, so a tick at the end of the loop reports nothing at all on a capture whose first event
    // is id 841 -- the exact silence this line exists to break.
    sweep.Tick(scanned);
    const bool bHasState = HasBoundState(st);
    if(bHasState)
      ids.push_back(eid);    // collected before the caps are tested: the id that trips one is kept
    const SweepRules::Step step = rules.Feed(bHasState, eid);
    if(step == SweepRules::Continue)
      continue;
    // The id that trips a rule was collected first (`SweepRules::Feed`), so `scanned`/`lastEid`
    // include it -- the merge must land on exactly the same numbers, and the selftest checks that.
    if(step == SweepRules::StopEmptyRun)
      stopped = "a run of ids with nothing bound";
    else if(step == SweepRules::StopMaxEvents)
      stopped = "--max-events";
    else
      stopped = "the id budget (the scan bound)";
    break;
  }
  lastEid = rules.m_LastEid;
  sweep.Done(scanned);
}

//: The sweep's bounds, in one place because a worker must compute exactly what its parent computed
//: or the slices it reports against are not the slices the parent split. Sets `opts.m_Bound` (the
//: sweep cache's key and header depend on it) as well as returning the range.
void ComputeSweepBounds(IReplayController *ctrl, DumpOptions &opts, int &until, size_t &idBudget,
                        int &lastEvent)
{
  const int kHardCap = 200000;
  lastEvent = LastEventId(ctrl);
  idBudget = lastEvent > 0 ? (size_t)lastEvent : ctrl->GetStructuredFile().chunks.size();
  until = opts.m_Until > 0 ? opts.m_Until : kHardCap;
  if((int)idBudget < until)
    until = (int)idBudget;
  opts.m_Bound = (int)idBudget;
}

// --------------------------------------------------------------------------- why the sweep is not
// parallel
//
// The sweep looks embarrassingly parallel -- one `SetFrameEvent` per id, each independent -- and a
// parallel version of it was built and measured before being removed: four worker processes, each
// opening its own replay and sweeping a slice of the range, their answers merged under the same
// stop rules (`SweepRules`). Mechanically it worked and it was fast: 68.4 s against 209.4 s for
// the same dump on the same afternoon (3.1x wall), the sweep phase itself 20.6 s against 71.8 s.
//
// It was removed because its answer is not the serial sweep's answer, and cannot be made so. The
// engine's pipeline state at an id is a function of *how replay reached that id* -- the same
// reason the writing pass below re-reads every id after a backwards first move: a forward walk
// leaves bindings in the reported state that a fresh replay to the same id does not report, and a
// worker process starts fresh, at its slice's head. Measured on `desktop-1`: the serial
// sweep collects 1186 ids, the parallel one 1169, and the 17 it loses are exactly the first ids
// of the last slice (979..995) -- a region where the serial walk still reports the previous
// command list's bindings (`vs=60993 ps=60994`, a row in every serial bundle to date) while both
// a cold-starting worker *and* the `state` command's own backwards double-jump report nothing
// bound at 979 and the next bindings only from 996. The serial answer and the fresh-engine answer
// disagree there, and the bundle's contract is the serial one: it is what every verified bundle
// contains, and re-defining the sweep to the fresh-engine answer is a change to what a bundle
// *is*, not a speed optimisation.
//
// The two things that would make a parallel sweep possible are both out of reach here.
// Reproducing the serial history per worker costs the whole prefix -- the reported state is a
// function of every replay pass before it, so the last worker would pay the entire range. And
// splitting at command-list boundaries, where a cold jump lands on a list's own first bindings,
// needs a mapping from the engine's event ids to the file's chunk stream -- two numberings that
// are independent by design (the engine numbers what a command list recorded, the file numbers
// chunks), and nothing in ROADMAP.md proposes to close that. Two incidental findings from the
// attempt are in REFERENCE §9: a spawned child must be given a stdin it can use (an inherited slot
// it cannot takes its whole stdio down -- three "successful" workers once left three empty logs),
// and simultaneous replay-device creations can leave one hung at zero CPU with no error, which is
// why any such design needs a deadline and a serial fallback rather than an unbounded wait.

//: The bundle producer (REFERENCE §9): one replay session, everything the engine alone can answer
//: written to disk, so the offline half can analyse a frame without a device. It is also the reason
//: a crash is survivable: files are written as they are produced, and the manifest lists what was
//: written, so a partial bundle says so.
int CmdDump(IReplayController *ctrl, ICaptureFile *file, const char *path,
            const std::vector<std::string> &args, bool bWantDisasm)
{
  DumpOptions opts;
  for(size_t i = 1; i < args.size(); i++)
  {
    const std::string &a = args[i];
    if(a == "--with-images")
      opts.m_bWithImages = true;
    else if(a == "--with-counters")
      opts.m_bWithCounters = true;
    else if(a == "--textures")
      opts.m_bWithTextures = true;
    else if(a == "--overwrite")
      opts.m_bOverwrite = true;
    else if(a == "--no-usage")
      opts.m_bNoUsage = true;
    else if(a == "--since" && i + 1 < args.size())
      opts.m_Since = ToInt(args[++i], 1);
    else if(a == "--until" && i + 1 < args.size())
      opts.m_Until = ToInt(args[++i], 0);
    else if(a == "--max-events" && i + 1 < args.size())
      opts.m_MaxEvents = ToInt(args[++i], 0);
    else if(a == "--events" && i + 1 < args.size())
      ParseEventList(args[++i], opts.m_ForceEvents);
    else if(a.size() > 2 && a[0] == '-' && a[1] == '-')
      return Fail(2, "unknown option '%s' for dump", a.c_str());
    else
      opts.m_OutDir = a;
  }

  if(opts.m_Since < 1)
    opts.m_Since = 1;

  // The directory has to be ours: writing a bundle into one that already holds another frame's files
  // would leave a mixture no manifest could describe. `--overwrite` says the old contents may be replaced.
  if(!MakeDir(opts.m_OutDir))
    return Fail(1, "cannot create the bundle directory %s", opts.m_OutDir.c_str());
  bool bEmpty = true;
  if(!DirIsEmpty(opts.m_OutDir, bEmpty))
    return Fail(1, "cannot read the bundle directory %s", opts.m_OutDir.c_str());
  if(!bEmpty && !opts.m_bOverwrite)
    return Fail(1, "%s is not empty (pass --overwrite to write into it)", opts.m_OutDir.c_str());

  static const char *kSubDirs[] = {"states", "cbuffers", "rt", "textures"};
  for(size_t i = 0; i < sizeof(kSubDirs) / sizeof(kSubDirs[0]); i++)
  {
    if(!MakeDir(std::filesystem::path(opts.m_OutDir) / kSubDirs[i]))
      return Fail(1, "cannot create %s\\%s", opts.m_OutDir.c_str(), kSubDirs[i]);
  }

  std::vector<std::string> written;    // bundle-relative paths, hashed into the manifest
  std::vector<std::pair<std::string, std::string>> skipped;    // what was not written, and why

  // ------------------------------------------------------------------ the id sweep (first, always)
  // Which ids are events is asked *before* anything else touches the engine, because it is the one
  // question whose answer stops being true afterwards: `SetFrameEvent(n, true)` on an id that is
  // not an event leaves the last replayed event's state in place, so once anything has replayed a
  // real event every forced non-event looks like it has state -- the sweep then never sees an empty
  // run and never ends. (Measured on this capture: `probe 120` alone finds 25-32 ids, and the same
  // `probe 120` after other commands finds ~120. `CmdProbe` carries the same warning.) The sweep
  // cannot *find* the end of a frame by asking: `SetFrameEvent(n, true)` past the last event clamps
  // to it, so every id beyond the frame reports the last event's state and a "no state any more"
  // test never fires. Measured on this capture: ids 1..120 hold 25-32 events, while `probe 4500`
  // reports 4405 ids with state -- and the structured file has 723 chunks, so those extra ids are
  // clamped, not real.
  //
  // What bounds the sweep is the action list: every driver ends a capture's action list with an
  // "End of Capture" action, so the largest event id in the tree is the frame's *last* event
  // (`LastEventId`), and no id past it is an event at all. Asking the tree is a read of what the
  // engine built while loading (no replay), so it is safe before anything else touches the engine.
  // This replaced the file's chunk count as the bound, which was only ever an upper limit and a
  // generous one: the sweep collected the whole clamped tail past the last event as if it were
  // events -- on `desktop-1`, ids 1306..2251 of a 2251-id scan (42% of it, one state repeated
  // with empty marker paths, 946 `SetFrameEvent` calls at ~18 ms each in the sweep and as many
  // again in the writing pass); on `desktop-2` the tail past its last event (1736) would be
  // 94% of a default dump, ~27,000 calls at ~47 ms. An action list that came back empty leaves
  // nothing to derive a bound from, and the chunk count is the fallback again. The empty-run test
  // still ends a sweep early on a sparse capture, and `--until` narrows the range further -- it can
  // no longer extend it past the frame's end, which was never a range anyone meant to ask for.
  //
  // **Why the pass below reads the state again instead of keeping what the sweep saw.** Because the
  // sweep's own refreshes leave an *incomplete* state, and that is measured, not assumed. Building
  // each row from the state the sweep had just refreshed produced `"shaders": "cs=11388 "`,
  // `"targets": []`, `"depth": "0"` for id 841 -- where the pass below, reading the same id *after*
  // the sweep has finished, gets
  // `"ps=11402 cs=11388 ms=11368 "`, a 1920x1080 target and a depth buffer. The difference is the
  // direction of travel: the pass's first move is *backwards* from where the sweep stopped, which
  // makes the engine replay the frame from its start and hand back a complete state, while a
  // forward step onto an event gives whatever the sweep's chunk-by-chunk accumulation had reached.
  // That restructure cut the dump from 89 s to 71 s and changed three files of the bundle, which is
  // how it was caught -- so the second read is not a redundant refresh to be optimised away: it is
  // what makes the state complete. (Both bundles were hashed against the reference to prove it; the
  // numbers are in REFERENCE §9.)
  int until = 0;
  size_t idBudget = 0;
  int lastEvent = 0;
  ComputeSweepBounds(ctrl, opts, until, idBudget, lastEvent);
  int scanned = 0, lastEid = 0;
  std::string stopped = "the end of the scan range";
  std::vector<int> ids;

  // The cached answer first: the sweep is the expensive part and its answer is a pure function of
  // the capture and the range (see the sweep cache above). A hit saves the scan and nothing else --
  // the pass below still walks every collected id, because the state it reads there is not the
  // state the sweep saw.
  const std::filesystem::path sweepCache = SweepCachePath(path, opts);
  SweepCache cached;
  if(ReadSweepCache(sweepCache, path, opts, cached))
  {
    ids = cached.m_Ids;
    scanned = cached.m_Scanned;
    lastEid = ids.back();
    stopped = cached.m_Stopped;
    Log("bundle: sweep answered from the cache: %d id(s) out of %d scanned (%s)", (int)ids.size(),
        scanned, stopped.c_str());
    Log("bundle:   (set $RDC_NO_CACHE, or delete %s, to scan again)", sweepCache.string().c_str());
    // The sweep also left the engine at the *end* of the scan, and the writing pass depends on
    // that: its first `SetFrameEvent` has to move *backwards* for the state to come out complete
    // (see the note above the sweep -- a forward step onto the first event gives one bound shader
    // and no render targets). One call puts the engine where the sweep would have left it, which is
    // the whole cost of a cache hit: 47 ms instead of the scan. Measured: without it, the warm
    // bundle differed from the cold one in five files.
    const ULONGLONG tWarm = Millis();
    MoveToEvent(ctrl, lastEid);
    ProfileAdd(kProfileSetFrameEvent, tWarm);
  }
  else
  {
    Log("bundle: sweeping ids %d..%d for bound state, at most %d id(s) (%s)", opts.m_Since, until,
        (int)idBudget,
        lastEvent > 0 ? "the frame's last event id, from the action list"
                      : "the file's chunk count: the action list came back empty");
    SweepForEvents(ctrl, opts, until, idBudget, ids, scanned, lastEid, stopped);
    WriteSweepCache(sweepCache, path, opts, ids, scanned, stopped.c_str());
  }
  Log("bundle: %d id(s) collected out of %d scanned (%s)", (int)ids.size(), scanned, stopped.c_str());

  // Formats and dimensions come from the resource list, not from the pipeline state: the state
  // names a target, the description says what it is. Indexed by id text once, because both readers
  // below look resources up per event and per row: the linear scans this replaces built an `IdText`
  // string per comparison, which on `desktop-2`'s 11082 resources is 11k x (5.6k buffers + 96
  // textures) of string churn -- measured, that alone was 10.6 s of a 312 s run.
  std::map<std::string, const TextureDescription *> textures;
  for(size_t i = 0; i < ctrl->GetTextures().size(); i++)
    textures[IdText(ctrl->GetTextures()[i].resourceId)] = &ctrl->GetTextures()[i];
  std::map<std::string, const BufferDescription *> buffers;
  for(size_t i = 0; i < ctrl->GetBuffers().size(); i++)
    buffers[IdText(ctrl->GetBuffers()[i].resourceId)] = &ctrl->GetBuffers()[i];

  // ------------------------------------------------------------------ capture.json
  {
    const JsonDocument bJson;
    const CaptureStdout out(std::filesystem::path(opts.m_OutDir) / "capture.json");
    if(!out.Ok())
      return Fail(1, "cannot write capture.json in %s", opts.m_OutDir.c_str());

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
    Field("captureBytes", FileBytes(AbsolutePath(path), captureBytes) ? (long long)captureBytes : 0);
    Field("absPath", AbsolutePath(path).string(),
          true);    // the object's last member: a comma here is not JSON
    g_Indent = 0;
    printf("}\n");
    written.push_back("capture.json");
  }
  Log("bundle: capture.json written");

  // ------------------------------------------------------------------ events.json + states/
  //
  // The call kind of every event, worked out once before anything is written: the sweep above established
  // which ids are events, and this says which of those are dispatches (`DispatchByEid` explains why).
  int callCount = 0;
  const ULONGLONG tKinds = Millis();
  const std::map<int, bool> dispatchKinds = DispatchByEid(ctrl, callCount);
  ProfileAdd(kProfileActions, tKinds);
  Log("bundle: %d call(s) classified from the engine's action flags", callCount);

  // What each call asked the GPU to do, from the same action tree (`CallVolumesByEid`): a draw's
  // vertices and instances, a dispatch's workgroups. This is the input the report's rankings have
  // been declaring unavailable -- "a draw's vertex count is the first input of the rule and no
  // bundle has it" -- and it is in the action list the engine already gave us.
  int volumeCalls = 0;
  const ULONGLONG tVolume = Millis();
  const std::map<int, CallVolume> callVolumes = CallVolumesByEid(ctrl, volumeCalls);
  ProfileAdd(kProfileActions, tVolume);
  Log("bundle: %d call(s) carry a work volume", volumeCalls);

  // The marker path of every event, from the same action list: one walk for the whole bundle rather
  // than one per event, because the tree is walked once per `MarkerPathAt` call.
  const ULONGLONG tMarkers = Millis();
  const std::map<int, std::string> markerPaths = MarkerPaths(ctrl);
  ProfileAdd(kProfileActions, tMarkers);
  Log("bundle: %d event(s) sit inside a marker", (int)markerPaths.size());

  int eventsWritten = 0;
  size_t stateFiles = 0;
  {
    const JsonDocument bJson;
    const CaptureStdout out(std::filesystem::path(opts.m_OutDir) / "events.json");
    if(!out.Ok())
      return Fail(1, "cannot write events.json in %s", opts.m_OutDir.c_str());

    PrintCaptureHeader(file, path);
    ArrayOpen("events");

    std::string previousKey;
    // The kind of the last call, for the events the action list does not name (`DispatchByEid`).
    bool bLastKindWasDispatch = false;
    Progress progress;
    progress.Begin("bundle: events, states and cbuffers", (int)ids.size());
    for(size_t index = 0; index < ids.size(); index++)
    {
      const int eid = ids[index];
      const ULONGLONG tMove = Millis();
      MoveToEvent(ctrl, eid);
      ProfileAdd(kProfileSetFrameEvent, tMove);
      const ULONGLONG tState = Millis();
      const D3D12Pipe::State *st = ctrl->GetD3D12PipelineState();
      ProfileAdd(kProfilePipelineState, tState);
      const ULONGLONG tRow = Millis();

      // Everything the state can be compared and hashed by, so the offline side does not have to
      // guess which fields matter.
      std::string shaderIds;
      bool bComputeBound = false;
      for(int i = 0; i < (int)ShaderStage::Count; i++)
      {
        const ShaderStage stage = (ShaderStage)i;
        const D3D12Pipe::Shader *sh = StageShader(st, stage);
        if(sh == NULL || sh->resourceId == ResourceId::Null())
          continue;
        shaderIds += Fmt("%s=%s ", StageName(stage), IdText(sh->resourceId).c_str());
        if(stage == ShaderStage::Compute)
          bComputeBound = true;
      }
      // The call kind is the action's, not the bound shaders': see `DispatchByEid`. An event the
      // action list does not name takes the kind of the call it follows, and `computeBound` --
      // whether a compute shader happens to be bound -- is only for a capture whose action list
      // came back empty.
      const std::map<int, bool>::const_iterator kind = dispatchKinds.find(eid);
      if(kind != dispatchKinds.end())
        bLastKindWasDispatch = kind->second;
      const bool bCompute = dispatchKinds.empty() ? bComputeBound : bLastKindWasDispatch;

      std::string targets;
      for(size_t slot = 0; slot < st->outputMerger.renderTargets.size(); slot++)
      {
        const ResourceId rt = st->outputMerger.renderTargets[slot].resource;
        std::string detail = IdText(rt);
        const std::map<std::string, const TextureDescription *>::const_iterator found =
            textures.find(detail);
        if(found != textures.end())
        {
          const TextureDescription &td = *found->second;
          detail += Fmt(" %ux%ux%u %s", td.width, td.height, td.depth, td.format.Name().c_str());
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

      // `marker` is the engine's own marker path for this event (`A > B`), from the action list: it
      // is what lets an offline rule name a pass in the engine's vocabulary instead of describing
      // its state, and it survives a re-capture where an event id does not.
      //
      // `volume` is what the *call* asked for, and it is absent for an event that is not a call (a
      // state setter, a marker, a barrier): a reader can tell "asked for nothing" from "not a
      // call". A draw's triangles are folded here because the topology is state -- `PrimitiveCount`
      // over the index/vertex count, times the instances, and 0 when the topology does not fix one
      // (the convention `mesh` uses). A dispatch's thread count needs the shader's `[numthreads]`,
      // so it is the call's own override when it has one (rare) and the bound compute shader's
      // reflection otherwise; 0 is "the engine published neither", not "no threads".
      std::string volume;
      const std::map<int, CallVolume>::const_iterator vol = callVolumes.find(eid);
      if(vol != callVolumes.end())
      {
        if(vol->second.m_bDispatch)
        {
          unsigned threadsPerGroup[3] = {vol->second.m_Threads[0], vol->second.m_Threads[1],
                                         vol->second.m_Threads[2]};
          if(threadsPerGroup[0] == 0 && threadsPerGroup[1] == 0 && threadsPerGroup[2] == 0)
          {
            const D3D12Pipe::Shader *cs = StageShader(st, ShaderStage::Compute);
            if(cs != NULL && cs->reflection != NULL)
            {
              for(int i = 0; i < 3; i++)
                threadsPerGroup[i] = cs->reflection->dispatchThreadsDimension[i];
            }
          }

          long long threads = 0;
          if(threadsPerGroup[0] > 0 && threadsPerGroup[1] > 0 && threadsPerGroup[2] > 0)
          {
            threads = (long long)vol->second.m_Groups[0] * vol->second.m_Groups[1] *
                      vol->second.m_Groups[2] * threadsPerGroup[0] * threadsPerGroup[1] *
                      threadsPerGroup[2];
          }

          volume =
              Fmt("\"volume\": {\"groups\": [%u, %u, %u], \"threadsPerGroup\": [%u, %u, %u], "
                  "\"threads\": %lld}",
                  (unsigned)vol->second.m_Groups[0], (unsigned)vol->second.m_Groups[1],
                  (unsigned)vol->second.m_Groups[2], threadsPerGroup[0], threadsPerGroup[1],
                  threadsPerGroup[2], threads);
        }
        else
        {
          const long long instances = vol->second.m_Instances > 0 ? vol->second.m_Instances : 1;
          const long long triangles =
              vol->second.m_Count > 0
                  ? PrimitiveCount(st->inputAssembly.topology, vol->second.m_Count) * instances
                  : 0;
          volume = Fmt("\"volume\": {\"vertices\": %lld, \"instances\": %lld, \"triangles\": %lld}",
                       vol->second.m_Count, instances, triangles);
        }
        volume += ", ";
      }

      ObjectRow(Fmt(
          "{\"eid\": %d, \"marker\": \"%s\", \"pso\": \"%s\", \"psoKind\": \"%s\", \"shaders\": "
          "\"%s\","
          " \"targets\": [%s], \"depth\": \"%s\", \"rootParameters\": %u, %s\"state\": \"%s\"}",
          eid,
          JsonEscape(markerPaths.count(eid) ? markerPaths.find(eid)->second : std::string()).c_str(),
          IdText(st->pipelineResourceId).c_str(), bCompute ? "compute" : "graphics",
          shaderIds.c_str(), targets.c_str(), depth.c_str(),
          (unsigned)st->rootSignature.parameters.size(), volume.c_str(), stateHash.c_str()));
      eventsWritten++;
      ProfileAdd(kProfileEventRow, tRow);

      // A state file per distinct state rather than per event: the documents are kilobytes each and
      // most events repeat the previous one's, but the *first* event and every change are exactly
      // the ones a reader wants. `--events` forces extra ids.
      bool bForce = false;
      for(size_t i = 0; i < opts.m_ForceEvents.size(); i++)
      {
        if(opts.m_ForceEvents[i] == eid)
          bForce = true;
      }
      if(previousKey.empty() || key != previousKey || bForce)
      {
        previousKey = key;
        const int rc =
            WriteEventDocuments(ctrl, file, path, st, eid, bWantDisasm, opts.m_OutDir, written);
        if(rc == 0)
          stateFiles++;
        else
          skipped.push_back(std::make_pair(Fmt("states/%d", eid), "could not be written"));

        // Images belong to the same events as the state files, and for the same reason: most events
        // repeat the previous picture. One per event put 715 images (354 MB) in a bundle whose
        // whole point was to be readable; at the state boundaries it is ~30 events' worth.
        if(opts.m_bWithImages)
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
            const std::string png =
                (std::filesystem::path(opts.m_OutDir) / "rt" / Fmt("%d_%d.png", eid, (int)slot)).string();
            TextureSave save;
            save.resourceId = rt;
            save.destType = FileType::PNG;
            const ULONGLONG tImage = Millis();
            const ResultDetails res = ctrl->SaveTexture(save, rdcstr(png.c_str()));
            ProfileAdd(kProfileImages, tImage);
            unsigned long long bytes = 0;
            if(res.OK() && FileBytes(png.c_str(), bytes) && bytes > 0)
              written.push_back(BundleRelative(opts.m_OutDir, png));
            else
              skipped.push_back(std::make_pair(
                  BundleRelative(opts.m_OutDir, png),
                  res.OK() ? std::string("the engine wrote an empty file") : ResultText(res)));
          }
        }
      }
      progress.Tick((int)index + 1);
    }
    progress.Done((int)ids.size());

    ArrayClose(false);    // the scan block and the totals below
    g_Indent = 1;
    Field("total", (long long)eventsWritten);
    Field("scanned", (long long)scanned);
    Field("scanFrom", (long long)opts.m_Since);
    Field("scanTo", (long long)lastEid);
    Field("scanStopped", stopped);
    Field("stateFiles", (long long)stateFiles, true);
    g_Indent = 0;
    printf("}\n");
    written.push_back("events.json");
  }
  Log("bundle: events.json written (%d id(s) with bound state, %d state file group(s))",
      eventsWritten, (int)stateFiles);

  // Everything that could be answered differently because the host was faster is behind us: no
  // `SetFrameEvent` follows this point, so from here the documents are written through a buffered
  // stdout. Their syscalls were 3.8 s of a 36.5 s dump (`resources.json` alone), and buffering a
  // document written *between* engine calls is the change that was measured and reverted -- the engine
  // hands back a different `states/841.state.json` when the host returns to it sooner (REFERENCE 9).
  SetDocumentBuffering(true);

  // ------------------------------------------------------------------ resources.json
  {
    const JsonDocument bJson;
    const CaptureStdout out(std::filesystem::path(opts.m_OutDir) / "resources.json");
    if(!out.Ok())
      return Fail(1, "cannot write resources.json in %s", opts.m_OutDir.c_str());

    PrintCaptureHeader(file, path);
    ArrayOpen("resources");

    const rdcarray<ResourceDescription> &resources = ctrl->GetResources();
    Progress progress;
    progress.Begin("bundle: resources.json", (int)resources.size());
    for(size_t i = 0; i < resources.size(); i++)
    {
      const ResourceDescription &r = resources[i];
      const std::string id = IdText(r.resourceId);

      const std::map<std::string, const TextureDescription *>::const_iterator texIt =
          textures.find(id);
      const TextureDescription *tex = (texIt == textures.end()) ? NULL : texIt->second;
      const std::map<std::string, const BufferDescription *>::const_iterator bufIt = buffers.find(id);
      const BufferDescription *buf = (bufIt == buffers.end()) ? NULL : bufIt->second;

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
      if(opts.m_bNoUsage)
      {
        Field("usage", std::string("(not collected: --no-usage)"), true);
      }
      else
      {
        const ULONGLONG tUsage = Millis();
        const rdcarray<EventUsage> usage = ctrl->GetUsage(r.resourceId);
        ProfileAdd(kProfileUsage, tUsage);
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
      progress.Tick((int)i + 1);
    }
    progress.Done((int)resources.size());

    ArrayClose(false);
    g_Indent = 1;
    Field("total", (long long)resources.size(), true);
    g_Indent = 0;
    printf("}\n");
    written.push_back("resources.json");
  }
  Log("bundle: resources.json written (%d resources)", (int)ctrl->GetResources().size());

  // ------------------------------------------------------------------ messages.json
  {
    const JsonDocument bJson;
    const CaptureStdout out(std::filesystem::path(opts.m_OutDir) / "messages.json");
    if(!out.Ok())
      return Fail(1, "cannot write messages.json in %s", opts.m_OutDir.c_str());

    PrintCaptureHeader(file, path);
    const ULONGLONG tMessages = Millis();
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
    g_Indent = 1;
    Field("total", (long long)msgs.size(), true);
    g_Indent = 0;
    printf("}\n");
    written.push_back("messages.json");
    ProfileAdd(kProfileMessages, tMessages);
  }
  Log("bundle: messages.json written (%d message(s))", (int)ctrl->GetDebugMessages().size());

  // ------------------------------------------------------------------ counters.json (optional)
  if(opts.m_bWithCounters)
  {
    Log("bundle: fetching counters (the slow part, when the driver supports them)");
    const JsonDocument bJson;
    const CaptureStdout out(std::filesystem::path(opts.m_OutDir) / "counters.json");
    if(!out.Ok())
      return Fail(1, "cannot write counters.json in %s", opts.m_OutDir.c_str());

    PrintCaptureHeader(file, path);
    const rdcarray<CounterResult> results = ctrl->FetchCounters(rdcarray<GPUCounter>());
    ArrayOpen("counters");
    for(size_t i = 0; i < results.size(); i++)
      ObjectRow(Fmt("{\"eid\": %u, \"counter\": %u, \"value\": %g}", (unsigned)results[i].eventId,
                    (unsigned)results[i].counter, results[i].value.d));
    ArrayClose(false);
    g_Indent = 1;
    Field("total", (long long)results.size(), true);
    g_Indent = 0;
    printf("}\n");
    written.push_back("counters.json");
  }

  // ------------------------------------------------------------------ textures/ (optional)
  if(opts.m_bWithTextures)
  {
    Log("bundle: saving %d texture(s) through the engine's decoder", (int)ctrl->GetTextures().size());
    Progress progress;
    progress.Begin("bundle: textures", (int)ctrl->GetTextures().size());
    for(size_t i = 0; i < ctrl->GetTextures().size(); i++)
    {
      const ULONGLONG tTexture = Millis();
      const TextureDescription &t = ctrl->GetTextures()[i];
      TextureSave save;
      save.resourceId = t.resourceId;
      save.destType = FileType::PNG;
      // A `std::string` because the engine wants one (`rdcstr`), built as a path because this is a
      // path: the join is the tool's, and the engine is handed the same bytes it always was.
      const std::string out =
          (std::filesystem::path(opts.m_OutDir) / "textures" / (IdText(t.resourceId) + ".png")).string();
      const ResultDetails res = ctrl->SaveTexture(save, rdcstr(out.c_str()));
      // A successful `SaveTexture` can still leave an empty file (measured: one texture in the Android
      // capture), and a 0-byte PNG in the manifest is worse than a line saying it could not be decoded.
      unsigned long long bytes = 0;
      if(res.OK() && FileBytes(out.c_str(), bytes) && bytes > 0)
        written.push_back(BundleRelative(opts.m_OutDir, out));
      else
        skipped.push_back(std::make_pair(
            BundleRelative(opts.m_OutDir, out),
            res.OK() ? std::string("the engine wrote an empty file") : ResultText(res)));
      ProfileAdd(kProfileTextures, tTexture);
      progress.Tick((int)i + 1);
    }
    progress.Done((int)ctrl->GetTextures().size());
  }

  // ------------------------------------------------------------------ manifest.json
  {
    const JsonDocument bJson;
    const CaptureStdout out(std::filesystem::path(opts.m_OutDir) / "manifest.json");
    if(!out.Ok())
      return Fail(1, "cannot write manifest.json in %s", opts.m_OutDir.c_str());

    unsigned long long captureBytes = 0;
    const std::filesystem::path captureAbs = AbsolutePath(path);
    printf("{\n");    // this writer builds its own document
    g_Indent = 1;
    Field("schemaVersion", (long long)kSchemaVersion);
    Field("bundleVersion", 1);
    Field("driver", std::string("replay_dump"));
    Field("renderdoc", std::string(g_GetVersionString ? g_GetVersionString() : "?"));
    Field("capture", std::string(path));
    Field("captureAbsolute", captureAbs.string());
    Field("captureBytes", FileBytes(captureAbs, captureBytes) ? (long long)captureBytes : 0);
    Field("captureSha256", Sha256File(captureAbs));
    Field("since", (long long)opts.m_Since);
    Field("until", (long long)opts.m_Until);
    Field("maxEvents", (long long)opts.m_MaxEvents);
    Field("withImages", (long long)(opts.m_bWithImages ? 1 : 0));
    Field("withCounters", (long long)(opts.m_bWithCounters ? 1 : 0));
    Field("withTextures", (long long)(opts.m_bWithTextures ? 1 : 0));
    Field("resourceUsage", std::string(opts.m_bNoUsage ? "not collected" : "collected"));
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
        " \"why\": \"`psoKind` says whether the event is a dispatch, and `volume` says what a draw"
        " or a dispatch asked for; the rest -- copy against clear against marker, and the call's"
        " own name -- is in the action list the engine exposes and this bundle does not write\"}"));
    ObjectRow(std::string(
        "{\"what\": \"the marker *path* of an event that is outside every marker\", \"why\": \"the"
        " path is empty there, which is the truth rather than a missing measurement; `draws`"
        " prints the same tree\"}"));
    ObjectRow(
        std::string("{\"what\": \"texture thumbnails\", \"why\": \"the engine decodes textures but"
                    " does not resize them; --textures writes full decodes\"}"));
    ObjectRow(
        std::string("{\"what\": \"an exact end to the id list\", \"why\": \"ids past the frame's"
                    " last event clamp to it, so the sweep is bounded by the file's chunk count"
                    " (REFERENCE §9) and may hold a few trailing repeats\"}"));
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
      const std::filesystem::path full = std::filesystem::path(opts.m_OutDir) / written[i];
      unsigned long long bytes = 0;
      FileBytes(full, bytes);    // `written[i]` is `/`-separated; Windows accepts either
      const std::string hash = Sha256File(full);
      ObjectRow(Fmt("{\"path\": \"%s\", \"bytes\": %llu, \"sha256\": \"%s\"}", written[i].c_str(),
                    bytes, hash.c_str()));
      totalBytes += bytes;
    }
    ArrayClose(false);

    g_Indent = 1;
    Field("fileCount", (long long)written.size());
    Field("fileBytes", (long long)totalBytes, true);
    g_Indent = 0;
    printf("}\n");
  }
  Log("bundle: manifest.json written (%d file(s))", (int)written.size());

  PrintCaptureHeader(file, path);
  Field("out", opts.m_OutDir);
  Field("events", (long long)eventsWritten);
  Field("stateFiles", (long long)stateFiles);
  Field("files", (long long)written.size());
  Field("skipped", (long long)skipped.size());
  Field("scanStopped", std::string(stopped), true);
  g_Indent = 0;
  if(g_bJson)
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
  const std::filesystem::path manifestPath = std::filesystem::path(dir) / "manifest.json";
  std::string text;
  if(!ReadWholeFile(manifestPath, text))
    return Fail(1, "cannot read %s", manifestPath.string().c_str());

  if(g_bJson)
    printf("{\n");
  g_Indent = g_bJson ? 1 : 0;
  Field("schemaVersion", (long long)kSchemaVersion);
  Field("manifest", manifestPath.string());

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

    // `rel` is the manifest's own `/`-separated path and a path takes either separator, so the loop
    // that rewrote every `/` to `\` was the driver doing by hand what Windows already does.
    const std::filesystem::path full = std::filesystem::path(dir) / rel;

    unsigned long long onDisk = 0;
    if(!FileBytes(full, onDisk))
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
      const std::string hash = Sha256File(full);
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

  g_Indent = g_bJson ? 1 : 0;
  Field("checked", (long long)checked);
  Field("problems", (long long)bad);
  // `fileCount`, not `files`: the array above already holds that name, and a repeated key in one
  // object is resolved by every parser to the last one -- so the count silently replaced the rows.
  // Writing the document's schema is what surfaced it (the schema cannot describe two members with
  // one name).
  Field("fileCount", (long long)(checked + bad), true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return bad == 0 ? 0 : 1;
}

// --------------------------------------------------------------------------- schema
//: The schemas themselves live in schema.cpp/schema.h, with the reasoning: they are data, and they
//: were the largest single block in this file. What follows prints them, writes them and checks
//: them, and is here because it reports through `Fail` and the run log like every other command.

//: Read one schema file, folding CRLF to LF: a checkout can rewrite a file's line endings, and the contract
//: is the JSON, not the ending. A missing or unreadable file is a difference, not an error.
