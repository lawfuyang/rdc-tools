// The driver's internal API.
//
// replay_dump is one program split into modules by what a reader is looking for: the text of the
// engine's names (text.cpp), the writer every command prints through (output.cpp), the replay
// session itself (capture.cpp), the capture's action tree (actions.cpp), the commands by area
// (commands_frame.cpp, commands_state.cpp), the bundle producer (bundle.cpp), the self-check
// (selftest.cpp) and the entry point (replay_dump.cpp). This header is what they share: the few
// globals, the shared types, and every function one module calls in another. A function that no
// other module calls stays `static` in its own file and is deliberately not listed here.
//
// The three things a replay host *must* do — REPLAY_PROGRAM_MARKER() at file scope,
// RENDERDOC_InitialiseReplay() before opening anything, RENDERDOC_ShutdownReplay() on the way out —
// are in capture.cpp and replay_dump.cpp; without the first two the engine runs with uninitialised
// global state and dies inside OpenCapture with an access violation.
#pragma once

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <sal.h>    // _Printf_format_string_

#include <bcrypt.h>    // SHA-256 for the bundle manifest
#include <io.h>        // _dup/_dup2: the bundle writes files, not stdout
#pragma comment(lib, "bcrypt.lib")

#include <cerrno>
#include <charconv>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <map>
#include <string>
#include <type_traits>
#include <vector>

#include "schema.h"    // the JSON contract (schema.cpp)

// By angle brackets, so /external:anglebrackets keeps the library's own warnings out of this build.
#include <renderdoc_replay.h>

// --------------------------------------------------------------------------- the DLL's entry points

typedef ICaptureFile *(RENDERDOC_CC *pOpenCaptureFile)();
typedef const char *(RENDERDOC_CC *pGetVersionString)();
typedef void(RENDERDOC_CC *pInitialiseReplay)(GlobalEnvironment env, const rdcarray<rdcstr> &args);
typedef void(RENDERDOC_CC *pShutdownReplay)();

// --------------------------------------------------------------------------- shared state
//
// Only the state more than one module touches. Each block names the module that *owns* it;
// everything else is file-static in its owner.

extern bool g_json;          // output.cpp: --json, switched on by the entry point
extern int g_indent;         // output.cpp: the writer's nesting level
extern FILE *g_logFile;      // capture.cpp: the log, or NULL for stderr
extern ULONGLONG g_start;    // capture.cpp: the process start, for the timing column
extern pGetVersionString g_GetVersionString;    // capture.cpp: resolved from the DLL, printed in headers

// --------------------------------------------------------------------------- limits
//
// Bounds on walks of engine data: the structured file's tree and a shader variable's members are
// the capture's to choose, and a corrupt or crafted file must not be able to exhaust the stack.

constexpr int kMaxTreeDepth = 256;
//: How deep the *action* walk goes before it gives up and says so: the marker nest is 3-5 levels in every
//: capture measured, and a cap is what keeps engine-supplied data from recursing without a bound.
constexpr int kMaxActionDepth = 64;
constexpr int kMaxValueDepth = 16;

// --------------------------------------------------------------------------- text (text.cpp)

std::string ResultText(const ResultDetails &res);
std::string UsageText(ResourceUsage usage);
std::string SeverityText(MessageSeverity severity);
std::string CounterText(GPUCounter counter);
const char *StageName(ShaderStage stage);
char RegisterLetter(DescriptorCategory category);
std::string VisibilityText(ShaderStageMask mask);
ShaderStage StageFromName(const char *name);
const D3D12Pipe::Shader *StageShader(const D3D12Pipe::State *d3d12, ShaderStage stage);
const rdcarray<ShaderStage> &ReportedStages();
const char *CompareFunctionText(CompareFunction fn);
const char *StencilOperationText(StencilOperation op);
const char *BlendMultiplierText(BlendMultiplier m);
const char *BlendOperationText(BlendOperation op);
const char *VarTypeText(VarType t);
std::string SignatureText(const SigParameter &sig);
std::string FormatValue(const ShaderVariable &v, int depth);
std::string FormatValue(const ShaderVariable &v);
void PrintVariables(const rdcarray<ShaderVariable> &vars, int depth);
std::string IdText(ResourceId id);

// --------------------------------------------------------------------------- the writer (output.cpp)

bool IsJson();
void SetJson(bool on);
std::string JsonEscape(const char *s);
std::string JsonEscape(const std::string &s);
std::string JsonEscape(const rdcstr &s);
void Indent();
void Field(const char *key, const std::string &value, bool last = false);
void Field(const char *key, const rdcstr &value, bool last = false);
void Field(const char *key, long long value, bool last = false);
void Flag(const char *key, bool value, bool last = false);
void ArrayOpen(const char *key);
void ArrayClose(bool last = true);
void Row(const std::string &text);
void ObjectRow(const std::string &object);
void ObjectOpen();
void ObjectOpenKey(const char *key);
void ObjectClose(bool last = true);
std::string FmtV(_Printf_format_string_ const char *fmt, va_list args);
std::string Fmt(_Printf_format_string_ const char *fmt, ...);

//: Runs a block with stdout pointing at a file, so a command written to print to the terminal
//: writes a file instead. A file-descriptor swap rather than a `FILE *` threaded through the
//: writers, because the helpers (`Field`, `Row`, `ArrayOpen`, ...) and the commands that use them
//: print in a dozen places each: one missed call site would silently corrupt a document, and the
//: descriptor is the only version of this that cannot miss one.
//:
//: `stdout` is unbuffered (setvbuf in `main`), so nothing has to be flushed before the swap, and
//: the guard restores the descriptor on every path out of the scope, early returns included.
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

// --------------------------------------------------------------------------- the session (capture.cpp)

ULONGLONG Millis();
void Log(const char *fmt, ...);
void Trace(const char *step);
int Fail(int code, _Printf_format_string_ const char *fmt, ...);
std::string AbsolutePath(const char *path);
std::string WorkingDirectory();
std::string DefaultLogStem();
FILE *OpenLog(const std::string &requested, bool perRun, std::string &openedAs);
HMODULE LoadReplayDLL();
bool InitialiseReplay(HMODULE dll, int argc, char **argv);
ICaptureFile *OpenCaptureFile(HMODULE dll);
void PrintCaptureHeader(ICaptureFile *file, const char *path);
bool ParseInt(const char *text, int &value);
int ToInt(const std::string &text, int fallback);

//: RAII over the three things a session must undo, in the order a session builds them: the replay
//: system, then the capture file, then the controller. Each closes what it was given on every path
//: out of scope. The bodies are in capture.cpp, next to the DLL function they call, which is not
//: visible here.
struct ReplaySystemGuard
{
  ReplaySystemGuard() = default;
  ~ReplaySystemGuard();
  ReplaySystemGuard(const ReplaySystemGuard &) = delete;
  ReplaySystemGuard &operator=(const ReplaySystemGuard &) = delete;
};

struct CaptureFileGuard
{
  explicit CaptureFileGuard(ICaptureFile *capture);
  ~CaptureFileGuard();
  CaptureFileGuard(const CaptureFileGuard &) = delete;
  CaptureFileGuard &operator=(const CaptureFileGuard &) = delete;

  ICaptureFile *file;
};

struct ControllerGuard
{
  explicit ControllerGuard(IReplayController *replay);
  ~ControllerGuard();
  ControllerGuard(const ControllerGuard &) = delete;
  ControllerGuard &operator=(const ControllerGuard &) = delete;

  IReplayController *ctrl;
};

// --------------------------------------------------------------------------- the action tree (actions.cpp)

//: One chunk of the structured file. `eid` is the depth-first index over *chunks* (parameters are descended
//: through without numbering), which is not the engine's event id -- that comes from the action list.
//: One action from the engine's own list, flattened: its id, its name, and the markers it sits inside.
//:
//: `eid` is `ActionDescription::eventId` -- the id `SetFrameEvent`, `probe` and a bundle's ids all use -- and
//: *not* the structured file's chunk numbering, which is a different one (measured: on `PC Renderer.rdc` the
//: chunk-derived numbers run to millions where the engine's event ids run to 2132, so the two never meet).
//: `flags` is the engine's own classification, so a call is a call and a marker is a marker: nothing here is
//: inferred from a name.
struct ActionNode
{
  int eid = 0;            // ActionDescription::eventId: the id SetFrameEvent, probe and a bundle all use
  int depth = 0;          // how deep in the marker nest, 0 at the root
  bool call = false;      // a draw/dispatch/copy rather than a marker
  bool marker = false;    // this row opens a marker (PushMarker/SetMarker)
  rdcstr name;            // the marker's custom name, or the call's own chunk name
  rdcstr path;            // the markers this row sits inside, `A > B`, empty at the root
};

bool IsMarkerPush(ActionFlags flags);
bool IsCallFlags(ActionFlags flags);
std::vector<ActionNode> ActionTree(IReplayController *ctrl, int &calls, bool &truncated);
std::map<int, std::string> MarkerPaths(IReplayController *ctrl);
std::string MarkerPathAt(IReplayController *ctrl, int eid);
void CollectDispatchKinds(const rdcarray<ActionDescription> &actions, std::map<int, bool> &kinds);
std::map<int, bool> DispatchByEid(IReplayController *ctrl, int &calls);

// --------------------------------------------------------------------------- the commands

int CmdInfo(IReplayController *ctrl, ICaptureFile *file, const char *path);
int CmdDraws(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxRows,
             const char *filter);
int CmdState(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid);
int CmdShaders(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
               bool wantDisasm);
int CmdCbuffer(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
               ShaderStage stage, int slot);
int CmdTextures(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *filter,
                const char *saveDir);
int CmdMesh(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid, int instance,
            int maxRows);
int CmdImage(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
             const char *outPath);
int CmdCounters(IReplayController *ctrl, ICaptureFile *file, const char *path);
int CmdDebug(IReplayController *ctrl, ICaptureFile *file, const char *path);
int CmdUsage(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *what);
int CmdProbe(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxEid);
int CmdBatch(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *batchPath);
bool SaveTargetImage(IReplayController *ctrl, ResourceId target, const char *outBase,
                     std::string &written, int32_t &width, int32_t &height);

// --------------------------------------------------------------------------- the bundle (bundle.cpp)

int CmdDump(IReplayController *ctrl, ICaptureFile *file, const char *path,
            const std::vector<std::string> &args, bool wantDisasm);
int CmdBundleVerify(const char *dir);

//: The small file helpers the bundle (and the self-check reading a schema off disk) share.
std::string Sha256File(const char *path);
bool ReadWholeFile(const char *path, std::string &text);
bool FileBytes(const char *path, unsigned long long &bytes);
bool MakeDir(const std::string &path);
bool DirIsEmpty(const std::string &path, bool &empty);
std::string BundleRelative(const std::string &root, const std::string &full);

// --------------------------------------------------------------------------- self-check (selftest.cpp)

int CmdSchema(const char *name, const char *outDir, const char *checkDir);
int CmdSelftest();
int CheckSchemasAgainstDir(const std::string &dir, bool report);
int WriteSchemasTo(const std::string &dir, const char *name, bool report);
bool ReadSchemaText(const std::string &path, std::string &text);
