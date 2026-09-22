// z.capture — the replay session: DLL, logging, guards, paths and argument helpers
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

//: readable after a run that had to be killed, since no later run can touch this one's file.
ULONGLONG g_Start = 0;
FILE *g_LogFile = NULL;

namespace
{
//: Off until a command says every engine call is behind it (`SetDocumentBuffering`); `common.h` and
//: REFERENCE 9 have what turning it on too early costs.
bool g_bBufferDocuments = false;

//: Whether this process has moved the replay yet. One bit, set by the one function that moves it,
//: and the whole of `AnyEventReplayed`: `probe` is the one command whose answer depends on the
//: engine being cold, and this is how it knows (commands_frame.cpp).
bool g_bReplayed = false;
}    // namespace

void SetDocumentBuffering(bool bOn)
{
  g_bBufferDocuments = bOn;
}

bool DocumentBuffering()
{
  return g_bBufferDocuments;
}

void MoveToEvent(IReplayController *ctrl, int eid)
{
  g_bReplayed = true;
  ctrl->SetFrameEvent((uint32_t)eid, true);    // the one `SetFrameEvent` left in the program
}

bool AnyEventReplayed()
{
  return g_bReplayed;
}

ULONGLONG Millis()
{
  return GetTickCount64();
}

void Log(const char *fmt, ...)
{
  char text[512];
  va_list args;
  va_start(args, fmt);
  vsnprintf(text, sizeof(text), fmt, args);
  va_end(args);

  const double seconds = (g_Start == 0) ? 0.0 : (Millis() - g_Start) / 1000.0;
  fprintf(stderr, "[replay_dump] %6.1fs  %s\n", seconds, text);
  fflush(stderr);

  if(g_LogFile != NULL)
  {
    SYSTEMTIME now;
    GetLocalTime(&now);
    fprintf(g_LogFile, "%04d-%02d-%02d %02d:%02d:%02d.%03d  %7.1fs  %s\n", now.wYear, now.wMonth,
            now.wDay, now.wHour, now.wMinute, now.wSecond, now.wMilliseconds, seconds, text);
    fflush(g_LogFile);
  }
}

//: The extra step tracing that only appears with `$RDC_REPLAY_DEBUG`: a crash inside the engine
//: leaves no traceback, so knowing which step it died on is the difference between a fix and a guess.
void Trace(const char *step)
{
  if(getenv("RDC_REPLAY_DEBUG") != NULL)
    Log("%s", step);
}

// --------------------------------------------------------------------------- profiling (common.h)

namespace
{
//: One measured call site. `name` is the only place a slot's name lives, so a name and its position
//: in the enum cannot drift apart.
struct ProfileBucket
{
  const char *name;
  unsigned long long ms;
  unsigned long long calls;
};

ProfileBucket g_Profile[kProfileCount] = {
    {"SetFrameEvent", 0, 0},
    {"GetD3D12PipelineState", 0, 0},
    {"state document", 0, 0},
    {"shaders document", 0, 0},
    {"cbuffer documents", 0, 0},
    {"event row", 0, 0},
    {"images", 0, 0},
    {"action tree", 0, 0},
    {"resources.json", 0, 0},
    {"messages.json", 0, 0},
    {"textures.json", 0, 0},
    {"usage lists", 0, 0},
    {"pixel history", 0, 0},
    {"crosscheck", 0, 0},
};

bool ProfileOn()
{
  static const bool bOn = (getenv("RDC_PROFILE") != NULL);
  return bOn;
}
}    // namespace

void ProfileAdd(ProfileSlot slot, unsigned long long since)
{
  if(slot < 0 || slot >= kProfileCount || !ProfileOn())
    return;
  const unsigned long long now = Millis();
  g_Profile[slot].ms += (now - since);
  g_Profile[slot].calls++;
}

void ProfileReport()
{
  if(!ProfileOn())
    return;
  bool bAny = false;
  for(int i = 0; i < kProfileCount; i++)
  {
    if(g_Profile[i].calls == 0)
      continue;
    if(!bAny)
    {
      Log("profile: where the time went (this run has $RDC_PROFILE set)");
      bAny = true;
    }
    Log("profile:   %-22s %8.2fs in %6llu call(s), %6.1f ms each", g_Profile[i].name,
        g_Profile[i].ms / 1000.0, g_Profile[i].calls,
        (double)g_Profile[i].ms / (double)g_Profile[i].calls);
  }
}

// --------------------------------------------------------------------------- progress (common.h)

namespace
{
//: Long enough to be worth a line, short enough that a stuck run is obvious within a coffee sip.
const ULONGLONG kProgressEveryMs = 10000;
//: A loop shorter than this says nothing at all: a summary line for two seconds of work is noise.
const ULONGLONG kProgressSummaryMs = 5000;

std::string HumanTime(unsigned long long ms)
{
  const unsigned long long seconds = (ms + 500) / 1000;
  if(seconds < 90)
    return Fmt("%llu s", seconds);
  if(seconds < 5400)
    return Fmt("%llu min %llu s", seconds / 60, seconds % 60);
  return Fmt("%llu h %llu min", seconds / 3600, (seconds / 60) % 60);
}
}    // namespace

void Progress::Begin(const char *what, int total)
{
  m_What = what;
  m_Total = total;
  m_Start = Millis();
  m_Last = m_Start;
  m_Logged = false;
}

void Progress::Tick(int done)
{
  const ULONGLONG now = Millis();
  if(now - m_Last < kProgressEveryMs)
    return;
  m_Last = now;
  m_Logged = true;

  const unsigned long long elapsed = now - m_Start;
  const double each = (done > 0) ? (double)elapsed / (double)done : 0.0;
  if(m_Total > 0 && done < m_Total && done > 0)
  {
    const unsigned long long left = (unsigned long long)(each * (m_Total - done));
    Log("%s: %d/%d (%d%%), %.0f ms each, ~%s left", m_What.c_str(), done, m_Total,
        (int)((100LL * done) / m_Total), each, HumanTime(left).c_str());
  }
  else
  {
    Log("%s: %d done, %.0f ms each", m_What.c_str(), done, each);
  }
}

void Progress::Done(int done)
{
  const unsigned long long elapsed = Millis() - m_Start;
  if(!m_Logged && elapsed < kProgressSummaryMs)
    return;
  const double each = (done > 0) ? (double)elapsed / (double)done : 0.0;
  if(m_Total > 0)
    Log("%s: %d/%d done in %.1fs (%.0f ms each)", m_What.c_str(), done, m_Total, elapsed / 1000.0,
        each);
  else
    Log("%s: %d done in %.1fs (%.0f ms each)", m_What.c_str(), done, elapsed / 1000.0, each);
}

//: A run that stops early has to say so *in the log*, not only on stderr. Without this the log just
//: ends at whatever step was reached, which is indistinguishable from a run that hung there -- and
//: that is not hypothetical: a failed `OpenFile`, which logs nothing after it, was read as a hang
//: until the same run was timed on its own and exited in 0.2 s. Both outputs get the same text, so
//: a log and a stderr capture say the same thing.
int Fail(int code, _Printf_format_string_ const char *fmt, ...)
{
  char text[512];
  va_list args;
  va_start(args, fmt);
  vsnprintf(text, sizeof(text), fmt, args);
  va_end(args);

  fprintf(stderr, "error: %s\n", text);
  Log("failed: %s", text);
  return code;
}

//: A path as this process will actually use it. Relative paths are resolved against the *working
//: directory*, which is not the directory of whatever launched the tool: a runner that starts the
//: exe from elsewhere gets `can't open ... errno 2` and no clue where it looked. Printing the
//: absolute form (and the working directory, once) turns that into an answer.
std::string AbsolutePath(const char *path)
{
  if(path == NULL || *path == '\0')
    return std::string();

  char buf[4096];
  const DWORD len = GetFullPathNameA(path, (DWORD)sizeof(buf), buf, NULL);
  if(len == 0 || len >= sizeof(buf))
    return std::string(path);
  return std::string(buf);
}

std::string WorkingDirectory()
{
  char buf[4096];
  const DWORD len = GetCurrentDirectoryA((DWORD)sizeof(buf), buf);
  return (len == 0 || len >= sizeof(buf)) ? std::string("?") : std::string(buf);
}

//: When a file was last written, as the raw `FILETIME` (100 ns ticks since 1601) so two of them can
//: be compared with `>`; 0 when it is not there, which is the answer a caller has to treat as "no
//: opinion" rather than "very old".
long long FileWriteTime(const std::string &path)
{
  WIN32_FILE_ATTRIBUTE_DATA info;
  if(!GetFileAttributesExA(path.c_str(), GetFileExInfoStandard, &info))
    return 0;
  return ((long long)info.ftLastWriteTime.dwHighDateTime << 32) | info.ftLastWriteTime.dwLowDateTime;
}

//: The file in `dir` whose name ends with one of the `count` suffixes and that was written last,
//: with its time; 0 when the directory is not there or holds none of them, and then `name` is left
//: alone. Suffixes rather than a wildcard because `FindFirstFile`'s `*.cpp` also matches `.cpp.swp`
//: on some systems' rules and this answers "is a source newer than the exe".
long long NewestSourceTime(const char *dir, const char *const *suffixes, int count, std::string &name)
{
  const std::string pattern = std::string(dir) + "\\*";
  WIN32_FIND_DATAA found;
  HANDLE search = FindFirstFileA(pattern.c_str(), &found);
  if(search == INVALID_HANDLE_VALUE)
    return 0;

  long long newest = 0;
  for(BOOL more = TRUE; more; more = FindNextFileA(search, &found))
  {
    if((found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0)
      continue;
    const std::string file(found.cFileName);
    bool bMatch = false;
    for(int i = 0; i < count && !bMatch; i++)
    {
      const size_t len = strlen(suffixes[i]);
      bMatch = file.size() >= len && file.compare(file.size() - len, len, suffixes[i]) == 0;
    }
    if(!bMatch)
      continue;
    const long long when = ((long long)found.ftLastWriteTime.dwHighDateTime << 32) |
                           found.ftLastWriteTime.dwLowDateTime;
    if(when > newest)
    {
      newest = when;
      name = file;
    }
  }
  FindClose(search);
  return newest;
}

//: This run's log base name, *without* the extension: `<exe stem>_<date>_<time>` beside the
//: executable, always, with no environment variable to set. A tool that has to be *told* where to
//: write its progress is a tool that produces none at the moment it matters -- and the log beside
//: the exe is also the one place a reader will look for it.
//:
//: One file *per run*, named after the second it starts. A single shared file has to choose between
//: two wrong answers: truncating loses the run that hung as soon as the next one starts, and
//: appending grows without bound while mixing the runs that are being compared -- and two runs
//: started at once interleave in either mode. Per-run files keep every run whole, and the name says
//: which is which. `--log <file>` names one exact file instead (see `OpenLog`).
//:
//: The extension is deliberately left to `OpenLog`, which adds it *after* the collision suffix:
//: `.log.txt` is two extensions, so completing the name here filed the second run of a second as
//: `..._14-32-07.log-2.txt` -- a suffix in the middle of the name.
std::string DefaultLogStem()
{
  char exe[4096];
  const DWORD len = GetModuleFileNameA(NULL, exe, (DWORD)sizeof(exe));
  std::string path =
      (len == 0 || len >= sizeof(exe)) ? std::string("replay_dump") : std::string(exe, len);

  const size_t slash = path.find_last_of("\\/");
  const size_t dot = path.find_last_of('.');
  if(dot != std::string::npos && (slash == std::string::npos || dot > slash))
    path = path.substr(0, dot);    // the exe's stem: the extension is dropped

  SYSTEMTIME now;
  GetLocalTime(&now);
  char name[4200];
  snprintf(name, sizeof(name), "%s_%04d-%02d-%02d_%02d-%02d-%02d", path.c_str(), now.wYear,
           now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond);
  return name;
}

//: Open this run's log, creating it if and only if the name is free (`wx`), because a per-run name
//: must not already exist and `wx` is what makes "is this name free?" atomic instead of a check
//: with a race behind it. Two runs starting in the same second then get `..._14-32-07.log.txt` and
//: `..._14-32-07-2.log.txt` rather than one quietly writing into the other's file.
//:
//: `--log <file>` names one exact file, which is opened the ordinary way: truncated, since it is
//: still *this* run's log and nothing else's.
std::string LibraryLogStem()
{
  // The library's own module, not the host process's executable: `GetModuleFileNameA(NULL, ...)` from
  // inside a DLL loaded by `python.exe` is `python.exe`, and the per-run log would land next to the
  // interpreter -- a directory a caller on a normal install cannot write to. The address of one of our
  // own functions is what names this DLL, which works whether it was loaded by name or by path.
  HMODULE self = NULL;
  if(!GetModuleHandleExA(
         GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
         (LPCSTR)&LibraryLogStem, &self))
    self = NULL;

  char module[4096];
  const DWORD len = GetModuleFileNameA(self, module, (DWORD)sizeof(module));
  std::string path =
      (len == 0 || len >= sizeof(module)) ? std::string("rdc_replay") : std::string(module, len);
  const size_t slash = path.find_last_of("\\/");
  const size_t dot = path.find_last_of('.');
  if(dot != std::string::npos && (slash == std::string::npos || dot > slash))
    path = path.substr(0, dot);    // the DLL's stem: the extension is dropped

  SYSTEMTIME now;
  GetLocalTime(&now);
  char name[4200];
  snprintf(name, sizeof(name), "%s_%04d-%02d-%02d_%02d-%02d-%02d", path.c_str(), now.wYear,
           now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond);
  return name;
}

void CloseLog()
{
  if(g_LogFile != NULL)
  {
    fclose(g_LogFile);
    g_LogFile = NULL;
  }
}

FILE *OpenLog(const std::string &requested, bool bPerRun, std::string &openedAs)
{
  const std::string stem = AbsolutePath(requested.c_str());
  if(!bPerRun)
  {
    openedAs = stem;
    return fopen(openedAs.c_str(), "w");
  }

  for(int n = 1; n <= 99; n++)
  {
    char name[4200];
    if(n == 1)
      snprintf(name, sizeof(name), "%s.log.txt", stem.c_str());
    else
      snprintf(name, sizeof(name), "%s-%d.log.txt", stem.c_str(), n);

    FILE *f = fopen(name, "wx");
    if(f != NULL)
    {
      openedAs = name;
      return f;
    }
    if(errno != EEXIST)
      break;    // a missing directory, no permission, ...
  }

  openedAs = stem;    // the refused name, for the warning
  return NULL;
}

pGetVersionString g_GetVersionString = NULL;
pShutdownReplay g_ShutdownReplay = NULL;
std::string g_DllOverride;

//: Which renderdoc.dll to load: `--dll` first, then `$RDC_RENDERDOC_DLL`, then the installed engine.
//:
//: The flag is not a convenience over the environment variable. Comparing two builds -- "does this
//: capture replay the same under 1.46 and under 1.47?" -- is the same command twice with two `--dll`s,
//: where the environment would have to be re-set between runs and cannot be set per run at all.
HMODULE LoadReplayDLL()
{
  const char *env = getenv("RDC_RENDERDOC_DLL");
  const char *fallback = "C:\\Program Files\\RenderDoc\\renderdoc.dll";
  const std::string path = !g_DllOverride.empty()
                               ? g_DllOverride
                               : (env && *env ? std::string(env) : std::string(fallback));

  HMODULE dll = LoadLibraryA(path.c_str());
  if(dll == NULL)
  {
    fprintf(
        stderr,
        "cannot load %s (pass --dll <path>, or set RDC_RENDERDOC_DLL, to name the renderdoc.dll "
        "to replay with)\n",
        path.c_str());
    return NULL;
  }
  g_GetVersionString = (pGetVersionString)GetProcAddress(dll, "RENDERDOC_GetVersionString");
  return dll;
}

//: Set the replay system up before anything is opened, exactly as RenderDoc's own CLI does: this is
//: what reads the config, opens the log and marks the process as a replay host. `args` is the command
//: line (the engine logs it, and honours `--crash` to keep its own crash handler out of the way).
bool InitialiseReplay(HMODULE dll, int argc, char **argv)
{
  pInitialiseReplay init = (pInitialiseReplay)GetProcAddress(dll, "RENDERDOC_InitialiseReplay");
  g_ShutdownReplay = (pShutdownReplay)GetProcAddress(dll, "RENDERDOC_ShutdownReplay");
  if(init == NULL)
  {
    fprintf(stderr, "renderdoc.dll has no RENDERDOC_InitialiseReplay export\n");
    return false;
  }
  rdcarray<rdcstr> args;
  for(int i = 0; i < argc; i++)
    args.push_back(rdcstr(argv[i]));
  init(GlobalEnvironment(), args);
  return true;
}

ICaptureFile *OpenCaptureFile(HMODULE dll)
{
  pOpenCaptureFile open = (pOpenCaptureFile)GetProcAddress(dll, "RENDERDOC_OpenCaptureFile");
  if(open == NULL)
  {
    fprintf(stderr, "renderdoc.dll has no RENDERDOC_OpenCaptureFile export\n");
    return NULL;
  }
  return open();
}

// The teardown order is the engine's, and it is easy to get wrong by hand: the controller must go
// before the capture file, and both before the replay system. Declaring one guard per step is what
// makes the order automatic -- destruction runs in reverse -- and makes it happen on *every* path
// out of `main`, including the early returns, which used to skip the shutdown entirely. The bodies
// are out of line because only this file sees `g_ShutdownReplay`, resolved from the DLL here.
ReplaySystemGuard::~ReplaySystemGuard()
{
  Trace("shutting the replay system down");
  if(g_ShutdownReplay != NULL)
    g_ShutdownReplay();
  Trace("replay system down");
}

CaptureFileGuard::CaptureFileGuard(ICaptureFile *capture) : file(capture)
{
}

CaptureFileGuard::~CaptureFileGuard()
{
  Trace("closing the capture file");
  if(file != NULL)
    file->Shutdown();
  Trace("capture file closed");
}

ControllerGuard::ControllerGuard(IReplayController *replay) : ctrl(replay)
{
}

ControllerGuard::~ControllerGuard()
{
  Trace("shutting the controller down");
  if(ctrl != NULL)
    ctrl->Shutdown();
  Trace("controller down");
}

// --------------------------------------------------------------------------- the capture header

void PrintCaptureHeader(ICaptureFile *file, const char *path)
{
  if(g_bJson)
    printf("{\n");
  g_Indent = g_bJson ? 1 : 0;
  Field("schemaVersion", (long long)kSchemaVersion);    // every document says what shape it is
  Field("capture", std::string(path));
  Field("renderdoc", std::string(g_GetVersionString ? g_GetVersionString() : "?"));
  Field("driver", file->DriverName());
  Field("localReplay", (long long)file->LocalReplaySupport());
  Field("machine", file->RecordedMachineIdent());
}

// --------------------------------------------------------------------------- the version guard
//
// Replay must be done by an engine at least as new as the one that recorded the capture. The engine
// checks the *logfile format* version itself and fails with a message of its own (rdcfile.cpp's
// `FileIncompatibleVersion`), but the *program* version -- the number a reader is holding, and the
// one that differs between the machine that captured and the machine that replays -- lives in the
// container header, where nothing was looking at it. Before this guard a mismatch surfaced as
// whatever the engine did next: a section it could not read, a resource it refused, or an answer
// quietly produced by another version's decoding, which looks exactly like an answer.
//
// The comparison is MAJOR.MINOR: what `RENDERDOC_GetVersionString` returns ("1.46") and what the
// header's recording string starts with ("1.46 e4bd23" -- version, then the commit). Unparsable
// versions are *not* a refusal, because the file may be from a fork or a development build, and a
// guard that guesses about a capture it cannot read is worse than no guard at all. So: older is
// refused, equal and newer are allowed, unknown is said and passed on.

bool ReadCaptureVersion(const char *path, CaptureVersion &out)
{
  FILE *f = fopen(path, "rb");
  if(f == NULL)
    return false;
  unsigned char header[32] = {0};
  const size_t read = fread(header, 1, sizeof(header), f);
  fclose(f);
  if(read != sizeof(header) || memcmp(header, "RDOC", 4) != 0)
    return false;    // not one of ours: the engine's own OpenFile reports it, with its own message

  out.logfile = (uint32_t)header[8] | ((uint32_t)header[9] << 8) | ((uint32_t)header[10] << 16) |
                ((uint32_t)header[11] << 24);
  const char *text = (const char *)header + 16;    // up to 16 bytes, NUL-padded
  size_t length = 0;
  while(length < 16 && text[length] != '\0')
    length++;
  out.program.assign(text, length);
  return true;
}

bool ParseMajorMinor(const std::string &text, int &major, int &minor)
{
  size_t i = 0;
  if(i < text.size() && (text[i] == 'v' || text[i] == 'V'))
    i++;    // a release tag as a build writes it ("v1.46"), not a form this driver prints
  const size_t first = i;
  while(i < text.size() && text[i] >= '0' && text[i] <= '9')
    i++;
  if(i == first)
    return false;
  major = atoi(text.substr(first, i - first).c_str());
  if(i >= text.size() || text[i] != '.')
    return false;
  i++;
  const size_t second = i;
  while(i < text.size() && text[i] >= '0' && text[i] <= '9')
    i++;
  if(i == second)
    return false;
  minor = atoi(text.substr(second, i - second).c_str());
  return true;
}

int CompareMajorMinor(const std::string &engine, const std::string &capture, bool &known)
{
  int engineMajor = 0, engineMinor = 0, captureMajor = 0, captureMinor = 0;
  known = ParseMajorMinor(engine, engineMajor, engineMinor) &&
          ParseMajorMinor(capture, captureMajor, captureMinor);
  if(!known)
    return 0;
  // Numerically, not as text: "1.9" is older than "1.10" and a string compare says the opposite.
  if(engineMajor != captureMajor)
    return engineMajor < captureMajor ? -1 : 1;
  if(engineMinor != captureMinor)
    return engineMinor < captureMinor ? -1 : 1;
  return 0;
}

int GuardCaptureVersion(const char *pathAbs, const char *pathAsGiven, std::string *why)
{
  if(g_GetVersionString == NULL)
    return 0;    // no version to compare with: the engine's own checks are all there is

  CaptureVersion capture;
  if(!ReadCaptureVersion(pathAbs, capture))
    return 0;    // not a container this can read: `OpenFile` says so, with the engine's own message

  const std::string engine = g_GetVersionString();
  bool known = false;
  const int order = CompareMajorMinor(engine, capture.program, known);
  if(!known)
  {
    Log("note: cannot compare versions: %s says '%s' (logfile version %u) and this engine says "
        "'%s'",
        pathAsGiven, capture.program.c_str(), (unsigned)capture.logfile, engine.c_str());
    return 0;
  }
  if(order < 0)
  {
    // One sentence, two destinations: the CLI prints it with `Fail`, the library returns it through
    // the ABI's `err` buffer (api.cpp), and the wording is therefore written once.
    const std::string text =
        Fmt("%s was recorded by RenderDoc %s, and this replay engine is %s: replay must be at "
            "least the capture's version, because an older engine answers from another version's "
            "decoding rather than reporting an error. Install a newer RenderDoc, or point at one "
            "with `--dll <path>` or $RDC_RENDERDOC_DLL.",
            pathAsGiven, capture.program.c_str(), engine.c_str());
    if(why != NULL)
    {
      *why = text;
      return 1;
    }
    return Fail(1, "%s", text.c_str());
  }
  if(order > 0)
    Log("note: %s was recorded by RenderDoc %s; this engine is %s -- newer than the capture, which "
        "is "
        "allowed (replaying an older capture is the tested direction)",
        pathAsGiven, capture.program.c_str(), engine.c_str());
  return 0;
}

bool ParseInt(const char *text, int &value)
{
  if(text == NULL || *text == '\0')
    return false;

  const char *end = text + strlen(text);
  long long parsed = 0;
  const std::from_chars_result result = std::from_chars(text, end, parsed);
  if(result.ec != std::errc() || result.ptr != end)
    return false;
  if(parsed < (long long)std::numeric_limits<int>::min() ||
     parsed > (long long)std::numeric_limits<int>::max())
    return false;

  value = (int)parsed;
  return true;
}

int ToInt(const std::string &text, int fallback)
{
  int value = 0;
  if(!ParseInt(text.c_str(), value))
  {
    fprintf(stderr, "warning: '%s' is not an integer, using %d\n", text.c_str(), fallback);
    return fallback;
  }
  return value;
}

//: The option spellings, in one place: `main` reads them out of `argv` and `SplitLine` out of a
//: batch line, and a rename must not be able to drift between the two.
