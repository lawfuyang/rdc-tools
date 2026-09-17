// z.main — the entry point: options, dispatch, help
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

// replay_dump — headless RenderDoc replay as a data source (REFERENCE §9).
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

REPLAY_PROGRAM_MARKER();

void Usage()
{
  printf(
      "replay_dump - headless RenderDoc replay as a data source (REFERENCE §9)\n"
      "\n"
      "usage: replay_dump <command> <capture.rdc> [args] [--json]\n"
      "\n"
      "  info    <rdc>                     renderdoc version, driver, API properties, counts\n"
      "  draws   <rdc> [max=80] [filter]   the action tree with event ids (markers and calls)\n"
      "  state   <rdc> <eid>               bound shaders, outputs and D3D12 root parameters\n"
      "  shaders <rdc> <eid> [--disasm]    reflection: cbuffers, bindings, signatures, "
      "disassembly\n"
      "  cb      <rdc> <eid> <stage> <slot> named values of one constant buffer\n"
      "  textures <rdc> [filter] [--save <dir>]   texture list; --save decodes to PNG\n"
      "  mesh    <rdc> <eid> [instance] [max]     post-VS vertices for one instance\n"
      "  image   <rdc> <eid> <out.bmp>     the texture display at that event, as a BMP\n"
      "  counters <rdc>                    available GPU counters and their values\n"
      "  debug   <rdc>                     debug messages (validation layer, etc.)\n"
      "  usage   <rdc> <resId>             every event that touches a resource\n"
      "  probe   <rdc> [maxEid=2000]       which event ids actually have pipeline state\n"
      "  dump    <rdc> [outDir=bundle]     the whole frame to disk, for the offline tool (ROADMAP "
      "§1)\n"
      "  bundle-verify <dir>               check a bundle's hashes and sizes (no device, no DLL)\n"
      "  batch   <rdc> <file>              run every command in <file> against one open capture\n"
      "  schema  [<name>] [--out|--check <dir>]   the JSON Schema for each --json document (no "
      "capture, no DLL)\n"
      "  selftest                          this program checking itself: writer, schemas, help, "
      "DLL\n"
      "\n"
      "Options (any position): --json, --log <file>, --disasm, --save <dir>, --out <dir>, --check "
      "<dir>.\n"
      "\n"
      "A batch file holds one command per line, in the same syntax minus the executable and the\n"
      "capture (`state 270 --json`), with `#` for comments. Each line's output is preceded by a\n"
      "`#=== <line>` marker so a stream can be split again. This is the cheap way to run many\n"
      "commands: opening a capture and standing the replay engine up costs ~3 s on a small "
      "capture\n"
      "and ~10 s on a 1.4 GB one, and batch pays it once for the whole file.\n"
      "\n"
      "`dump` writes a bundle for the offline tool (ROADMAP §1): events.json for every id with "
      "bound\n"
      "state, states/<eid>.state.json and .shaders.json for the first event and every state "
      "change, one\n"
      "cbuffers/ file per constant block, resources.json with each resource's usage list, and\n"
      "messages.json. The manifest lists every file with its byte count and SHA-256, and it names "
      "what\n"
      "the bundle cannot contain (the replay API exposes no action list, so a call's kind, its "
      "counts and\n"
      "its marker are not in it). `--with-images` adds rt/ images at the state events, "
      "`--textures`\n"
      "decodes every texture to PNG (full size: the engine decodes but does not resize), "
      "`--with-counters`\n"
      "fetches the counters. `--since`/`--until` pin the id range (the default scans until 256 ids "
      "in a row\n"
      "have nothing bound), `--max-events` caps how many events are written, `--events 270,452` "
      "forces extra\n"
      "state files, `--no-usage` skips the usage lists (the slow part) and `--overwrite` reuses a "
      "folder.\n"
      "`bundle-verify <dir>` re-hashes a bundle with no device involved, so it can be checked "
      "anywhere.\n"
      "\n"
      "Every --json document carries `schemaVersion` (1 today), and `schema` prints the JSON "
      "Schema for\n"
      "each kind: `schema --out schema` writes the checked-in copies, and the offline tool "
      "validates real\n"
      "documents against those (`python src\\py\\rdc_analysis.py validate <bundle> schema`). "
      "`schema "
      "--check <dir>`\n"
      "compares that folder with this driver and exits non-zero when they disagree, which is what "
      "keeps a\n"
      "committed copy from going stale after a document changes. `selftest` needs no\n"
      "capture: it checks the JSON writer (escaping, separators, balance), the schema table, this "
      "help\n"
      "text and the renderdoc.dll it would load, and it skips the DLL checks rather than failing "
      "when\n"
      "RenderDoc is not installed.\n"
      "\n"
      "Progress goes to stderr and to one log file per run, <exe name>_<date>_<time>.log.txt "
      "beside\n"
      "the executable (--log <file> names one exact file and truncates it); the timings in it are\n"
      "what to read when a run looks stuck. Event ids are the engine's, and they are not the "
      "offline\n"
      "tool's chunk indices: `probe` lists the ids that actually have pipeline state.\n"
      "$RDC_RENDERDOC_DLL overrides the renderdoc.dll to load (default: the installed one) and\n"
      "$RDC_REPLAY_DEBUG=1 traces every step, for when the engine takes the process down. Run one\n"
      "replay at a time: the engine creates a device per process, and two at once on one GPU is "
      "what\n"
      "makes it look stuck.\n");
}

//: A command-line integer, validated: `atoi` answers 0 for anything that is not a number, and 0
//: silently means "no limit" for a row count and "event 0" for an event id, so a typo changed what
//: the command did rather than failing. An unparsable argument is reported and the default is used.
const char *kJsonFlag = "--json";
const char *kDisasmFlag = "--disasm";
const char *kSaveFlag = "--save";
const char *kLogFlag = "--log";

void Usage();

//: Runs one command against an already-open capture. Shared by `main` and `batch`, so a command
//: name and its arguments mean the same thing however they were spelled.
int DispatchCommand(IReplayController *ctrl, ICaptureFile *file, const char *path,
                    const std::vector<std::string> &args, bool bWantDisasm, const char *saveDir)
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
    return CmdShaders(ctrl, file, path, ToInt(args[1], 0), bWantDisasm);
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
    return CmdDump(ctrl, file, path, args, bWantDisasm);
  if(!strcmp(cmd, "bundle-verify") && args.size() > 1)
    return CmdBundleVerify(args[1].c_str());

  Fail(2, "unknown command '%s' (or missing arguments)", cmd);
  Usage();
  return 2;
}

//: Splits a command line into arguments, taking the options out as it goes. Double quotes group a
//: token, which `--save` and `image` need for a path with spaces.
void SplitLine(const std::string &line, std::vector<std::string> &args, bool &bJson, bool &bDisasm,
               std::string &saveDir)
{
  std::string token;
  bool bQuoted = false;
  bool bWantSaveDir = false;
  for(size_t i = 0; i <= line.size(); i++)
  {
    const char c = (i < line.size()) ? line[i] : ' ';
    if(c == '"')
    {
      bQuoted = !bQuoted;
      continue;
    }
    if(!bQuoted && (c == ' ' || c == '\t'))
    {
      if(token.empty())
        continue;
      if(token == kJsonFlag)
        bJson = true;
      else if(token == kDisasmFlag)
        bDisasm = true;
      else if(token == kSaveFlag)
        bWantSaveDir = true;
      else if(bWantSaveDir)
      {
        saveDir = token;
        bWantSaveDir = false;
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
int CmdBatch(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *batchPath)
{
  FILE *f = fopen(batchPath, "rb");
  if(f == NULL)
    return Fail(2, "cannot read batch file %s", batchPath);

  int ret = 0, ran = 0;
  bool bSawProbe = false, sawOther = false;
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
      bool bJson = false, bDisasm = false;
      std::string saveDir;
      SplitLine(line.substr(first), args, bJson, bDisasm, saveDir);

      // `probe` forces non-events, and a forced non-event leaves the last real event's state in
      // place; whichever ran second, one of the two answers would be wrong. It belongs in its own
      // run, and saying so here is cheaper than explaining a mysteriously different answer.
      const bool bIsProbe = !args.empty() && args[0] == "probe";
      bSawProbe = bSawProbe || bIsProbe;
      sawOther = sawOther || !bIsProbe;

      // The marker is what lets a caller split the stream back into one output per command. It is
      // printed in both formats: JSON has no comment syntax, and guessing where one object ends and
      // the next begins is not something a consumer should have to do.
      printf("#=== %s\n", line.substr(first).c_str());
      fflush(stdout);

      g_bJson = bJson;
      const ULONGLONG started = Millis();
      const int code =
          DispatchCommand(ctrl, file, path, args, bDisasm, saveDir.empty() ? NULL : saveDir.c_str());
      ran++;
      ret = (code != 0) ? code : ret;
      Log("batch %d: %s -> exit %d in %.1fs", ran, line.substr(first).c_str(), code,
          (Millis() - started) / 1000.0);
    }

    if(ch == EOF)
      break;
    line.clear();
  }

  if(bSawProbe && sawOther)
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
  g_Start = Millis();    // progress timings are relative to this

  std::vector<std::string> args;
  bool bWantDisasm = false;
  const char *saveDir = NULL;
  std::string logPath = DefaultLogStem();
  bool bPerRunLog = true;     // until `--log` names one exact file
  std::string schemaOut;      // `--out <dir>`: where `schema` writes them
  std::string schemaCheck;    // `--check <dir>`: the copy to verify against
  for(int i = 1; i < argc; i++)
  {
    if(!strcmp(argv[i], kJsonFlag))
      g_bJson = true;
    else if(!strcmp(argv[i], kDisasmFlag))
      bWantDisasm = true;
    else if(!strcmp(argv[i], kSaveFlag) && i + 1 < argc)
      saveDir = argv[++i];
    else if(!strcmp(argv[i], kLogFlag) && i + 1 < argc)
    {
      logPath = argv[++i];
      bPerRunLog = false;
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

  // `schema` and `selftest` are about this program rather than about a frame: no capture path, no
  // device, and no log file -- they answer before anything is loaded, so a machine with no
  // RenderDoc and no GPU still gets the half of the selftest that does not need them.
  if(!strcmp(args[0].c_str(), "schema"))
    return CmdSchema(args.size() > 1 ? args[1].c_str() : NULL,
                     schemaOut.empty() ? NULL : schemaOut.c_str(),
                     schemaCheck.empty() ? NULL : schemaCheck.c_str());
  if(!strcmp(args[0].c_str(), "selftest"))
    return CmdSelftest();

  const char *cmd = args[0].c_str();
  if(args.size() < 2)
  {
    fprintf(stderr, "error: %s needs a capture path\n", cmd);
    return 2;
  }

  // Checking a bundle re-hashes files: no DLL, no device, no capture. It runs here, before anything
  // is loaded, so a bundle can be verified on a machine where RenderDoc is not even installed.
  if(!strcmp(cmd, "bundle-verify"))
    return CmdBundleVerify(args[1].c_str());

  // The log is opened only once the command is known to be runnable, so `--help` and a command
  // without a capture path -- which do nothing -- leave no file behind. The absolute name goes into
  // the first line: a per-run name carries a timestamp, so it is not something to guess, and a
  // wrong capture path is what makes the working directory worth stating.
  if(!logPath.empty())
  {
    std::string openedAs;
    g_LogFile = OpenLog(logPath, bPerRunLog, openedAs);
    if(g_LogFile == NULL)
      fprintf(stderr, "warning: cannot write the log file %s\n", openedAs.c_str());
    else
      Log("log file: %s", openedAs.c_str());
  }

  // Every path is made absolute here, and the working directory is logged: a path that only works
  // from one directory is otherwise indistinguishable from a missing file.
  const std::string pathAbs = AbsolutePath(args[1].c_str());
  const char *path = args[1].c_str();    // as given: what the output shows
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
  const ReplaySystemGuard replaySystem;    // declared first, so it shuts down last

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
    ret = DispatchCommand(ctrl, file, path, cmdArgs, bWantDisasm,
                          saveDirAbs.empty() ? NULL : saveDirAbs.c_str());
  }
  Log("done: exit %d after %.1fs", ret, (Millis() - started) / 1000.0);

  // The controller, the capture file and the replay system are torn down by the guards above, in
  // that order, as this function returns.
  //
  // The DLL is deliberately not freed: the objects are owned by it, and RenderDoc's own tools let
  // the process exit instead of unloading the engine underneath its own state.
  if(g_LogFile != NULL)
    fclose(g_LogFile);
  return ret;
}
