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
}    // namespace

void SetDocumentBuffering(bool bOn)
{
  g_bBufferDocuments = bOn;
}

bool DocumentBuffering()
{
  return g_bBufferDocuments;
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
    {"SetFrameEvent", 0, 0},    {"GetD3D12PipelineState", 0, 0}, {"state document", 0, 0},
    {"shaders document", 0, 0}, {"cbuffer documents", 0, 0},     {"event row", 0, 0},
    {"images", 0, 0},           {"action tree", 0, 0},           {"resources.json", 0, 0},
    {"messages.json", 0, 0},    {"textures.json", 0, 0},         {"usage lists", 0, 0},
    {"pixel history", 0, 0},
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

HMODULE LoadReplayDLL()
{
  const char *env = getenv("RDC_RENDERDOC_DLL");
  std::string path = env && *env ? env : "C:\\Program Files\\RenderDoc\\renderdoc.dll";

  HMODULE dll = LoadLibraryA(path.c_str());
  if(dll == NULL)
  {
    fprintf(stderr, "cannot load %s (set RDC_RENDERDOC_DLL to the renderdoc.dll to use)\n",
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
  if(g_ShutdownReplay != NULL)
    g_ShutdownReplay();
}

CaptureFileGuard::CaptureFileGuard(ICaptureFile *capture) : file(capture)
{
}

CaptureFileGuard::~CaptureFileGuard()
{
  if(file != NULL)
    file->Shutdown();
}

ControllerGuard::ControllerGuard(IReplayController *replay) : ctrl(replay)
{
}

ControllerGuard::~ControllerGuard()
{
  if(ctrl != NULL)
    ctrl->Shutdown();
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
