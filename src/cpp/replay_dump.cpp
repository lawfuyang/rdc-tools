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
// call them event ids; that held on the two Unreal captures and does not on `desktop-2`,
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
// Build: `cmake -S . -B build -A x64 && cmake --build build --config Release` (MSVC plus an import
// library made from the installed DLL's exports -- AGENTS.md and REFERENCE §9 have the flags).
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
      "  find    <rdc> <substring> [max=40]   events whose call name or marker path matches, and\n"
      "                                        resources whose name matches (case-insensitive)\n"
      "  state   <rdc> <eid>               bound shaders, outputs and D3D12 root parameters\n"
      "  statediff <rdc> <eidA> <eidB>     one changed field per line between two events' states\n"
      "  buffer  <rdc> <resId|name> [offset] [len] [--as u32|f32|hex|ascii]   a buffer's "
      "contents,\n"
      "                                    read through the engine at the current event\n"
      "  pixelhistory <rdc> <eid|last> <resId|name> <x> <y> [--mip N] [--slice N] [--sample N]\n"
      "          [--cast typeless|float|unorm|snorm|uint|sint|uscaled|sscaled|depth|srgb] "
      "[--max N]\n"
      "                                    why this pixel is this colour: every event up to <eid> "
      "that\n"
      "                                    tried to write it, the test that rejected each, and "
      "the\n"
      "                                    value before, from and after it (`last` is the whole "
      "frame)\n"
      "  shaders <rdc> <eid> [--disasm]    reflection: cbuffers, bindings, signatures, "
      "disassembly\n"
      "  cb      <rdc> <eid> <stage> <slot> named values of one constant buffer\n"
      "  textures <rdc> [filter] [--save <dir>] [--mip N] [--slice N] [--sample N] [--raw]\n"
      "          [--cast <type>] [--range min,max]\n"
      "                                    texture list; --save decodes one subresource at a time "
      "to\n"
      "                                    PNG, or writes the engine's own bytes with --raw\n"
      "  formats <rdc>                     every format in the frame's texture list: how many "
      "resources\n"
      "                                    use it, what it is made of, and whether the engine can "
      "make a\n"
      "                                    picture of it\n"
      "  cubemap <rdc> <resId|name> [outDir=cross]   a cubemap as six faces plus the engine's "
      "cruciform\n"
      "  histogram <rdc> <resId|name> [--eid N] [--mip N] [--slice N] [--sample N] [--cast TYPE]\n"
      "                                    [--channels rgba] [--rows N]   a target's statistics: "
      "the engine's\n"
      "                                    min/max and its own histogram of it -- the numbers "
      "behind \"is this\n"
      "                                    black? blown out? clipping?\", instead of opening a PNG "
      "and looking\n"
      "  mesh    <rdc> <eid> [instance] [max] [--stage vsin|vsout|gsout|taskout|meshout] [--obj "
      "<file>]\n"
      "                                    one instance's vertices at that stage, with the "
      "primitive\n"
      "                                    count and the position bounds; --obj exports a "
      "Wavefront OBJ\n"
      "  image   <rdc> <eid> <out.bmp> [--overlay <name>] [--mip N] [--slice N] [--sample N]\n"
      "          [--cast <type>] [--hdr M] [--gamma]   the texture display at that event, as a "
      "BMP\n"
      "  sheet   <rdc> [outDir=sheet] [--every N] [--max N] [--tile N] [--list]   one image per "
      "pass,\n"
      "                                    a montage of them and an index; --list writes nothing\n"
      "  imgdiff <rdc> <a.bmp> <b.bmp> [--out <heat.bmp>]   how two images differ: how many "
      "pixels,\n"
      "                                    how far, and a perceptual hash of each\n"
      "  patch   <rdc> <eid> <stage> [--from <file>] [--enc hlsl|dxbc|dxil|glsl|spirv] [--entry "
      "<name>]\n"
      "          [outDir=patch] [--flag name=value] [--dump <file>] [--compare] [--encodings]\n"
      "                                    build a shader for this replay target, substitute it "
      "for\n"
      "                                    the capture's own and see what the frame does "
      "(--compare\n"
      "                                    renders before and after and writes both plus a diff "
      "map)\n"
      "  counters <rdc> [--per-pass] [--passes <file>] [--top N]   GPU counters per event; "
      "--per-pass\n"
      "                                    folds one counter over each pass and lists the dearest\n"
      "  crosscheck <rdc> [eid] [--since N] [--until N] [--max-events N] [--max N]\n"
      "                                    what the reflections say a shader wants against what "
      "the\n"
      "                                    state says it was given: the vs->ps link, each stage's\n"
      "                                    bindings against the root signature, and the render\n"
      "                                    targets against the pixel shader's outputs\n"
      "  watch   <rdc> <name> [--since A] [--until B] [--max-events N] [--stage <stage>] [--all]\n"
      "                                    one reflection member's value at every event of a "
      "range:\n"
      "                                    one row per *change*, so a uniform that is right at "
      "one\n"
      "                                    draw and wrong at the next is one command. A name is a\n"
      "                                    member path (`Light.intensity`) or a bare member\n"
      "                                    (`intensity`); a matched struct reports its members\n"
      "  debug   <rdc> [--group] [--fail-on high|medium|low|info]\n"
      "                                    debug messages (validation layer, etc.); --group folds "
      "each\n"
      "                                    distinct message into one row with its count and its "
      "eid\n"
      "                                    range, and --fail-on exits 1 when anything at or above "
      "that\n"
      "                                    severity was reported\n"
      "  trace   <rdc> <eid> --pixel <x,y> | --vertex <v[,inst[,idx[,view]]]> | --thread "
      "<gx,gy,gz,tx,ty,tz>\n"
      "          | --mesh-thread <gx,gy,gz,tx,ty,tz> [--sample N] [--primitive N] [--view N]\n"
      "          [--max-steps N] [--all]   one shader invocation, stepped: the inputs it started "
      "with,\n"
      "                                    one row per step with every variable that changed, and "
      "the\n"
      "                                    values it ended with. The stage follows from the "
      "selector\n"
      "                                    (pixel=ps, vertex=vs, thread=cs, mesh-thread=ms). A "
      "DXIL\n"
      "                                    shader is stepped through its debug data, so a capture\n"
      "                                    without it answers with the file the engine looked for; "
      "a\n"
      "                                    DXBC one steps from its bytecode, and the output says "
      "which\n"
      "  usage   <rdc> <resId>             every event that touches a resource\n"
      "  probe   <rdc> [maxEid|last]       which event ids actually have pipeline state; the "
      "whole\n"
      "                                    frame by default, or the first <maxEid> ids, and the\n"
      "                                    answer is cached beside the stream (a repeat is "
      "seconds)\n"
      "  dump    <rdc> [outDir=bundle]     the whole frame to disk, for the offline tool "
      "(REFERENCE "
      "§9)\n"
      "  bundle-verify <dir>               check a bundle's hashes and sizes (no device, no DLL)\n"
      "  batch   <rdc> <file>              run every command in <file> against one open capture\n"
      "  multi   <rdc> <line>...           several commands from a command line, in one session\n"
      "  schema  [<name>] [--out|--check <dir>]   the JSON Schema for each --json document (no "
      "capture, no DLL)\n"
      "  selftest                          this program checking itself: writer, schemas, help, "
      "DLL\n"
      "\n"
      "Options (any position): --json, --log <file>, --disasm, --save <dir>, --out <dir>, --check\n"
      "<dir>, --at-marker <path>, --dll <path> (the renderdoc.dll to replay with), --pdb <dir>\n"
      "(repeatable, or $RDC_PDB: where to look for shader debug info, for `trace`).\n"
      "\n"
      "An event id argument may be a *marker path* instead of a number: `state BasePass` and\n"
      "`state \"Scene > BasePass\"` both work, matching the name inside the path first, then a\n"
      "component of it, then a substring -- the same rule `--at-marker <path>` uses, which "
      "supplies\n"
      "the event id for a command that takes one *instead* of a positional id (naming both is an\n"
      "error, not a silent preference; `cb --at-marker X ps 0` is how the stage and slot are "
      "given)\n"
      "(and sets the current event for a command that\n"
      "reads at it, like `buffer`). A marker path survives a re-capture where an id does not. The\n"
      "word `last` is the frame's own last event, so `pixelhistory last ...` is the whole frame "
      "and\n"
      "`statediff 100 last` compares an event against the end of it -- no `probe` needed first.\n"
      "Where the path resolved to is written to the log, so an answer taken from a path can be\n"
      "checked: `find <substring>` lists the paths and their ids.\n"
      "\n"
      "`sheet` and `patch` are the frame's pictures. `sheet` renders the last event of every pass "
      "and\n"
      "lays the images out in a montage with an index that names them. `patch` builds a shader for "
      "this\n"
      "target out of a file you edited, substitutes it for the capture's own, replays the frame, "
      "and\n"
      "with `--compare` writes the before and after images plus their difference. What can be "
      "built is\n"
      "the target's business -- `patch --encodings` prints the list -- and the disassembly `patch "
      "--dump`\n"
      "writes is readable but is not one of them.\n"
      "\n"
      "`--repl` and `--stdin` keep the capture open and read commands from the terminal (or a "
      "pipe),\n"
      "one per line, exactly as a batch file spells them: `replay_dump <capture> --repl`. "
      "`--repl`\n"
      "prints a prompt, `--stdin` does not; `help` prints this text, `quit` leaves. A line that "
      "fails\n"
      "is logged and the session continues, which is the whole point -- a session costs ~4-11 s "
      "to\n"
      "stand up and each command in it costs only itself.\n"
      "\n"
      "A batch file holds one command per line, in the same syntax minus the executable and the\n"
      "capture (`state 270 --json`), with `#` for comments. Each line's output is preceded by a\n"
      "`#=== <line>` marker so a stream can be split again. This is the cheap way to run many\n"
      "commands: opening a capture and standing the replay engine up costs ~7 s on a 1.5 GB "
      "capture,\n"
      "and batch pays it once for the whole file. `multi <rdc> \"state 270\" \"shaders 270\"` is "
      "the\n"
      "same thing from a command line -- one line per argument, spelled exactly as a batch file\n"
      "spells it -- for a caller that has the lines in hand and no file to put them in.\n"
      "\n"
      "`counters --per-pass` folds one counter over each pass: `FetchCounters` answers per event "
      "and\n"
      "takes no range, so the per-event list is summed here between a pass's first and last event "
      "id.\n"
      "The passes come from the frame's markers (consecutive calls sharing a marker path are one\n"
      "pass) or from `--passes <file>`, one `<first eid> <last eid> [<name>]` line per pass. "
      "Which\n"
      "counter is the cost is the engine's choice -- `EventGPUDuration` when this replay produced\n"
      "one, the first it produced otherwise -- and it is named in the document; a replay with no\n"
      "counter results says so rather than printing a table of zeros, because GPU counters are a\n"
      "driver feature and are not available everywhere.\n"
      "\n"
      "`crosscheck` compares two things the capture *states* against each other, so every line is "
      "a\n"
      "fact with an event id on it: the vertex shader's outputs against the pixel shader's "
      "inputs,\n"
      "each stage's bindings against the root signature's declared ranges, and the render "
      "targets'\n"
      "formats against the pixel shader's outputs. Both sides need shader reflection, and a "
      "capture\n"
      "with stripped shaders has none -- so `linksChecked`, `bindingsChecked` and "
      "`targetsChecked`\n"
      "say how much was actually compared, and an empty findings list next to three zeros means\n"
      "nothing was, not that the frame is clean.\n"
      "\n"
      "`dump` writes a bundle for the offline tool (REFERENCE §9): events.json for every id with "
      "bound\n"
      "state, states/<eid>.state.json and .shaders.json for the first event and every state "
      "change, one\n"
      "cbuffers/ file per constant block, resources.json with each resource's usage list, and\n"
      "messages.json. The manifest lists every file with its byte count and SHA-256, and it names "
      "what\n"
      "the bundle cannot contain (the action list gives a call's kind, workload and marker, and "
      "only those\n"
      "three are taken from it; the finer kind -- copy against clear against marker -- and a "
      "call's own name\n"
      "are not). `--with-images` adds rt/ images at the state events, "
      "`--textures`\n"
      "decodes every texture to PNG (full size: the engine decodes but does not resize), "
      "`--with-counters`\n"
      "fetches the counters: `counters.json` per event, and `counters-passes.json` the engine's "
      "own "
      "fold over\n"
      "its passes -- the same document `counters --per-pass` prints. `--since`/`--until` pin the "
      "id "
      "range (the default scans until 256 ids "
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
      "`probe` must still be the *first* command of a session -- it is the one answer the engine "
      "cannot\n"
      "give after another command has replayed, because a forced non-event keeps the state of the "
      "last\n"
      "real one -- and a session that breaks that rule is told so on stderr. What the cache does "
      "change:\n"
      "a *second* `probe` reads the first one's answer (seconds instead of the sweep), and a probe "
      "that\n"
      "is not first reads it too, where before it would have answered from its own warm engine.\n"
      "--dll <path> (or $RDC_RENDERDOC_DLL) names the renderdoc.dll to replay with -- the flag "
      "first,\n"
      "then the environment, then the installed engine -- and a capture recorded by a *newer* "
      "RenderDoc\n"
      "than the one loaded is refused, with both versions named, instead of being replayed by an "
      "engine\n"
      "that does not know the file.\n"
      "--pdb <dir> (repeatable, or $RDC_PDB with `;` between the directories) is where the engine "
      "should look\n"
      "for the debug info a shader names -- the\n"
      "`.pdb` matched by hash, which a DXIL shader is stepped *through*, and which almost no "
      "shipped capture\n"
      "carries: without it `trace` can only say which file the engine went looking for, and that "
      "file's name\n"
      "is a hash, so guessing where it lives is not a thing a reader can do. Each directory is "
      "searched\n"
      "recursively, by file name; the list is what the engine's own `DXBC_Debug_SearchDirPaths` "
      "setting\n"
      "holds, which RenderDoc's UI is otherwise the only way to set, and it is replaced rather "
      "than added to\n"
      "-- this process starts with none. A directory that is not there is warned about instead "
      "of being\n"
      "searched in silence, because the engine's answer to a typo is the same as its answer to a "
      "PDB that\n"
      "was never built there.\n"
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

//: Says so, once, when the offline tool has no RenderDoc source tree to name chunks from.
//:
//: This driver does not read that tree -- it asks the installed engine, which is the point of it --
//: so this is not a prerequisite for the command about to run. It is a prerequisite for *reading
//: the answer*: the offline tool names every chunk id from the tree, and a fresh clone that never
//: fetched it prints `Chunk1203` where a name belongs. The tool fetches it on demand (README 1.1),
//: so the one useful thing this program can do is say that it has not been fetched yet.
//:
//: Deliberately quiet in three ways, because a hint that repeats is noise: nothing is printed when
//: `$RENDERDOC_SRC` is set (the reader pointed at their own tree, and it is not this program's
//: business whether it is complete), nothing when the tree is there, and it goes to stderr so
//: `--json` on stdout stays a document.
void WarnIfRenderdocSrcMissing()
{
  if(getenv("RENDERDOC_SRC") != NULL)
    return;

  char exe[4096];
  const DWORD len = GetModuleFileNameA(NULL, exe, (DWORD)sizeof(exe));
  if(len == 0 || len >= sizeof(exe))
    return;
  // Where this module lives is a load's own path, which only Win32 can answer; from there the tree
  // is two levels up, which is what the chain of `find_last_of` calls was spelling out by hand.
  const std::filesystem::path root =
      std::filesystem::path(std::string(exe, len)).parent_path().parent_path();

  const std::filesystem::path core = root / "renderdoc-src" / "renderdoc" / "core" / "core.h";
  std::error_code ec;
  if(std::filesystem::exists(core, ec) && !ec)
    return;

  fprintf(stderr,
          "note: %s has no RenderDoc source, so the offline tool will print chunk ids\n"
          "      rather than chunk names. It fetches the tree on demand -- the first command that "
          "needs\n"
          "      one, or `python src\\py\\rdc_analysis.py bootstrap` to do it now (README section "
          "1.1).\n",
          (root / "renderdoc-src").string().c_str());
}

//: Says so, in the log, when this executable is older than the sources it was built from.
//:
//: Because a replay host answers from the code it was compiled with and cannot see that it has been
//: superseded: after an edit and before a build, every command runs the *previous* revision and
//: says nothing about it -- which is the one kind of wrong answer this tool exists to not produce,
//: and it is invisible from the outside (the data looks exactly like data from a fixed build).
//: Measured in this repository's own history: a stale `bin\replay_dump.exe` was used for a whole
//: verification pass and only the elapsed time of a command gave it away.
//:
//: Quiet when the answer would be a guess: nothing when the exe's own path or the `src\cpp` folder
//: cannot be read (it was copied out of the tree, or run from a CI checkout) and nothing when the
//: newest source is older, which is every normal run. A build that changes nothing about the binary
//: -- a comment, a whitespace edit -- still warns, deliberately: the warning is about being able to
//: trust the answers, and "the source is newer than the code that ran" is exactly that question. It
//: is not an error, because running an old build on purpose is legitimate (that is how a bundle
//: from the previous revision gets reproduced).
void WarnIfDriverIsStale()
{
  char exe[4096];
  const DWORD len = GetModuleFileNameA(NULL, exe, (DWORD)sizeof(exe));
  if(len == 0 || len >= sizeof(exe))
    return;
  const std::filesystem::path exePath(std::string(exe, len));
  const std::optional<FileTime> exeTime = FileWriteTime(exePath);
  if(!exeTime)
    return;    // no answer about this binary, so whatever came next would be a guess

  const std::filesystem::path root = exePath.parent_path().parent_path();    // <root>\bin\<exe>

  // The driver's own translation units and headers, and the build file that can change the binary
  // without touching either (`/WX` off, a new source glob, a different renderdoc.dll path). Both
  // sides are `file_time_type`s from the same filesystem, and the difference becomes seconds below:
  // neither the epoch nor the tick size has to be assumed anywhere.
  const char *const kSuffixes[] = {".cpp", ".h"};
  std::filesystem::path newest;
  std::optional<FileTime> newestTime = NewestSourceTime(root / "src" / "cpp", kSuffixes, 2, newest);
  // What to print: the *name* is what the comparison needed, but a warning that says `common.h` is
  // one path-guess away from being useless, so the folder goes in front of it here.
  std::filesystem::path where =
      newest.empty() ? std::filesystem::path() : std::filesystem::path("src") / "cpp" / newest;
  const char *const kBuildFile[] = {"CMakeLists.txt"};
  std::filesystem::path buildFile;
  const std::optional<FileTime> buildTime = NewestSourceTime(root, kBuildFile, 1, buildFile);
  if(buildTime && (!newestTime || *buildTime > *newestTime))
  {
    newestTime = buildTime;
    where = buildFile;
  }

  if(!newestTime || *newestTime <= *exeTime)
    return;

  // Seconds, because the difference is what a reader wants to judge it by. The remainder is dropped
  // rather than rounded: "0 s newer" next to a warning would read as a false alarm.
  const long long newer =
      std::chrono::duration_cast<std::chrono::seconds>(*newestTime - *exeTime).count();
  Log("warning: this replay_dump.exe is older than its sources: %s was written %lld s later, so "
      "every answer from this run is the previous build's",
      where.string().c_str(), newer);
  Log("         build it with: cmake --build build --config Release");
}

//: The event id an argument names: a number, a marker path (`BasePass`, `Scene > BasePass`), which
//: `ResolveMarkerPath` turns into the first call inside it, or `last`. `how` receives what the
//: argument resolved to, so the caller can log the path that won rather than what was typed.
bool ParseEidArg(IReplayController *ctrl, const std::string &text, int &eid, std::string &how)
{
  int parsed = 0;
  if(ParseInt(text.c_str(), parsed))
  {
    eid = parsed;
    return true;
  }
  if(text == "last")
  {
    // The frame's own last event, from the engine's action list (cached per process, and the same
    // tree `draws` prints). Not a guess at a chunk count: ids past the frame's end *clamp* to the
    // last event rather than coming back empty, so an over-large number would answer the same
    // question -- with a number that looks like it exists.
    int calls = 0;
    bool bTruncated = false;
    const std::vector<ActionNode> rows = ActionTree(ctrl, calls, bTruncated);
    if(rows.empty())
      return false;
    eid = rows[rows.size() - 1].m_Eid;
    how = "the frame's last event";
    return true;
  }
  const int resolved = ResolveMarkerPath(ctrl, text.c_str(), how);
  if(resolved < 0)
    return false;
  eid = resolved;
  return true;
}

//: Takes the options the dispatcher owns out of an argument list: `--as <mode>` and
//: `--at-marker <path>`. Per *line* rather than per process, which is what lets one batch file or
//: REPL session point different commands at different markers.
void TakeDispatchOptions(std::vector<std::string> &args, std::string &asMode, std::string &atMarker)
{
  std::vector<std::string> kept;
  for(size_t i = 0; i < args.size(); i++)
  {
    if(args[i] == "--as" && i + 1 < args.size())
    {
      asMode = args[++i];
      continue;
    }
    if(args[i] == "--at-marker" && i + 1 < args.size())
    {
      atMarker = args[++i];
      continue;
    }
    kept.push_back(args[i]);
  }
  args.swap(kept);
}

//: A byte offset or length: decimal, or `0x` hex. `ToInt` is int-sized and a buffer can be larger.
unsigned long long ParseSize(const std::string &text)
{
  return strtoull(text.c_str(), NULL, 0);
}

//: The value of `--name` in an argument list, or `fallback` when it is absent, and whether it is
//: there at all. Options are read here rather than inside each command because the dispatcher is
//: what makes them mean the same thing in `main`, in a batch file and in a session -- and because a
//: command that parses its own options cannot be told apart from one that silently ignored a typo.
const char *OptValue(const std::vector<std::string> &args, const char *name, const char *fallback)
{
  for(size_t i = 0; i + 1 < args.size(); i++)
  {
    if(args[i] == name)
      return args[i + 1].c_str();
  }
  return fallback;
}

bool HasOpt(const std::vector<std::string> &args, const char *name)
{
  for(size_t i = 0; i < args.size(); i++)
  {
    if(args[i] == name)
      return true;
  }
  return false;
}

//: A `pixelhistory` subresource option: a position, so a negative one is a typo rather than a clamp
//: (`ToInt` would turn it into a huge unsigned index and the command's own bounds check would
//: report it as a slice that does not exist).
bool ParseIndexOpt(const std::vector<std::string> &args, const char *name, uint32_t &value)
{
  const char *text = OptValue(args, name, "0");
  int parsed = 0;
  if(!ParseInt(text, parsed) || parsed < 0)
    return false;
  value = (uint32_t)parsed;
  return true;
}

//: Whether an argument asks for an option rather than naming a positional. The picture commands take their
//: positionals first (`mesh 270 1 8 --stage gsout`), and once an option is seen nothing after it is a
//: positional -- otherwise `--stage gsout` would leave `gsout` to be read as the instance.
bool IsOption(const std::string &arg)
{
  return !arg.empty() && arg[0] == '-';
}

//: The options the picture commands (`image`, `textures`, `cubemap`, and the bundle's `rt/` images)
//: share, parsed in one place so `main`, a batch file and a library session cannot disagree about
//: them -- the same reason every other option is parsed in this file. `why` receives the refusal,
//: because each of these is a value a typo in changes *the question*: `--cast uint` on a float
//: texture gives integers that still look like numbers, and `--slice 7` on a four-slice array asks
//: about a slice that is not there.
bool PictureOptionsFromArgs(const std::vector<std::string> &args, PictureOptions &opts,
                            std::string &why)
{
  if(!ParseIndexOpt(args, "--mip", opts.m_Sub.mip) ||
     !ParseIndexOpt(args, "--slice", opts.m_Sub.slice) ||
     !ParseIndexOpt(args, "--sample", opts.m_Sub.sample))
  {
    why = "--mip, --slice and --sample are positions: they take a number from 0 up";
    return false;
  }

  const char *castName = OptValue(args, "--cast", NULL);
  if(castName != NULL)
  {
    if(!CastFromName(castName, opts.m_Cast))
    {
      why =
          Fmt("'%s' is not a component type (typeless, float, unorm, snorm, uint, sint, uscaled, "
              "sscaled, depth, srgb)",
              castName);
      return false;
    }
    opts.m_bCastGiven = true;
  }

  const char *overlayName = OptValue(args, "--overlay", NULL);
  if(overlayName != NULL)
  {
    if(!OverlayFromName(overlayName, opts.m_Overlay))
    {
      why = Fmt("'%s' is not an overlay (%s)", overlayName, OverlayNames(", ").c_str());
      return false;
    }
    opts.m_bOverlayGiven = true;
  }

  // The display path's tonemapping: a multiplier for float/HDR content, because the engine cannot know the
  // range a capture meant; and whether to read the values as linear and show them as gamma.
  const char *hdr = OptValue(args, "--hdr", NULL);
  if(hdr != NULL)
  {
    char *end = NULL;
    const double value = strtod(hdr, &end);
    if(end == hdr || *end != '\0' || !(value > 0.0))
    {
      why = Fmt("'%s' is not an --hdr multiplier: it takes one positive number", hdr);
      return false;
    }
    opts.m_HdrMultiplier = (float)value;
  }
  opts.m_bGamma = HasOpt(args, "--gamma");

  // The save path's tonemapping, and the one that matters for a float/HDR texture on disk: the
  // range that becomes 0..255. `min,max` because a single number could only be a scale, and the
  // interesting case is a measured range -- an SH buffer's values are nowhere near 0..1.
  const char *range = OptValue(args, "--range", NULL);
  if(range != NULL)
  {
    double low = 0.0, high = 0.0;
    char tail = '\0';
    if(sscanf(range, "%lf , %lf %c", &low, &high, &tail) != 2 || !(high > low))
    {
      why = Fmt("'%s' is not a --range: it takes two numbers, 'min,max', with max above min", range);
      return false;
    }
    opts.m_BlackPoint = (float)low;
    opts.m_WhitePoint = (float)high;
  }

  // `--raw` is the save path's own switch: the engine's bytes rather than a picture of them.
  opts.m_bRaw = HasOpt(args, "--raw");
  return true;
}

//: A comma-separated list of numbers from 0 up, as the `trace` selectors are written (`12,34`,
//: `3,0,7,0`). One parser for all three selectors: a second one is how `--vertex 3,0,7,0` would
//: come to accept a trailing comma that `--thread 1,0,0,0,0,0` rejects.
bool ParseIntList(const char *text, std::vector<int> &out, std::string &why, const char *option)
{
  const std::string all(text);
  size_t start = 0;
  for(size_t i = 0; i <= all.size(); i++)
  {
    if(i < all.size() && all[i] != ',')
      continue;
    const std::string item = all.substr(start, i - start);
    start = i + 1;
    int value = 0;
    if(item.empty() || !ParseInt(item.c_str(), value) || value < 0)
    {
      why = Fmt("%s takes comma-separated numbers from 0 up ('%s' is not one)", option,
                item.empty() ? text : item.c_str());
      return false;
    }
    out.push_back(value);
  }
  return true;
}

//: One selector's numbers, checked against what its API takes and put where the command reads them.
//: The counts are the API's own: `DebugPixel` wants a co-ordinate pair, `DebugVertex` a vertex and
//: instance and then (optionally) the index to read inputs with and the view, `DebugThread` and
//: `DebugMeshThread` a workgroup index and a thread index within it.
bool TraceNumbersFromList(TraceInvocation kind, const char *text, TraceRequest &req, std::string &why)
{
  const char *option = kind == TraceInvocation::Pixel        ? "--pixel"
                       : kind == TraceInvocation::Vertex     ? "--vertex"
                       : kind == TraceInvocation::MeshThread ? "--mesh-thread"
                                                             : "--thread";
  std::vector<int> n;
  if(!ParseIntList(text, n, why, option))
    return false;

  if(kind == TraceInvocation::Pixel)
  {
    if(n.size() != 2)
    {
      why = Fmt("--pixel takes two numbers, 'x,y' in the target's own space ('%s' has %d)", text,
                (int)n.size());
      return false;
    }
    req.m_X = (uint32_t)n[0];
    req.m_Y = (uint32_t)n[1];
    return true;
  }

  if(kind == TraceInvocation::Vertex)
  {
    if(n.empty() || n.size() > 4)
    {
      why =
          Fmt("--vertex takes 'v[,inst[,idx[,view]]]' ('%s' has %d number(s))", text, (int)n.size());
      return false;
    }
    req.m_VertId = (uint32_t)n[0];
    req.m_InstId = n.size() > 1 ? (uint32_t)n[1] : 0;
    // The third number is the index the vertex *inputs* are read with, with the draw's own offsets
    // applied -- not something the vertex id tells us, which is why the API asks for it separately.
    // A caller who gives only the vertex id gets that id, which is right for a non-indexed draw and
    // wrong for one that reads an index buffer; the command logs which of the two it assumed.
    req.m_bIndexGiven = n.size() > 2;
    req.m_Index = req.m_bIndexGiven ? (uint32_t)n[2] : req.m_VertId;
    req.m_VertexView = n.size() > 3 ? (uint32_t)n[3] : 0;
    return true;
  }

  if(n.size() != 6)
  {
    why = Fmt("%s takes six numbers, 'gx,gy,gz,tx,ty,tz' ('%s' has %d)", option, text, (int)n.size());
    return false;
  }
  for(int i = 0; i < 3; i++)
  {
    req.m_Group[i] = (uint32_t)n[i];
    req.m_Thread[i] = (uint32_t)n[i + 3];
  }
  return true;
}

//: `trace`'s command line: which of the engine's four debugging entry points to run, and against
//: which invocation. Parsed here with the other option helpers for the reason this file exists --
//: one place decides what an option means, so `main`, a batch file and a library session cannot
//: disagree -- and an option this command does not know is refused rather than ignored, because a
//: silently dropped
//: `--pixel` would run *nothing* and a silently dropped `--max-steps` would run to the end.
bool TraceRequestFromArgs(const std::vector<std::string> &args, TraceRequest &req, std::string &why)
{
  const char *const kUsage =
      "`trace <eid> --pixel <x,y> | --vertex <v[,inst[,idx[,view]]]> | --thread "
      "<gx,gy,gz,tx,ty,tz> | "
      "--mesh-thread <gx,gy,gz,tx,ty,tz> [--sample N] [--primitive N] [--view N] [--max-steps N] "
      "[--all]`";

  int selectors = 0;
  bool bAll = false, bMaxSteps = false;
  for(size_t i = 1; i < args.size(); i++)
  {
    const std::string &arg = args[i];
    const bool bHasValue = i + 1 < args.size();

    const TraceInvocation kind = arg == "--pixel"         ? TraceInvocation::Pixel
                                 : arg == "--vertex"      ? TraceInvocation::Vertex
                                 : arg == "--thread"      ? TraceInvocation::Thread
                                 : arg == "--mesh-thread" ? TraceInvocation::MeshThread
                                                          : TraceInvocation::None;
    if(kind != TraceInvocation::None)
    {
      if(!bHasValue)
      {
        why = Fmt("%s needs its numbers: %s", arg.c_str(), kUsage);
        return false;
      }
      if(++selectors > 1)
      {
        why =
            "one invocation at a time: --pixel, --vertex, --thread and --mesh-thread are four ways "
            "to "
            "name the same thing (the stage they run follows from which one is given)";
        return false;
      }
      req.m_Kind = kind;
      if(!TraceNumbersFromList(kind, args[++i].c_str(), req, why))
        return false;
    }
    else if(arg == "--sample" || arg == "--primitive" || arg == "--view")
    {
      if(!bHasValue)
      {
        why = Fmt("%s takes a number from 0 up", arg.c_str());
        return false;
      }
      int value = 0;
      if(!ParseInt(args[++i].c_str(), value) || value < 0)
      {
        why = Fmt("%s takes a number from 0 up", arg.c_str());
        return false;
      }
      if(arg == "--sample")
        req.m_Sample = (uint32_t)value;
      else if(arg == "--primitive")
        req.m_Primitive = (uint32_t)value;
      else
        req.m_PixelView = (uint32_t)value;
    }
    else if(arg == "--max-steps")
    {
      if(!bHasValue)
      {
        why = Fmt("--max-steps takes a number of steps from 1 up (`--all` runs to the end): %s",
                  kUsage);
        return false;
      }
      int value = 0;
      if(!ParseInt(args[++i].c_str(), value) || value <= 0)
      {
        why =
            "--max-steps takes a number of steps from 1 up (`--all` runs the invocation to its "
            "end)";
        return false;
      }
      req.m_MaxSteps = value;
      bMaxSteps = true;
    }
    else if(arg == "--all")
    {
      bAll = true;
    }
    else if(IsOption(arg))
    {
      why = Fmt("unknown option '%s' for trace: %s", arg.c_str(), kUsage);
      return false;
    }
    // Anything else is the event id, which the dispatcher took apart before calling this.
  }

  if(bAll && bMaxSteps)
  {
    why =
        "--all and --max-steps say different things: one runs the invocation to its end, the other "
        "stops after N steps";
    return false;
  }
  if(bAll)
    req.m_MaxSteps = 0;    // no cap, up to `kTraceStepCap`

  if(selectors == 0)
  {
    why = Fmt("trace needs exactly one invocation: %s", kUsage);
    return false;
  }

  // The pixel-only options are refused rather than ignored when another selector was given: they
  // are `DebugPixelInputs`' fields, and `--thread 1,0,0,0,0,0 --sample 2` would otherwise look like
  // it selected a sample of a compute dispatch.
  if(req.m_Kind != TraceInvocation::Pixel &&
     (req.m_Sample != kTraceNoPreference || req.m_Primitive != kTraceNoPreference ||
      req.m_PixelView != kTraceNoPreference))
  {
    why = Fmt(
        "--sample, --primitive and --view choose a fragment, so they belong with --pixel, not with "
        "%s",
        TraceInvocationName(req.m_Kind));
    return false;
  }

  return true;
}

//: How many arguments a command needs before it can run, the command itself counted: the same
//: numbers the dispatch table below enforces (`args.size() > 4` for `pixelhistory` is 5 here). They
//: are listed once, for the one question that has to be answered *before* dispatch -- whether a
//: positional event id was given alongside `--at-marker` -- because every command in the
//: `bTakesEid` list below has its event id as its first positional.
int MinArgs(const char *cmd)
{
  if(!strcmp(cmd, "pixelhistory"))
    return 5;    // <eid> <resId|name> <x> <y>
  if(!strcmp(cmd, "cb"))
    return 4;    // <eid> <stage> <slot>
  if(!strcmp(cmd, "image"))
    return 3;    // <eid> <out.bmp>
  if(!strcmp(cmd, "patch"))
    return 3;    // <eid> <stage>
  if(!strcmp(cmd, "statediff"))
    return 3;    // <eidA> <eidB>
  if(!strcmp(cmd, "state") || !strcmp(cmd, "shaders") || !strcmp(cmd, "mesh"))
    return 2;    // <eid>: `mesh`'s instance and cap are optional, and its id is what comes first
  if(!strcmp(cmd, "trace"))
    return 2;    // <eid>: which invocation to run is an option, so the id is the only positional
  return 1;
}

//: Whether a command's event id may be left out -- in which case the argument after the command is
//: not necessarily one, because an option can sit in its place and a whole-frame sweep is the
//: meaning of no id at all.
//:
//: Only `crosscheck`. Every other command in the `bTakesEid` list *needs* its id, and that is what
//: lets their callers count arguments instead of looking at them.
bool CommandIdIsOptional(const char *cmd)
{
  return strcmp(cmd, "crosscheck") == 0;
}

//: Whether an argument list already carries the command's positional event id -- the test that
//: decides whether `--at-marker` is a duplicate of one.
//:
//: Counting is how this was decided, and it is right for a command whose id is *required*: a list
//: long enough to hold one has one. Where the id is optional an option can sit in its slot
//: (`crosscheck --at-marker X --max 5` is not a duplicate id), so there the answer is what
//: `args[1]` *is* -- an event id, `last`, or a marker path, none of which begins with '-'.
bool CommandHasPositionalId(const char *cmd, const std::vector<std::string> &args)
{
  if(CommandIdIsOptional(cmd))
    return args.size() > 1 && args[1].compare(0, 1, "-") != 0;
  return (int)args.size() >= MinArgs(cmd);
}

//: Runs one command against an already-open capture. Shared by `main`, `batch` and `--repl`, so a
//: command name, its arguments and its options mean the same thing however they were spelled.
int DispatchCommand(IReplayController *ctrl, ICaptureFile *file, const char *path,
                    const std::vector<std::string> &given, bool bWantDisasm, const char *saveDir)
{
  if(given.empty())
    return 2;

  std::vector<std::string> args = given;
  std::string asMode, atMarkerText;
  TakeDispatchOptions(args, asMode, atMarkerText);
  if(args.empty())
    return 2;

  const char *cmd = args[0].c_str();

  // A marker path stands in for an event id wherever one is expected, and `--at-marker` is the same
  // resolution with the path in an option instead of in the argument. Resolving here -- once, before
  // dispatch -- is what keeps every command below unaware of both: `state BasePass` reads args[1] and
  // finds a number in it. A path that matches nothing is an error rather than a silent event 0.
  const bool bTakesEid = !strcmp(cmd, "state") || !strcmp(cmd, "shaders") || !strcmp(cmd, "cb") ||
                         !strcmp(cmd, "mesh") || !strcmp(cmd, "image") ||
                         !strcmp(cmd, "statediff") || !strcmp(cmd, "patch") ||
                         !strcmp(cmd, "pixelhistory") || !strcmp(cmd, "crosscheck") ||
                         !strcmp(cmd, "trace");
  if(!atMarkerText.empty())
  {
    std::string matched;
    const int eid = ResolveMarkerPath(ctrl, atMarkerText.c_str(), matched);
    if(eid < 0)
      return Fail(2, "--at-marker '%s' matches no marker path in this capture (try `find`)",
                  atMarkerText.c_str());
    Log("--at-marker %s -> eid %d [%s]", atMarkerText.c_str(), eid, matched.c_str());
    if(bTakesEid)
    {
      // The option *supplies* the id, so a positional one as well is the argument given twice -- and
      // quietly dropping it is how `statediff 100 --at-marker X` would silently compare X against 100
      // instead of 100 against the end of the frame. Counting decides it: a command's first positional
      // is its event id, so an argument list long enough to hold one is a list that has one. Erasing
      // the slot to make room instead -- which is what this did -- took the *next* positional with it
      // whenever the id had been left out (`image --at-marker X out.bmp` ran `image X`, which is
      // `unknown command`), because nothing here can tell a missing id from a resource name.
      if(CommandHasPositionalId(cmd, args))
        return Fail(2,
                    "%s already has its event id ('%s'): `--at-marker` supplies one *instead* of a "
                    "positional, not as well as one",
                    cmd, args[1].c_str());
      args.insert(args.begin() + 1, Fmt("%d", eid));
    }
    else
    {
      // A command with no id argument (`buffer`) reads at the current event: this chooses it.
      MoveToEvent(ctrl, eid);
    }
  }
  // `args` may have been rewritten just above -- a positional erased, an id inserted -- and that
  // can move the vector's storage, so the pointer taken before it is re-taken rather than left
  // dangling (which read as `unknown command ''`: the comparison ran on freed bytes).
  cmd = args[0].c_str();
  if(bTakesEid)
  {
    const size_t slots = !strcmp(cmd, "statediff") ? 2 : 1;
    for(size_t slot = 1; slot <= slots; slot++)
    {
      if(args.size() <= slot)
        break;
      // An option in the id's slot means no id was given -- but only where an id may be left out:
      // `crosscheck --since 1` is a sweep, not a check of an event called `--since`. Anywhere else
      // the slot holds an id, and letting an option stand there turned a typo'd option into eid 0:
      // measured, `state --bogus` printed `eid 0` and exited 0 where it used to say `--bogus` is
      // neither an id nor a marker path. A wrong id answering with an empty state is exactly what
      // this tool must not do quietly.
      if(CommandIdIsOptional(cmd) && args[slot].compare(0, 1, "-") == 0)
        break;
      int eid = 0;
      std::string resolved;
      if(ParseEidArg(ctrl, args[slot], eid, resolved))
      {
        if(!resolved.empty())
          Log("%s %s -> eid %d [%s]", cmd, args[slot].c_str(), eid, resolved.c_str());
        args[slot] = Fmt("%d", eid);
      }
      else
      {
        return Fail(2, "'%s' is neither an event id nor a marker path in this capture (try `find`)",
                    args[slot].c_str());
      }
    }
  }

  if(!strcmp(cmd, "info"))
    return CmdInfo(ctrl, file, path);
  if(!strcmp(cmd, "draws"))
    return CmdDraws(ctrl, file, path, args.size() > 1 ? ToInt(args[1], 80) : 80,
                    args.size() > 2 ? args[2].c_str() : NULL);
  if(!strcmp(cmd, "find"))
    return CmdFind(ctrl, file, path, args.size() > 1 ? args[1].c_str() : NULL,
                   args.size() > 2 ? ToInt(args[2], 40) : 40);
  if(!strcmp(cmd, "state") && args.size() > 1)
    return CmdState(ctrl, file, path, ToInt(args[1], 0));
  if(!strcmp(cmd, "statediff") && args.size() > 2)
    return CmdStateDiff(ctrl, file, path, ToInt(args[1], 0), ToInt(args[2], 0));
  if(!strcmp(cmd, "buffer") && args.size() > 1)
    return CmdBuffer(ctrl, file, path, args[1].c_str(), args.size() > 2 ? ParseSize(args[2]) : 0,
                     args.size() > 3 ? ParseSize(args[3]) : 0,
                     asMode.empty() ? NULL : asMode.c_str());
  if(!strcmp(cmd, "pixelhistory") && args.size() > 4)
  {
    int x = 0, y = 0;
    if(!ParseInt(args[3].c_str(), x) || !ParseInt(args[4].c_str(), y) || x < 0 || y < 0)
      return Fail(2, "pixelhistory needs a pixel: '%s %s' is not a pair of co-ordinates",
                  args[3].c_str(), args[4].c_str());
    CompType cast = CompType::Typeless;
    const char *castName = OptValue(args, "--cast", "typeless");
    if(!CastFromName(castName, cast))
      return Fail(2,
                  "'%s' is not a component type (typeless, float, unorm, snorm, uint, sint, "
                  "uscaled, sscaled, depth, srgb)",
                  castName);
    uint32_t mip = 0, slice = 0, sample = 0;
    if(!ParseIndexOpt(args, "--mip", mip) || !ParseIndexOpt(args, "--slice", slice) ||
       !ParseIndexOpt(args, "--sample", sample))
      return Fail(2, "--mip, --slice and --sample are positions: they take a number from 0 up");
    const Subresource sub(mip, slice, sample);
    return CmdPixelHistory(ctrl, file, path, ToInt(args[1], 0), args[2].c_str(), (unsigned)x,
                           (unsigned)y, sub, cast, ToInt(OptValue(args, "--max", "200"), 200));
  }
  if(!strcmp(cmd, "sheet"))
    return CmdSheet(ctrl, file, path, args.size() > 1 ? args[1].c_str() : NULL,
                    ToInt(OptValue(args, "--every", "1"), 1),
                    ToInt(OptValue(args, "--max", "24"), 24),
                    ToInt(OptValue(args, "--tile", "320"), 320), HasOpt(args, "--list"));
  if(!strcmp(cmd, "imgdiff") && args.size() > 2)
    return CmdImgDiff(file, path, args[1].c_str(), args[2].c_str(), OptValue(args, "--out", NULL));
  if(!strcmp(cmd, "patch"))
    return CmdPatch(ctrl, file, path, args);
  if(!strcmp(cmd, "shaders") && args.size() > 1)
    return CmdShaders(ctrl, file, path, ToInt(args[1], 0), bWantDisasm);
  if(!strcmp(cmd, "cb") && args.size() > 3)
  {
    const ShaderStage stage = StageFromName(args[2].c_str());
    if(stage == ShaderStage::Invalid)
      return Fail(2, "'%s' is not a shader stage (vs hs ds gs ps cs as ms)", args[2].c_str());
    return CmdCbuffer(ctrl, file, path, ToInt(args[1], 0), stage, ToInt(args[3], 0));
  }
  if(!strcmp(cmd, "formats"))
    return CmdFormats(ctrl, file, path);
  if(!strcmp(cmd, "cubemap") && args.size() > 1)
  {
    PictureOptions opts;
    std::string why;
    if(!PictureOptionsFromArgs(args, opts, why))
      return Fail(2, "%s", why.c_str());
    // The directory is the second positional and optional, like `dump`'s: `cubemap frame.rdc 271`
    // writes beside the tool as `cross/`.
    return CmdCubemap(ctrl, file, path, args[1].c_str(),
                      args.size() > 2 && !IsOption(args[2]) ? args[2].c_str() : NULL, opts);
  }
  if(!strcmp(cmd, "histogram") && args.size() > 1)
  {
    PictureOptions opts;
    std::string why;
    if(!PictureOptionsFromArgs(args, opts, why))
      return Fail(2, "%s", why.c_str());
    // Two ways to name the target, and the second is what `image` uses: a resource id or name, or
    // the render target 0 bound at an event (`--eid`). They are exclusive because they answer the
    // same question -- "which picture" -- and a command that silently preferred one would be a
    // statistic of the other.
    const int eid = ToInt(OptValue(args, "--eid", "0"), 0);
    if(eid != 0 && !IsOption(args[1]))
      return Fail(2, "histogram takes a resource or --eid, not both");
    return CmdHistogram(ctrl, file, path, args[1].c_str(), eid, opts,
                        ToInt(OptValue(args, "--rows", "32"), 32),
                        OptValue(args, "--channels", NULL));
  }
  if(!strcmp(cmd, "textures"))
  {
    PictureOptions opts;
    std::string why;
    if(!PictureOptionsFromArgs(args, opts, why))
      return Fail(2, "%s", why.c_str());
    // The filter is optional and is the first positional; `--save <dir>` is a global option
    // (SplitLine), which is why it arrives as its own parameter.
    return CmdTextures(ctrl, file, path,
                       args.size() > 1 && !IsOption(args[1]) ? args[1].c_str() : NULL, saveDir, opts);
  }
  if(!strcmp(cmd, "mesh") && args.size() > 1)
  {
    // The positionals come first, as the help text shows, and the *first* option ends them: `mesh
    // 270 --stage gsout` has no instance rather than reading `gsout` as one. `at` only advances
    // over positionals, so an option's value is never mistaken for one.
    int instance = 0, maxRows = 16;
    size_t at = 2;
    if(at < args.size() && !IsOption(args[at]))
      instance = ToInt(args[at++], instance);
    if(at < args.size() && !IsOption(args[at]))
      maxRows = ToInt(args[at++], maxRows);
    MeshDataStage stage = MeshDataStage::VSOut;
    const char *stageName = OptValue(args, "--stage", NULL);
    if(stageName != NULL && !MeshStageFromName(stageName, stage))
      return Fail(2, "'%s' is not a mesh stage (%s)", stageName, MeshStageNames(", ").c_str());
    return CmdMesh(ctrl, file, path, ToInt(args[1], 0), instance, maxRows, stage,
                   OptValue(args, "--obj", NULL));
  }
  if(!strcmp(cmd, "image") && args.size() > 2)
  {
    PictureOptions opts;
    std::string why;
    if(!PictureOptionsFromArgs(args, opts, why))
      return Fail(2, "%s", why.c_str());
    return CmdImage(ctrl, file, path, ToInt(args[1], 0), args[2].c_str(), opts);
  }
  if(!strcmp(cmd, "counters"))
    return CmdCounters(ctrl, file, path, HasOpt(args, "--per-pass"),
                       OptValue(args, "--passes", NULL), ToInt(OptValue(args, "--top", "5"), 5));
  if(!strcmp(cmd, "crosscheck"))
    // The id is optional, so `args[1]` is only one when it is not an option -- `crosscheck --since
    // 1` would otherwise ask `ToInt` to read `--since` and warn about it.
    return CmdCrosscheck(
        ctrl, file, path, CommandHasPositionalId(cmd, args) ? ToInt(args[1], 0) : 0,
        ToInt(OptValue(args, "--since", "0"), 0), ToInt(OptValue(args, "--until", "0"), 0),
        ToInt(OptValue(args, "--max-events", "0"), 0), ToInt(OptValue(args, "--max", "200"), 200));
  if(!strcmp(cmd, "watch") && args.size() > 1)
    return CmdWatch(ctrl, file, path, args);
  if(!strcmp(cmd, "debug"))
    return CmdDebug(ctrl, file, path, args);
  if(!strcmp(cmd, "trace"))
  {
    // The event is the only positional and every other argument is an option, so a caller who wrote
    // `trace --pixel 12,34` gets told what is missing rather than the dispatcher's fall-through
    // (`unknown command`), which is what a command with no argument at all otherwise ends in.
    if(args.size() <= 1)
      return Fail(
          2,
          "trace needs the event to run at: `trace <eid> --pixel <x,y>` (or --vertex / --thread "
          "/ --mesh-thread)");
    TraceRequest req;
    std::string why;
    if(!TraceRequestFromArgs(args, req, why))
      return Fail(2, "%s", why.c_str());
    return CmdTrace(ctrl, file, path, ToInt(args[1], 0), req);
  }
  if(!strcmp(cmd, "usage") && args.size() > 1)
    return CmdUsage(ctrl, file, path, args[1].c_str());
  if(!strcmp(cmd, "probe"))
  {
    // No argument means the whole frame (0 = "to the frame's own last event", commands_frame.cpp),
    // and a number caps the scan where it always did. The word is accepted as well as the default
    // because "the whole frame" is a thing a reader asks for by name after reading that a capped
    // range can come back empty on a big capture.
    if(args.size() <= 1 || args[1] == "last" || args[1] == "all")
      return CmdProbe(ctrl, file, path, 0);
    int cap = 0;
    if(!ParseInt(args[1].c_str(), cap) || cap <= 0)
      return Fail(2, "probe: '%s' is not an event id or 'last'", args[1].c_str());
    return CmdProbe(ctrl, file, path, cap);
  }
  if(!strcmp(cmd, "dump"))
    return CmdDump(ctrl, file, path, args, bWantDisasm);
  if(!strcmp(cmd, "multi"))
    return CmdMulti(ctrl, file, path, args);
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

//: `text` without a leading UTF-8 BOM. An editor's "UTF-8 with BOM" save, or a PowerShell pipe,
//: puts one there -- and it is invisible in every editor that matters, so the first token becomes
//: `\xEF\xBB\xBFstate` and the only symptom is "unknown command". Stripped rather than diagnosed.
std::string WithoutBom(const std::string &text)
{
  if(text.size() >= 3 && (unsigned char)text[0] == 0xEF && (unsigned char)text[1] == 0xBB &&
     (unsigned char)text[2] == 0xBF)
    return text.substr(3);
  return text;
}

//: The body `batch`, `--repl`/`--stdin` and `multi` all are: one line at a time against one open
//: capture.
//:
//: Factored out when `multi` was added rather than written a third time, because the expensive part
//: is the session they share and the subtle parts are the same in all three: the `#=== <line>`
//: marker that lets a caller split the stream back into one output per command, `probe`'s rule (it
//: forces non-events, which leaves stale state behind, so it belongs in its own run -- whichever of
//: the two ran second would answer wrong), and keeping the worst exit code so a scripted run still
//: gets a useful status.
struct CommandSequence
{
  const char *what = "batch";
  int ret = 0;
  int ran = 0;
  bool sawProbe = false;
  bool sawOther = false;

  //: Runs one line; blank and `#` lines are skipped, as in a batch file, and `false` says skipped
  //: rather than run -- which is also what a line that splits to nothing is.
  bool RunLine(IReplayController *ctrl, ICaptureFile *file, const char *path, const std::string &line)
  {
    const size_t first = line.find_first_not_of(" \t");
    if(first == std::string::npos || line[first] == '#')
      return false;

    const std::string text = WithoutBom(line.substr(first));
    std::vector<std::string> args;
    bool bJson = false, bDisasm = false;
    std::string saveDir;
    SplitLine(text, args, bJson, bDisasm, saveDir);
    if(args.empty())
      return false;

    const bool bIsProbe = args[0] == "probe";
    sawProbe = sawProbe || bIsProbe;
    sawOther = sawOther || !bIsProbe;

    // The marker is what lets a caller split the stream back into one output per command. It is
    // printed in both formats: JSON has no comment syntax, and guessing where one object ends and
    // the next begins is not something a consumer should have to do.
    printf("#=== %s\n", text.c_str());
    fflush(stdout);

    g_bJson = bJson;
    const ULONGLONG started = Millis();
    const int code =
        DispatchCommand(ctrl, file, path, args, bDisasm, saveDir.empty() ? NULL : saveDir.c_str());
    ran++;
    ret = (code != 0) ? code : ret;
    Log("%s %d: %s -> exit %d in %.1fs", what, ran, text.c_str(), code,
        (Millis() - started) / 1000.0);
    return true;
  }

  //: The warning the sequence owes at the end, and the count that says how much the session carried.
  void Finish() const
  {
    if(sawProbe && sawOther)
    {
      Log("warning: this %s mixes `probe` with other commands; probe forces non-events, which "
          "leaves "
          "stale state behind, so run it on its own",
          what);
    }
    Log("%s finished: %d command(s)", what, ran);
  }
};

//: Runs a file of command lines against one open capture. The point is the cost of a replay
//: session, not the cost of the commands: standing the engine up and opening the capture is ~7 s on
//: a 1.5 GB capture, and this pays it once for the whole file.
int CmdBatch(IReplayController *ctrl, ICaptureFile *file, const char *path,
             const std::filesystem::path &batchPath)
{
  FILE *f = FileOpen(batchPath, "rb");
  if(f == NULL)
    return Fail(2, "cannot read batch file %s", batchPath.string().c_str());

  CommandSequence seq;
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

    seq.RunLine(ctrl, file, path, line);

    if(ch == EOF)
      break;
    line.clear();
  }

  fclose(f);
  seq.Finish();
  return seq.ret;
}

//: Several commands on one command line against one open capture:
//: `multi <capture> "state 413" "shaders 413" "crosscheck 413"`.
//:
//: The fourth spelling of one idea -- `batch` for a file, `--stdin`/`--repl` for a pipe, the
//: library for a caller with its own handle -- for the caller that has the lines in hand and no
//: file to put them in. It exists because of what a session costs: measured on the 1.55 GB UE
//: capture, `info` is 7.07 s and `state` 7.30 s, while six commands through one session are 7.14 s
//: in total, so a command run on its own is ~7 s of engine around ~6 ms of work.
//:
//: One *line* is one argument, so the shell decides where lines end and a line with a quote in it
//: needs that quote escaped the way the shell asks. Everything else is a batch file's own syntax:
//: options included, `#` lines skipped, and the same `#=== <line>` markers on the way out.
int CmdMulti(IReplayController *ctrl, ICaptureFile *file, const char *path,
             const std::vector<std::string> &args)
{
  if(args.size() < 2)
  {
    return Fail(
        2,
        "multi needs at least one command line: `multi <rdc> \"state 413\" \"shaders 413\"` "
        "(one session, so the ~7 s of standing the engine up is paid once)");
  }

  CommandSequence seq;
  seq.what = "multi";
  for(size_t i = 1; i < args.size(); i++)
    seq.RunLine(ctrl, file, path, args[i]);
  seq.Finish();
  return seq.ret;
}

//: `--repl` / `--stdin`: commands from the terminal (or a pipe) against one open capture.
//:
//: `batch` already pays the device setup once for a file of commands, and this is the same loop with
//: stdin as the file -- because what is being avoided is the ~4-11 s of standing a session up per
//: question, not the file. `--repl` prints a prompt; `--stdin` is the same without one, for a pipe.
//: `help` prints the usage, `quit`/`exit` (or end of input) leaves.
//:
//: A failing line does not end the session: every command returns a code and `Fail` logs and returns
//: rather than exiting, which is also why `batch` can run a failing line and carry on to the next. The
//: loop keeps the worst code it saw, so a scripted pipe still gets a useful exit status.
int CmdRepl(IReplayController *ctrl, ICaptureFile *file, const char *path, bool bPrompt)
{
  CommandSequence seq;
  seq.what = "repl";
  std::string line;
  if(bPrompt)
    printf("%s is open: one command per line, `help` for the list, `quit` to leave.\n", path);

  for(;;)
  {
    if(bPrompt)
    {
      printf("rdc> ");
      fflush(stdout);
    }
    line.clear();
    int ch = 0;
    while((ch = fgetc(stdin)) != EOF && ch != '\n')
    {
      if(ch != '\r')
        line += (char)ch;
    }
    const bool bAtEnd = (ch == EOF);
    const size_t first = line.find_first_not_of(" \t");
    const std::string text =
        first == std::string::npos ? std::string() : WithoutBom(line.substr(first));
    if(text == "quit" || text == "exit")
      break;
    if(text == "help" || text == "--help")
    {
      Usage();
      if(bAtEnd)
        break;
      continue;
    }

    // `RunLine` skips a blank line and a `#` comment itself -- which is what an empty `text` and a
    // `#` one are -- so there is nothing to test for here.
    if(!text.empty())
      seq.RunLine(ctrl, file, path, text);
    if(bAtEnd)
      break;
  }

  seq.Finish();
  return seq.ret;
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
  bool bPerRunLog = true;              // until `--log` names one exact file
  std::string schemaOut;               // `--out <dir>`: where `schema` writes them
  std::string schemaCheck;             // `--check <dir>`: the copy to verify against
  std::vector<std::string> pdbDirs;    // `--pdb <dir>`, repeatable: the shader debug search paths
  // `$RDC_PDB` is the same list from the environment, and it is the *only* way in for a caller that
  // does not write the command line: the library (api.cpp) takes no options of its own, and
  // `sweep`/`batch` run lines that have no room for a global flag. `;` separates, as it does in
  // every path list on Windows, and the flag adds to what the environment asked for rather than
  // replacing it -- "where to look" is a list where more places to look is never the surprising
  // reading.
  if(const char *env = getenv("RDC_PDB"))
  {
    std::string list = env;
    size_t at = 0;
    while(at <= list.size())
    {
      const size_t end = list.find(';', at);
      const std::string dir =
          list.substr(at, end == std::string::npos ? std::string::npos : end - at);
      if(!dir.empty())
        pdbDirs.push_back(dir);
      if(end == std::string::npos)
        break;
      at = end + 1;
    }
  }
  bool bRepl = false, bReplQuiet = false;    // `--repl` / `--stdin`: commands from stdin
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
    else if(!strcmp(argv[i], "--dll") && i + 1 < argc)
      g_DllOverride = argv[++i];
    else if(!strcmp(argv[i], "--pdb") && i + 1 < argc)
      pdbDirs.push_back(argv[++i]);
    else if(!strcmp(argv[i], "--repl"))
      bRepl = true;
    else if(!strcmp(argv[i], "--stdin"))
      bReplQuiet = true;
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

  // `--repl`/`--stdin` name no command: the capture is the only argument, and the commands come from
  // stdin. Spelling it as a command (`repl <capture>`) inside the program is what lets the rest of
  // this function -- the log, the warning, the session setup, the dispatch below -- stay one path.
  if(bRepl || bReplQuiet)
    args.insert(args.begin(), std::string(bRepl ? "repl" : "stdin"));

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

  // The one environment prerequisite this program has an opinion about, checked here rather than at
  // startup so `--help`, `schema` and `selftest` say nothing about it: everything below this line
  // opens the capture and answers with ids the offline tool will want to name.
  WarnIfRenderdocSrcMissing();

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

  // Into the log rather than only to stderr: a stale run has to say so in the file it leaves
  // behind, which is the one place a reader compares two runs by. It sits after the log opens and
  // before the working directory, so it is the first thing the log says about the run itself.
  WarnIfDriverIsStale();

  // Every path is made absolute here, and the working directory is logged: a path that only works
  // from one directory is otherwise indistinguishable from a missing file. What the *engine* is
  // handed is the narrow form of a path the driver resolved once (`pathAbsText`), because its ABI
  // takes `const char *`
  // -- and `path` stays the caller's own spelling, which is what the output shows them.
  const std::filesystem::path pathAbs = AbsolutePath(args[1].c_str());
  const std::string pathAbsText = pathAbs.string();
  const char *path = args[1].c_str();    // as given: what the output shows
  std::filesystem::path saveDirAbs, batchPathAbs;
  if(saveDir != NULL)
    saveDirAbs = AbsolutePath(saveDir);
  if(args.size() > 2 && !strcmp(cmd, "batch"))
    batchPathAbs = AbsolutePath(args[2].c_str());

  Log("working directory: %s", WorkingDirectory().c_str());
  Log("loading renderdoc.dll");
  HMODULE dll = LoadReplayDLL();
  if(dll == NULL)
    return Fail(1, "cannot load renderdoc.dll");

  // Before the replay system and the device: a capture recorded by a newer RenderDoc than the
  // engine just loaded is refused here, with both versions named, rather than by whatever the
  // engine would otherwise do with a file it does not know.
  const int versionCode = GuardCaptureVersion(pathAbsText.c_str(), path);
  if(versionCode != 0)
    return versionCode;

  Log("starting the replay system");
  if(!InitialiseReplay(dll, argc, argv))
    return Fail(1, "RENDERDOC_InitialiseReplay failed");
  const ReplaySystemGuard replaySystem;    // declared first, so it shuts down last

  // `--pdb` sits here on purpose: after the engine has read its config (which is what
  // `RENDERDOC_InitialiseReplay` does) and before anything loads a shader, so the paths are in
  // place for the first capture open and nothing can overwrite them. A directory that is not there
  // is said out loud
  // -- the engine searches a path it cannot read exactly as it searches an empty one, and the
  // difference between "the PDB is elsewhere" and "you typed the folder wrong" is one this command
  // line already knows.
  if(!pdbDirs.empty())
  {
    for(const std::string &missing : MissingShaderDebugPaths(pdbDirs))
      fprintf(stderr, "warning: --pdb %s is not a directory, so nothing will be found under it\n",
              missing.c_str());

    if(SetShaderDebugPaths(dll, pdbDirs))
    {
      for(const std::string &dir : pdbDirs)
        Log("shader debug search path: %s", AbsolutePath(dir).string().c_str());
    }
  }

  Trace("RENDERDOC_OpenCaptureFile");
  ICaptureFile *file = OpenCaptureFile(dll);
  if(file == NULL)
    return 1;
  const CaptureFileGuard captureFile(file);

  // The capture is opened by its absolute path (a relative one depends on the working directory),
  // while the *output* keeps the path as it was given, so a command's text is the same however it
  // was invoked. The absolute form and the working directory go to the log, where a wrong path is
  // the thing being diagnosed.
  Log("reading the container of %s", pathAbsText.c_str());
  Trace("OpenFile");
  const ResultDetails res = file->OpenFile(pathAbsText.c_str(), "rdc", NULL);
  if(!res.OK())
    return Fail(1, "cannot open %s: %s", pathAbsText.c_str(), ResultText(res).c_str());

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
  // Zeroing the whole struct rules out a layout disagreement with the DLL, and for the pointer and
  // vendor members all-zero *is* "no override". One member is not a bool, though, so it is worth
  // naming what this asks for rather than what it looks like: `optimisation` is
  // `ReplayOptimisationLevel` (an enum whose first member, `NoOptimisation`, is 0) where the
  // struct's own default is `Balanced`.
  //
  // That is deliberate, and it was measured rather than assumed. Three `info` runs per level on
  // the 1.55 GB UE capture, medians: none 7.07 s, balanced 6.90 s, fastest 6.85 s -- 0.2 s apart,
  // inside this machine's run-to-run spread (one `none` run took 8.86 s). The session is the
  // *engine opening the capture*, so the level buys nothing worth having, while `Balanced`'s
  // documented trade -- "resources appearing cleared instead of containing contents from prior
  // frames where those resources are written to before being read" -- is exactly the kind of answer
  // a `state` document or a `crosscheck` row must not change quietly. The most faithful level is
  // therefore the one a diagnostic tool should ask for.
  const rdcpair<ResultDetails, IReplayController *> opened = file->OpenCapture(opts, NULL);
  Trace("OpenCapture returned");
  if(!opened.first.OK())
    return Fail(1, "cannot replay %s: %s", path, ResultText(opened.first).c_str());
  IReplayController *ctrl = opened.second;
  const ControllerGuard controller(ctrl);
  Log("replay ready");

  int ret;
  const ULONGLONG started = Millis();
  if(!strcmp(cmd, "repl") || !strcmp(cmd, "stdin"))
  {
    ret = CmdRepl(ctrl, file, path, !strcmp(cmd, "repl"));
  }
  else if(!strcmp(cmd, "batch") && args.size() > 2)
  {
    ret = CmdBatch(ctrl, file, path, batchPathAbs);
  }
  else
  {
    // The command, then everything after the capture path: `DispatchCommand` takes the command as
    // `args[0]` and never the capture, exactly as a batch line spells it (`state 270`).
    std::vector<std::string> cmdArgs;
    cmdArgs.push_back(args[0]);
    cmdArgs.insert(cmdArgs.end(), args.begin() + 2, args.end());
    ret = DispatchCommand(ctrl, file, path, cmdArgs, bWantDisasm,
                          saveDirAbs.empty() ? NULL : saveDirAbs.string().c_str());
  }
  Log("done: exit %d after %.1fs", ret, (Millis() - started) / 1000.0);

  // The breakdown goes last, after the exit line, so the log's answer to "what happened" is unchanged and
  // the answer to "where did the minutes go" is underneath it -- and only when `$RDC_PROFILE` asked.
  ProfileReport();

  // The controller, the capture file and the replay system are torn down by the guards above, in
  // that order, as this function returns.
  //
  // The DLL is deliberately not freed: the objects are owned by it, and RenderDoc's own tools let
  // the process exit instead of unloading the engine underneath its own state.
  if(g_LogFile != NULL)
    fclose(g_LogFile);
  return ret;
}
