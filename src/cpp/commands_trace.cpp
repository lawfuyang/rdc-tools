// z.commands_trace — `trace`: one shader invocation, stepped (REFERENCE §9)
//
// Every other command in this program reads what the frame *did*: what was bound at an event, what
// a constant block held, which pixels a draw wrote. This one runs a single invocation of a shader
// and reports what happened *inside* it, instruction by instruction — the question no read of the
// file can reach, and the reason the engine's own debugger is worth a command.
//
// Three facts about that debugger shape everything below, and each of them is a decision rather
// than a detail:
//
//  * **A trace is one stage.** `DebugPixel`, `DebugVertex`, `DebugThread` and `DebugMeshThread`
//  each run
//    one shader; a pixel's whole history is *two* traces (the vertex shader that made its inputs,
//    then the pixel shader), and this command runs the one whose invocation the caller named. Which
//    stage that is follows from the selector, and the document prints `trace->stage` — the engine's
//    own answer — rather than the selector's, so a mismatch would be visible rather than assumed
//    away.
//
//  * **What the engine needs to step a shader depends on what the shader is.** A DXBC (SM5) shader
//  is
//    interpreted from its own bytecode, so it steps with no debug info at all — what debug info
//    adds there is the *source*: the file and line per instruction (`instInfo`) and the
//    source-level names
//    (`sourceVars`). A DXIL shader (DXC, and every UE shader in this project's captures) is stepped
//    *through* the debug data DXC emitted, so without it there is no trace at all. Measured, on the
//    Android capture: `trace 289 --pixel 640,360` answers `sourceDebugInfo is 0` and the engine's
//    loading log says `Did not find debug data for '<hash>.pdb'` — the search for a PDB that is not
//    beside the capture, and one the caller can point the engine at with `--pdb <dir>` (`main`
//    writes the engine's own shader-debug search paths, which are otherwise a UI-only setting). So
//    which of the two a reader has is *said* (`sourceDebugInfo`) rather than assumed, and the
//    refusal names both the file the engine went looking for and the flag that answers it.
//
//  * **A trace that could not be run comes back empty.** The engine answers NULL, or a trace whose
//    `debugger` is NULL (RenderDoc's own callers treat that second form as invalid too — every
//    viewer in qrenderdoc tests `trace->debugger == NULL`), and the *reason* is not in the return
//    value: it keeps it in its log and, for a shader it knows it cannot run, in
//    `ShaderDebugInfo::debugStatus`. The refusal below is therefore built out of what the
//    reflection actually holds, says which causes it can and cannot tell apart, and names the fix
//    rather than printing "failed".
//
// What is printed is the three things the roadmap's item asks for: the invocation's **inputs** (the
// values its first instruction sees), one row per **step** with the variables that changed on it,
// and the **outputs** — the variable list after the last step, which is what the invocation ended
// up producing. The outputs are accumulated the way RenderDoc's own UI accumulates them
// (`ShaderViewer`): a change's name is its `before` name when it has one and its `after` name
// otherwise, an empty `after` name is a variable leaving scope, and every other change is a new
// value. That rule lives in `TraceApplyChange` below, on its own, because it is the one piece of
// this command that is wrong *quietly* — and the selftest pins it without needing a device.

#include "common.h"

namespace
{

//: The engine's shader-loading log, whose *first* line is the one that says what happened ("Found
//: debug data in the shader", "Did not find debug data for '<hash>.pdb'"). The rest of it is the
//: search: the default file name it derived and every path it tried, which is what a reader wants
//: only when the first line is a failure -- so the sentence below quotes the first line and names
//: the file the whole log is in.
std::string FirstLogLine(const rdcstr &log)
{
  std::string text = log.c_str();
  const size_t end = text.find('\n');
  if(end != std::string::npos)
    text = text.substr(0, end);
  while(!text.empty() && (text[text.size() - 1] == '\r' || text[text.size() - 1] == ' '))
    text.erase(text.size() - 1);
  return text;
}

//: The same log, whole, with every line indented so it sits under the sentence that introduces it.
//: For the case where the engine found nothing: each line is a path it tried or a place it decided
//: not to look, and that list *is* the answer to "where does this PDB have to be?".
std::string IndentedLog(const rdcstr &log)
{
  std::string out;
  const std::string text = log.c_str();
  size_t at = 0;
  while(at < text.size())
  {
    const size_t end = text.find('\n', at);
    const std::string line = text.substr(at, end == std::string::npos ? std::string::npos : end - at);
    if(!line.empty())
      out += Fmt("\n       %s", line.c_str());
    if(end == std::string::npos)
      break;
    at = end + 1;
  }
  return out;
}

//: The reason a trace could not be run, as a sentence: the facts the reflection holds, then what to do
//: about them, in the order a reader can act. Two measured cases shape it, and they need *different*
//: advice -- the engine's answer is the same empty trace for both:
//:
//:  * a DXIL shader whose debug data is absent (`sourceDebugInfo: 0`, the Android capture): the engine
//:    had nothing to step, and its loading log names the file it went looking for;
//:  * a shader that *has* its debug data (`sourceDebugInfo: 1`, the HobbyRenderer capture) and still
//:    produced no trace: the data was found ("Found debug data in the shader") and the invocation is what
//:    failed, which for a pixel is usually a co-ordinate no fragment wrote.
//:
//: So this branches on that one field rather than guessing, and points at `--vertex`/`--thread` as the way
//: to tell the two apart: an invocation with no fragment to find cannot fail for want of one.
std::string NoTraceText(const TraceRequest &req, int eid, ShaderStage stage,
                        const D3D12Pipe::Shader *sh, const ShaderDebugInfo *info)
{
  std::string out =
      Fmt("the engine produced no trace for %s at eid %d (%s, res%s)",
          TraceInvocationText(req).c_str(), eid, StageName(stage), IdText(sh->resourceId).c_str());

  if(info != NULL)
  {
    out += Fmt(": sourceDebugInfo is %s", info->sourceDebugInformation ? "1" : "0");
    if(!info->debugStatus.empty())
      out += Fmt(", and the engine says '%s'", info->debugStatus.c_str());
  }
  else
  {
    // No reflection at all: the engine's answer to `GetShader` for this event was empty, which is a
    // different situation from a shader it refused to debug (that one is refused above, in the
    // engine's own words), and saying so is more use than silence.
    out += ": the engine published no reflection for this shader";
  }

  if(info != NULL && !info->debugInfoLoadingLog.empty())
  {
    // A *missing* debug-data case is a search, and every line of the log is part of the answer: which
    // paths the engine tried is exactly what says where the PDB has to be, or what to pass to `--pdb`.
    // With debug data present the rest of the log is noise, so that case keeps the first line only.
    if(info->sourceDebugInformation)
      out += Fmt("\n       its shader loading log says: %s",
                 FirstLogLine(info->debugInfoLoadingLog).c_str());
    else
      out += Fmt("\n       and its shader loading log is the search, in the engine's own words:%s",
                 IndentedLog(info->debugInfoLoadingLog).c_str());
  }

  if(info != NULL && !info->sourceDebugInformation)
  {
    out +=
        "\n       the debug data is what the engine steps a DXIL shader through, so it has to be "
        "findable: embedded in the shader with `-Zi -Qembed_debug`, or the PDB the shader names "
        "(the file "
        "the log above is about) in a folder given as `--pdb <dir>` -- the engine searches those "
        "directories "
        "recursively, by file name, and the name is a hash, so the folder is the part worth "
        "naming. A "
        "DXBC "
        "(SM5) shader is stepped from its own bytecode and needs none "
        "of this, so if that is what this capture holds, the cause is the invocation instead.";
  }
  else
  {
    out +=
        "\n       the shader has its debug data, so the *invocation* is what the engine could not "
        "run: "
        "for a pixel, a co-ordinate no fragment wrote or none that passed the depth test at it "
        "(`state "
        "<eid>` shows the target and its viewport); for a vertex or a thread, an index outside the "
        "draw "
        "or dispatch; or a feature its interpreter does not implement. `--vertex 0`/`--thread "
        "0,0,0 "
        "0,0,0` on the same event is the cheapest way to tell an empty co-ordinate from an engine "
        "limitation, because an invocation with no fragment to find cannot fail for want of one. "
        "The "
        "engine's own log (`--log <file>`) has whatever it was willing to say.";
  }

  return out;
}

//: `FreeTrace` on every path out of the command, `Fail` returns included. A trace owns an
//: engine-side debugger -- `ReplayController` keeps a list of them and frees them in `FreeTrace` --
//: so a return that misses it leaks one per run, and `--repl` or a `batch` file runs many.
class TraceGuard
{
public:
  TraceGuard(IReplayController *ctrl, ShaderDebugTrace *trace) : m_Ctrl(ctrl), m_Trace(trace) {}
  ~TraceGuard()
  {
    if(m_Trace != NULL)
      m_Ctrl->FreeTrace(m_Trace);
  }
  TraceGuard(const TraceGuard &) = delete;
  TraceGuard &operator=(const TraceGuard &) = delete;

  ShaderDebugTrace *Get() const { return m_Trace; }

private:
  IReplayController *m_Ctrl;
  ShaderDebugTrace *m_Trace;
};

//: A JSON array of strings, escaped. The step rows carry a nested list (the variables that changed
//: on that step) and `Row`'s one-string-per-line shape has nowhere to put it, so the one place in
//: this command that writes an object by hand writes its own array too.
std::string JsonStringArray(const std::vector<std::string> &items)
{
  std::string out = "[";
  for(size_t i = 0; i < items.size(); i++)
    out += (i ? ", \"" : "\"") + JsonEscape(items[i]) + "\"";
  return out + "]";
}

//: The callstack as one string, oldest function first — the order `ShaderDebugState` documents
//: ("the oldest/outer function is first in the list, the newest/inner function is last"), which is
//: also the order a reader says it in.
std::string CallstackText(const rdcarray<rdcstr> &stack)
{
  std::string out;
  for(size_t i = 0; i < stack.size(); i++)
    out += (i ? " > " : "") + std::string(stack[i].c_str());
  return out;
}

}    // namespace

// --------------------------------------------------------------------------- the vocabulary

const char *TraceInvocationName(TraceInvocation kind)
{
  switch(kind)
  {
    case TraceInvocation::Pixel: return "pixel";
    case TraceInvocation::Vertex: return "vertex";
    case TraceInvocation::Thread: return "thread";
    case TraceInvocation::MeshThread: return "mesh-thread";
    default: return "?";
  }
}

std::string TraceInvocationText(const TraceRequest &req)
{
  switch(req.m_Kind)
  {
    case TraceInvocation::Pixel: return Fmt("pixel %u,%u", req.m_X, req.m_Y);
    case TraceInvocation::Vertex:
      return Fmt("vertex %u,%u,%u,%u", req.m_VertId, req.m_InstId, req.m_Index, req.m_VertexView);
    case TraceInvocation::Thread:
      return Fmt("thread %u,%u,%u %u,%u,%u", req.m_Group[0], req.m_Group[1], req.m_Group[2],
                 req.m_Thread[0], req.m_Thread[1], req.m_Thread[2]);
    case TraceInvocation::MeshThread:
      return Fmt("mesh-thread %u,%u,%u %u,%u,%u", req.m_Group[0], req.m_Group[1], req.m_Group[2],
                 req.m_Thread[0], req.m_Thread[1], req.m_Thread[2]);
    default: return "no invocation";
  }
}

ShaderStage TraceStage(TraceInvocation kind)
{
  // The stage each API runs. `DebugPixel` is the pixel shader by definition, `DebugThread` the compute
  // one, `DebugMeshThread` the mesh one -- so the selector *is* the stage, and there is no separate
  // `--stage`: a `--stage ps` alongside `--thread` could only disagree with the call about to be made.
  switch(kind)
  {
    case TraceInvocation::Pixel: return ShaderStage::Pixel;
    case TraceInvocation::Vertex: return ShaderStage::Vertex;
    case TraceInvocation::Thread: return ShaderStage::Compute;
    case TraceInvocation::MeshThread: return ShaderStage::Mesh;
    default: return ShaderStage::Invalid;
  }
}

std::string TraceFlagsText(ShaderEvents flags)
{
  // The engine's three flags (`ShaderEvents`), by the name the reader needs rather than the enum's:
  // what makes each one interesting is the question it answers. A bit this build does not know is
  // *said* rather than dropped -- `0x8` is more use than silence, and it is how a newer engine's
  // flag shows up as itself instead of as "none".
  static const struct
  {
    uint32_t m_Bit;
    const char *m_Text;
  } kFlags[] = {
      {(uint32_t)ShaderEvents::SampleLoadGather, "sample/load/gather"},
      {(uint32_t)ShaderEvents::GeneratedNanOrInf, "nan/inf"},
      {(uint32_t)ShaderEvents::DebugBreak, "debugbreak"},
  };

  const uint32_t bits = (uint32_t)flags;
  std::string out;
  uint32_t known = 0;
  for(size_t i = 0; i < sizeof(kFlags) / sizeof(kFlags[0]); i++)
  {
    if((bits & kFlags[i].m_Bit) == 0)
      continue;
    out += out.empty() ? kFlags[i].m_Text : Fmt(", %s", kFlags[i].m_Text);
    known |= kFlags[i].m_Bit;
  }
  if(bits & ~known)
  {
    // `~known` picks the name up from the engine's own definition of the flag rather than from a
    // number written here, which is what keeps this honest across versions.
    const uint32_t unknown = bits & ~known;
    out += out.empty() ? Fmt("0x%x", unknown) : Fmt(", 0x%x", unknown);
  }
  return out.empty() ? std::string("none") : out;
}

std::string TraceChangeText(const ShaderVariableChange &change)
{
  // Three shapes, and the two empty-name cases are the ones a reader would otherwise misread: a
  // variable that came into scope has no `before`, and one that went out of scope has no `after`.
  // The engine's own wording for the second is that the variable "stopped existing" on this step.
  if(change.before.name.empty())
    return Fmt("%s = %s (new)", change.after.name.c_str(), FormatValue(change.after).c_str());
  if(change.after.name.empty())
    return Fmt("%s left scope", change.before.name.c_str());
  return Fmt("%s: %s -> %s", change.before.name.c_str(), FormatValue(change.before).c_str(),
             FormatValue(change.after).c_str());
}

void TraceApplyChange(std::map<std::string, std::string> &vars, const ShaderVariableChange &change)
{
  // The name is the `before` name when there is one and the `after` name otherwise, and an empty
  // `after` name is a removal: RenderDoc's own UI applies a state this way
  // (`ShaderViewer::AddCurrentState`), and getting it wrong is invisible -- the list simply holds a
  // variable that no longer exists, or loses one that does, under a name the reader is looking for.
  const std::string name = change.before.name.empty() ? std::string(change.after.name.c_str())
                                                      : std::string(change.before.name.c_str());
  if(name.empty())
    return;    // a change with no name on either side says nothing about any variable

  if(change.after.name.empty())
  {
    vars.erase(name);
    return;
  }
  vars[name] = FormatValue(change.after);
}

std::string TraceSourceAt(const ShaderDebugTrace &trace, const ShaderReflection *refl,
                          uint32_t instruction)
{
  // `instInfo` is **not** indexed by instruction -- `ShaderDebugTrace`'s own warning says so: it
  // holds the *unique* mappings in instruction order, and the last one at or below the instruction
  // asked about is the one that applies. Hence the binary search: the array is one entry per
  // instruction in the worst case, and a linear scan per step would turn a 10,000-step trace into
  // minutes of nothing.
  const rdcarray<InstructionSourceInfo> &info = trace.instInfo;
  size_t lo = 0, hi = info.size();
  while(lo < hi)
  {
    const size_t mid = lo + (hi - lo) / 2;
    if(info[mid].instruction <= instruction)
      lo = mid + 1;
    else
      hi = mid;
  }
  if(lo == 0)
    return std::string();    // nothing at or below it: no source mapping covers this instruction

  const LineColumnInfo &line = info[lo - 1].lineInfo;
  if(line.fileIndex < 0)
    return std::string();    // the mapping exists but names no file

  // The file *name* comes from the reflection's debug info, by index; a trace whose reflection is
  // missing (or whose index is out of range) still gets a location, because the index is still a fact.
  const int32_t index = line.fileIndex;
  std::string file;
  if(refl != NULL && (size_t)index < refl->debugInfo.files.size())
    file = refl->debugInfo.files[(size_t)index].filename.c_str();
  else
    file = Fmt("file%d", (int)index);

  if(line.lineStart == 0)
    return file;
  if(line.lineEnd > line.lineStart)
    return Fmt("%s:%u-%u", file.c_str(), (unsigned)line.lineStart, (unsigned)line.lineEnd);
  return Fmt("%s:%u", file.c_str(), (unsigned)line.lineStart);
}

// --------------------------------------------------------------------------- the command

//: `trace <rdc> <eid> --pixel <x,y> | --vertex <v[,inst[,idx[,view]]]> | --thread
//: <gx,gy,gz,tx,ty,tz>`
//: `| --mesh-thread <gx,gy,gz,tx,ty,tz> [--sample N] [--primitive N] [--view N] [--max-steps N]
//: [--all]`
//:
//: One shader invocation, stepped. The invocation is named rather than discovered, because there is
//: no sensible default: "the pixel at 0,0" is a question about the frame, not about the shader, and
//: a command that picked one would be answering a different question than the caller asked. Exactly
//: one selector is therefore required, and `TraceRequestFromArgs` (replay_dump.cpp, where the
//: option helpers live) refuses anything else before this runs.
//:
//: The document is three lists, which are the three things a trace holds: `inputs` (the values the
//: invocation started with), `steps` (one row per step: where it was, what events fired there, and
//: every variable that changed, as `before -> after`), and `outputs` (the variable list after the
//: last step). `sourceDebugInfo` says what the engine had to work with -- and on a DXIL capture it
//: is also the difference between a trace and no trace at all (`NoTraceText`, above) -- while
//: `steps`, `outputs` and `truncated` say how much of the invocation the document actually holds.
int CmdTrace(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
             const TraceRequest &req)
{
  const ShaderStage stage = TraceStage(req.m_Kind);

  // The capability gate first, and in the engine's terms (`APIProperties.shaderDebugging`, the same
  // bit `info` prints): a driver that cannot debug shaders *at all* is a different answer from a
  // shader it cannot run, and the two need different fixes from the reader.
  const APIProperties props = ctrl->GetAPIProperties();
  if(!props.shaderDebugging)
    return Fail(1,
                "this capture's driver does not support shader debugging (`info` prints it as "
                "`shaderDebugging: 0`): the engine can say what was bound at an event and what its "
                "values were, but not what the shader did with them");

  MoveToEvent(ctrl, eid);

  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  const D3D12Pipe::Shader *sh = StageShader(d3d12, stage);
  if(sh == NULL || sh->resourceId == ResourceId::Null())
    return Fail(1, "no %s shader is bound at eid %d", StageName(stage), eid);

  // The reflection of the shader the engine *bound*, not of the entry point it would disassemble by
  // default (`BoundReflection`): the debug info hangs off this one, and a shader with several entry
  // points can have it for one of them.
  const ShaderReflection *refl = BoundReflection(ctrl, d3d12, stage, sh);
  const ShaderDebugInfo *info = refl != NULL ? &refl->debugInfo : NULL;

  // `debuggable` is the engine's own verdict and `debugStatus` its own sentence ("a simple
  // explanation of why the shader is not supported for debugging", in the API's words). When they
  // are there, nothing here has to guess -- and the shader is refused *before* a trace is asked
  // for, so the answer is the engine's rather than an empty trace's.
  if(info != NULL && !info->debuggable)
    return Fail(1, "res%s (%s at eid %d) cannot be debugged: %s", IdText(sh->resourceId).c_str(),
                StageName(stage), eid,
                info->debugStatus.empty() ? "the engine says so and gives no reason"
                                          : info->debugStatus.c_str());

  // A default that the answer cannot show: `DebugVertex`'s third id is the index the vertex
  // *inputs* are read with, which the vertex id stands in for on a non-indexed draw and does not on
  // an indexed one. It is logged rather than left implicit, because "the values are wrong" and "the
  // index was the wrong one" look identical in a trace.
  if(req.m_Kind == TraceInvocation::Vertex && !req.m_bIndexGiven)
    Log("trace: --vertex %u: idx defaulted to the vertex id (%u) -- pass `v,inst,idx` for a draw "
        "that "
        "reads its vertex inputs through an index buffer",
        req.m_VertId, req.m_Index);

  // The four APIs, one of them. `DebugPixelInputs` defaults to `~0U` for each of its three fields,
  // which the API documents as "no preference": a random fragment writing that co-ordinate, no
  // particular sample, the first view. `TraceRequest` carries the same default, so "the caller did
  // not say" and "the engine chooses" stay the same thing.
  ShaderDebugTrace *trace = NULL;
  if(req.m_Kind == TraceInvocation::Pixel)
  {
    DebugPixelInputs inputs;
    inputs.sample = req.m_Sample;
    inputs.primitive = req.m_Primitive;
    inputs.view = req.m_PixelView;
    trace = ctrl->DebugPixel(req.m_X, req.m_Y, inputs);
  }
  else if(req.m_Kind == TraceInvocation::Vertex)
  {
    trace = ctrl->DebugVertex(req.m_VertId, req.m_InstId, req.m_Index, req.m_VertexView);
  }
  else
  {
    rdcfixedarray<uint32_t, 3> group, thread;
    group[0] = req.m_Group[0];
    group[1] = req.m_Group[1];
    group[2] = req.m_Group[2];
    thread[0] = req.m_Thread[0];
    thread[1] = req.m_Thread[1];
    thread[2] = req.m_Thread[2];
    trace = req.m_Kind == TraceInvocation::MeshThread ? ctrl->DebugMeshThread(group, thread)
                                                      : ctrl->DebugThread(group, thread);
  }

  // The two forms of "no trace", and the second is why this is not just a NULL test: the engine returns
  // a trace object whose `debugger` is NULL when it could not build one, and every caller in RenderDoc's
  // own Qt UI tests exactly this before touching the trace. Either way, it owns nothing to free.
  if(trace == NULL || trace->debugger == NULL)
  {
    if(trace != NULL)
      ctrl->FreeTrace(trace);
    return Fail(1, "%s", NoTraceText(req, eid, stage, sh, info).c_str());
  }

  // From here on the trace is the engine's until `FreeTrace`, on every path -- including the ones
  // below that only exist because a document was being written.
  const TraceGuard guard(ctrl, trace);
  const ShaderDebugTrace *t = guard.Get();

  // ------------------------------------------------------------------ the document
  PrintCaptureHeader(file, path);
  Field("event", (long long)eid);
  // What the *engine* says it debugged, and what it was asked for: two fields rather than one, so a
  // disagreement is visible (the selector picks the stage, and this is the check that it picked the
  // one the trace ran).
  Field("stage", std::string(StageName(t->stage)));
  Field("shader", IdText(sh->resourceId));
  Field("stageAsked", std::string(StageName(stage)));
  Field("invocation", TraceInvocationText(req));
  if(req.m_Kind == TraceInvocation::Pixel && req.m_Sample != kTraceNoPreference)
    Field("sample", (long long)req.m_Sample);
  if(req.m_Kind == TraceInvocation::Pixel && req.m_Primitive != kTraceNoPreference)
    Field("primitive", (long long)req.m_Primitive);
  if(req.m_Kind == TraceInvocation::Pixel && req.m_PixelView != kTraceNoPreference)
    Field("view", (long long)req.m_PixelView);
  if(info != NULL)
  {
    Flag("debuggable", info->debuggable);
    // The two fields the roadmap's item is about: whether the source half exists, and the engine's
    // own word when the shader is one it will not run. Printed on success too, because "it stepped,
    // but without source lines" is the answer most runs will give on a shipped capture.
    Flag("sourceDebugInfo", info->sourceDebugInformation);
    if(!info->debugStatus.empty())
      Field("debugStatus", info->debugStatus);
  }
  // What the trace carries besides the stepping: the counts, not the values. The values are `cb`'s
  // answer (and `state`'s), read through the reflection's names rather than through the interpreter's
  // own view of them -- printing them here a second time would be a second answer to one question.
  Field("constantBlocks", (long long)t->constantBlocks.size());
  Field("resources", (long long)(t->readOnlyResources.size() + t->readWriteResources.size()));
  Field("samplers", (long long)t->samplers.size());
  Field("sourceVariables", (long long)t->sourceVars.size());

  if(!IsJson())
    printf("inputs:\n");
  ArrayOpen("inputs");
  for(size_t i = 0; i < t->inputs.size(); i++)
    Row(Fmt("%s = %s", t->inputs[i].name.c_str(), FormatValue(t->inputs[i]).c_str()));
  ArrayClose(false);

  // The stepping. `ContinueDebug` runs an implementation-defined number of steps per call and
  // returns them; an empty list means the invocation has finished ("if the list is empty, the
  // debugging process has completed, further calls will return an empty list"). The cap is applied
  // *between* steps rather than between calls, because one call can return thousands and a cap the
  // engine could not see would be no cap at all.
  std::map<std::string, std::string> outputs;
  long long steps = 0;
  bool bTruncated = false;

  if(!IsJson())
    printf("steps:\n");
  ArrayOpen("steps");
  for(;;)
  {
    if(req.m_MaxSteps > 0 && steps >= req.m_MaxSteps)
    {
      bTruncated = true;
      break;
    }
    if(steps >= kTraceStepCap)
    {
      // `--all` on a shader that never leaves an unbounded loop would step until the process died;
      // a cap that is *said* is better than one that is discovered. No measured shader comes close.
      bTruncated = true;
      break;
    }

    const rdcarray<ShaderDebugState> batch = ctrl->ContinueDebug(t->debugger);
    if(batch.empty())
      break;    // the invocation ran to its end

    for(size_t i = 0; i < batch.size(); i++)
    {
      const ShaderDebugState &state = batch[i];
      if(req.m_MaxSteps > 0 && steps >= req.m_MaxSteps)
      {
        bTruncated = true;
        break;
      }
      steps++;

      std::vector<std::string> changes;
      for(size_t c = 0; c < state.changes.size(); c++)
      {
        changes.push_back(TraceChangeText(state.changes[c]));
        TraceApplyChange(outputs, state.changes[c]);
      }

      const std::string source = TraceSourceAt(*t, refl, state.nextInstruction);
      const std::string flags = TraceFlagsText(state.flags);
      const std::string stack = CallstackText(state.callstack);

      if(IsJson())
      {
        ObjectRow(Fmt(
            "{\"step\": %lld, \"instruction\": %u, \"events\": \"%s\", \"source\": \"%s\", "
            "\"callstack\": \"%s\", \"changes\": %s}",
            steps, (unsigned)state.nextInstruction, JsonEscape(flags).c_str(),
            JsonEscape(source).c_str(), JsonEscape(stack).c_str(), JsonStringArray(changes).c_str()));
      }
      else
      {
        // One line, fixed columns as far as the variable parts allow: the instruction and the
        // source location are what a reader scans for, and the changes follow after a `|` so that a
        // step with ten of them still reads as one step.
        std::string line = Fmt("step %-5lld instr %-6u %-10s", steps,
                               (unsigned)state.nextInstruction, flags.c_str());
        if(!source.empty())
          line += " " + source;
        if(!stack.empty())
          line += "  [" + stack + "]";
        printf("%s\n", line.c_str());
        for(size_t c = 0; c < changes.size(); c++)
          printf("        | %s\n", changes[c].c_str());
      }
    }
  }
  ArrayClose(false);

  if(!IsJson())
    printf("outputs (after %lld step(s)%s):\n", steps, bTruncated ? ", stopped early" : "");
  ArrayOpen("outputs");
  for(std::map<std::string, std::string>::const_iterator it = outputs.begin(); it != outputs.end();
      ++it)
    Row(Fmt("%s = %s", it->first.c_str(), it->second.c_str()));
  // Not the last member, although it is the last *list*: the three counts below are only known once the
  // stepping has run, and they belong next to it rather than in front of a number nobody has yet.
  ArrayClose(false);

  g_Indent = g_bJson ? 1 : 0;
  // `stepCount`, not `steps`: the array above owns that name, and two members with one key is a document
  // every parser resolves to the second -- the defect `bundle-verify` shipped once (schema.h).
  Field("stepCount", steps);
  // The three counts that make the lists above readable as a document: how long the invocation was, how
  // many variables it ended up with, and whether the run stopped before the end. `truncated` is what
  // stops "the outputs" from being read as "what the shader produced" when the cap was reached first.
  Field("variables", (long long)outputs.size());
  Flag("truncated", bTruncated, true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");    // `PrintCaptureHeader` opened it; every document closes its own

  Log("trace: %s of %s at eid %d: %lld step(s), %d variable(s)%s", TraceInvocationText(req).c_str(),
      StageName(t->stage), eid, steps, (int)outputs.size(),
      bTruncated ? ", stopped at the step cap before the end" : "");

  return 0;
}
