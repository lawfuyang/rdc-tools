// replay_dump — headless RenderDoc replay as a data source (README §9).
//
// The offline tool (rdc_analysis.py) reads the *file*: the container, the chunk stream, the payload
// layouts. This tool asks the *engine* instead, which is the only way to get frame data exactly:
// named uniform values, shader reflection and disassembly, decoded textures, post-VS geometry, the
// rendered image, GPU counters, debug messages. Everything the offline tool deliberately leaves to
// replay lives here.
//
// Event ids are the *engine's*, not the file's. The offline tool prints chunk indices and used to
// call them event ids; that held on the two Unreal captures and does not on the hobby-renderer one,
// because RenderDoc numbers only what a command list recorded. `probe` lists the ids that really
// have pipeline state, and every command here takes engine ids.
//
// It talks to the installed renderdoc.dll through the replay API (renderdoc/api/replay/
// renderdoc_replay.h), so it needs no build of RenderDoc itself: the DLL is loaded at runtime and the
// handful of C entry points are resolved by name. The interfaces are C++ vtables, which is why this
// is C++ rather than Python -- RenderDoc 1.46's Python module (renderdoc.pyd) is not installed.
//
// Three things a replay host *must* do, none of them optional:
//   1. REPLAY_PROGRAM_MARKER() at file scope, so the DLL knows this is a replay program;
//   2. RENDERDOC_InitialiseReplay(env, args) before opening anything;
//   3. RENDERDOC_ShutdownReplay() on the way out.
// Without (1) or (2) the engine runs with uninitialised global state and dies inside OpenCapture
// with an access violation.
//
// Build: build_replay.ps1 (MSVC + an import library made from the installed DLL's exports).
//
// One deliberate interface gap, because "fix it" is the wrong answer:
//
//   RenderDoc's stringisers for `ResultCode`, `ResourceUsage`, `MessageSeverity` and `GPUCounter`
//   live in its stringise.cpp and are **not exported** by the DLL, while the headers still call them
//   from inline code (`ResultDetails::Message()` is `ToStr(code)` when there is no detail string).
//   Supplying those template specialisations here would make it link -- that is how this was found --
//   but RenderDoc's own definitions exist and are unreachable from this file, which is
//   [ifndr:temp.expl.spec.unreachable.declaration]: an implicit instantiation occurs while an
//   unreachable explicit specialisation would have matched. If the two were ever linked into one
//   image they would also be two definitions that do not match ([basic.def.odr],
//   [ifndr:basic.def.odr.definition.matches]). So there are no specialisations here at all: the
//   helpers below print the same numeric text the tool has always printed, and `ResultDetails` is
//   read through its public `internal_msg`/`code` members instead of through `Message()`.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <sal.h>                                     // _Printf_format_string_

#include <io.h>                                      // _dup/_dup2: the bundle writes files, not stdout

#include <bcrypt.h>                                  // SHA-256 for the bundle manifest
#pragma comment(lib, "bcrypt.lib")

#include <cerrno>
#include <charconv>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <string>
#include <type_traits>
#include <vector>

// By angle brackets, so /external:anglebrackets keeps the library's own warnings out of this build.
#include <renderdoc_replay.h>

REPLAY_PROGRAM_MARKER();

// --------------------------------------------------------------------------- enum text
//
// The enums below have fixed underlying types, so every value of the underlying type is a valid
// value of the enum and printing one as a number is exact -- no static_cast can be out of range
// ([dcl.enum], [ub:expr.static.cast.enum.outside.range] applies only to enums without one).

static const char *ResultCodeName(ResultCode code)
{
  switch(code)
  {
    case ResultCode::Succeeded: return "Succeeded";
    case ResultCode::UnknownError: return "UnknownError";
    case ResultCode::InternalError: return "InternalError";
    case ResultCode::FileNotFound: return "FileNotFound";
    case ResultCode::InjectionFailed: return "InjectionFailed";
    case ResultCode::IncompatibleProcess: return "IncompatibleProcess";
    case ResultCode::NetworkIOFailed: return "NetworkIOFailed";
    case ResultCode::NetworkRemoteBusy: return "NetworkRemoteBusy";
    case ResultCode::NetworkVersionMismatch: return "NetworkVersionMismatch";
    case ResultCode::FileIOFailed: return "FileIOFailed";
    case ResultCode::FileIncompatibleVersion: return "FileIncompatibleVersion";
    case ResultCode::FileCorrupted: return "FileCorrupted";
    case ResultCode::FileUnrecognised: return "FileUnrecognised";
    case ResultCode::ImageUnsupported: return "ImageUnsupported";
    case ResultCode::APIUnsupported: return "APIUnsupported";
    case ResultCode::APIInitFailed: return "APIInitFailed";
    case ResultCode::APIIncompatibleVersion: return "APIIncompatibleVersion";
    case ResultCode::APIHardwareUnsupported: return "APIHardwareUnsupported";
    case ResultCode::APIDataCorrupted: return "APIDataCorrupted";
    case ResultCode::APIReplayFailed: return "APIReplayFailed";
    case ResultCode::JDWPFailure: return "JDWPFailure";
    case ResultCode::AndroidGrantPermissionsFailed: return "AndroidGrantPermissionsFailed";
    case ResultCode::AndroidABINotFound: return "AndroidABINotFound";
    case ResultCode::AndroidAPKFolderNotFound: return "AndroidAPKFolderNotFound";
    case ResultCode::AndroidAPKInstallFailed: return "AndroidAPKInstallFailed";
    case ResultCode::AndroidAPKVerifyFailed: return "AndroidAPKVerifyFailed";
    case ResultCode::RemoteServerConnectionLost: return "RemoteServerConnectionLost";
    case ResultCode::OutOfMemory: return "OutOfMemory";
  }
  return NULL;                                       // unnamed: the caller prints the number
}

//: A failed operation as one line. `internal_msg`, when the engine supplied one, is the same text
//: `ResultDetails::Message()` would return and already names the code, so it is used verbatim; its
//: storage belongs to the engine and is only valid until `RENDERDOC_ShutdownReplay` (the header says
//: so), which is why this copies into a `std::string` straight away.
static std::string ResultText(const ResultDetails &res)
{
  if(res.internal_msg != NULL && !res.internal_msg->empty())
    return std::string(res.internal_msg->c_str());

  const char *name = ResultCodeName(res.code);
  char buf[64];
  if(name != NULL)
    snprintf(buf, sizeof(buf), "%s", name);
  else
    snprintf(buf, sizeof(buf), "ResultCode(%u)", (unsigned)res.code);
  return buf;
}

//: The numeric forms below are this tool's established output: RenderDoc's own names live in its
//: unexported stringise.cpp, and the API headers are where the number is looked up.
static std::string UsageText(ResourceUsage usage)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "usage(%u)", (unsigned)usage);
  return buf;
}

static std::string SeverityText(MessageSeverity severity)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "severity(%u)", (unsigned)severity);
  return buf;
}

static std::string CounterText(GPUCounter counter)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "counter(%u)", (unsigned)counter);
  return buf;
}

// --------------------------------------------------------------------------- output helpers

static bool g_json = false;
//: Stamped into every `--json` document and every file the bundle writes, so a consumer can refuse a
//: document it does not understand instead of guessing at a shape. The schemas themselves are the table
//: further down (`schema [<name>]`), and this number is what they all declare.
static const int kSchemaVersion = 1;
static int g_indent = 0;

//: The output format is fixed for the run (it comes from `--json` before anything else happens), so
//: the commands ask rather than read the flag: a raw global read at thirty call sites is how the
//: text and JSON paths drift apart.
static bool IsJson()
{
  return g_json;
}

//: One row of an array. `g_firstRow` is reset by `ArrayOpen` so commas land between rows and never
//: after the last one -- a trailing comma is not JSON.
static bool g_firstRow = true;

//: The number inside a `ResourceId`. Its value is private and RenderDoc's own stringiser for it is
//: not exported, so this copies the 8 bytes out exactly the way RenderDoc's `DoStringise<ResourceId>`
//: does (`core.cpp`) -- the struct is a `uint64_t` wrapper by design. Ids then read the same way the
//: offline tool prints them (`res1234`).
//:
//: The two static_asserts are what make that byte copy defensible rather than hopeful: `memcpy` into
//: a `uint64_t` is only defined for a trivially copyable source of the same size ([basic.types],
//: [class.mem]), and if either stops being true this fails to compile instead of reading whatever
//: the object happens to look like.
static std::string IdText(ResourceId id)
{
  static_assert(std::is_trivially_copyable<ResourceId>::value,
                "ResourceId must stay trivially copyable for the byte copy below to be defined");
  static_assert(sizeof(ResourceId) == sizeof(uint64_t), "ResourceId is a uint64_t wrapper");

  uint64_t value = 0;
  memcpy(&value, &id, sizeof(value));
  char buf[32];
  snprintf(buf, sizeof(buf), "%llu", (unsigned long long)value);
  return buf;
}

//: Progress goes to stderr, never stdout: stdout is the command's output and something may be
//: parsing it. The same lines also go to a log file, *one per run* and beside the executable
//: (`--log <file>` names one exact file instead). That is what makes a long batch watchable -- and
//: readable after a run that had to be killed, since no later run can touch this one's file.
static ULONGLONG g_start = 0;
static FILE *g_logFile = NULL;

static ULONGLONG Millis()
{
  return GetTickCount64();
}

static void Log(const char *fmt, ...)
{
  char text[512];
  va_list args;
  va_start(args, fmt);
  vsnprintf(text, sizeof(text), fmt, args);
  va_end(args);

  const double seconds = (g_start == 0) ? 0.0 : (Millis() - g_start) / 1000.0;
  fprintf(stderr, "[replay_dump] %6.1fs  %s\n", seconds, text);
  fflush(stderr);

  if(g_logFile != NULL)
  {
    SYSTEMTIME now;
    GetLocalTime(&now);
    fprintf(g_logFile, "%04d-%02d-%02d %02d:%02d:%02d.%03d  %7.1fs  %s\n", now.wYear, now.wMonth,
            now.wDay, now.wHour, now.wMinute, now.wSecond, now.wMilliseconds, seconds, text);
    fflush(g_logFile);
  }
}

//: The extra step tracing that only appears with `$RDC_REPLAY_DEBUG`: a crash inside the engine
//: leaves no traceback, so knowing which step it died on is the difference between a fix and a guess.
static void Trace(const char *step)
{
  if(getenv("RDC_REPLAY_DEBUG") != NULL)
    Log("%s", step);
}

//: A run that stops early has to say so *in the log*, not only on stderr. Without this the log just
//: ends at whatever step was reached, which is indistinguishable from a run that hung there -- and
//: that is not hypothetical: a failed `OpenFile`, which logs nothing after it, was read as a hang
//: until the same run was timed on its own and exited in 0.2 s. Both outputs get the same text, so a
//: log and a stderr capture say the same thing.
static int Fail(int code, _Printf_format_string_ const char *fmt, ...)
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
static std::string AbsolutePath(const char *path)
{
  if(path == NULL || *path == '\0')
    return std::string();

  char buf[4096];
  const DWORD len = GetFullPathNameA(path, (DWORD)sizeof(buf), buf, NULL);
  if(len == 0 || len >= sizeof(buf))
    return std::string(path);
  return std::string(buf);
}

static std::string WorkingDirectory()
{
  char buf[4096];
  const DWORD len = GetCurrentDirectoryA((DWORD)sizeof(buf), buf);
  return (len == 0 || len >= sizeof(buf)) ? std::string("?") : std::string(buf);
}

//: This run's log base name, *without* the extension: `<exe stem>_<date>_<time>` beside the
//: executable, always, with no environment variable to set. A tool that has to be *told* where to
//: write its progress is a tool that produces none at the moment it matters -- and the log beside the
//: exe is also the one place a reader will look for it.
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
static std::string DefaultLogStem()
{
  char exe[4096];
  const DWORD len = GetModuleFileNameA(NULL, exe, (DWORD)sizeof(exe));
  std::string path =
      (len == 0 || len >= sizeof(exe)) ? std::string("replay_dump") : std::string(exe, len);

  const size_t slash = path.find_last_of("\\/");
  const size_t dot = path.find_last_of('.');
  if(dot != std::string::npos && (slash == std::string::npos || dot > slash))
    path = path.substr(0, dot);                      // the exe's stem: the extension is dropped

  SYSTEMTIME now;
  GetLocalTime(&now);
  char name[4200];
  snprintf(name, sizeof(name), "%s_%04d-%02d-%02d_%02d-%02d-%02d", path.c_str(), now.wYear,
           now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond);
  return name;
}

//: Open this run's log, creating it if and only if the name is free (`wx`), because a per-run name
//: must not already exist and `wx` is what makes "is this name free?" atomic instead of a check with
//: a race behind it. Two runs starting in the same second then get `..._14-32-07.log.txt` and
//: `..._14-32-07-2.log.txt` rather than one quietly writing into the other's file.
//:
//: `--log <file>` names one exact file, which is opened the ordinary way: truncated, since it is
//: still *this* run's log and nothing else's.
static FILE *OpenLog(const std::string &requested, bool perRun, std::string &openedAs)
{
  const std::string stem = AbsolutePath(requested.c_str());
  if(!perRun)
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
      break;                                         // a missing directory, no permission, ...
  }

  openedAs = stem;                                   // the refused name, for the warning
  return NULL;
}

static std::string JsonEscape(const char *s)
{
  std::string out;
  for(; s && *s; s++)
  {
    switch(*s)
    {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if((unsigned char)*s < 0x20)
        {
          char buf[8];
          snprintf(buf, sizeof(buf), "\\u%04x", (unsigned)(unsigned char)*s);
          out += buf;
        }
        else
        {
          out += *s;
        }
    }
  }
  return out;
}

static std::string JsonEscape(const std::string &s)
{
  return JsonEscape(s.c_str());
}

static std::string JsonEscape(const rdcstr &s)
{
  return JsonEscape(s.c_str());
}

static void Indent()
{
  if(g_json)
    for(int i = 0; i < g_indent; i++)
      fputs("  ", stdout);
}

//: One `key: value` line in text mode, `"key": "value",` in JSON mode.
//:
//: The value is escaped: it routinely carries a Windows path (`capture`), an engine-supplied name
//: or a debug message, and a single unescaped backslash in any of them makes the whole document
//: unparseable -- which is exactly what `--json` did before this.
static void Field(const char *key, const std::string &value, bool last = false)
{
  Indent();
  if(g_json)
    printf("\"%s\": \"%s\"%s\n", key, JsonEscape(value).c_str(), last ? "" : ",");
  else
    printf("%-18s %s\n", key, value.c_str());
}

static void Field(const char *key, const rdcstr &value, bool last = false)
{
  Field(key, std::string(value.c_str()), last);
}

static void Field(const char *key, long long value, bool last = false)
{
  Indent();
  if(g_json)
    printf("\"%s\": %lld%s\n", key, value, last ? "" : ",");
  else
    printf("%-18s %lld\n", key, value);
}

//: `g_firstRow` is the "this item needs no separator" state of the array *currently* being written.
//: Arrays nest -- a stage object holds arrays of its own -- so the state nests too: `ArrayOpen` saves the
//: enclosing array's flag and `ArrayClose` restores it. Without this, writing an *empty* nested array left
//: the enclosing array looking like it had just started, the next item was written with no comma, and the
//: document did not parse: `states/<eid>.shaders.json` was invalid for the hobby capture for exactly that
//: reason (a stage whose signature arrays were empty), and `resources.json` would have hit it on any
//: resource with an empty usage list.
static std::vector<bool> g_firstRowStack;

static void ArrayOpen(const char *key)
{
  Indent();
  if(g_json)
    printf("\"%s\": [\n", key);
  g_firstRowStack.push_back(g_firstRow);
  g_indent++;
  g_firstRow = true;
}

static void ArrayClose(bool last = true)
{
  g_indent--;
  Indent();
  if(g_json)
    printf("]%s\n", last ? "" : ",");
  if(!g_firstRowStack.empty())
  {
    g_firstRow = g_firstRowStack.back();
    g_firstRowStack.pop_back();
  }
}

//: The separator in front of the next item of the current array, and the bookkeeping for the one
//: after it. Writing it *before* an item is what makes a trailing comma impossible: there is no
//: point at which the writer knows an item is last, and a comma after the last one is not JSON.
static const char *TakeSeparator()
{
  const char *sep = g_firstRow ? "" : ",\n";
  g_firstRow = false;
  return sep;
}

static void Row(const std::string &text)
{
  if(g_json)
  {
    Indent();
    printf("%s\"%s\"\n", TakeSeparator(), JsonEscape(text.c_str()).c_str());
  }
  else
  {
    printf("%s\n", text.c_str());
  }
}

//: An item of the enclosing array that is an object rather than a string -- `draws` writes one row
//: per event. Sharing `TakeSeparator` with `Row` is what keeps that row from carrying a trailing
//: comma, which it used to do for every event including the last.
static void ObjectRow(const std::string &object)
{
  if(g_json)
  {
    Indent();
    printf("%s%s\n", TakeSeparator(), object.c_str());
  }
  else
  {
    printf("%s\n", object.c_str());
  }
}

//: The `{` of an object that is an item of the enclosing array, for an object whose members are
//: written by the calls that follow rather than assembled into one string first. The separator goes
//: in front of it exactly as for `Row`/`ObjectRow`, so an object item never carries a trailing comma
//: either, and the members inside indentation one level deeper than the brace.
static void ObjectOpen()
{
  if(IsJson())
  {
    Indent();
    fputs(TakeSeparator(), stdout);
    fputs("{\n", stdout);
    g_indent++;
  }
}

//: The matching `}`. Nothing follows it: whether the *enclosing* array has more items is the next
//: item's separator to write, and whether the array is the last member is its `ArrayClose` to say.
static void ObjectClose()
{
  if(IsJson())
  {
    g_indent--;
    Indent();
    fputs("}\n", stdout);
  }
}

//: printf-style formatting for the output lines. The SAL annotation makes the compiler check every
//: call site's arguments against the format string, which is the only way a varargs helper like
//: this stays honest -- a mismatch is undefined behaviour ([expr.call]: the argument must match the
//: parameter after the default argument promotions), and it is also how the tool would print
//: nonsense. The buffer grows to fit instead of truncating at a fixed size, because a truncated
//: JSON row is not valid JSON and a truncated text row is not the data the reader asked for.
static std::string FmtV(_Printf_format_string_ const char *fmt, va_list args)
{
  va_list counted;
  va_copy(counted, args);
  const int needed = vsnprintf(NULL, 0, fmt, counted);
  va_end(counted);

  if(needed <= 0)
    return std::string();

  std::vector<char> buf((size_t)needed + 1);
  vsnprintf(buf.data(), buf.size(), fmt, args);
  return std::string(buf.data(), (size_t)needed);
}

static std::string Fmt(_Printf_format_string_ const char *fmt, ...)
{
  va_list args;
  va_start(args, fmt);
  std::string text = FmtV(fmt, args);
  va_end(args);
  return text;
}

static const char *StageName(ShaderStage stage)
{
  switch(stage)
  {
    case ShaderStage::Vertex: return "vs";
    case ShaderStage::Hull: return "hs";
    case ShaderStage::Domain: return "ds";
    case ShaderStage::Geometry: return "gs";
    case ShaderStage::Pixel: return "ps";
    case ShaderStage::Compute: return "cs";
    case ShaderStage::Amplification: return "as";
    case ShaderStage::Mesh: return "ms";
    default: return "?";
  }
}

//: The bound shader of one stage, out of the D3D12 pipeline state. The matching `PipeState`
//: accessors exist but are not exported from the DLL, so the state struct is the way in -- and the
//: reflection is then fetched through the virtual controller API.
static const D3D12Pipe::Shader *StageShader(const D3D12Pipe::State *d3d12, ShaderStage stage)
{
  if(d3d12 == NULL)
    return NULL;
  switch(stage)
  {
    case ShaderStage::Vertex: return &d3d12->vertexShader;
    case ShaderStage::Hull: return &d3d12->hullShader;
    case ShaderStage::Domain: return &d3d12->domainShader;
    case ShaderStage::Geometry: return &d3d12->geometryShader;
    case ShaderStage::Pixel: return &d3d12->pixelShader;
    case ShaderStage::Compute: return &d3d12->computeShader;
    case ShaderStage::Amplification: return &d3d12->ampShader;
    case ShaderStage::Mesh: return &d3d12->meshShader;
    default: return NULL;
  }
}

static ShaderStage StageFromName(const char *name)
{
  // The stages this tool reports and their command-line spellings, in one place, so `StageName` and
  // the command line cannot drift apart (the order is also the order the commands print them in).
  static const struct
  {
    const char *name;
    ShaderStage stage;
  } kStages[] = {
      {"vs", ShaderStage::Vertex},        {"hs", ShaderStage::Hull},
      {"ds", ShaderStage::Domain},        {"gs", ShaderStage::Geometry},
      {"ps", ShaderStage::Pixel},         {"cs", ShaderStage::Compute},
      {"as", ShaderStage::Amplification}, {"ms", ShaderStage::Mesh},
  };

  for(size_t i = 0; i < sizeof(kStages) / sizeof(kStages[0]); i++)
    if(strcmp(name, kStages[i].name) == 0)
      return kStages[i].stage;

  // `ShaderStage::Invalid` is the enum's own sentinel (`Invalid = Count`), so "no such stage" has a
  // defined spelling rather than an out-of-range value.
  return ShaderStage::Invalid;
}

//: The stages the commands report, in reporting order. Enumerating the enum instead would also walk
//: the eight ray-tracing stages, which have no D3D12 pipeline state and can never be bound here.
static const ShaderStage kReportedStages[] = {
    ShaderStage::Vertex, ShaderStage::Hull,  ShaderStage::Domain, ShaderStage::Geometry,
    ShaderStage::Pixel,  ShaderStage::Compute, ShaderStage::Amplification, ShaderStage::Mesh,
};

// --------------------------------------------------------------------------- renderdoc.dll loading

typedef ICaptureFile *(RENDERDOC_CC *pOpenCaptureFile)();
typedef const char *(RENDERDOC_CC *pGetVersionString)();
typedef void(RENDERDOC_CC *pInitialiseReplay)(GlobalEnvironment env, const rdcarray<rdcstr> &args);
typedef void(RENDERDOC_CC *pShutdownReplay)();

static pGetVersionString g_GetVersionString = NULL;
static pShutdownReplay g_ShutdownReplay = NULL;

static HMODULE LoadReplayDLL()
{
  const char *env = getenv("RDC_RENDERDOC_DLL");
  std::string path = env && *env ? env : "C:\\Program Files\\RenderDoc\\renderdoc.dll";

  HMODULE dll = LoadLibraryA(path.c_str());
  if(dll == NULL)
  {
    fprintf(stderr, "cannot load %s (set RDC_RENDERDOC_DLL to the renderdoc.dll to use)\n", path.c_str());
    return NULL;
  }
  g_GetVersionString = (pGetVersionString)GetProcAddress(dll, "RENDERDOC_GetVersionString");
  return dll;
}

//: Set the replay system up before anything is opened, exactly as RenderDoc's own CLI does: this is
//: what reads the config, opens the log and marks the process as a replay host. `args` is the command
//: line (the engine logs it, and honours `--crash` to keep its own crash handler out of the way).
static bool InitialiseReplay(HMODULE dll, int argc, char **argv)
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

static ICaptureFile *OpenCaptureFile(HMODULE dll)
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
// out of `main`, including the early returns, which used to skip the shutdown entirely.
struct ReplaySystemGuard
{
  ~ReplaySystemGuard()
  {
    if(g_ShutdownReplay != NULL)
      g_ShutdownReplay();
  }
};

struct CaptureFileGuard
{
  explicit CaptureFileGuard(ICaptureFile *capture) : file(capture) {}
  ~CaptureFileGuard()
  {
    if(file != NULL)
      file->Shutdown();
  }
  CaptureFileGuard(const CaptureFileGuard &) = delete;
  CaptureFileGuard &operator=(const CaptureFileGuard &) = delete;

  ICaptureFile *file;
};

struct ControllerGuard
{
  explicit ControllerGuard(IReplayController *replay) : ctrl(replay) {}
  ~ControllerGuard()
  {
    if(ctrl != NULL)
      ctrl->Shutdown();
  }
  ControllerGuard(const ControllerGuard &) = delete;
  ControllerGuard &operator=(const ControllerGuard &) = delete;

  IReplayController *ctrl;
};

// --------------------------------------------------------------------------- action tree

//: One chunk of the structured file. `eid` is the depth-first index over *chunks* (parameters are
//: descended through without numbering), which is exactly the event id `SetFrameEvent` takes and the
//: UI shows -- and it is the same number the offline tool prints as its chunk index.
struct ActionRow
{
  int eid;
  int depth;
  rdcstr name;
  uint32_t chunkID;
};

//: Whether a chunk is an *event*. RenderDoc's event ids number what the command list recorded --
//: draws, dispatches, copies, markers -- while device-level calls (resource and PSO creation,
//: `SetName`, descriptor writes) are in the structured file but are not events. So the event id is
//: **not** the chunk index the offline tool prints: on one capture those agreed (its command-list
//: chunks start early), on another they were off by tens of thousands. `probe` shows which ids the
//: engine really has.
static bool IsAction(const rdcstr &name)
{
  return name.beginsWith("ID3D12GraphicsCommandList") || name.beginsWith("ID3D12CommandList") ||
         name.beginsWith("ID3D12VideoCommandList") || name.beginsWith("ID3D12VideoEncodeCommandList");
}

//: The structured file is a tree whose depth is the capture's to choose, so the walk is bounded: a
//: corrupt or crafted file must not be able to exhaust the stack. Real nesting is a handful of
//: levels (a command list inside a frame) and the cap is far above that.
static const int kMaxTreeDepth = 256;

//: Walks one object and everything below it, numbering every *structured-data object* -- chunks and
//: their parameters alike -- which is what makes the ids line up with the engine's (`probe` is how
//: that was established). `truncated` records whether the depth cap was ever reached, so a command
//: can say so rather than present a partial tree as the whole one.
static void Flatten(const SDObject *obj, int depth, int &next, std::vector<ActionRow> &rows,
                    bool &truncated)
{
  const int id = next++;
  if(obj->type.basetype == SDBasic::Chunk)
  {
    // The tag says this object is a chunk; the cast says which kind. `static_cast` rather than a
    // C-style cast, so only the derived-to-base relationship can be involved ([expr.cast]).
    const SDChunk *chunk = static_cast<const SDChunk *>(obj);
    ActionRow row;
    row.eid = IsAction(chunk->name) ? id : 0;
    row.depth = depth;
    row.name = chunk->name;
    row.chunkID = chunk->metadata.chunkID;
    rows.push_back(row);
    depth++;
  }

  if(depth >= kMaxTreeDepth)
  {
    truncated = true;
    return;
  }

  const size_t children = obj->NumChildren();
  for(size_t i = 0; i < children; i++)
    Flatten(obj->GetChild(i), depth, next, rows, truncated);
}

//: A draw, dispatch or copy: what a frame is *read* through, as opposed to the state and marker
//: chunks that also carry an event id.
static bool IsCall(const rdcstr &name)
{
  const char *n = strstr(name.c_str(), "::");
  n = (n != NULL) ? n + 2 : name.c_str();
  return strncmp(n, "Draw", 4) == 0 || strncmp(n, "Dispatch", 8) == 0 ||
         strncmp(n, "ExecuteIndirect", 15) == 0 || strncmp(n, "Copy", 4) == 0 ||
         strncmp(n, "Clear", 5) == 0 || strncmp(n, "Present", 7) == 0 ||
         strncmp(n, "ResolveSubresource", 18) == 0 || strncmp(n, "BeginRenderPass", 15) == 0;
}

static std::vector<ActionRow> Actions(IReplayController *ctrl, bool &truncated)
{
  const SDFile &sd = ctrl->GetStructuredFile();
  std::vector<ActionRow> rows;
  int next = 1;
  truncated = false;
  for(size_t i = 0; i < sd.chunks.size(); i++)
    Flatten(sd.chunks[i], 0, next, rows, truncated);
  return rows;
}

// --------------------------------------------------------------------------- value formatting

//: How deep a struct-of-structs is expanded before the rest is elided. The tree comes from the
//: shader, so bounding it bounds both the work and the stack, the same reasoning as kMaxTreeDepth.
static const int kMaxValueDepth = 16;

//: A shader variable as `name = value`, recursing into structs and arrays (a constant buffer is a
//: tree of these). Vector components are formatted to 6 significant digits, which is enough to read
//: a matrix by eye without drowning in noise.
static std::string FormatValue(const ShaderVariable &v, int depth)
{
  const ShaderValue &val = v.value;

  if(!v.members.empty())
  {
    if(depth >= kMaxValueDepth)
      return "{...}";
    std::string out = "{ ";
    for(size_t i = 0; i < v.members.size(); i++)
      out += (i ? ", " : "") + std::string(v.members[i].name.c_str()) + "=" +
             FormatValue(v.members[i], depth + 1);
    return out + " }";
  }

  const int count = v.rows * v.columns;
  std::string out;
  for(int i = 0; i < count && i < 16; i++)
  {
    if(i)
      out += (v.columns > 0 && i % v.columns == 0) ? " | " : ", ";
    switch(v.type)
    {
      case VarType::Float: out += Fmt("%g", val.f32v[i]); break;
      case VarType::Double: out += Fmt("%g", val.f64v[i]); break;
      case VarType::Half: out += Fmt("%g", (float)val.f16v[i]); break;
      case VarType::SInt: out += Fmt("%d", val.s32v[i]); break;
      case VarType::UInt: out += Fmt("%u", val.u32v[i]); break;
      case VarType::SShort: out += Fmt("%d", (int)val.s16v[i]); break;
      case VarType::UShort: out += Fmt("%u", (unsigned)val.u16v[i]); break;
      case VarType::SLong: out += Fmt("%lld", (long long)val.s64v[i]); break;
      case VarType::ULong: out += Fmt("%llu", (unsigned long long)val.u64v[i]); break;
      case VarType::SByte: out += Fmt("%d", (int)val.s8v[i]); break;
      case VarType::UByte:
      case VarType::Bool:
      case VarType::Enum: out += Fmt("%u", (unsigned)val.u8v[i]); break;
      default: out += "?";
    }
  }
  return out.empty() ? "-" : out;
}

static std::string FormatValue(const ShaderVariable &v)
{
  return FormatValue(v, 0);
}

static void PrintVariables(const rdcarray<ShaderVariable> &vars, int depth)
{
  for(size_t i = 0; i < vars.size(); i++)
  {
    const ShaderVariable &v = vars[i];
    const std::string pad((size_t)depth * 2, ' ');
    const std::string value = FormatValue(v);
    if(IsJson())
    {
      // One `name = value` per array element, with the nesting shown the way the text output shows
      // it. This used to emit `"name": "value",` *inside* an array -- object syntax in a list, with
      // a trailing comma, which no JSON parser accepts.
      Row(Fmt("%s%s = %s", pad.c_str(), v.name.c_str(), value.c_str()));
    }
    else
    {
      printf("  %s%-28s %s\n", pad.c_str(), v.name.c_str(), value.c_str());
    }
    if(!v.members.empty() && depth < kMaxValueDepth)
      PrintVariables(v.members, depth + 1);
  }
}

// --------------------------------------------------------------------------- commands

static void PrintCaptureHeader(ICaptureFile *file, const char *path)
{
  if(g_json)
    printf("{\n");
  g_indent = g_json ? 1 : 0;
  Field("schemaVersion", (long long)kSchemaVersion);   // every document says what shape it is
  Field("capture", std::string(path));
  Field("renderdoc", std::string(g_GetVersionString ? g_GetVersionString() : "?"));
  Field("driver", file->DriverName());
  Field("localReplay", (long long)file->LocalReplaySupport());
  Field("machine", file->RecordedMachineIdent());
}

static int CmdInfo(IReplayController *ctrl, ICaptureFile *file, const char *path)
{
  PrintCaptureHeader(file, path);
  APIProperties props = ctrl->GetAPIProperties();
  const SDFile &sd = ctrl->GetStructuredFile();

  Field("pipelineType", (long long)props.pipelineType);
  Field("localRenderer", (long long)props.localRenderer);
  Field("remoteReplay", (long long)props.remoteReplay);
  Field("vendor", (long long)props.vendor);
  Field("shaderDebugging", (long long)props.shaderDebugging);
  Field("chunks", (long long)sd.chunks.size());
  Field("resources", (long long)ctrl->GetResources().size());
  Field("textures", (long long)ctrl->GetTextures().size());
  Field("buffers", (long long)ctrl->GetBuffers().size());
  Field("debugMessages", (long long)ctrl->GetDebugMessages().size(), true);

  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

static int CmdDraws(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxRows,
                    const char *filter)
{
  PrintCaptureHeader(file, path);
  bool truncated = false;
  const std::vector<ActionRow> rows = Actions(ctrl, truncated);

  int shown = 0, events = 0;
  ArrayOpen("events");
  for(size_t i = 0; i < rows.size(); i++)
  {
    const ActionRow &r = rows[i];
    if(r.eid == 0)
      continue;                                    // a device-level chunk: not an event
    const bool call = IsCall(r.name);
    events++;
    if(filter != NULL && *filter != '\0' && strstr(r.name.c_str(), filter) == NULL)
      continue;
    if(!call && filter == NULL)
      continue;                                    // without a filter: calls and markers only
    if(maxRows > 0 && shown >= maxRows)
      continue;

    if(g_json)
    {
      ObjectRow(Fmt("{\"eid\": %d, \"depth\": %d, \"chunkID\": %u, \"name\": \"%s\"}", r.eid, r.depth,
                    r.chunkID, JsonEscape(r.name).c_str()));
    }
    else
    {
      const std::string pad((size_t)r.depth * 2, ' ');
      printf("%-7d %-5d %s%s\n", r.eid, r.depth, pad.c_str(), r.name.c_str());
    }
    shown++;
  }
  ArrayClose(false);                                 // totalChunks/totalEvents/shown follow
  g_indent = g_json ? 1 : 0;
  Field("totalChunks", (long long)rows.size());
  Field("totalEvents", events);
  Field("shown", shown, !truncated);
  if(truncated)
  {
    // Only reachable on a capture whose action tree is deeper than kMaxTreeDepth: say so rather
    // than presenting a partial tree as the whole one.
    Field("truncated", std::string("action tree deeper than the recursion limit"), true);
  }
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

//: The pipeline state at one event: which shaders are bound (with their entry points), the input
//: assembler, the outputs, and -- for D3D12 -- the root signature and every root parameter that is
//: set. This is the "what is bound, exactly" answer the offline tool can only approximate.
static int CmdState(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid)
{
  ctrl->SetFrameEvent(eid, true);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("api", (long long)ctrl->GetAPIProperties().pipelineType);

  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();

  ArrayOpen("shaders");
  for(size_t i = 0; i < sizeof(kReportedStages) / sizeof(kReportedStages[0]); i++)
  {
    const ShaderStage stage = kReportedStages[i];
    const D3D12Pipe::Shader *sh = StageShader(d3d12, stage);
    if(sh == NULL || sh->resourceId == ResourceId::Null())
      continue;
    Row(Fmt("%-3s res%-7s", StageName(stage), IdText(sh->resourceId).c_str()));
  }
  ArrayClose(false);                                 // renderTargets follows

  ArrayOpen("renderTargets");
  if(d3d12)
  {
    for(int i = 0; i < (int)d3d12->outputMerger.renderTargets.size(); i++)
    {
      if(d3d12->outputMerger.renderTargets[i].resource == ResourceId::Null())
        continue;
      Row(Fmt("slot %d  res%s", i,
              IdText(d3d12->outputMerger.renderTargets[i].resource).c_str()));
    }
  }
  ArrayClose(d3d12 == NULL);                         // depthTarget/rootSignature follow if there is state
  if(d3d12)
    Field("depthTarget", IdText(d3d12->outputMerger.depthTarget.resource));

  // The root parameters as *set* at this event: the register and space the signature declares, plus
  // whichever of the three kinds this parameter is -- a root descriptor (a resource), a table (a
  // heap and an offset into it) or root constants (words of data).
  if(d3d12)
  {
    Field("rootSignature", IdText(d3d12->rootSignature.resourceId));
    ArrayOpen("rootParameters");
    for(int i = 0; i < (int)d3d12->rootSignature.parameters.size(); i++)
    {
      const D3D12Pipe::RootParam &rp = d3d12->rootSignature.parameters[i];
      std::string detail;
      if(rp.descriptor.resource != ResourceId::Null())
        detail = Fmt("res%s", IdText(rp.descriptor.resource).c_str());
      else if(rp.heap != ResourceId::Null())
        detail = Fmt("heap%s+0x%x", IdText(rp.heap).c_str(), rp.heapByteOffset);
      else if(!rp.constants.empty())
        detail = Fmt("%d words", (int)rp.constants.size() / 4);
      Row(Fmt("rp%-3d reg=%u space=%u %s", i, rp.reg, rp.space, detail.c_str()));
    }
    ArrayClose();
  }

  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

//: The shader reflection: constant blocks with their names and bind points, the resource bindings,
//: the input/output signatures, and the disassembly. This is what names a root parameter, and what
//: the offline tool's removed `sig`/`dxbc` harvest was trying to guess at.
static int CmdShaders(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
                      bool wantDisasm)
{
  ctrl->SetFrameEvent(eid, true);
  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  if(d3d12 == NULL)
    return Fail(1, "no D3D12 pipeline state at eid %d", eid);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);

  // One object per bound stage, in a `stages` array. They cannot be members of one flat object:
  // two bound stages then repeat every key (`stage`, `resource`, `constantBlocks`, ...) and every
  // JSON reader keeps only the last value of a repeated key, so the vertex shader's whole reflection
  // was silently dropped on any draw that had one. The array also makes the empty case (no stage
  // bound -- a copy, a marker, or an event with no pipeline state) a valid document, where a flat
  // object used to end on `eid`'s separator with nothing after it.
  ArrayOpen("stages");
  for(size_t i = 0; i < sizeof(kReportedStages) / sizeof(kReportedStages[0]); i++)
  {
    const ShaderStage stage = kReportedStages[i];
    const D3D12Pipe::Shader *stageState = StageShader(d3d12, stage);
    const ResourceId shader = stageState ? stageState->resourceId : ResourceId::Null();
    if(shader == ResourceId::Null())
      continue;

    // An empty entry-point name means "the default one"; `ShaderEntryPoint` carries the stage too.
    const ShaderReflection *refl =
        ctrl->GetShader(d3d12->pipelineResourceId, shader, ShaderEntryPoint(rdcstr(), stage));

    ObjectOpen();

    if(refl == NULL)
    {
      if(IsJson())
      {
        Field(Fmt("%s (no reflection)", StageName(stage)).c_str(), IdText(shader));
      }
      else
      {
        printf("  %-3s res%-7s (no reflection)\n", StageName(stage), IdText(shader).c_str());
      }
      continue;
    }

    Field("stage", std::string(StageName(stage)));
    Field("resource", IdText(shader));
    Field("entry", refl->entryPoint);
    Field("encoding", (long long)refl->encoding);
    Field("bytes", (long long)refl->rawBytes.size());

    ArrayOpen("constantBlocks");
    for(size_t b = 0; b < refl->constantBlocks.size(); b++)
    {
      const ConstantBlock &cb = refl->constantBlocks[b];
      Row(Fmt("cbuffer[%d] %-28s b%d s%d %d bytes, %d variables", (int)b, cb.name.c_str(),
              cb.fixedBindNumber, cb.fixedBindSetOrSpace, (int)cb.byteSize,
              (int)cb.variables.size()));
    }
    ArrayClose(false);                               // more members of this stage follow

    ArrayOpen("readOnlyResources");
    for(size_t r = 0; r < refl->readOnlyResources.size(); r++)
      Row(Fmt("%s t%d s%d n%d", refl->readOnlyResources[r].name.c_str(),
              refl->readOnlyResources[r].fixedBindNumber,
              refl->readOnlyResources[r].fixedBindSetOrSpace,
              (int)refl->readOnlyResources[r].bindArraySize));
    ArrayClose(false);                               // more members of this stage follow

    ArrayOpen("readWriteResources");
    for(size_t r = 0; r < refl->readWriteResources.size(); r++)
      Row(Fmt("%s u%d s%d n%d", refl->readWriteResources[r].name.c_str(),
              refl->readWriteResources[r].fixedBindNumber,
              refl->readWriteResources[r].fixedBindSetOrSpace,
              (int)refl->readWriteResources[r].bindArraySize));
    ArrayClose(false);                               // more members of this stage follow

    ArrayOpen("inputSignature");
    for(size_t s = 0; s < refl->inputSignature.size(); s++)
      Row(Fmt("%s%d reg%d", refl->inputSignature[s].semanticName.c_str(),
              refl->inputSignature[s].semanticIndex, refl->inputSignature[s].regIndex));
    ArrayClose(false);                               // outputSignature follows

    ArrayOpen("outputSignature");
    for(size_t s = 0; s < refl->outputSignature.size(); s++)
      Row(Fmt("%s%d reg%d", refl->outputSignature[s].semanticName.c_str(),
              refl->outputSignature[s].semanticIndex, refl->outputSignature[s].regIndex));
    ArrayClose(!wantDisasm);

    if(wantDisasm)
    {
      // The disassembly is not part of the reflection: the controller generates it on request, per
      // target, and an empty target means "the native one".
      rdcstr asmText = ctrl->DisassembleShader(d3d12->pipelineResourceId, refl, rdcstr());
      ArrayOpen("disassembly");
      // one entry per line so JSON consumers can diff it
      const std::string text(asmText.c_str());
      size_t start = 0;
      while(start <= text.size())
      {
        const size_t nl = text.find('\n', start);
        Row(text.substr(start, nl == std::string::npos ? std::string::npos : nl - start));
        if(nl == std::string::npos)
          break;
        start = nl + 1;
      }
      ArrayClose();
    }

    ObjectClose();                                   // this stage's object
  }
  ArrayClose(true);                                  // `stages` is the object's last member

  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

//: The named values of one constant buffer at one event: the answer to "what is actually in the
//: buffer bound at rp7", which the offline tool cannot give at all.
static int CmdCbuffer(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
                      ShaderStage stage, int slot)
{
  ctrl->SetFrameEvent(eid, true);
  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  const D3D12Pipe::Shader *sh = StageShader(d3d12, stage);
  if(sh == NULL || sh->resourceId == ResourceId::Null())
    return Fail(1, "no %s shader is bound at eid %d", StageName(stage), eid);

  // The pipeline object is what the reflection is looked up through, and it is the bound PSO -- the
  // same one whether this is a draw or a dispatch. The entry point comes from the reflection, which
  // is where the real name lives (the D3D12 state does not carry one).
  const ShaderReflection *refl =
      ctrl->GetShader(d3d12->pipelineResourceId, sh->resourceId, ShaderEntryPoint(rdcstr(), stage));
  rdcstr entry = refl ? refl->entryPoint : rdcstr();

  // Which buffer to read: the engine wants it explicitly, and the slot is a register, so the bound
  // root-descriptor CBV with that register is the answer. A CBV inside a descriptor table (what a
  // bindless design uses) has no resource in the state, and then the read comes back zeroed -- which
  // is why the resolved buffer is printed.
  ResourceId buffer = ResourceId::Null();
  uint64_t bufferOffset = 0;
  for(size_t i = 0; i < d3d12->rootSignature.parameters.size(); i++)
  {
    const D3D12Pipe::RootParam &rp = d3d12->rootSignature.parameters[i];
    if(rp.reg == (uint32_t)slot && rp.descriptor.resource != ResourceId::Null())
    {
      buffer = rp.descriptor.resource;
      bufferOffset = rp.descriptor.byteOffset;
      break;
    }
  }

  rdcarray<ShaderVariable> vars = ctrl->GetCBufferVariableContents(
      d3d12->pipelineResourceId, sh->resourceId, stage, entry, (uint32_t)slot, buffer, bufferOffset, 0);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("stage", std::string(StageName(stage)));
  Field("slot", (long long)slot);
  Field("shader", IdText(sh->resourceId));
  Field("buffer", buffer == ResourceId::Null()
                      ? std::string("(none bound as a root descriptor: values will be zero)")
                      : Fmt("res%s+0x%llx", IdText(buffer).c_str(),
                            (unsigned long long)bufferOffset));

  ArrayOpen("variables");
  PrintVariables(vars, 0);
  ArrayClose();
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

static int CmdTextures(IReplayController *ctrl, ICaptureFile *file, const char *path,
                       const char *filter, const char *saveDir)
{
  PrintCaptureHeader(file, path);
  const rdcarray<TextureDescription> &texs = ctrl->GetTextures();

  int shown = 0;
  ArrayOpen("textures");
  for(size_t i = 0; i < texs.size(); i++)
  {
    const TextureDescription &t = texs[i];
    std::string id = IdText(t.resourceId);
    if(filter && *filter && strstr(id.c_str(), filter) == NULL)
      continue;
    if(IsJson())
    {
      // An element of the array, so it goes through `ObjectRow`: writing it out by hand meant a
      // hardcoded trailing comma, and therefore a document no parser would read.
      ObjectRow(Fmt("{\"resource\": \"%s\", \"dimension\": %d, \"width\": %u, \"height\": %u,"
                    " \"depth\": %u, \"mips\": %u, \"arraySize\": %u, \"samples\": %u,"
                    " \"format\": \"%s\", \"bytes\": %llu}",
                    id.c_str(), (int)t.dimension, t.width, t.height, t.depth, t.mips, t.arraysize,
                    t.msSamp, JsonEscape(t.format.Name().c_str()).c_str(),
                    (unsigned long long)t.byteSize));
    }
    else
    {
      // The dimension is printed as its `TextureDimension` value; the format name comes from the
      // DLL's own `RENDERDOC_ResourceFormatName`.
      printf("res%-7s dim=%d %ux%ux%u mips=%u arr=%u smp=%u fmt=%s %.2f MB\n", id.c_str(),
             (int)t.dimension, t.width, t.height, t.depth, t.mips, t.arraysize, t.msSamp,
             t.format.Name().c_str(), (double)t.byteSize / 1048576.0);
    }
    shown++;

    if(saveDir && *saveDir)
    {
      TextureSave save;
      save.resourceId = t.resourceId;
      save.destType = FileType::PNG;
      const std::string out = Fmt("%s\\tex_%s.png", saveDir, id.c_str());
      const ResultDetails res = ctrl->SaveTexture(save, rdcstr(out.c_str()));
      if(!res.OK())
      {
        fprintf(stderr, "  warning: could not save res%s: %s\n", id.c_str(),
                ResultText(res).c_str());
      }
      else if(IsJson())
      {
        // Progress belongs on stderr in JSON mode: a bare line inside the object would make the
        // document unparseable, which is what used to happen here.
        fprintf(stderr, "  -> %s\n", out.c_str());
      }
      else
      {
        printf("  -> %s\n", out.c_str());
      }
    }
  }
  ArrayClose(false);                                 // total/shown follow
  g_indent = g_json ? 1 : 0;
  Field("total", (long long)texs.size());
  Field("shown", shown, true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

//: Post-VS geometry for one instance: what the vertex shader actually emitted, per vertex. This is
//: the "which instance SH reached the pixel shader" question the offline tool cannot answer.
static int CmdMesh(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
                   int instance, int maxRows)
{
  ctrl->SetFrameEvent(eid, true);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("instance", (long long)instance);

  const MeshFormat mesh = ctrl->GetPostVSData((uint32_t)instance, 0, MeshDataStage::VSOut);
  const bool hasData = mesh.vertexResourceId != ResourceId::Null() && mesh.vertexByteStride != 0;

  Field("topology", (long long)mesh.topology);
  Field("vertexResource", IdText(mesh.vertexResourceId));
  Field("vertexStride", (long long)mesh.vertexByteStride);
  Field("vertexBytes", (long long)mesh.vertexByteSize);
  Field("indexResource", IdText(mesh.indexResourceId));
  Field("indexBytes", (long long)mesh.indexByteSize);
  // Whether this is the last member of the object depends on whether the stream follows, and the
  // separator has to agree with that: `last` is the one thing the writer cannot work out alone.
  Field("baseVertex", (long long)mesh.baseVertex, !hasData);

  if(!hasData)
  {
    if(IsJson())
      printf("}\n");
    else
      printf("(no post-VS data for this event)\n");
    return 0;
  }

  // The data is the post-VS stream, so it is printed as the floats it is: `stride / 4` per vertex.
  // Which float is which attribute is the shader reflection's business (`shaders <eid>`), not
  // something this buffer can say.
  //
  // A binary32 has no trap representations, so every bit pattern that comes back is a value that can
  // be printed; the assert keeps that assumption attached to the code that relies on it.
  static_assert(std::numeric_limits<float>::is_iec559, "the vertex stream is read as IEEE-754 binary32");
  static_assert(sizeof(float) == 4, "a vertex component is four bytes");

  const bytebuf data = ctrl->GetBufferData(mesh.vertexResourceId, mesh.vertexByteOffset,
                                           mesh.vertexByteSize);
  const size_t stride = mesh.vertexByteStride;      // non-zero: checked above (no division by zero)
  const size_t count = (stride != 0) ? data.size() / stride : 0;
  const size_t comps = stride / sizeof(float);

  ArrayOpen("vertices");
  for(size_t v = 0; v < count && (maxRows <= 0 || (long long)v < maxRows); v++)
  {
    std::string line = Fmt("[%llu]", (unsigned long long)v);
    for(size_t c = 0; c < comps; c++)
    {
      // The offset is computed in `size_t` and checked before it is used: `v * stride` in 32 bits
      // could wrap, and a wrapped offset would read outside the buffer ([expr.add]).
      const size_t offset = v * stride + c * sizeof(float);
      if(offset + sizeof(float) > data.size())
        break;
      float f = 0.0f;
      memcpy(&f, data.data() + offset, sizeof(float));
      line += Fmt(" %g", (double)f);
    }
    Row(line);
  }
  ArrayClose(false);                                 // vertexCount/componentsPerVertex follow
  g_indent = g_json ? 1 : 0;
  Field("vertexCount", (long long)count);
  Field("componentsPerVertex", (long long)comps, true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

//: Little-endian field writers: BMP's byte order is fixed by the file format, not by the machine
//: that happens to be writing it, so the bytes are written one at a time rather than by copying a
//: host-order integer into the header.
static void PutLE16(uint8_t *dst, uint16_t value)
{
  dst[0] = (uint8_t)(value & 0xffu);
  dst[1] = (uint8_t)((value >> 8) & 0xffu);
}

static void PutLE32(uint8_t *dst, uint32_t value)
{
  dst[0] = (uint8_t)(value & 0xffu);
  dst[1] = (uint8_t)((value >> 8) & 0xffu);
  dst[2] = (uint8_t)((value >> 16) & 0xffu);
  dst[3] = (uint8_t)((value >> 24) & 0xffu);
}

//: A 24-bit BMP of the texture display at one event. BMP rather than PNG because it needs no
//: encoder: the pixels come back as RGBA and the header is 54 bytes.
//:
//: The size arithmetic is done in `size_t` and checked before it is used, because the width and
//: height come from the engine as `int32_t`: `width * height * 4` in `int` can overflow
//: ([expr.mul], [ub:expr.mul.representable.type.result]), and the wrapped value is exactly what a
//: bounds check would then be trusting. Every write is checked too -- a short write leaves a
//: truncated image that otherwise looks like success.
static bool WriteBMP(const char *path, const bytebuf &rgba, int32_t width, int32_t height)
{
  if(width <= 0 || height <= 0)
    return false;

  const size_t w = (size_t)width;
  const size_t h = (size_t)height;

  // The buffer has to hold w*h pixels of 4 bytes; the division detects a wrapped product.
  const size_t needed = w * h * 4;
  if(needed / 4 / h != w || rgba.size() < needed)
    return false;

  const size_t rowBytes = w * 3;
  const size_t pad = (4 - (rowBytes % 4)) % 4;
  const size_t imageSize = (rowBytes + pad) * h;
  if(imageSize > 0xffffffffu - 54u)
    return false;                                    // the header's size fields are 32-bit

  uint8_t header[54] = {};
  header[0] = 'B';
  header[1] = 'M';
  PutLE32(header + 2, (uint32_t)(54u + imageSize));
  PutLE32(header + 10, 54u);
  PutLE32(header + 14, 40u);
  PutLE32(header + 18, (uint32_t)w);
  PutLE32(header + 22, (uint32_t)h);
  PutLE16(header + 26, 1u);
  PutLE16(header + 28, 24u);
  PutLE32(header + 34, (uint32_t)imageSize);

  FILE *f = fopen(path, "wb");
  if(f == NULL)
    return false;

  bool ok = fwrite(header, 1, sizeof(header), f) == sizeof(header);
  std::vector<uint8_t> row(rowBytes + pad, 0);
  for(size_t line = 0; line < h && ok; line++)
  {
    const size_t y = h - 1 - line;                   // BMP rows are bottom-up
    const uint8_t *px = rgba.data() + y * w * 4;
    for(size_t x = 0; x < w; x++)
    {
      row[x * 3 + 0] = px[x * 4 + 2];
      row[x * 3 + 1] = px[x * 4 + 1];
      row[x * 3 + 2] = px[x * 4 + 0];
    }
    ok = fwrite(row.data(), 1, row.size(), f) == row.size();
  }
  return (fclose(f) == 0) && ok;
}

//: Defined with the bundle (§2), used here too: `image` and the bundle's `rt/` images save a target
//: through the same code so the two cannot drift apart.
static bool SaveTargetImage(IReplayController *ctrl, ResourceId target, const char *outBase,
                            std::string &written, int32_t &width, int32_t &height);

static int CmdImage(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
                    const char *outPath)
{
  ctrl->SetFrameEvent(eid, true);

  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  ResourceId rt = d3d12 && !d3d12->outputMerger.renderTargets.empty()
                      ? d3d12->outputMerger.renderTargets[0].resource
                      : ResourceId::Null();
  if(rt == ResourceId::Null())
    return Fail(1, "nothing is bound to render target 0 at eid %d", eid);

  int32_t width = 0, height = 0;
  std::string used;
  const bool ok = SaveTargetImage(ctrl, rt, outPath, used, width, height);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("resource", IdText(rt));
  Field("width", (long long)width);
  Field("height", (long long)height);
  Field("file", used);
  Field("written", ok ? 1 : 0, true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return ok ? 0 : 1;
}

static int CmdCounters(IReplayController *ctrl, ICaptureFile *file, const char *path)
{
  PrintCaptureHeader(file, path);
  rdcarray<CounterResult> results = ctrl->FetchCounters(rdcarray<GPUCounter>());

  // RenderDoc's own counter names live in its (unexported) stringise.cpp, so the enum value is what
  // gets printed here; `GPUCounter`'s numbering is in the API headers.
  ArrayOpen("counters");
  for(size_t i = 0; i < results.size(); i++)
    Row(Fmt("eid %-7u %-18s = %f", (unsigned)results[i].eventId,
            CounterText(results[i].counter).c_str(), results[i].value.d));
  ArrayClose(false);                                 // total follows
  g_indent = g_json ? 1 : 0;
  Field("total", (long long)results.size(), true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

static int CmdDebug(IReplayController *ctrl, ICaptureFile *file, const char *path)
{
  PrintCaptureHeader(file, path);
  rdcarray<DebugMessage> msgs = ctrl->GetDebugMessages();

  ArrayOpen("messages");
  for(size_t i = 0; i < msgs.size(); i++)
    Row(Fmt("eid %-6u %-8s %s", (unsigned)msgs[i].eventId, SeverityText(msgs[i].severity).c_str(),
            msgs[i].description.c_str()));
  ArrayClose(false);                                 // total follows
  g_indent = g_json ? 1 : 0;
  Field("total", (long long)msgs.size(), true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

//: Which events touch a resource: the way to answer "where does this buffer come from". The argument
//: is an id (as `textures` prints it) or a resource name -- `ResourceId` cannot be constructed from a
//: number outside the DLL, so the match is made against what the engine itself reports.
static int CmdUsage(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *what)
{
  PrintCaptureHeader(file, path);
  Field("resource", std::string(what));

  ResourceId id = ResourceId::Null();
  const rdcarray<ResourceDescription> &res = ctrl->GetResources();
  for(size_t i = 0; i < res.size(); i++)
  {
    if(IdText(res[i].resourceId) == what || (!res[i].name.empty() && res[i].name == what))
    {
      id = res[i].resourceId;
      Field("matched", res[i].name.empty() ? IdText(id) : std::string(res[i].name.c_str()));
      break;
    }
  }
  if(id == ResourceId::Null())
    return Fail(1, "no resource with id or name '%s'", what);

  rdcarray<EventUsage> usage = ctrl->GetUsage(id);

  ArrayOpen("usage");
  for(size_t i = 0; i < usage.size(); i++)
    Row(Fmt("eid %-7u %s", (unsigned)usage[i].eventId, UsageText(usage[i].usage).c_str()));
  ArrayClose(false);                                 // total follows
  g_indent = g_json ? 1 : 0;
  Field("total", (long long)usage.size(), true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

//: Scan a range of event ids and report the ones that actually have pipeline state. This exists
//: because the event numbering is *not* guaranteed to be the chunk index the offline tool prints: it
//: matched exactly on one capture and did not on another, and `SetFrameEvent` accepts any number
//: (forcing an event that does not exist) rather than failing, so the only reliable answer is to ask
//: the engine which ids are real.
//:
//: It must be the *first* thing the process asks. `SetFrameEvent(n, true)` on an id that is not an
//: event does not clear the pipeline state: it leaves the last replayed event's state in place, so
//: after any other command a forced non-event looks like it has state. Measured on the Android
//: capture: `probe 120` alone reports 25-32 ids, and the same `probe 120` after eight other commands
//: reports ~120. The first answer is the true one; a batch file should therefore put `probe` first.
static int CmdProbe(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxEid)
{
  PrintCaptureHeader(file, path);
  int found = 0;
  ArrayOpen("events");
  for(int eid = 1; eid <= maxEid; eid++)
  {
    ctrl->SetFrameEvent(eid, true);
    const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
    if(d3d12 == NULL)
      continue;

    int shaders = 0;
    for(int i = 0; i < (int)ShaderStage::Count; i++)
    {
      const D3D12Pipe::Shader *sh = StageShader(d3d12, (ShaderStage)i);
      if(sh != NULL && sh->resourceId != ResourceId::Null())
        shaders++;
    }
    if(shaders == 0 && d3d12->rootSignature.resourceId == ResourceId::Null())
      continue;

    Row(Fmt("eid %-7d shaders=%d rootSig=%s params=%d", eid, shaders,
            IdText(d3d12->rootSignature.resourceId).c_str(),
            (int)d3d12->rootSignature.parameters.size()));
    found++;
  }
  ArrayClose(false);                                 // scanned/withState follow
  g_indent = g_json ? 1 : 0;
  Field("scanned", (long long)maxEid);
  Field("withState", found, true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

// --------------------------------------------------------------------------- the bundle (ROADMAP §2)

//: Runs a block with stdout pointing at a file, so a command written to print to the terminal writes a
//: file instead. A file-descriptor swap rather than a `FILE *` threaded through the writers, because the
//: helpers (`Field`, `Row`, `ArrayOpen`, ...) and the commands that use them print in a dozen places
//: each: one missed call site would silently corrupt a document, and the descriptor is the only version
//: of this that cannot miss one.
//:
//: `stdout` is unbuffered (setvbuf in `main`), so nothing has to be flushed before the swap, and the
//: guard restores the descriptor on every path out of the scope, early returns included.
class CaptureStdout
{
public:
  explicit CaptureStdout(const char *path)
  {
    const int fd = _fileno(stdout);
    m_saved = _dup(fd);
    m_file = fopen(path, "wb");
    if(m_file != NULL && m_saved >= 0)
      _dup2(_fileno(m_file), fd);
  }
  ~CaptureStdout()
  {
    fflush(stdout);
    if(m_saved >= 0)
      _dup2(m_saved, _fileno(stdout));
    if(m_file != NULL)
      fclose(m_file);
    if(m_saved >= 0)
      _close(m_saved);
  }
  CaptureStdout(const CaptureStdout &) = delete;
  CaptureStdout &operator=(const CaptureStdout &) = delete;
  bool Ok() const { return m_file != NULL && m_saved >= 0; }

private:
  FILE *m_file = NULL;
  int m_saved = -1;
};

//: The bundle's documents are JSON whatever the terminal was asked for: they are read by the offline
//: tool, not by a person, so `dump` forces the JSON writer on for the duration of one document.
class JsonDocument
{
public:
  JsonDocument() : m_saved(g_json) { g_json = true; }
  ~JsonDocument() { g_json = m_saved; }
  JsonDocument(const JsonDocument &) = delete;
  JsonDocument &operator=(const JsonDocument &) = delete;

private:
  bool m_saved;
};

//: SHA-256 of a file, through the OS (`bcrypt`), so the manifest's hashes are not a second implementation
//: of a digest to get wrong. An empty result means it could not be read, and the reason is on stderr; a
//: bundle whose hashes are missing is a bundle nobody can check.
static std::string Sha256File(const char *path)
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
  if(BCryptGetProperty(alg, BCRYPT_OBJECT_LENGTH, (PUCHAR)&objectBytes, sizeof(objectBytes), &ignored, 0) < 0)
    objectBytes = 0;
  std::vector<unsigned char> object(objectBytes);
  unsigned char digest[32];

  FILE *f = fopen(path, "rb");
  bool ok = false;
  if(f != NULL)
  {
    ok = BCryptCreateHash(alg, &hash, object.empty() ? NULL : object.data(), (ULONG)object.size(), NULL,
                          0, 0) >= 0;
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

static bool ReadWholeFile(const char *path, std::string &text)
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

static bool FileBytes(const char *path, unsigned long long &bytes)
{
  WIN32_FILE_ATTRIBUTE_DATA info;
  if(GetFileAttributesExA(path, GetFileExInfoStandard, &info) == 0)
    return false;
  bytes = ((unsigned long long)info.nFileSizeHigh << 32) | (unsigned long long)info.nFileSizeLow;
  return true;
}

static bool MakeDir(const std::string &path)
{
  if(CreateDirectoryA(path.c_str(), NULL) != 0)
    return true;
  return GetLastError() == ERROR_ALREADY_EXISTS;
}

static bool DirIsEmpty(const std::string &path, bool &empty)
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

//: A path inside the bundle, relative to its root and with forward slashes, so the manifest reads the same
//: whichever way the root was spelled.
static std::string BundleRelative(const std::string &root, const std::string &full)
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

//: FNV-1a over the state fields this driver read, so "did anything change between these two events" is
//: one string comparison. It is our hash of our own fields -- the engine exposes no state hash -- and the
//: manifest names exactly which fields go into it.
static std::string StateHash(const std::string &text)
{
  uint64_t h = 1469598103934665603ull;
  for(size_t i = 0; i < text.size(); i++)
  {
    h ^= (unsigned char)text[i];
    h *= 1099511628211ull;
  }
  return Fmt("%016llx", (unsigned long long)h);
}

//: Whether an id has anything bound. `probe` uses this exact test, and it has to be this one: the engine
//: exposes no action list, so "is this id an event" is answered by what is there when you ask -- and the
//: pipeline *object* id is not part of it. A forced non-event carries a leftover non-null
//: `pipelineResourceId` (measured: a sweep that tested it reported every id as an event and ran to the
//: guard), while the shaders and the root signature are the fields that are only there for a real one.
static bool HasBoundState(const D3D12Pipe::State *st)
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

//: Writes one render target the way the engine displays it: the display readback as a BMP, and when that
//: comes back empty, the engine's own encoder (a PNG beside the BMP's name). `written` receives whichever
//: path was actually used, because the manifest hashes files rather than intentions. Extracted from
//: `CmdImage` so `image` and the bundle save a target through one code path.
static bool SaveTargetImage(IReplayController *ctrl, ResourceId target, const char *outBase,
                            std::string &written, int32_t &width, int32_t &height)
{
  IReplayOutput *out = ctrl->CreateOutput(CreateHeadlessWindowingData(256, 256),
                                          ReplayOutputType::Texture);
  if(out == NULL)
  {
    fprintf(stderr, "warning: could not create a texture output for res%s\n", IdText(target).c_str());
    return false;
  }

  TextureDisplay disp;
  disp.resourceId = target;
  disp.typeCast = CompType::Typeless;
  disp.rangeMin = 0.0f;
  disp.rangeMax = 1.0f;
  out->SetTextureDisplay(disp);
  out->Display();

  const rdcpair<int32_t, int32_t> dims = out->GetDimensions();
  const bytebuf pixels = out->ReadbackOutputTexture();
  width = dims.first;
  height = dims.second;
  bool ok = WriteBMP(outBase, pixels, dims.first, dims.second);
  out->Shutdown();
  written = std::string(outBase);

  if(!ok)
  {
    TextureSave save;
    save.resourceId = target;
    save.destType = FileType::PNG;
    const std::string png = std::string(outBase) + ".png";
    const ResultDetails res = ctrl->SaveTexture(save, rdcstr(png.c_str()));
    if(res.OK())
    {
      written = png;
      ok = true;
    }
  }
  return ok;
}

//: The per-event documents: the state, the reflection, and one file per constant block of every bound
//: stage. Each is written through the command that already produces it (the descriptor swap), so a file
//: is exactly what that command prints -- there is no second writer to drift away from the first.
static int WriteEventDocuments(IReplayController *ctrl, ICaptureFile *file, const char *path,
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

//: Defined with the CLI helpers further down; `dump`'s options take integers, and `atoi` would turn a
//: typo into a plausible number (`--since` becoming 0) instead of saying that it is not a number.
static bool ParseInt(const char *text, int &value);
static int ToInt(const std::string &text, int fallback);

static void ParseEventList(const std::string &text, std::vector<int> &out)
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

//: The bundle producer (ROADMAP §2): one replay session, everything the engine alone can answer written
//: to disk, so the offline half can analyse a frame without a device. It is also the reason a crash is
//: survivable: files are written as they are produced, and the manifest lists what was written, so a
//: partial bundle says so.
static int CmdDump(IReplayController *ctrl, ICaptureFile *file, const char *path,
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

  std::vector<std::string> written;                  // bundle-relative paths, hashed into the manifest
  std::vector<std::pair<std::string, std::string>> skipped;   // what was not written, and why

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
  const int kEmptyRun = 256;                         // consecutive ids with nothing bound that end a sweep
  const int kHardCap = 200000;
  const size_t idBudget = ctrl->GetStructuredFile().chunks.size();
  const int until = opts.until > 0 ? opts.until : kHardCap;
  int scanned = 0, emptyRun = 0, lastEid = 0;
  const char *stopped = "the end of the scan range";
  std::vector<int> ids;

  Log("bundle: sweeping ids %d..%d for bound state, at most %d id(s) (the file's chunk count)", opts.since,
      until, (int)idBudget);
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

  // Formats and dimensions come from the resource list, not from the pipeline state: the state names a
  // target, the description says what it is.
  std::vector<std::pair<std::string, const TextureDescription *>> textures;
  for(size_t i = 0; i < ctrl->GetTextures().size(); i++)
    textures.push_back(std::make_pair(IdText(ctrl->GetTextures()[i].resourceId), &ctrl->GetTextures()[i]));

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
    Field("captureBytes", FileBytes(AbsolutePath(path).c_str(), captureBytes) ? (long long)captureBytes : 0);
    Field("absPath", AbsolutePath(path), true);      // the object's last member: a comma here is not JSON
    g_indent = 0;
    printf("}\n");
    written.push_back("capture.json");
  }
  Log("bundle: capture.json written");

  // ------------------------------------------------------------------ events.json + states/
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
    for(size_t index = 0; index < ids.size(); index++)
    {
      const int eid = ids[index];
      ctrl->SetFrameEvent(eid, true);
      const D3D12Pipe::State *st = ctrl->GetD3D12PipelineState();

      // Everything the state can be compared and hashed by, so the offline side does not have to guess
      // which fields matter.
      std::string shaderIds;
      bool compute = false;
      for(int i = 0; i < (int)ShaderStage::Count; i++)
      {
        const ShaderStage stage = (ShaderStage)i;
        const D3D12Pipe::Shader *sh = StageShader(st, stage);
        if(sh == NULL || sh->resourceId == ResourceId::Null())
          continue;
        shaderIds += Fmt("%s=%s ", StageName(stage), IdText(sh->resourceId).c_str());
        if(stage == ShaderStage::Compute)
          compute = true;
      }

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
          Fmt("pso=%s;shaders=%s;targets=%s;depth=%s;rs=%s;rp=%u", IdText(st->pipelineResourceId).c_str(),
              shaderIds.c_str(), targets.c_str(), depth.c_str(),
              IdText(st->rootSignature.resourceId).c_str(),
              (unsigned)st->rootSignature.parameters.size());
      const std::string stateHash = StateHash(key);

      ObjectRow(Fmt("{\"eid\": %d, \"pso\": \"%s\", \"psoKind\": \"%s\", \"shaders\": \"%s\","
                    " \"targets\": [%s], \"depth\": \"%s\", \"rootParameters\": %u, \"state\": \"%s\"}",
                    eid, IdText(st->pipelineResourceId).c_str(),
                    compute ? "compute" : "graphics", shaderIds.c_str(), targets.c_str(), depth.c_str(),
                    (unsigned)st->rootSignature.parameters.size(), stateHash.c_str()));
      eventsWritten++;

      // A state file per distinct state rather than per event: the documents are kilobytes each and most
      // events repeat the previous one's, but the *first* event and every change are exactly the ones a
      // reader wants. `--events` forces extra ids.
      bool force = false;
      for(size_t i = 0; i < opts.forceEvents.size(); i++)
      {
        if(opts.forceEvents[i] == eid)
          force = true;
      }
      if(previousKey.empty() || key != previousKey || force)
      {
        previousKey = key;
        const int rc = WriteEventDocuments(ctrl, file, path, st, eid, wantDisasm, opts.outDir, written);
        if(rc == 0)
          stateFiles++;
        else
          skipped.push_back(std::make_pair(Fmt("states/%d", eid), "could not be written"));

        // Images belong to the same events as the state files, and for the same reason: most events repeat
        // the previous picture. One per event put 715 images (354 MB) in a bundle whose whole point was to
        // be readable; at the state boundaries it is ~30 events' worth.
        if(opts.withImages)
        {
          for(size_t slot = 0; slot < st->outputMerger.renderTargets.size() && slot < 4; slot++)
          {
            const ResourceId rt = st->outputMerger.renderTargets[slot].resource;
            if(rt == ResourceId::Null())
              continue;
            // The engine's own encoder, not the display readback `image` uses: a PNG of the resource is
            // ~10x smaller than the BMP the display path writes (a 1920x1080 target is 6 MB as BMP), and
            // the bundle wants the target as it is, not as a viewer would tone-map it.
            const std::string png = Fmt("%s\\rt\\%d_%d.png", opts.outDir.c_str(), eid, (int)slot);
            TextureSave save;
            save.resourceId = rt;
            save.destType = FileType::PNG;
            const ResultDetails res = ctrl->SaveTexture(save, rdcstr(png.c_str()));
            unsigned long long bytes = 0;
            if(res.OK() && FileBytes(png.c_str(), bytes) && bytes > 0)
              written.push_back(BundleRelative(opts.outDir, png));
            else
              skipped.push_back(std::make_pair(BundleRelative(opts.outDir, png),
                                               res.OK() ? std::string("the engine wrote an empty file")
                                                        : ResultText(res)));
          }
        }
      }
    }

    ArrayClose(false);                               // the scan block and the totals below
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
  Log("bundle: events.json written (%d id(s) with bound state, %d state file group(s))", eventsWritten,
      (int)stateFiles);

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
      // device. The values are the engine's numeric `ResourceUsage`; the names for those values live in
      // RenderDoc's headers, which the offline tool already reads out of the source tree.
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
        skipped.push_back(std::make_pair(BundleRelative(opts.outDir, out),
                                         res.OK() ? std::string("the engine wrote an empty file")
                                                  : ResultText(res)));
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
    printf("{\n");                                   // this writer builds its own document
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
    Field("stateHashInputs", std::string("pso, shader ids, render targets, depth target, root signature"
                                         " id, root parameter count"));
    Field("statesRule", std::string("a state file for the first event with bound state and for every event"
                                    " whose state hash differs from the previous one, plus --events"));

    // By design, not by accident: what this bundle *cannot* contain, said here so a reader does not
    // conclude that the frame has no copies, no markers and no counts.
    ArrayOpen("notInThisBundle");
    ObjectRow(std::string("{\"what\": \"the kind of each call (draw/copy/clear/marker)\", \"why\": \"the"
                          " replay API exposes no action list (ROADMAP §3), so an id is only an id with"
                          " bound state\"}"));
    ObjectRow(std::string("{\"what\": \"per-event triangle and thread counts\", \"why\": \"the same: those"
                          " live in the captured call arguments, not in the pipeline state\"}"));
    ObjectRow(std::string("{\"what\": \"marker and pass names\", \"why\": \"the same: markers are actions,"
                          " and the action list is not exposed\"}"));
    ObjectRow(std::string("{\"what\": \"texture thumbnails\", \"why\": \"the engine decodes textures but"
                          " does not resize them; --textures writes full decodes\"}"));
    ObjectRow(std::string("{\"what\": \"an exact end to the id list\", \"why\": \"ids past the frame's"
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
      FileBytes(full.c_str(), bytes);                // a '/' in the path is accepted by the Win32 API
      const std::string hash = Sha256File(full.c_str());
      ObjectRow(Fmt("{\"path\": \"%s\", \"bytes\": %llu, \"sha256\": \"%s\"}", written[i].c_str(), bytes,
                    hash.c_str()));
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

//: `bundle-verify <dir>`: re-hashes what the manifest lists and reports anything missing, resized or
//: changed. No device, no DLL and no capture are involved, which is the point -- a bundle that arrives
//: from another machine can be checked before it is trusted.
//:
//: The manifest is JSON and this is not a JSON parser: the `files` entries are matched against the exact
//: shape this driver writes, and an entry that does not match stops the check rather than passing
//: silently. Full document validation belongs to the offline side (`python -m json.tool`, README §10).
static int CmdBundleVerify(const char *dir)
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
    const int fields = sscanf(
        text.c_str() + pos,
        "{\"path\": \"%511[^\"]\", \"bytes\": %llu, \"sha256\": \"%79[0-9a-f]\"}", rel, &bytes, digest);
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
        Row(Fmt("%-46s sha256 %s, the manifest says %s", rel, hash.empty() ? "(unreadable)" : hash.c_str(),
                digest));
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
  // `fileCount`, not `files`: the array above already holds that name, and a repeated key in one object is
  // resolved by every parser to the last one -- so the count silently replaced the rows. Writing the
  // document's schema is what surfaced it (the schema cannot describe two members with one name).
  Field("fileCount", (long long)(checked + bad), true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return bad == 0 ? 0 : 1;
}

// --------------------------------------------------------------------------- schema
//
// Every `--json` document needs a contract that is not "read the writer and infer it": the frame report and
// the offline tool read these documents, and a shape change has already gone unnoticed once (a stage's
// members were written flat into one object, so the repeated keys silently dropped the vertex shader).
// Documents therefore carry `schemaVersion`, and this table is the schema for each kind.
//
// The schemas are hand-written because the writers are hand-written: there is no descriptor to generate
// both from, and inventing one for fifteen documents is more machinery than it saves. They stay honest by
// being *used*: `schema --out <dir>` writes them, the copy in `schema/` is checked in, and
// `python rdc_analysis.py validate <bundle> schema` validates real documents against that copy -- so a
// schema that has drifted from its writer fails a run instead of misleading a reader. Writing them found a
// defect immediately: `bundle-verify` wrote `"files"` twice in one object (the per-file rows, then their
// count), which every parser resolves to the count.
//
// The keyword subset is exactly what the offline validator implements -- type, required, properties, items,
// enum, const, description, additionalProperties. `additionalProperties` is `false` throughout, so an
// unlisted member is a validation failure rather than something a consumer discovers later.
struct SchemaDoc
{
  const char *name;                                       // `schema <name>`; what validate matches a file by
  const char *writtenBy;                                  // which command writes it, for the index
  const char *text;
};

static const SchemaDoc kSchemas[] = {
    {"capture", "info, dump (capture.json)", R"sc({
  "title": "capture",
  "description": "A capture's own facts. `info` writes the short form; the bundle's capture.json adds the byte count, the absolute path and the pixel-history flag.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "pipelineType",
               "localRenderer", "remoteReplay", "vendor", "shaderDebugging", "chunks", "resources",
               "textures", "buffers", "debugMessages"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string", "description": "the path as given on the command line"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string", "description": "the capture's API, e.g. D3D12"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "pipelineType": {"type": "integer"},
    "localRenderer": {"type": "integer"},
    "remoteReplay": {"type": "integer"},
    "vendor": {"type": "integer"},
    "shaderDebugging": {"type": "integer"},
    "pixelHistory": {"type": "integer"},
    "chunks": {"type": "integer"},
    "resources": {"type": "integer"},
    "textures": {"type": "integer"},
    "buffers": {"type": "integer"},
    "debugMessages": {"type": "integer"},
    "captureBytes": {"type": "integer"},
    "absPath": {"type": "string"}
  },
  "additionalProperties": false
})sc"},

    {"events", "dump (events.json)", R"sc({
  "title": "events",
  "description": "Every id with bound state, and what was bound. One entry per state change plus the ids the scan was told to include. A state hash repeats when nothing changed, so a consumer can group without re-reading the state files.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "events", "total",
               "scanned", "scanFrom", "scanTo", "scanStopped", "stateFiles"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "events": {"type": "array", "items": {
      "type": "object",
      "required": ["eid", "pso", "psoKind", "shaders", "targets", "depth", "rootParameters", "state"],
      "properties": {
        "eid": {"type": "integer"},
        "pso": {"type": "string", "description": "the pipeline state object's resource id"},
        "psoKind": {"enum": ["graphics", "compute"]},
        "shaders": {"type": "string", "description": "`vs=2348 ps=2349`, stages in a fixed order"},
        "targets": {"type": "array", "items": {"type": "string"},
                    "description": "`<id> <w>x<h>x<d> <FORMAT>`, the output-merge state at this id"},
        "depth": {"type": "string", "description": "the depth target's resource id, `0` for none"},
        "rootParameters": {"type": "integer"},
        "state": {"type": "string", "description": "hash of what the state file was written from"}
      },
      "additionalProperties": false
    }},
    "total": {"type": "integer"},
    "scanned": {"type": "integer"},
    "scanFrom": {"type": "integer"},
    "scanTo": {"type": "integer"},
    "scanStopped": {"type": "string", "description": "why the sweep stopped, empty when it reached the end"},
    "stateFiles": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"resources", "dump (resources.json)", R"sc({
  "title": "resources",
  "description": "Every resource the engine knows, with the usage list it was gathered from. A texture carries its format and dimensions, a buffer its size in bytes; `usage` is what makes \"who touched this\" answerable.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "resources", "total"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "resources": {"type": "array", "items": {
      "type": "object",
      "required": ["resource", "name", "kind"],
      "properties": {
        "resource": {"type": "string"},
        "name": {"type": "string", "description": "the application's name, empty when it has none"},
        "kind": {"enum": ["texture", "buffer", "other"]},
        "format": {"type": "string"},
        "dimension": {"type": "integer"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "depth": {"type": "integer"},
        "mips": {"type": "integer"},
        "arraySize": {"type": "integer"},
        "samples": {"type": "integer"},
        "bytes": {"type": "integer"},
        "usage": {"type": ["array", "string"], "items": {
          "type": "object",
          "required": ["eid", "usage"],
          "properties": {"eid": {"type": "integer"}, "usage": {"type": "integer"}},
          "additionalProperties": false
        }, "description": "every event that touched this resource; a short string instead when the bundle was written with --no-usage, and the manifest's resourceUsage says why"},
        "usageCount": {"type": "integer"},
        "firstEvent": {"type": "integer", "description": "absent with --no-usage, like `usage`"},
        "lastEvent": {"type": "integer"}
      },
      "additionalProperties": false
    }},
    "total": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"manifest", "dump (manifest.json)", R"sc({
  "title": "manifest",
  "description": "What the bundle is and what is in it: the capture it was written from (with its hash), the flags it was written with, every file with its size and SHA-256, and the list of things the bundle deliberately does not contain. This is what a reader checks before trusting the rest.",
  "type": "object",
  "required": ["schemaVersion", "bundleVersion", "driver", "renderdoc", "capture", "captureAbsolute",
               "captureBytes", "captureSha256", "since", "until", "maxEvents", "withImages", "withCounters",
               "withTextures", "resourceUsage", "stateHashInputs", "statesRule", "notInThisBundle", "skipped",
               "files", "fileCount", "fileBytes"],
  "properties": {
    "schemaVersion": {"const": 1},
    "bundleVersion": {"type": "integer", "description": "the layout of the bundle itself"},
    "driver": {"type": "string"},
    "renderdoc": {"type": "string"},
    "capture": {"type": "string"},
    "captureAbsolute": {"type": "string"},
    "captureBytes": {"type": "integer"},
    "captureSha256": {"type": "string"},
    "since": {"type": "integer"},
    "until": {"type": "integer"},
    "maxEvents": {"type": "integer"},
    "withImages": {"type": "integer"},
    "withCounters": {"type": "integer"},
    "withTextures": {"type": "integer"},
    "resourceUsage": {"type": "string", "description": "`collected`, or why the usage lists are absent"},
    "stateHashInputs": {"type": "string", "description": "what the events' state hash is computed from"},
    "statesRule": {"type": "string", "description": "when a state file is written"},
    "notInThisBundle": {"type": "array", "items": {
      "type": "object",
      "required": ["what", "why"],
      "properties": {"what": {"type": "string"}, "why": {"type": "string"}},
      "additionalProperties": false
    }},
    "skipped": {"type": "array", "items": {"type": "string"}},
    "files": {"type": "array", "items": {
      "type": "object",
      "required": ["path", "bytes", "sha256"],
      "properties": {"path": {"type": "string"}, "bytes": {"type": "integer"}, "sha256": {"type": "string"}},
      "additionalProperties": false
    }},
    "fileCount": {"type": "integer"},
    "fileBytes": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"state", "state, dump (states/<eid>.state.json)", R"sc({
  "title": "state",
  "description": "One event's bound state: the capture header, then that event. The arrays are the driver's own rows, which are text by design -- they carry the engine's names verbatim.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "api",
               "shaders", "renderTargets", "depthTarget", "rootSignature", "rootParameters"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "api": {"type": "integer"},
    "shaders": {"type": "array", "items": {"type": "string"},
                "description": "`vs  res2348`, one row per bound stage -- including the stages this call kind does not use"},
    "renderTargets": {"type": "array", "items": {"type": "string"}},
    "depthTarget": {"type": "string", "description": "a resource id, `0` for none"},
    "rootSignature": {"type": "string"},
    "rootParameters": {"type": "array", "items": {"type": "string"}}
  },
  "additionalProperties": false
})sc"},

    {"shaders", "shaders, dump (states/<eid>.shaders.json)", R"sc({
  "title": "shaders",
  "description": "The reflection of every stage bound at one event, one object per stage -- an array, because two stages share every member name and a flat object would let a reader keep only the last.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "stages"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "stages": {"type": "array", "items": {
      "type": "object",
      "required": ["stage", "resource", "entry", "encoding", "bytes", "constantBlocks",
                   "readOnlyResources", "readWriteResources", "inputSignature", "outputSignature"],
      "properties": {
        "stage": {"type": "string", "description": "vs hs ds gs ps cs as ms"},
        "resource": {"type": "string"},
        "entry": {"type": "string"},
        "encoding": {"type": "integer"},
        "bytes": {"type": "integer"},
        "constantBlocks": {"type": "array", "items": {"type": "string"}},
        "readOnlyResources": {"type": "array", "items": {"type": "string"}},
        "readWriteResources": {"type": "array", "items": {"type": "string"}},
        "inputSignature": {"type": "array", "items": {"type": "string"}},
        "outputSignature": {"type": "array", "items": {"type": "string"}}
      },
      "additionalProperties": false
    }}
  },
  "additionalProperties": false
})sc"},

    {"messages", "debug, dump (messages.json)", R"sc({
  "title": "messages",
  "description": "The engine's own messages, one row each (`eid <n>  <severity>  <text>`), plus the header. Rows are strings: they are the same text the terminal prints, so nothing is lost between the two forms.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "messages", "total"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "messages": {"type": "array", "items": {"type": "string"}},
    "total": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"counters", "counters, dump (counters.json)", R"sc({
  "title": "counters",
  "description": "The driver's counter results, one row each (`eid <n>  <name> = <value>`), plus the header. Only written when the bundle was asked for them and the driver supports them.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "counters", "total"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "counters": {"type": "array", "items": {"type": "string"}},
    "total": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"textures", "textures", R"sc({
  "title": "textures",
  "description": "Every texture the engine knows, with the format and dimensions from the resource description.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "textures", "total",
               "shown"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "textures": {"type": "array", "items": {
      "type": "object",
      "required": ["resource", "dimension", "width", "height", "depth", "mips", "arraySize", "samples",
                   "format", "bytes"],
      "properties": {
        "resource": {"type": "string"},
        "dimension": {"type": "integer"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "depth": {"type": "integer"},
        "mips": {"type": "integer"},
        "arraySize": {"type": "integer"},
        "samples": {"type": "integer"},
        "format": {"type": "string"},
        "bytes": {"type": "integer"}
      },
      "additionalProperties": false
    }},
    "total": {"type": "integer"},
    "shown": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"draws", "draws", R"sc({
  "title": "draws",
  "description": "The structured file's draw-like chunks in order. `eid` is the *engine's* id where one is known and the chunk index where it is not: the two spaces are not the same (README §9), and `probe` lists the ids that have state.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "events",
               "totalChunks", "totalEvents", "shown"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "events": {"type": "array", "items": {
      "type": "object",
      "required": ["eid", "depth", "chunkID", "name"],
      "properties": {
        "eid": {"type": "integer"},
        "depth": {"type": "integer"},
        "chunkID": {"type": "integer"},
        "name": {"type": "string"}
      },
      "additionalProperties": false
    }},
    "totalChunks": {"type": "integer"},
    "totalEvents": {"type": "integer"},
    "shown": {"type": "integer"},
    "truncated": {"type": "string", "description": "present only when the action tree was deeper than the recursion limit"}
  },
  "additionalProperties": false
})sc"},

    {"probe", "probe", R"sc({
  "title": "probe",
  "description": "Which ids in a range have pipeline state. Rows are strings (`eid <n>  shaders=.. rootSig=.. params=..`), the same text the terminal prints.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "events",
               "scanned", "withState"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "events": {"type": "array", "items": {"type": "string"}},
    "scanned": {"type": "integer"},
    "withState": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"cb", "cb", R"sc({
  "title": "cb",
  "description": "One constant buffer at one event: what it is bound to and one row per reflection variable, with the value read from the data.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "stage",
               "slot", "shader", "buffer", "variables"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "stage": {"type": "string"},
    "slot": {"type": "integer"},
    "shader": {"type": "string"},
    "buffer": {"type": "string"},
    "variables": {"type": "array", "items": {"type": "string"}}
  },
  "additionalProperties": false
})sc"},

    {"mesh", "mesh", R"sc({
  "title": "mesh",
  "description": "One draw's mesh: the state that feeds it, and -- when the capture has post-VS data -- the vertices the vertex shader emitted.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "instance",
               "topology", "vertexResource", "vertexStride", "vertexBytes", "indexResource", "indexBytes"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "instance": {"type": "integer"},
    "topology": {"type": "integer"},
    "vertexResource": {"type": "string"},
    "vertexStride": {"type": "integer"},
    "vertexBytes": {"type": "integer"},
    "indexResource": {"type": "string"},
    "indexBytes": {"type": "integer"},
    "baseVertex": {"type": "integer"},
    "vertices": {"type": "array", "items": {"type": "string"}},
    "vertexCount": {"type": "integer"},
    "componentsPerVertex": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"image", "image", R"sc({
  "title": "image",
  "description": "One render target saved to a file, and whether the write succeeded.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "resource",
               "width", "height", "file", "written"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "resource": {"type": "string"},
    "width": {"type": "integer"},
    "height": {"type": "integer"},
    "file": {"type": "string"},
    "written": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"bundle-verify", "bundle-verify", R"sc({
  "title": "bundle-verify",
  "description": "The result of re-hashing a bundle against its manifest: one row per file that is missing, short, long or whose hash differs, and the counts. `problems` is the exit code.",
  "type": "object",
  "required": ["schemaVersion", "manifest", "files", "checked", "problems", "fileCount"],
  "properties": {
    "schemaVersion": {"const": 1},
    "manifest": {"type": "string"},
    "files": {"type": "array", "items": {"type": "string"}},
    "checked": {"type": "integer"},
    "problems": {"type": "integer"},
    "fileCount": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},
};

static const int kSchemaCount = (int)(sizeof(kSchemas) / sizeof(kSchemas[0]));

static const SchemaDoc *FindSchema(const char *name)
{
  for(int i = 0; i < kSchemaCount; i++)
  {
    if(!strcmp(kSchemas[i].name, name))
      return &kSchemas[i];
  }
  return NULL;
}

//: Read one schema file, folding CRLF to LF: a checkout can rewrite a file's line endings, and the contract
//: is the JSON, not the ending. A missing or unreadable file is a difference, not an error.
static bool ReadSchemaText(const std::string &path, std::string &text)
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

//: How many ways `dir` differs from this driver's table: a schema file that is missing, one whose text
//: differs, and a `<name>.schema.json` for a document kind the table no longer has -- which is worse than
//: useless, because a reader would take it for current. One code path for `schema --check` and for the
//: selftest, so the mode that is checked is the mode that runs.
static int CheckSchemasAgainstDir(const std::string &dir, bool report)
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
    want += "\n";                                    // `--out` ends the file with a newline, and so must this
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
static int WriteSchemasTo(const std::string &dir, const char *name, bool report)
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

//: `schema [<name>] [--out <dir>] [--check <dir>]`. Without a name it prints the index -- every kind, the
//: command that writes it and the version they all declare. `--out` writes `<dir>/<name>.schema.json` for
//: each, which is how the checked-in `schema/` folder is made; `--check` compares that folder against this
//: driver's table and exits non-zero when they differ. The check is what makes "regenerate after changing a
//: document" enforceable rather than a convention: a generated file that is committed *can* go stale, and
//: the only cure is a command that says so.
static int CmdSchema(const char *name, const char *outDir, const char *checkDir)
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
  printf("`schema <name>` prints one; `schema --out <dir>` writes them all where a consumer can read "
         "them, which is how the checked-in schema/ folder is made; `schema --check <dir>` fails when that "
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
static void Usage();

struct SelfTest
{
  int passed = 0, failed = 0, skipped = 0;

  void Ok(const char *name) { printf("ok      %s\n", name); passed++; }
  void Skipped(const char *name, const char *why) { printf("skipped %s -- %s\n", name, why); skipped++; }
  void Failed(const char *name, const char *why) { printf("FAILED  %s -- %s\n", name, why); failed++; }
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
    printf("FAILED  %s\n          got      %s\n          expected %s\n", name, got.c_str(), want.c_str());
    failed++;
  }
};

//: Whether `text` is one balanced JSON object. Not a parser -- it cannot tell a wrong member from a right
//: one -- but it catches the failure that matters for a document nobody looks at before it ships: a
//: truncated write, or a separator bug that leaves the object open. The writer's output is checked with it
//: here, and the offline validator checks the documents themselves against the schema.
static bool JsonBalanced(const std::string &text)
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

static int CmdSelftest()
{
  SelfTest t;

  // ------------------------------------------------------------------ the writer's helpers
  t.Equal(JsonEscape("a\"b"), "a\\\"b", "json-escape-quote");
  t.Equal(JsonEscape("a\\b"), "a\\\\b", "json-escape-backslash");
  t.Equal(JsonEscape("a\nb\tc\rd"), "a\\nb\\tc\\rd", "json-escape-controls");
  t.Equal(JsonEscape(std::string("\x01")), "\\u0001", "json-escape-low-byte");
  // A Windows path is the common case and the one that broke documents before escaping existed.
  t.Equal(JsonEscape("renderdoc-src\\Android.rdc"), "renderdoc-src\\\\Android.rdc", "json-escape-path");

  // The separator machinery is a state machine, and its two failure modes are opposite: a missing comma is
  // unreadable and a trailing one is too. Writing a small document and looking at the bytes pins both --
  // reading the writer cannot, which is how a hardcoded trailing comma got into a document once.
  {
    const std::string path = DefaultLogStem() + ".selftest.json";
    {
      const JsonDocument json;                       // JSON, whatever the terminal asked for
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

    // The separators are compared with the whitespace removed, because the writer puts a separator *in
    // front* of the item that needs one (and indents to its own taste): what must be exactly right is the
    // sequence of commas, and pinning the bytes would pin the layout instead.
    std::string flat;
    for(size_t i = 0; i < text.size(); i++)
    {
      const char c = text[i];
      if(c != ' ' && c != '\n' && c != '\r' && c != '\t')
        flat += c;
    }
    t.Check(flat.find("[\"one\",{\"k\":2},\"two\"]") != std::string::npos, "writer-separators",
            "array items are not comma-separated in the right places (the separator goes in front of an "
            "item, and never in front of the first)");
    t.Check(flat.find(",}") == std::string::npos && flat.find(",]") == std::string::npos,
            "writer-no-trailing-comma", "a trailing comma makes the document unreadable");
    t.Check(flat.find("\"n\":3") != std::string::npos, "writer-last-field", "the `last` member is missing");
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
      if(text.find("\"schemaVersion\"") == std::string::npos || text.find("\"const\": 1") == std::string::npos)
        versioned = false;
      if(text.empty() || text[0] != '{' || text[text.size() - 1] != '}')
        objects = false;
    }
    t.Check(unique, "schema-names-unique", "two schemas share a name");
    t.Check(versioned, "schema-declares-version", "a schema does not describe schemaVersion as a const");
    t.Check(objects, "schema-is-one-object", "a schema is not a single JSON object");
    t.Check(FindSchema("state") != NULL && FindSchema("manifest") != NULL && FindSchema("events") != NULL,
            "schema-covers-the-bundle", "a document the bundle depends on has no schema");
    t.Check(kSchemaVersion == 1, "schema-version-known", "the offline validator does not know this version");
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

    // Leave the folder as it was found: a leftover file would fail the *next* run's clean check, on another
    // day, in a directory nobody connects to this one.
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
    t.Check(usage.find("schema") != std::string::npos, "usage-lists-schema", "the usage text omits schema");
    t.Check(usage.find("selftest") != std::string::npos, "usage-lists-selftest",
            "the usage text omits selftest");
    t.Check(usage.find("dump") != std::string::npos, "usage-lists-dump", "the usage text omits dump");

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
    printf("the documents themselves are checked with `python rdc_analysis.py validate <bundle> schema`\n");
  return t.failed == 0 ? 0 : 1;
}

// --------------------------------------------------------------------------- CLI

static void Usage()
{
  printf(
      "replay_dump - headless RenderDoc replay as a data source (README §9)\n"
      "\n"
      "usage: replay_dump <command> <capture.rdc> [args] [--json]\n"
      "\n"
      "  info    <rdc>                     renderdoc version, driver, API properties, counts\n"
      "  draws   <rdc> [max=80] [filter]   the action tree with event ids (markers and calls)\n"
      "  state   <rdc> <eid>               bound shaders, outputs and D3D12 root parameters\n"
      "  shaders <rdc> <eid> [--disasm]    reflection: cbuffers, bindings, signatures, disassembly\n"
      "  cb      <rdc> <eid> <stage> <slot> named values of one constant buffer\n"
      "  textures <rdc> [filter] [--save <dir>]   texture list; --save decodes to PNG\n"
      "  mesh    <rdc> <eid> [instance] [max]     post-VS vertices for one instance\n"
      "  image   <rdc> <eid> <out.bmp>     the texture display at that event, as a BMP\n"
      "  counters <rdc>                    available GPU counters and their values\n"
      "  debug   <rdc>                     debug messages (validation layer, etc.)\n"
      "  usage   <rdc> <resId>             every event that touches a resource\n"
      "  probe   <rdc> [maxEid=2000]       which event ids actually have pipeline state\n"
      "  dump    <rdc> [outDir=bundle]     the whole frame to disk, for the offline tool (ROADMAP §2)\n"
      "  bundle-verify <dir>               check a bundle's hashes and sizes (no device, no DLL)\n"
      "  batch   <rdc> <file>              run every command in <file> against one open capture\n"
      "  schema  [<name>] [--out|--check <dir>]   the JSON Schema for each --json document (no capture, no DLL)\n"
      "  selftest                          this program checking itself: writer, schemas, help, DLL\n"
      "\n"
      "Options (any position): --json, --log <file>, --disasm, --save <dir>, --out <dir>, --check <dir>.\n"
      "\n"
      "A batch file holds one command per line, in the same syntax minus the executable and the\n"
      "capture (`state 270 --json`), with `#` for comments. Each line's output is preceded by a\n"
      "`#=== <line>` marker so a stream can be split again. This is the cheap way to run many\n"
      "commands: opening a capture and standing the replay engine up costs ~3 s on a small capture\n"
      "and ~10 s on a 1.4 GB one, and batch pays it once for the whole file.\n"
      "\n"
      "`dump` writes a bundle for the offline tool (ROADMAP §2): events.json for every id with bound\n"
      "state, states/<eid>.state.json and .shaders.json for the first event and every state change, one\n"
      "cbuffers/ file per constant block, resources.json with each resource's usage list, and\n"
      "messages.json. The manifest lists every file with its byte count and SHA-256, and it names what\n"
      "the bundle cannot contain (the replay API exposes no action list, so a call's kind, its counts and\n"
      "its marker are not in it). `--with-images` adds rt/ images at the state events, `--textures`\n"
      "decodes every texture to PNG (full size: the engine decodes but does not resize), `--with-counters`\n"
      "fetches the counters. `--since`/`--until` pin the id range (the default scans until 256 ids in a row\n"
      "have nothing bound), `--max-events` caps how many events are written, `--events 270,452` forces extra\n"
      "state files, `--no-usage` skips the usage lists (the slow part) and `--overwrite` reuses a folder.\n"
      "`bundle-verify <dir>` re-hashes a bundle with no device involved, so it can be checked anywhere.\n"
      "\n"
      "Every --json document carries `schemaVersion` (1 today), and `schema` prints the JSON Schema for\n"
      "each kind: `schema --out schema` writes the checked-in copies, and the offline tool validates real\n"
      "documents against those (`python rdc_analysis.py validate <bundle> schema`). `schema --check <dir>`\n"
      "compares that folder with this driver and exits non-zero when they disagree, which is what keeps a\n"
      "committed copy from going stale after a document changes. `selftest` needs no\n"
      "capture: it checks the JSON writer (escaping, separators, balance), the schema table, this help\n"
      "text and the renderdoc.dll it would load, and it skips the DLL checks rather than failing when\n"
      "RenderDoc is not installed.\n"
      "\n"
      "Progress goes to stderr and to one log file per run, <exe name>_<date>_<time>.log.txt beside\n"
      "the executable (--log <file> names one exact file and truncates it); the timings in it are\n"
      "what to read when a run looks stuck. Event ids are the engine's, and they are not the offline\n"
      "tool's chunk indices: `probe` lists the ids that actually have pipeline state.\n"
      "$RDC_RENDERDOC_DLL overrides the renderdoc.dll to load (default: the installed one) and\n"
      "$RDC_REPLAY_DEBUG=1 traces every step, for when the engine takes the process down. Run one\n"
      "replay at a time: the engine creates a device per process, and two at once on one GPU is what\n"
      "makes it look stuck.\n");
}

//: A command-line integer, validated: `atoi` answers 0 for anything that is not a number, and 0
//: silently means "no limit" for a row count and "event 0" for an event id, so a typo changed what
//: the command did rather than failing. An unparsable argument is reported and the default is used.
static bool ParseInt(const char *text, int &value)
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

static int ToInt(const std::string &text, int fallback)
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
static const char *kJsonFlag = "--json";
static const char *kDisasmFlag = "--disasm";
static const char *kSaveFlag = "--save";
static const char *kLogFlag = "--log";

static void Usage();

//: Runs one command against an already-open capture. Shared by `main` and `batch`, so a command
//: name and its arguments mean the same thing however they were spelled.
static int DispatchCommand(IReplayController *ctrl, ICaptureFile *file, const char *path,
                           const std::vector<std::string> &args, bool wantDisasm, const char *saveDir)
{
  if(args.empty())
    return 2;

  const char *cmd = args[0].c_str();
  if(!strcmp(cmd, "info"))
    return CmdInfo(ctrl, file, path);
  if(!strcmp(cmd, "draws"))
    return CmdDraws(ctrl, file, path, args.size() > 1 ? ToInt(args[1], 80) : 80,
                    args.size() > 2 ? args[2].c_str() : NULL);
  if(!strcmp(cmd, "state") && args.size() > 1)
    return CmdState(ctrl, file, path, ToInt(args[1], 0));
  if(!strcmp(cmd, "shaders") && args.size() > 1)
    return CmdShaders(ctrl, file, path, ToInt(args[1], 0), wantDisasm);
  if(!strcmp(cmd, "cb") && args.size() > 3)
  {
    const ShaderStage stage = StageFromName(args[2].c_str());
    if(stage == ShaderStage::Invalid)
      return Fail(2, "'%s' is not a shader stage (vs hs ds gs ps cs as ms)", args[2].c_str());
    return CmdCbuffer(ctrl, file, path, ToInt(args[1], 0), stage, ToInt(args[3], 0));
  }
  if(!strcmp(cmd, "textures"))
    return CmdTextures(ctrl, file, path, args.size() > 1 ? args[1].c_str() : NULL, saveDir);
  if(!strcmp(cmd, "mesh") && args.size() > 1)
    return CmdMesh(ctrl, file, path, ToInt(args[1], 0), args.size() > 2 ? ToInt(args[2], 0) : 0,
                   args.size() > 3 ? ToInt(args[3], 16) : 16);
  if(!strcmp(cmd, "image") && args.size() > 2)
    return CmdImage(ctrl, file, path, ToInt(args[1], 0), args[2].c_str());
  if(!strcmp(cmd, "counters"))
    return CmdCounters(ctrl, file, path);
  if(!strcmp(cmd, "debug"))
    return CmdDebug(ctrl, file, path);
  if(!strcmp(cmd, "usage") && args.size() > 1)
    return CmdUsage(ctrl, file, path, args[1].c_str());
  if(!strcmp(cmd, "probe"))
    return CmdProbe(ctrl, file, path, args.size() > 1 ? ToInt(args[1], 2000) : 2000);
  if(!strcmp(cmd, "dump"))
    return CmdDump(ctrl, file, path, args, wantDisasm);
  if(!strcmp(cmd, "bundle-verify") && args.size() > 1)
    return CmdBundleVerify(args[1].c_str());

  Fail(2, "unknown command '%s' (or missing arguments)", cmd);
  Usage();
  return 2;
}

//: Splits a command line into arguments, taking the options out as it goes. Double quotes group a
//: token, which `--save` and `image` need for a path with spaces.
static void SplitLine(const std::string &line, std::vector<std::string> &args, bool &json, bool &disasm,
                      std::string &saveDir)
{
  std::string token;
  bool quoted = false;
  bool wantSaveDir = false;
  for(size_t i = 0; i <= line.size(); i++)
  {
    const char c = (i < line.size()) ? line[i] : ' ';
    if(c == '"')
    {
      quoted = !quoted;
      continue;
    }
    if(!quoted && (c == ' ' || c == '\t'))
    {
      if(token.empty())
        continue;
      if(token == kJsonFlag)
        json = true;
      else if(token == kDisasmFlag)
        disasm = true;
      else if(token == kSaveFlag)
        wantSaveDir = true;
      else if(wantSaveDir)
      {
        saveDir = token;
        wantSaveDir = false;
      }
      else
        args.push_back(token);
      token.clear();
      continue;
    }
    token += c;
  }
}

//: Runs a file of command lines against one open capture. The point is the cost of a replay
//: session, not the cost of the commands: standing the engine up and opening the capture is ~4 s on
//: a small capture and ~11 s on a 1.4 GB one, and this pays it once for the whole file.
static int CmdBatch(IReplayController *ctrl, ICaptureFile *file, const char *path,
                    const char *batchPath)
{
  FILE *f = fopen(batchPath, "rb");
  if(f == NULL)
    return Fail(2, "cannot read batch file %s", batchPath);

  int ret = 0, ran = 0;
  bool sawProbe = false, sawOther = false;
  std::string line;
  for(;;)
  {
    const int ch = fgetc(f);
    if(ch != EOF && ch != '\n')
    {
      if(ch != '\r')
        line += (char)ch;
      continue;
    }

    // A blank line or a `#` comment is skipped, so a batch file can be annotated.
    const size_t first = line.find_first_not_of(" \t");
    if(first != std::string::npos && line[first] != '#')
    {
      std::vector<std::string> args;
      bool json = false, disasm = false;
      std::string saveDir;
      SplitLine(line.substr(first), args, json, disasm, saveDir);

      // `probe` forces non-events, and a forced non-event leaves the last real event's state in
      // place; whichever ran second, one of the two answers would be wrong. It belongs in its own
      // run, and saying so here is cheaper than explaining a mysteriously different answer.
      const bool isProbe = !args.empty() && args[0] == "probe";
      sawProbe = sawProbe || isProbe;
      sawOther = sawOther || !isProbe;

      // The marker is what lets a caller split the stream back into one output per command. It is
      // printed in both formats: JSON has no comment syntax, and guessing where one object ends and
      // the next begins is not something a consumer should have to do.
      printf("#=== %s\n", line.substr(first).c_str());
      fflush(stdout);

      g_json = json;
      const ULONGLONG started = Millis();
      const int code = DispatchCommand(ctrl, file, path, args, disasm,
                                       saveDir.empty() ? NULL : saveDir.c_str());
      ran++;
      ret = (code != 0) ? code : ret;
      Log("batch %d: %s -> exit %d in %.1fs", ran, line.substr(first).c_str(), code,
          (Millis() - started) / 1000.0);
    }

    if(ch == EOF)
      break;
    line.clear();
  }

  if(sawProbe && sawOther)
  {
    Log("warning: this batch mixes `probe` with other commands; probe forces non-events, which "
        "leaves stale state behind, so run it on its own");
  }

  fclose(f);
  Log("batch finished: %d command(s)", ran);
  return ret;
}

int main(int argc, char **argv)
{
  // Unbuffered: the replay engine is third-party code that can take the process down with it, and
  // losing the output that was already produced would hide exactly what happened.
  setvbuf(stdout, NULL, _IONBF, 0);
  setvbuf(stderr, NULL, _IONBF, 0);
  g_start = Millis();                                // progress timings are relative to this

  std::vector<std::string> args;
  bool wantDisasm = false;
  const char *saveDir = NULL;
  std::string logPath = DefaultLogStem();
  bool perRunLog = true;                             // until `--log` names one exact file
  std::string schemaOut;                             // `--out <dir>`: where `schema` writes them
  std::string schemaCheck;                           // `--check <dir>`: the copy to verify against
  for(int i = 1; i < argc; i++)
  {
    if(!strcmp(argv[i], kJsonFlag))
      g_json = true;
    else if(!strcmp(argv[i], kDisasmFlag))
      wantDisasm = true;
    else if(!strcmp(argv[i], kSaveFlag) && i + 1 < argc)
      saveDir = argv[++i];
    else if(!strcmp(argv[i], kLogFlag) && i + 1 < argc)
    {
      logPath = argv[++i];
      perRunLog = false;
    }
    else if(!strcmp(argv[i], "--out") && i + 1 < argc)
      schemaOut = argv[++i];
    else if(!strcmp(argv[i], "--check") && i + 1 < argc)
      schemaCheck = argv[++i];
    else
      args.push_back(argv[i]);
  }

  if(args.empty() || args[0] == "--help" || args[0] == "-h")
  {
    Usage();
    return args.empty() ? 2 : 0;
  }

  // `schema` and `selftest` are about this program rather than about a frame: no capture path, no device,
  // and no log file -- they answer before anything is loaded, so a machine with no RenderDoc and no GPU
  // still gets the half of the selftest that does not need them.
  if(!strcmp(args[0].c_str(), "schema"))
    return CmdSchema(args.size() > 1 ? args[1].c_str() : NULL, schemaOut.empty() ? NULL : schemaOut.c_str(),
                     schemaCheck.empty() ? NULL : schemaCheck.c_str());
  if(!strcmp(args[0].c_str(), "selftest"))
    return CmdSelftest();

  const char *cmd = args[0].c_str();
  if(args.size() < 2)
  {
    fprintf(stderr, "error: %s needs a capture path\n", cmd);
    return 2;
  }

  // Checking a bundle re-hashes files: no DLL, no device, no capture. It runs here, before anything is
  // loaded, so a bundle can be verified on a machine where RenderDoc is not even installed.
  if(!strcmp(cmd, "bundle-verify"))
    return CmdBundleVerify(args[1].c_str());

  // The log is opened only once the command is known to be runnable, so `--help` and a command
  // without a capture path -- which do nothing -- leave no file behind. The absolute name goes into
  // the first line: a per-run name carries a timestamp, so it is not something to guess, and a wrong
  // capture path is what makes the working directory worth stating.
  if(!logPath.empty())
  {
    std::string openedAs;
    g_logFile = OpenLog(logPath, perRunLog, openedAs);
    if(g_logFile == NULL)
      fprintf(stderr, "warning: cannot write the log file %s\n", openedAs.c_str());
    else
      Log("log file: %s", openedAs.c_str());
  }

  // Every path is made absolute here, and the working directory is logged: a path that only works
  // from one directory is otherwise indistinguishable from a missing file.
  const std::string pathAbs = AbsolutePath(args[1].c_str());
  const char *path = args[1].c_str();                // as given: what the output shows
  std::string saveDirAbs, batchPathAbs;
  if(saveDir != NULL)
    saveDirAbs = AbsolutePath(saveDir);
  if(args.size() > 2 && !strcmp(cmd, "batch"))
    batchPathAbs = AbsolutePath(args[2].c_str());

  Log("working directory: %s", WorkingDirectory().c_str());
  Log("loading renderdoc.dll");
  HMODULE dll = LoadReplayDLL();
  if(dll == NULL)
    return Fail(1, "cannot load renderdoc.dll");

  Log("starting the replay system");
  if(!InitialiseReplay(dll, argc, argv))
    return Fail(1, "RENDERDOC_InitialiseReplay failed");
  const ReplaySystemGuard replaySystem;              // declared first, so it shuts down last

  Trace("RENDERDOC_OpenCaptureFile");
  ICaptureFile *file = OpenCaptureFile(dll);
  if(file == NULL)
    return 1;
  const CaptureFileGuard captureFile(file);

  // The capture is opened by its absolute path (a relative one depends on the working directory),
  // while the *output* keeps the path as it was given, so a command's text is the same however it
  // was invoked. The absolute form and the working directory go to the log, where a wrong path is
  // the thing being diagnosed.
  Log("reading the container of %s", pathAbs.c_str());
  Trace("OpenFile");
  const ResultDetails res = file->OpenFile(pathAbs.c_str(), "rdc", NULL);
  if(!res.OK())
    return Fail(1, "cannot open %s: %s", pathAbs.c_str(), ResultText(res).c_str());

  // This is where a run looks stuck, and it is worth saying so before it happens: the engine builds
  // its own copy of the frame and creates a replay device, which is ~3 s for a small capture and
  // ~10 s for a 1.4 GB one, and longer if another replay session is competing for the same GPU.
  // A batch file pays it once for the whole file.
  Log("opening the capture and creating the replay device (this is the slow part; if another "
      "replay is running, that is why)");
  // Every member of ReplayOptions is default-initialised in the header, but zeroing the whole struct
  // also rules out a layout disagreement with the DLL: all-zero means "no overrides" either way.
  ReplayOptions opts;
  memset(&opts, 0, sizeof(opts));
  const rdcpair<ResultDetails, IReplayController *> opened = file->OpenCapture(opts, NULL);
  Trace("OpenCapture returned");
  if(!opened.first.OK())
    return Fail(1, "cannot replay %s: %s", path, ResultText(opened.first).c_str());
  IReplayController *ctrl = opened.second;
  const ControllerGuard controller(ctrl);
  Log("replay ready");

  int ret;
  const ULONGLONG started = Millis();
  if(!strcmp(cmd, "batch") && args.size() > 2)
  {
    ret = CmdBatch(ctrl, file, path, batchPathAbs.c_str());
  }
  else
  {
    // The command, then everything after the capture path: `DispatchCommand` takes the command as
    // `args[0]` and never the capture, exactly as a batch line spells it (`state 270`).
    std::vector<std::string> cmdArgs;
    cmdArgs.push_back(args[0]);
    cmdArgs.insert(cmdArgs.end(), args.begin() + 2, args.end());
    ret = DispatchCommand(ctrl, file, path, cmdArgs, wantDisasm,
                          saveDirAbs.empty() ? NULL : saveDirAbs.c_str());
  }
  Log("done: exit %d after %.1fs", ret, (Millis() - started) / 1000.0);

  // The controller, the capture file and the replay system are torn down by the guards above, in
  // that order, as this function returns.
  //
  // The DLL is deliberately not freed: the objects are owned by it, and RenderDoc's own tools let
  // the process exit instead of unloading the engine underneath its own state.
  if(g_logFile != NULL)
    fclose(g_logFile);
  return ret;
}
