// The driver's internal API.
//
// replay_dump is one program split into modules by what a reader is looking for: the text of the
// engine's names (text.cpp), the writer every command prints through (output.cpp), the replay
// session itself (capture.cpp), the capture's action tree (actions.cpp), the commands by area
// (commands_frame.cpp, commands_state.cpp), the frame's pictures and the one experiment
// (commands_image.cpp, commands_patch.cpp), the images behind them (image.cpp), the bundle producer
// (bundle.cpp), the self-check (selftest.cpp) and the entry point (replay_dump.cpp). This header is
// what they share: the few globals, the shared types, and every function one module calls in
// another. A function that no other module calls stays `static` in its own file and is deliberately
// not listed here.
//
// A definition longer than a line is not here either: this file is a list of what may be called, so
// the bodies of the types declared below are in common.cpp, and only a one-line body (`Ok()`, a
// guard's constructor) is written out in place.
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

#include <algorithm>    // std::min/std::max, for the image scaling (image.cpp)
#include <cerrno>
#include <charconv>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>    // every path and every filesystem operation: no raw Win32 path calls
#include <limits>
#include <map>
#include <optional>
#include <string>
#include <string_view>
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

extern bool g_bJson;         // output.cpp: --json, switched on by the entry point
extern int g_Indent;         // output.cpp: the writer's nesting level
extern FILE *g_LogFile;      // capture.cpp: the log, or NULL for stderr
extern ULONGLONG g_Start;    // capture.cpp: the process start, for the timing column
extern pGetVersionString g_GetVersionString;    // capture.cpp: resolved from the DLL, printed in headers
//: capture.cpp: the engine's shutdown entry point, resolved once by `InitialiseReplay`. The exe shuts
//: the system down through its guard at the end of `main`; the library (api.cpp) does it when its DLL
//: unloads, because the system is the *process's* and a session must not take it down (`api.h`).
extern pShutdownReplay g_ShutdownReplay;
extern std::string g_DllOverride;    // capture.cpp: `--dll <path>`, ahead of $RDC_RENDERDOC_DLL

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
bool SeverityFromName(const char *name, MessageSeverity &out);
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
//: A format as the type its components carry and how many: `unorm4`, `float2`.
//: `ResourceFormat::Name()` is the engine's own word for the format, but it is a call into the DLL
//: and it is the *named* format only -- a cast target, or a format the engine has no name for, is
//: better described by what is in the struct.
std::string FormatText(const ResourceFormat &fmt);
//: Which family a component type belongs to: UInt, SInt, or Float.
//:
//: Everything that is not an integer -- unorm, snorm, the scaled types, sRGB, a depth format -- is
//: Float, because that is what the shader sees and what a texture typed as one of them stores: a
//: `float4` written to a `R8G8B8A8_UNORM` target is the normal case, and calling that a mismatch
//: would be a false positive on nearly every pass in every capture. `Typeless` stays Typeless,
//: which is how "the state does not say" is kept distinct from "the state says float".
CompType ComponentClass(CompType type);
std::string FormatValue(const ShaderVariable &v, int depth);
std::string FormatValue(const ShaderVariable &v);
void PrintVariables(const rdcarray<ShaderVariable> &vars, int depth);
std::string IdText(ResourceId id);

// --------------------------------------------------------------------------- the writer (output.cpp)

bool IsJson();
void SetJson(bool bOn);
std::string JsonEscape(const char *s);
std::string JsonEscape(std::string_view s);
std::string JsonEscape(const rdcstr &s);
void Indent();
void Field(const char *key, std::string_view value, bool bLast = false);
void Field(const char *key, const rdcstr &value, bool bLast = false);
//: Integers only, and deliberately so: a `double` overload was tried here and made every existing
//: `Field(key, 0)` ambiguous (int converts to both), which is a compile error in twenty call sites
//: rather than a wrong number in one. A fractional value is written as text with `Fmt("%.3f", ...)`
//: and typed as a string in its schema -- see `percentDiffering` in commands_image.cpp.
void Field(const char *key, long long value, bool bLast = false);
void Flag(const char *key, bool bValue, bool bLast = false);
void ArrayOpen(const char *key);
void ArrayClose(bool bLast = true);
void Row(std::string_view text);
void ObjectRow(std::string_view object);
void ObjectOpen();
void ObjectOpenKey(const char *key);
void ObjectClose(bool bLast = true);
std::string FmtV(_Printf_format_string_ const char *fmt, va_list args);
std::string Fmt(_Printf_format_string_ const char *fmt, ...);

//: Whether the next document's writes go through a buffered stdout, and whether that is allowed yet.
//:
//: A document is a great many small `printf`s -- 11,082 rows in `resources.json` -- and while stdout is
//: unbuffered (so a crash still leaves what a command printed) each one is a write syscall, measured at
//: 3.8 s of a 36.5 s bundle dump. Buffering is only legal once the caller says **no engine call
//: follows**: `setvbuf(stdout, NULL, _IOFBF, ...)` before the events loop was measured to move
//: `states/841.state.json` and `events.json`, because the engine's answer depends on how fast the host
//: returns to it. The bundle writer turns it on after its last `SetFrameEvent` (REFERENCE 9).
void SetDocumentBuffering(bool bOn);
bool DocumentBuffering();
const size_t kDocBuffer = 1 << 20;

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
  explicit CaptureStdout(const std::filesystem::path &path);
  ~CaptureStdout();
  CaptureStdout(const CaptureStdout &) = delete;
  CaptureStdout &operator=(const CaptureStdout &) = delete;
  bool Ok() const { return m_File != NULL && m_Saved >= 0; }

private:
  FILE *m_File = NULL;
  int m_Saved = -1;
  bool m_bBuffered = false;
};

//: The bundle's documents are JSON whatever the terminal was asked for: they are read by the offline
//: tool, not by a person, so `dump` forces the JSON writer on for the duration of one document.
class JsonDocument
{
public:
  JsonDocument() : m_bSaved(g_bJson) { g_bJson = true; }
  ~JsonDocument() { g_bJson = m_bSaved; }
  JsonDocument(const JsonDocument &) = delete;
  JsonDocument &operator=(const JsonDocument &) = delete;

private:
  bool m_bSaved;
};

// --------------------------------------------------------------------------- the session (capture.cpp)

ULONGLONG Millis();
void Log(const char *fmt, ...);
void Trace(const char *step);

//: Where a long run's time went, one slot per thing that can dominate it.
//:
//: The engine is a single-threaded black box behind a call, so "why does this take minutes" is
//: answered by timing the calls -- and the answer is not the obvious one: on `desktop-2` a
//: *cold jump* over 1400 chunks costs 0.2 s while 400 swept ids cost 18.8 s, so the cost is
//: per-call refresh, not replay. Measured rather than reasoned about is the whole point of these
//: slots.
//:
//: Compiled in but off unless `$RDC_PROFILE=1`: a run that does not ask pays two clock reads per
//: call site, and none of the arithmetic.
enum ProfileSlot
{
  kProfileSetFrameEvent,    // the engine moving to (and refreshing) an event
  kProfilePipelineState,    // copying the state out to read it
  kProfileStateDoc,         // CmdState: the state document
  kProfileShadersDoc,       // CmdShaders: the shader/reflection document
  kProfileCBuffers,         // the constant-block documents (descriptor resolution is in here)
  kProfileEventRow,         // the per-event row: key, hash, marker, targets, JSON
  kProfileImages,           // SaveTexture at a state change
  kProfileActions,          // walking the engine's action tree (draws, kinds, markers)
  kProfilePixelHistory,     // the engine's pixel history: it re-runs the frame's draws
  kProfileResources,        // resources.json
  kProfileMessages,         // messages.json
  kProfileTextures,         // textures.json and texture decoding
  kProfileUsage,            // the usage lists the bundle carries
  kProfileCrosscheck,       // the cross-check sweep: one SetFrameEvent per event, plus the checks
  kProfileCount
};

void ProfileAdd(ProfileSlot slot, unsigned long long since);
void ProfileReport();

//: Real-time progress for a loop that can run for minutes.
//:
//: Time-based, not count-based: the count-based version (one line per 2000 ids) stayed silent for
//: 164 s on `desktop-2`, because `--max-events 900` stopped the sweep at id 1740 -- before
//: the first line was ever due. A line carries the rate and what is left, which is what a reader
//: wants while waiting: not "how far", but "how much longer".
class Progress
{
public:
  Progress() : m_Total(0), m_Start(0), m_Last(0), m_Logged(false) {}

  void Begin(const char *what, int total);
  void Tick(int done);
  void Done(int done);

private:
  std::string m_What;
  int m_Total;
  ULONGLONG m_Start;
  ULONGLONG m_Last;
  bool m_Logged;
};
int Fail(int code, _Printf_format_string_ const char *fmt, ...);
//: A path as this process will actually use it: absolute, with `.` and `..` collapsed, so a relative
//: path works from any directory. Nothing for a NULL or empty path, and the given path when it cannot
//: be resolved at all -- a path that only works from one directory is otherwise indistinguishable from
//: a missing file, which is why the callers print this form rather than what they were given.
std::filesystem::path AbsolutePath(const std::filesystem::path &path);
std::string WorkingDirectory();
//: When a file was last written, or nothing when it is not there. The epoch of a `file_time_type`
//: is the filesystem's business and not the driver's (a raw `FILETIME` count used to be returned
//: and compared, which only worked because Windows is what MSVC builds for), so two of these are
//: compared with `<` and
//: `>` and a difference is turned into seconds at the one place that prints one.
using FileTime = std::filesystem::file_time_type;
std::optional<FileTime> FileWriteTime(const std::filesystem::path &path);
//: The newest file in `dir` whose name ends with one of the `count` suffixes, with its own path
//: when there is one; nothing when there is none. Suffix rather than `path::extension` on purpose:
//: the offline tool's own staleness check (`rdc_driver.SOURCE_SUFFIXES`) matches the same way, and
//: the two have to agree about what a source is. The primitive behind the driver's staleness check,
//: and device-free, so `selftest` pins it.
std::optional<FileTime> NewestSourceTime(const std::filesystem::path &dir,
                                         const char *const *suffixes, int count,
                                         std::filesystem::path &newest);
std::string DefaultLogStem();
//: The log stem for a *library* session: `GetModuleFileName` of the library itself rather than of the
//: host process, because a DLL loaded by `python.exe` would otherwise write `python.exe_<date>.log.txt`
//: next to the interpreter -- a directory the caller may not even be able to write to.
std::string LibraryLogStem();
//: `openedAs` is the name for the *log's own lines* and for the warning when it cannot be opened: a
//: `std::string` because it is printed, not opened (`FileOpen` is handed the path).
FILE *OpenLog(const std::filesystem::path &requested, bool bPerRun, std::string &openedAs);
//: Flush and close the log, and forget it, so a library session leaves one finished file behind.
//: `Log` after this goes nowhere, which is what a closed log means.
void CloseLog();
HMODULE LoadReplayDLL();
bool InitialiseReplay(HMODULE dll, int argc, char **argv);
ICaptureFile *OpenCaptureFile(HMODULE dll);
void PrintCaptureHeader(ICaptureFile *file, const char *path);

//: The container's fixed 32-byte header, as far as the version guard needs it -- RenderDoc's own
//: `FileHeader` (serialise/rdcfile.cpp): `magic "RDOC" | version u32 | headerLength u32 |
//: progVersion 16 bytes, NUL-padded`. The replay API has no accessor for it, so the guard reads it
//: here, exactly as the offline tool reads the same bytes (`rdc_stream.parse_container`).
struct CaptureVersion
{
  uint32_t logfile = 0;    // the logfile format version, which the engine also checks itself
  std::string program;     // "1.46 e4bd23": the RenderDoc that recorded it, and its commit
};

bool ReadCaptureVersion(const std::filesystem::path &path, CaptureVersion &out);
//: `1.46`, `1.46 e4bd23` and `v1.9.2` all name a release; anything else does not, and says so.
bool ParseMajorMinor(const std::string &text, int &major, int &minor);
//: -1 engine older, 0 equal, +1 engine newer; `known` false when either side does not parse, which
//: is deliberately not a verdict -- a guard that guesses is worse than no guard.
int CompareMajorMinor(const std::string &engine, const std::string &capture, bool &known);
//: Refuse a capture recorded by a newer RenderDoc than the engine about to replay it, before the
//: engine is asked to do anything: a `Fail` code (1) when it refuses, 0 otherwise.
//: `why` (nullable) receives the refusal's own sentence instead of it being printed: the CLI passes
//: NULL and gets the message on stderr with exit 1, the library passes a string and returns it
//: through the ABI's `err` (api.h). Same wording either way, written once.
//: `pathAsGiven` is what the *message* uses (`AbsolutePath` collapses `..` and would otherwise hand
//: the reader a path they never typed), and the two are the same file by construction.
int GuardCaptureVersion(const std::filesystem::path &pathAbs,
                        const std::filesystem::path &pathAsGiven, std::string *why = NULL);
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

//: One chunk of the structured file. `eid` is the depth-first index over *chunks* (parameters are
//: descended through without numbering), which is not the engine's event id -- that comes from the
//: action list. One action from the engine's own list, flattened: its id, its name, and the markers
//: it sits inside.
//:
//: `eid` is `ActionDescription::eventId` -- the id `SetFrameEvent`, `probe` and a bundle's ids all
//: use -- and *not* the structured file's chunk numbering, which is a different one (measured: on
//: `desktop-1` the chunk-derived numbers run to millions where the engine's event ids run to
//: 2132, so the two never meet). `flags` is the engine's own classification, so a call is a call
//: and a marker is a marker: nothing here is inferred from a name.
struct ActionNode
{
  int m_Eid = 0;    // ActionDescription::eventId: the id SetFrameEvent, probe and a bundle all use
  int m_Depth = 0;           // how deep in the marker nest, 0 at the root
  bool m_bCall = false;      // a draw/dispatch/copy rather than a marker
  bool m_bMarker = false;    // this row opens a marker (PushMarker/SetMarker)
  rdcstr m_Name;             // the marker's custom name, or the call's own chunk name
  rdcstr m_Path;             // the markers this row sits inside, `A > B`, empty at the root
};

bool IsMarkerPush(ActionFlags flags);
bool IsCallFlags(ActionFlags flags);
std::vector<ActionNode> ActionTree(IReplayController *ctrl, int &calls, bool &bTruncated);
std::map<int, std::string> MarkerPaths(IReplayController *ctrl);
std::string MarkerPathAt(IReplayController *ctrl, int eid);

//: `text` lowercased, for the case-insensitive halves of a search (`find`, a marker path given
//: where an event id is expected). The names being matched are the engine's, which are ASCII.
std::string LowerAscii(std::string_view text);

//: The first call whose marker path answers for `text`, or -1 when nothing does; `matched` receives the
//: path that won, so a caller can print what it resolved to rather than what was typed. Full path
//: first, then a component of one (`BasePass` answers for `Scene > BasePass`), then a substring.
int ResolveMarkerPath(IReplayController *ctrl, const char *text, std::string &matched);

//: One pass, as a range of event ids with the marker path its events sit inside.
//:
//: `FetchCounters` answers per event and there is no event-range parameter (REFERENCE §9), so folding
//: a counter over a pass means folding it over `[first, last]` here. Two ways to get the ranges:
//:
//: * `PassesFromActions`: the engine's own action tree, grouped into maximal runs of consecutive
//:   calls sharing a marker path. A path is `A > B`, so a nested marker is its own pass and a pass
//:   ends where the next one's path begins.
//: * `ReadPassRanges`: a file, one pass per line, `<first> <last> [<name>]`, `#` for a comment.
//:   For a pass list that did not come from this frame's markers -- the offline tool's own
//:   grouping, or a range someone is looking at -- which is why the name is optional and free text.
//:
//: `first`/`last` are inclusive event ids; a pass with no calls never appears.
struct PassRange
{
  std::string m_Name;
  int m_First = 0;
  int m_Last = 0;
};

std::vector<PassRange> PassesFromActions(const std::vector<ActionNode> &rows);
bool ReadPassRanges(const char *path, std::vector<PassRange> &ranges, std::string &why);

//: One counter folded over one pass.
struct PassCost
{
  int m_Index = 0;    // 1-based, matching the terminal's `pass 1`
  std::string m_Name;
  int m_First = 0;
  int m_Last = 0;
  int m_Events = 0;      // calls in the pass, from the action tree
  int m_Measured = 0;    // of those, how many the counter produced a value for
  double m_Sum = 0.0;
  double m_Max = 0.0;
};

std::vector<PassCost> FoldPassCosts(const rdcarray<CounterResult> &results, GPUCounter cost,
                                    CompType resultType, const std::vector<PassRange> &passes,
                                    const std::vector<ActionNode> &rows);

void CollectDispatchKinds(const rdcarray<ActionDescription> &actions, std::map<int, bool> &kinds);
std::map<int, bool> DispatchByEid(IReplayController *ctrl, int &calls);

//: The largest event id in the action tree -- which is the frame's *last* event, not merely its
//: largest action: every driver ends a capture's action list with an "End of Capture" action
//: (`AddEvent()` then `AddAction()`, one such block per driver -- d3d12_commands.cpp has D3D12's),
//: so nothing the engine numbers comes after it and `SetFrameEvent` on any id past it clamps to it.
//: 0 when the action list is empty, which the caller must treat as "no bound is derivable" rather
//: than "the frame has no events" -- the caller falls back to a coarser bound.
int LastEventId(IReplayController *ctrl);

//: Move the replay to `eid`, refreshing even if that id is already current. Every command moves the
//: engine through here, and the reason it is a function rather than 18 calls to
//: `ctrl->SetFrameEvent` is `AnyEventReplayed` below: `probe`'s answer is only trustworthy on a
//: *cold* engine (commands_frame.cpp says why), and a cached answer must only be written when it was.
void MoveToEvent(IReplayController *ctrl, int eid);

//: True once any command has moved the replay. A process that has not moved it yet is the state
//: `probe`'s answer requires -- and the state a probe cache may be written from (see `MoveToEvent`).
bool AnyEventReplayed();

// --------------------------------------------------------------------------- the commands

int CmdInfo(IReplayController *ctrl, ICaptureFile *file, const char *path);
int CmdDraws(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxRows,
             const char *filter);
int CmdFind(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *needle,
            int maxRows);
int CmdState(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid);
int CmdStateDiff(IReplayController *ctrl, ICaptureFile *file, const char *path, int eidA, int eidB);
int CmdBuffer(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *what,
              unsigned long long offset, unsigned long long length, const char *asMode);
int CmdSheet(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *outDir,
             int every, int maxPasses, int tileWidth, bool bList);
//: Two files and a heat map: this one needs no engine, like `bundle-verify`. It still runs inside a
//: session (the command line opens the capture), but nothing about the answer depends on the device.
int CmdImgDiff(ICaptureFile *file, const char *path, const char *aPath, const char *bPath,
               const char *outPath);
int CmdPatch(IReplayController *ctrl, ICaptureFile *file, const char *path,
             const std::vector<std::string> &args);
//: `pixelhistory` and the vocabulary its answers are phrased in. The text helpers are declared here
//: because the device-free selftest checks them directly: a value printed from the engine's
//: "invalid" sentinel, or a rejection reason missing from the list a verdict is built from, is a
//: wrong answer that reading the code does not reveal.
bool CastFromName(std::string_view name, CompType &type);
const char *CastText(CompType type);

//: The `--overlay` vocabulary: RenderDoc's `DebugOverlay`, one spelling per member and the list in the
//: engine's own order (`none` first). The overlay is drawn *into* the picture by the engine's display
//: path, which is the point of it being here rather than in our own image code -- `wireframe` is the
//: topology the frame actually rasterised, and the `quad`/`triangle-size` pairs are the cost hunches
//: (`...Pass` measures over a pass, `...Draw` over one draw, and the difference is why both exist).
bool OverlayFromName(std::string_view name, DebugOverlay &overlay);
const char *OverlayText(DebugOverlay overlay);
std::string OverlayNames(const char *separator);

//: How a picture is asked for, shared by the commands that produce one. One struct rather than six
//: parameters, and one place fills it from the command line (`replay_dump.cpp`), so `main`, a batch
//: file and a library session cannot disagree about what `--mip 2` means -- the same reason
//: `dump`'s options are a struct (bundle.cpp).
//:
//: The display fields (`m_Overlay`, `m_HdrMultiplier`, `m_bGamma`) and the save fields
//: (`m_BlackPoint`, `m_WhitePoint`, `m_bRaw`) are used by the two different paths; a caller sets
//: the ones its path reads.
struct PictureOptions
{
  //: Which subresource to show or save. `mip`/`slice`/`sample` are positions, so a negative one is
  //: a typo, and the parsing refuses it before this struct exists.
  Subresource m_Sub;
  //: What the numbers are read as: the texture's own format unless `--cast` said otherwise. A typeless
  //: texture needs one -- the display path has nothing to show without a type to read it as.
  CompType m_Cast = CompType::Typeless;
  bool m_bCastGiven = false;
  //: The overlay, and whether one was asked for at all (`NoOverlay` is also a valid answer).
  DebugOverlay m_Overlay = DebugOverlay::NoOverlay;
  bool m_bOverlayGiven = false;
  //: The display path's tonemapping, both of them `TextureDisplay` fields rather than ours: a multiplier
  //: for float/HDR content, and whether to read the values as linear and display them as gamma.
  float m_HdrMultiplier = 1.0f;
  bool m_bGamma = false;
  //: The save path's black/white point mapping (`TextureComponentMapping`): the range that becomes
  //: 0..255, which is how a float/HDR texture turns into a file a person can look at.
  float m_BlackPoint = 0.0f;
  float m_WhitePoint = 1.0f;
  //: `--raw`: write the undecoded bytes the engine hands back, not a decoded picture.
  bool m_bRaw = false;
};

//: Set a `TextureSave` from the options the save path shares: the cast (only when one was asked for
//: -- the engine reads the texture's own format otherwise), the subresource, and the black/white
//: point range. One place, so a texture saved by `textures --save`, one saved by `cubemap` and the
//: fallback inside `SaveTargetImage` cannot be three opinions about the same command line.
void ApplySaveOptions(TextureSave &save, const PictureOptions &opts);
std::string PixelValueText(const PixelValue &value, CompType type);
std::string ModificationColorText(const ModificationValue &value, CompType type);
std::string ModificationDepthText(const ModificationValue &value);
std::string ModificationStencilText(const ModificationValue &value);
std::string ModificationValueText(const ModificationValue &value, CompType type);
std::string RejectionText(const PixelModification &mod);
int CmdPixelHistory(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
                    const char *what, unsigned x, unsigned y, const Subresource &sub,
                    CompType typeCast, int maxRows);
int CmdShaders(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
               bool bWantDisasm);
int CmdCbuffer(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
               ShaderStage stage, int slot);

//: One constant block's bytes and the variables over them, at the *current* event: what `CmdCbuffer`
//: prints and what `watch` reads at every event of a range. The two share this rather than the
//: resolution, because a second copy of it is exactly how a `watch` row and a `cb` document would come to
//: disagree about which buffer a block is (`BlockRead`'s own comment, commands_state.cpp).
struct BlockValues
{
  ResourceId m_Buffer = ResourceId::Null();
  uint64_t m_Offset = 0;
  uint64_t m_Length = 0;
  rdcarray<ShaderVariable> m_Variables;
};

//: The reflection of the shader that is actually bound, rather than of the entry point the engine
//: would disassemble by default (`commands_state.cpp` has why that difference is not cosmetic).
const ShaderReflection *BoundReflection(IReplayController *ctrl, const D3D12Pipe::State *st,
                                        ShaderStage stage, const D3D12Pipe::Shader *sh);
void BlockRead(IReplayController *ctrl, const D3D12Pipe::State *st, const ShaderReflection *refl,
               ShaderStage stage, const D3D12Pipe::Shader *sh, int slot, BlockValues &out);

//: `watch <name> [--since A] [--until B] [--max-events N] [--stage <stage>] [--all]`: one reflection
//: member's value at every event of a range, as one row per *change* (commands_watch.cpp).
int CmdWatch(IReplayController *ctrl, ICaptureFile *file, const char *path,
             const std::vector<std::string> &args);
//: `watch`'s two name rules, on their own because both are wrong *quietly*: a substring match would
//: watch `intensityScale` alongside `Light.intensity`, and a case-sensitive one would miss a name
//: the reader typed in their own casing. The selftest pins them on a hand-built reflection tree,
//: which needs no device: `WatchPaths` is the whole collection step, and `WatchNameMatches` is one
//: path's rule.
bool WatchSameName(const std::string &a, const std::string &b);
bool WatchNameMatches(const std::string &path, const std::string &want);
std::vector<std::string> WatchPaths(const rdcarray<ShaderVariable> &vars, const std::string &want);
int CmdTextures(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *filter,
                const char *saveDir, const PictureOptions &opts);
//: A `resId|name` argument, resolved: `<id>` is taken as an id and anything else is matched against
//: the engine's names (case-insensitively, both directions -- `state` and `pixelhistory` have
//: always done it this way, and `cubemap` needs the same answer). `name` receives what it resolved
//: to, for the output, and `why` the refusal when it does not: a caller has to name the resource it
//: could not find, and a wrong id is otherwise indistinguishable from an empty frame.
ResourceId ResolveResourceArg(IReplayController *ctrl, const char *what, std::string &name,
                              std::string &why);
//: The `--stage` vocabulary for `mesh`: `MeshDataStage`, in the engine's own order. The engine
//: aliases `AmpOut` to `TaskOut` (an older name for the same stage), so the text says `taskout` for
//: both and the parse takes either spelling -- one stage, two names, and the document prints the
//: one the enum means.
bool MeshStageFromName(std::string_view name, MeshDataStage &stage);
const char *MeshStageText(MeshDataStage stage);
std::string MeshStageNames(const char *separator);
//: How many primitives a draw covers, from its topology and its vertex (or index) count. 0 means
//: *not derived* rather than none: a strip's primitive count depends on where the strip breaks, and
//: a meshlet list's on the meshlets, so a number there would be a claim this cannot make -- the
//: caller says so in its own words instead (`primitivesNote`). Free of the controller so the
//: arithmetic can be checked.
long long PrimitiveCount(Topology topology, long long count);
//: The per-component extremes of an interleaved vertex stream, over its first three components: `x
//: y z` of the position, whatever else the stride carries. A sanity line, not a claim about the
//: mesh -- a stage whose data is not what a reader assumed shows it here (a meshlet list read as
//: positions is off by orders of magnitude). A vertex whose three components are not all finite is
//: dropped *whole*: one `NaN` would poison every later comparison, and mixing two finite components
//: of a vertex the shader never emitted into a box would be a smaller lie of the same kind.
//: `m_bAny` says whether any vertex was usable at all.
struct Bounds3
{
  float m_Min[3] = {0.0f, 0.0f, 0.0f};
  float m_Max[3] = {0.0f, 0.0f, 0.0f};
  bool m_bAny = false;    // false when no vertex had three finite components
};
Bounds3 VertexBounds(const bytebuf &data, size_t stride, size_t count);
//: `--obj <file>`: the vertices and indices as a Wavefront OBJ, which is what an external viewer
//: reads. `positions` is the interleaved vertex stream and `stride` is its bytes per vertex -- the
//: first three floats of each vertex are taken as its position, because that is the one convention
//: the file format and the post-VS stream share. Returns the number of vertices written, or -1 when
//: the file cannot be written; `indices` may be empty (a non-indexed draw, whose faces are the
//: vertices in order).
long long WriteObj(const std::filesystem::path &path, const bytebuf &positions, size_t stride,
                   size_t count, const std::vector<uint32_t> &indices, Topology topology,
                   std::string &why);
int CmdMesh(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid, int instance,
            int maxRows, MeshDataStage stage, const char *objPath);
int CmdImage(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
             const char *outPath, const PictureOptions &opts);
//: A cubemap as six pictures plus the engine's own cruciform: `<outDir>/face0.png` .. `face5.png`
//: (the D3D order: +X, -X, +Y, -Y, +Z, -Z) and `<outDir>/cross.png`. The cruciform is
//: `TextureSliceMapping`'s `cubeCruciform`, which is the engine drawing the classic unfolded cross
//: into transparent black -- so there is no layout code here to get the rotations wrong.
int CmdCubemap(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *what,
               const char *outDir, const PictureOptions &opts);
//: The format coverage audit: every format in the frame's texture list, how many resources use it, what it
//: is made of, and whether the engine can decode it -- with the reason when it cannot (REFERENCE §9).
int CmdFormats(IReplayController *ctrl, ICaptureFile *file, const char *path);
//: The two halves of that audit, free of the controller (a `ResourceFormat` is a value): what a
//: format is made of, as one line, and whether the engine can make a picture of it -- with
//: `bNeedsCast` for the typeless case, where the answer is "not without being told how", and `why`
//: for the reason either way.
std::string FormatShape(const ResourceFormat &format);
bool FormatPicture(const ResourceFormat &format, bool &bNeedsCast, std::string &why);
int CmdCounters(IReplayController *ctrl, ICaptureFile *file, const char *path, bool bPerPass,
                const char *passesPath, int topN);
//: The cross-checks (REFERENCE §9): what the reflections say a shader wants against what the state says
//: it was given. Deterministic, because both sides are in the capture -- no heuristic and no guess.
//:
//: `SignatureLinkText` is declared here because the device-free selftest checks it directly: an
//: empty return meaning "this links" is the one thing a reader cannot see from the call site.
std::string SignatureLinkText(const SigParameter &written, const SigParameter &read);
int CmdCrosscheck(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid, int since,
                  int until, int maxEvents, int maxRows);
//: One debug message's identity and reach: what makes two of them "the same message", and what they
//: did over the frame.
//:
//: The engine gives every distinct message a `messageID` (its hash of the text and where it came
//: from), which is the whole reason this can be a table rather than a wall -- a validation error
//: repeated at 400 events is one row with a count. Severity, category and source are part of the
//: identity rather than decoration: the same id reported at another severity is another finding.
//: `firstEid`/`lastEid` turn "somewhere in the frame" into a range, and `text` is the first
//: description seen -- the engine's id is derived from the text, so every member of a group carries
//: the same one.
struct DebugGroup
{
  MessageSeverity severity = MessageSeverity::Info;
  MessageCategory category = MessageCategory::Undefined;
  MessageSource source = MessageSource::API;
  uint32_t messageID = 0;
  uint32_t firstEid = 0;
  uint32_t lastEid = 0;
  uint32_t count = 0;
  std::string text;
};

//: The messages folded into groups, most severe first, then the loudest, then the earliest: the
//: order a reader triages in, and stable for equal keys so two runs print the same table.
std::vector<DebugGroup> GroupDebugMessages(const rdcarray<DebugMessage> &messages);
int CmdDebug(IReplayController *ctrl, ICaptureFile *file, const char *path,
             const std::vector<std::string> &args);
int CmdUsage(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *what);
int CmdProbe(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxEid);
int CmdBatch(IReplayController *ctrl, ICaptureFile *file, const char *path,
             const std::filesystem::path &batchPath);

// --------------------------------------------------------------------------- the CLI (replay_dump.cpp)
//
// One line of the command language, and one command run from it, in the module that owns the syntax.
// They are declared here because they have *three* callers now -- `batch`, `--repl` and the library
// (api.cpp) -- and a second copy of the syntax is exactly how a batch line and a library line would
// come to mean different things.

//: Splits one line into arguments, taking the options out as it goes. Quoted tokens group, which
//: `--save` and `image` need for a path with spaces; `--json` and `--disasm` come back as flags
//: rather than as arguments, and `--save`'s value in `saveDir`.
void SplitLine(const std::string &line, std::vector<std::string> &args, bool &bJson, bool &bDisasm,
               std::string &saveDir);
//: `text` without a leading UTF-8 BOM, which an editor's "UTF-8 with BOM" save leaves in a script.
std::string WithoutBom(const std::string &text);
//: One command, from its own name onwards (`args[0]` is the command, never the capture). Prints the
//: command's document to stdout and returns its exit code -- 2 for an unknown command, which is
//: also what a caller that spelled the command wrong needs to see.
int DispatchCommand(IReplayController *ctrl, ICaptureFile *file, const char *path,
                    const std::vector<std::string> &args, bool bWantDisasm, const char *saveDir);
//: One warning when the RenderDoc source tree is not where the chunk names come from, said once per
//: process; the library says it too, because a session's ids are named the same way a run's are.
void WarnIfRenderdocSrcMissing();

bool SaveTargetImage(IReplayController *ctrl, ResourceId target, const char *outBase,
                     const PictureOptions &opts, std::string &written, int32_t &width,
                     int32_t &height);

// --------------------------------------------------------------------------- the bundle (bundle.cpp)

//: The sweep's stop rules, on their own so they can be pinned without a capture: the serial sweep
//: applies them as it walks (bundle.cpp `SweepForEvents`), and the selftest feeds them shapes that
//: trip each rule. They were shared with a parallel sweep once -- see bundle.cpp's "why the sweep
//: is not parallel" for how that turned out -- and what remains is the rules themselves, which are
//: the sweep's contract in one place.
struct SweepRules
{
  enum Step
  {
    Continue,
    StopEmptyRun,
    StopMaxEvents,
    StopIdBudget
  };

  static const int kEmptyRunStop = 256;    // consecutive ids with nothing bound that end a sweep

  int m_EmptyRun = 0;
  int m_LastEid = 0;
  size_t m_Count = 0;
  int m_MaxEvents = 0;      // `--max-events`, 0 = no cap
  size_t m_IdBudget = 0;    // the scan bound: the frame's last event id, or the chunk count

  SweepRules(int maxEvents, size_t idBudget) : m_MaxEvents(maxEvents), m_IdBudget(idBudget) {}

  //: One id's answer folded into the running state. A with-state id is recorded *before* the caps
  //: are tested, so the id that trips a cap is collected and is the last one -- the serial loop did
  //: the same, and `lastEid`/`scanned` are compared against it.
  Step Feed(bool bHasState, int eid);
};

int CmdDump(IReplayController *ctrl, ICaptureFile *file, const char *path,
            const std::vector<std::string> &args, bool bWantDisasm);
int CmdBundleVerify(const char *dir);

//: One row of `probe`'s answer: an id with pipeline state, and the three things the row prints.
//: Kept as data rather than as a formatted line so the cache can hold it and reprint it unchanged.
struct ProbeRow
{
  int m_Eid = 0;
  int m_Shaders = 0;
  std::string m_RootSig;
  int m_Params = 0;
};

//: Every id with state over a scanned prefix, in id order. `m_Scanned` is how far that prefix
//: reaches: ids 1..`m_Scanned` were swept *in order*, which is what makes the rows an answer to
//: `probe <m_Scanned>` and not merely to the ids they name (see bundle.cpp's probe cache).
struct ProbeCache
{
  std::vector<ProbeRow> m_Rows;
  int m_Scanned = 0;
};

//: How often a probe's scan is flushed to its cache file, in ids: a sweep of a five-figure frame is
//: minutes of `SetFrameEvent`, and a run killed at minute twenty should keep what it established
//: (bundle.cpp's probe cache says why that prefix is still a valid answer).
const int kProbeFlushEvery = 1024;

//: The id `probe` should scan to: the caller's cap, the frame's own last event when that is
//: smaller, or the whole frame for `probe <rdc> last` (a cap of 0 means "the bound, whatever it
//: is"). Declared here because the device-free selftest pins the arithmetic -- the difference
//: between "the frame" and "two thousand ids" is the difference between an answer and a silently
//: empty one on a five-figure frame.
int ProbeUntil(int cap, int lastEvent);

//: The probe cache's file path for this capture, or an empty string when caching is off or the
//: engine version is not known. Lives with the sweep cache (bundle.cpp) because it is the same
//: idea, the same directory and the same text format.
std::filesystem::path ProbeCachePath(const std::filesystem::path &path);
bool ReadProbeCache(const std::filesystem::path &cachePath, const std::filesystem::path &path,
                    int lastEvent, ProbeCache &out);
void WriteProbeCache(const std::filesystem::path &cachePath, const std::filesystem::path &path,
                     int lastEvent, const ProbeCache &cache);

//: The small file helpers the bundle (and the self-check reading a schema off disk) share. Each
//: takes a `std::filesystem::path`: a path is the thing these operate on, and a `std::string` is
//: how one is *printed* -- `.string()` at the `printf` is the only way back, which is what keeps a
//: path from being taken apart and rebuilt by hand (a `"\\"` between two halves is a path separator
//: only on Windows, and only when neither half already ends in one). Opens a file for the
//: byte-level work below: the one place a path becomes a `FILE *`. The CRT's narrow `fopen` reads
//: its bytes in the machine's ANSI codepage while a `path` carries the form Windows actually uses,
//: so opening through the path is what keeps the two consistent -- a path `std::filesystem` can see
//: is a path this can open. `mode` is a narrow ASCII mode (`"rb"`, `"wb"`, `"wx"`); NULL when it
//: fails.
FILE *FileOpen(const std::filesystem::path &path, const char *mode);
std::string Sha256File(const std::filesystem::path &path);
//: The same digest over a buffer in memory: `shaders` identifies a shader by its bytes with this.
std::string Sha256Bytes(const void *data, size_t size);
bool ReadWholeFile(const std::filesystem::path &path, std::string &text);
bool FileBytes(const std::filesystem::path &path, unsigned long long &bytes);
//: The folder, and every one above it that is not there yet: a destination a caller names is one to make
//: (`dump cap.rdc out/frames/cap1`, `sheet cap.rdc shots/frame12`, `patch ... out/tries/fix1`) rather than
//: one to prepare by hand. False when a component cannot be created, which is what the callers'
//: `cannot create <what>` messages report. An existing folder is success, and so is a trailing separator.
bool MakeDir(const std::filesystem::path &path);
//: Removes a file or an empty folder and ignores why it could not be, for the places that are
//: cleaning up scratch of their own: "already gone" is the state the caller wanted. Anything that
//: has to know why uses `std::filesystem::remove` with its own `error_code`.
void RemoveQuiet(const std::filesystem::path &path);
//: Whether a path exists, with the error dropped. `exists` has a throwing overload and the driver
//: never lets an exception out of a helper: a directory that cannot be read is "not there" to a
//: caller cleaning up after itself, and anything that has to tell those apart asks with its own
//: `error_code`.
bool ExistsQuiet(const std::filesystem::path &path);
//: True when the folder could be read and holds nothing; false when it could not be read at all,
//: which the caller has to tell apart from "empty" before it writes into it.
bool DirIsEmpty(const std::filesystem::path &path, bool &bEmpty);
//: A path inside the bundle, relative to its root and with forward slashes, so the manifest reads
//: the same whichever way the root was spelled.
std::string BundleRelative(const std::filesystem::path &root, const std::filesystem::path &full);

// --------------------------------------------------------------------------- images (image.cpp)

//: A decoded image: 8-bit RGBA, top-down (the order the engine's readback and the BMP writer use).
struct ImageData
{
  int m_Width = 0;
  int m_Height = 0;
  bytebuf m_Rgba;

  bool Valid() const;

  //: Exactly `count` pixels, every byte `value`. `rdcarray` (which `bytebuf` is) has neither
  //: `assign` nor a two-argument `resize`: clear, size, then fill.
  void Reset(size_t count, uint8_t value = 255);
};

//: A 24-bit BMP: the writer behind `image`, the bundle's `rt/` images, the contact sheet and the
//: difference map. `ReadBMPImage` takes the uncompressed 24/32-bit files back in, ours and a viewer's.
bool WriteBMP(const char *path, const bytebuf &rgba, int32_t width, int32_t height);
bool WriteBMPImage(const char *path, const ImageData &img);
bool ReadBMPImage(const char *path, ImageData &img, std::string &why);
ImageData DownscaleImage(const ImageData &src, int maxWidth, int maxHeight);
ImageData MakeMontage(const std::vector<ImageData> &tiles, int columns, int tileWidth,
                      int tileHeight, int gutter);
uint64_t DifferenceHash(const ImageData &img);
long long ImagePixelDelta(const ImageData &a, const ImageData &b, int &maxDelta,
                          long long &sumDelta, ImageData *heat);
bool ReadTargetImage(IReplayController *ctrl, ResourceId target, const PictureOptions &opts,
                     ImageData &img, std::string &why);
ResourceId FirstRenderTarget(const D3D12Pipe::State *st);

// --------------------------------------------------------------------------- self-check (selftest.cpp)

int CmdSchema(const char *name, const char *outDir, const char *checkDir);
int CmdSelftest();
int CheckSchemasAgainstDir(const std::string &dir, bool bReport);
int WriteSchemasTo(const std::string &dir, const char *name, bool bReport);
bool ReadSchemaText(const std::string &path, std::string &text);
