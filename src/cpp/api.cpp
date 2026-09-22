// z.api — the driver as a library: the C ABI's implementation (the contract is in api.h).
//
// Part of replay_dump; the ABI's declarations are in api.h, its contract in REFERENCE 9, and its Python
// caller is `src/py/rdc_replay.py`.
//
// This file owns two things that live for different lengths of time, and keeping them apart is the
// whole design:
//
//  * **the replay system and the log are the process's** -- RenderDoc's own rule, and the first version
//    of this file learned it the hard way: a session that shut the system down and a second session that
//    initialised it again faulted inside the teardown (the first close was clean, every one after it
//    crashed, which is what "once per process" means when it is ignored). They are built on the first
//    `RdcReplayOpen` and torn down when this DLL unloads, and the app that ships with RenderDoc opens
//    captures the same way: one system, many captures;
//  * **a capture file and its controller are the session's**, opened and closed per handle, which is
//    what makes a session a resource a caller can hold across many commands.
//
// It deliberately does none of the work of a command: `DispatchCommand` is the dispatcher main and
// `batch` use, so a library line and a batch line cannot drift apart.

#include "api.h"
#include "common.h"

#include <fcntl.h>
#include <io.h>
#include <functional>
#include <optional>

namespace
{
//: The process's replay system, its DLL and its log. Built on the first session, destroyed when this
//: DLL unloads -- which is the one place a shutdown belongs when the system is once per process.
struct Module
{
  HMODULE m_Dll = NULL;
  bool m_bSystemUp = false;
  bool m_bLogOpen = false;

  ~Module()
  {
    if(m_bSystemUp)
    {
      Trace("shutting the replay system down (the process is done with it)");
      if(g_ShutdownReplay != NULL)
        g_ShutdownReplay();
      m_bSystemUp = false;
    }
    CloseLog();
  }
};

Module &Process()
{
  static Module module;
  return module;
}

//: One open session: the capture and its controller, and the path **as the caller gave it** -- every
//: document prints that, so a library answer and a command-line answer to the same question are the
//: same text. The two guards are `optional` because each is built by the step that has something to
//: guard: a session that cannot open its capture must not shut down a controller it never had.
struct Session
{
  std::optional<CaptureFileGuard> m_File;
  std::optional<ControllerGuard> m_Controller;

  ICaptureFile *m_pFile = NULL;
  IReplayController *m_pController = NULL;
  std::string m_Path;
};

void SetErr(char *err, int errLen, const char *fmt, ...)
{
  if(err == NULL || errLen <= 0)
    return;
  va_list args;
  va_start(args, fmt);
  vsnprintf(err, (size_t)errLen, fmt, args);
  va_end(args);
}

//: A unique file for one command's stdout. `%TEMP%` rather than the cache directory: this is
//: scratch that lives for one call, and the cache is a place a reader is meant to be able to trust.
std::filesystem::path TempOutputPath()
{
  static int counter = 0;
  // `temp_directory_path` is `%TMP%`, then `%TEMP%`, then `%USERPROFILE%` -- the same order
  // `GetTempPath` uses. A machine with none of them set gets the working directory, where the file
  // is at least somewhere the caller has already been told about.
  std::error_code ec;
  const std::filesystem::path base = std::filesystem::temp_directory_path(ec);
  return (ec ? std::filesystem::path(".") : base) /
         Fmt("rdc_replay_%lu_%d.out", (unsigned long)GetCurrentProcessId(), ++counter);
}

//: The whole file as a `malloc`'d NUL-terminated buffer, or NULL when it cannot be read. The caller
//: frees it with `RdcReplayFree`, which is `free`.
char *ReadWholeFileMalloc(const std::filesystem::path &path)
{
  FILE *f = FileOpen(path, "rb");
  if(f == NULL)
    return NULL;
  std::string text;
  char buffer[65536];
  size_t got = 0;
  while((got = fread(buffer, 1, sizeof(buffer), f)) > 0)
    text.append(buffer, got);
  fclose(f);

  char *out = (char *)malloc(text.size() + 1);
  if(out == NULL)
    return NULL;
  memcpy(out, text.data(), text.size());
  out[text.size()] = '\0';
  return out;
}

//: Run one command with its stdout captured, and return the document the way the command line would
//: have written it. Two details are the whole function, and both are about *equality* with the CLI:
//:
//:  * `_setmode(_O_TEXT)` right after `CaptureStdout` has taken stdout over. It `_dup2`s a file
//:  opened
//:    `"wb"` onto stdout, and `_dup2` gives the target the source's mode -- binary, so nothing
//:    would be translated and the document would carry LF where the command line carries CRLF.
//:    Measured before the fix: the same `info` was 435 bytes on the command line and 418 here, one
//:    byte per line. The console's own mode comes back with `CaptureStdout`'s restore, which dup2s
//:    the saved descriptor back; the bundle's documents are written the same way and stay LF, which
//:    is what they have always been (`capture.json` in a bundle: 0 CRLF, 21 lines);
//:  * the file is read back *after* the scope ends, so the last flush has happened.
char *RunCaptured(const std::function<int()> &body, int *code)
{
  const std::filesystem::path path = TempOutputPath();
  {
    const CaptureStdout capture(path);
    if(!capture.Ok())
    {
      *code = 1;
      return NULL;
    }
    _setmode(_fileno(stdout), _O_TEXT);
    *code = body();
  }
  char *text = ReadWholeFileMalloc(path);
  RemoveQuiet(path);    // best effort: a scratch file left behind is not a failed call
  return text;
}

//: Bring the process's replay system up, once. `logPath` names the log for the whole process (NULL
//: means the per-run name beside this DLL), and only the call that actually built the system uses
//: it: the log and the system are both the process's, so a second session logs into the running
//: file rather than opening a second one. Returns false and fills `err` when it cannot come up.
bool OpenEngine(const char *logPath, std::string *err)
{
  Module &module = Process();
  if(module.m_bSystemUp)
  {
    Log("library session on the running replay system (%s)", RDC_REPLAY_ABI);
    return true;
  }

  const bool bDefaultLog = logPath == NULL || *logPath == '\0';
  const std::string stem = bDefaultLog ? LibraryLogStem() : std::string(logPath);
  std::string openedAs;
  g_LogFile = OpenLog(stem, bDefaultLog, openedAs);
  module.m_bLogOpen = true;
  if(g_LogFile != NULL)
    Log("log file: %s", openedAs.c_str());
  Log("library session: %s", RDC_REPLAY_ABI);

  Log("loading renderdoc.dll");
  module.m_Dll = LoadReplayDLL();
  if(module.m_Dll == NULL)
  {
    *err = "cannot load renderdoc.dll (--dll names one, or $RDC_RENDERDOC_DLL)";
    return false;
  }

  Log("starting the replay system");
  if(!InitialiseReplay(module.m_Dll, 0, NULL))
  {
    *err = "RENDERDOC_InitialiseReplay failed";
    return false;
  }
  module.m_bSystemUp = true;
  return true;
}

Session *AsSession(void *handle)
{
  return (Session *)handle;
}
}    // namespace

const char *RdcReplayAbi(void)
{
  return RDC_REPLAY_ABI;
}

void *RdcReplayOpen(const char *capturePath, const char *logPath, char *err, int errLen)
{
  const bool bWithCapture = capturePath != NULL && *capturePath != '\0';
  std::string why;

  if(!OpenEngine(logPath, &why))
  {
    SetErr(err, errLen, "%s", why.c_str());
    return NULL;
  }

  std::filesystem::path pathAbs;
  if(bWithCapture)
  {
    WarnIfRenderdocSrcMissing();
    Log("working directory: %s", WorkingDirectory().c_str());
    pathAbs = AbsolutePath(capturePath);

    // Before the capture is opened and the device created, exactly as the CLI does: a capture recorded
    // by a newer RenderDoc is refused by name rather than handed to an engine that does not know it.
    if(GuardCaptureVersion(pathAbs, capturePath, &why) != 0)
    {
      SetErr(err, errLen, "%s", why.c_str());
      return NULL;
    }
  }

  Session *session = new(std::nothrow) Session;
  if(session == NULL)
  {
    SetErr(err, errLen, "out of memory opening the session");
    return NULL;
  }
  if(bWithCapture)
    session->m_Path = capturePath;
  else
    return session;    // capture-free: `schema` is the command this session is for

  Trace("RENDERDOC_OpenCaptureFile");
  ICaptureFile *file = OpenCaptureFile(Process().m_Dll);
  if(file == NULL)
  {
    delete session;
    SetErr(err, errLen, "renderdoc.dll answered no capture file");
    return NULL;
  }
  session->m_File.emplace(file);
  session->m_pFile = file;

  // The engine's ABI is `rdcstr`-based, so the narrow form goes across the boundary and the same
  // bytes are handed over that the driver resolved: `pathAbs` is the path, and this is where it
  // becomes text again.
  const std::string pathAbsText = pathAbs.string();
  Log("reading the container of %s", pathAbsText.c_str());
  Trace("OpenFile");
  const ResultDetails res = file->OpenFile(pathAbsText.c_str(), "rdc", NULL);
  if(!res.OK())
  {
    // The message is copied out before anything unwinds: RenderDoc's own documentation says a result's
    // text lives only until the replay system is shut down, so it is not a string to hold on to.
    const std::string text = ResultText(res);
    delete session;
    SetErr(err, errLen, "cannot open %s: %s", pathAbsText.c_str(), text.c_str());
    return NULL;
  }

  Log("opening the capture and creating the replay device (this is the slow part; if another "
      "replay "
      "is running, that is why)");
  ReplayOptions opts;
  memset(&opts, 0, sizeof(opts));
  const rdcpair<ResultDetails, IReplayController *> opened = file->OpenCapture(opts, NULL);
  if(!opened.first.OK())
  {
    const std::string text = ResultText(opened.first);
    delete session;
    SetErr(err, errLen, "cannot replay %s: %s", capturePath, text.c_str());
    return NULL;
  }
  session->m_Controller.emplace(opened.second);
  session->m_pController = opened.second;
  Log("replay ready");
  return session;
}

int RdcReplayCommand(void *handle, const char *line, char **out, char *err, int errLen)
{
  if(out != NULL)
    *out = NULL;
  Session *session = AsSession(handle);
  if(session == NULL)
  {
    SetErr(err, errLen, "no session");
    return 2;
  }
  if(line == NULL)
  {
    SetErr(err, errLen, "no command");
    return 2;
  }

  const std::string given(line);
  const size_t first = given.find_first_not_of(" \t");
  const std::string text =
      first == std::string::npos ? std::string() : WithoutBom(given.substr(first));
  std::vector<std::string> args;
  bool bJson = false, bDisasm = false;
  std::string saveDir;
  SplitLine(text, args, bJson, bDisasm, saveDir);
  if(args.empty())
  {
    SetErr(err, errLen, "empty command");
    return 2;
  }

  Log("library command: %s", text.c_str());
  const ULONGLONG started = Millis();
  int code = 0;

  if(session->m_pController == NULL)
  {
    // A capture-free session answers exactly the commands that are about this program rather than
    // about a frame -- the same rule `main` applies before it looks for a capture path.
    if(args[0] != "schema" || args.size() > 2)
    {
      SetErr(err, errLen, "'%s' needs a capture: this session was opened without one",
             args[0].c_str());
      return 2;
    }
    const char *name = args.size() > 1 ? args[1].c_str() : NULL;
    if(out == NULL)
      code = CmdSchema(name, NULL, NULL);
    else
      *out = RunCaptured([name]() { return CmdSchema(name, NULL, NULL); }, &code);
  }
  else
  {
    g_bJson = bJson;
    // The command's document is written where `printf` would have gone, then read back: the
    // commands print, and a library that asked them not to would be a second output path to keep in
    // step with the writer. With `out` NULL there is nothing to read back and the document goes to
    // the caller's own stdout, which is what a caller that wants to *watch* a command asks for.
    const char *save = saveDir.empty() ? NULL : saveDir.c_str();
    if(out == NULL)
      code = DispatchCommand(session->m_pController, session->m_pFile, session->m_Path.c_str(),
                             args, bDisasm, save);
    else
      *out = RunCaptured(
          [&]() {
            return DispatchCommand(session->m_pController, session->m_pFile,
                                   session->m_Path.c_str(), args, bDisasm, save);
          },
          &code);
  }

  if(out != NULL && *out == NULL)
  {
    SetErr(err, errLen, "cannot read back the command's output");
    return 1;
  }
  Log("library command done: exit %d after %.1fs", code, (Millis() - started) / 1000.0);
  return code;
}

void RdcReplayClose(void *handle)
{
  Session *session = AsSession(handle);
  if(session == NULL)
    return;
  // The capture and its controller are the session's, so `delete` closes them -- controller first,
  // then capture file, which is the order the engine builds them in. The replay system stays up: it
  // is the process's, and a second session is allowed to open another capture on it.
  delete session;
}

void RdcReplayFree(void *block)
{
  free(block);
}
