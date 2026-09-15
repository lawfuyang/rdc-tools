// replay_dump — headless RenderDoc replay as a data source (ROADMAP §1).
//
// The offline tool (rdc_analysis.py) reads the *file*: the container, the chunk stream, the payload
// layouts. This tool asks the *engine* instead, which is the only way to get frame data exactly:
// named uniform values, shader reflection and disassembly, decoded textures, post-VS geometry, the
// rendered image, GPU counters, debug messages. Everything the offline tool deliberately leaves to
// replay lives here, and the two agree on event ids -- a draw's EID here is the chunk index the
// offline tool prints, which was an assumption until this tool confirmed it.
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

#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "renderdoc_replay.h"

REPLAY_PROGRAM_MARKER();

// The headers' containers (`rdcstr`, `rdcarray`) allocate through the DLL, and their inline
// `ResultDetails::Message()` stringises a `ResultCode`. RenderDoc defines that specialisation in its
// own stringise.cpp and does not export it, so this translation unit supplies one -- the headers only
// need *a* definition to link, and the numeric code is all a message here uses.
template <>
rdcstr DoStringise(const ResultCode &el)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "ResultCode(%d)", (int)el);
  return rdcstr(buf);
}

// The same applies to every other enum the headers stringise inline. RenderDoc's own names for them
// live in stringise.cpp, which is not exported, so these print the numeric value -- honest, and for
// `usage`/`debug` enough to look the value up in RenderDoc's enums.
template <>
rdcstr DoStringise(const ResourceUsage &el)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "usage(%d)", (int)el);
  return rdcstr(buf);
}

template <>
rdcstr DoStringise(const MessageSeverity &el)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "severity(%d)", (int)el);
  return rdcstr(buf);
}

template <>
rdcstr DoStringise(const GPUCounter &el)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "counter(%d)", (int)el);
  return rdcstr(buf);
}

// --------------------------------------------------------------------------- output helpers

static bool g_json = false;
static int g_indent = 0;

//: One row of an array. `g_firstRow` is reset by `ArrayOpen` so commas land between rows and never
//: after the last one -- a trailing comma is not JSON.
static bool g_firstRow = true;

//: The number inside a `ResourceId`. Its value is private and RenderDoc's own stringiser for it is
//: not exported, so this copies the 8 bytes out exactly the way RenderDoc's `DoStringise<ResourceId>`
//: does (`core.cpp`) -- the struct is a `uint64_t` wrapper by design. Ids then read the same way the
//: offline tool prints them (`res1234`).
static std::string IdText(ResourceId id)
{
  uint64_t value = 0;
  memcpy(&value, &id, sizeof(value));
  char buf[32];
  snprintf(buf, sizeof(buf), "%llu", (unsigned long long)value);
  return buf;
}

//: Progress on stderr when `$RDC_REPLAY_DEBUG` is set: a crash inside the replay engine leaves no
//: traceback, so knowing which step it died on -- and how long it ran -- is the difference between a
//: fix and a guess.
static ULONGLONG g_traceStart = 0;

static void Trace(const char *step)
{
  if(getenv("RDC_REPLAY_DEBUG"))
  {
    if(g_traceStart == 0)
      g_traceStart = GetTickCount64();
    fprintf(stderr, "[replay_dump] +%5llums %s\n", GetTickCount64() - g_traceStart, step);
    fflush(stderr);
  }
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
static void Field(const char *key, const std::string &value, bool last = false)
{
  Indent();
  if(g_json)
    printf("\"%s\": \"%s\"%s\n", key, value.c_str(), last ? "" : ",");
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

static void ArrayOpen(const char *key)
{
  Indent();
  if(g_json)
    printf("\"%s\": [\n", key);
  g_indent++;
  g_firstRow = true;
}

static void ArrayClose(bool last = true)
{
  g_indent--;
  Indent();
  if(g_json)
    printf("]%s\n", last ? "" : ",");
}

static void Row(const std::string &text)
{
  if(g_json)
  {
    Indent();
    printf("%s\"%s\"\n", g_firstRow ? "" : ",\n", JsonEscape(text.c_str()).c_str());
    g_firstRow = false;
  }
  else
  {
    printf("%s\n", text.c_str());
  }
}

static std::string Fmt(const char *fmt, ...)
{
  char buf[1024];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  return buf;
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
  if(!strcmp(name, "vs")) return ShaderStage::Vertex;
  if(!strcmp(name, "hs")) return ShaderStage::Hull;
  if(!strcmp(name, "ds")) return ShaderStage::Domain;
  if(!strcmp(name, "gs")) return ShaderStage::Geometry;
  if(!strcmp(name, "ps")) return ShaderStage::Pixel;
  if(!strcmp(name, "cs")) return ShaderStage::Compute;
  if(!strcmp(name, "as")) return ShaderStage::Amplification;
  if(!strcmp(name, "ms")) return ShaderStage::Mesh;
  return ShaderStage::Invalid;
}

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

static void Flatten(const SDObject *obj, int depth, int &next, std::vector<ActionRow> &out)
{
  // Every structured-data object takes the next id, chunks and their parameters alike: that is what
  // makes the numbering line up with the engine's (verified with `probe`).
  int id = next++;
  if(obj->type.basetype == SDBasic::Chunk)
  {
    const SDChunk *chunk = (const SDChunk *)obj;
    ActionRow row;
    row.eid = IsAction(chunk->name) ? id : 0;
    row.depth = depth;
    row.name = chunk->name;
    row.chunkID = chunk->metadata.chunkID;
    out.push_back(row);
    depth++;
  }

  for(size_t i = 0; i < obj->NumChildren(); i++)
    Flatten(obj->GetChild(i), depth, next, out);
}

//: A draw, dispatch or copy: what a frame is *read* through, as opposed to the state and marker
//: chunks that also carry an event id.
static bool IsCall(const rdcstr &name)
{
  const char *n = strstr(name.c_str(), "::");
  n = n ? n + 2 : name.c_str();
  return !strncmp(n, "Draw", 4) || !strncmp(n, "Dispatch", 8) || !strncmp(n, "ExecuteIndirect", 15) ||
         !strncmp(n, "Copy", 4) || !strncmp(n, "Clear", 5) || !strncmp(n, "Present", 7) ||
         !strncmp(n, "ResolveSubresource", 18) || !strncmp(n, "BeginRenderPass", 15);
}

static std::vector<ActionRow> Actions(IReplayController *ctrl)
{
  const SDFile &sd = ctrl->GetStructuredFile();
  std::vector<ActionRow> rows;
  int next = 1;
  for(size_t i = 0; i < sd.chunks.size(); i++)
    Flatten(sd.chunks[i], 0, next, rows);
  return rows;
}

// --------------------------------------------------------------------------- value formatting

//: A shader variable as `name = value`, recursing into structs and arrays (a constant buffer is a
//: tree of these). Vector components are formatted to 6 significant digits, which is enough to read a
//: matrix by eye without drowning in noise.
static std::string FormatValue(const ShaderVariable &v)
{
  const ShaderValue &val = v.value;

  if(!v.members.empty())
  {
    std::string out = "{ ";
    for(size_t i = 0; i < v.members.size(); i++)
      out += (i ? ", " : "") + std::string(v.members[i].name.c_str()) + "=" +
             FormatValue(v.members[i]);
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

static void PrintVariables(const rdcarray<ShaderVariable> &vars, int depth)
{
  for(size_t i = 0; i < vars.size(); i++)
  {
    const ShaderVariable &v = vars[i];
    std::string pad(depth * 2, ' ');
    if(g_json)
    {
      Indent();
      printf("\"%s\": \"%s\",\n", JsonEscape(v.name).c_str(),
             JsonEscape(FormatValue(v).c_str()).c_str());
    }
    else
    {
      printf("  %s%-28s %s\n", pad.c_str(), v.name.c_str(), FormatValue(v).c_str());
    }
    if(!v.members.empty())
      PrintVariables(v.members, depth + 1);
  }
}

// --------------------------------------------------------------------------- commands

static void PrintCaptureHeader(ICaptureFile *file, const char *path)
{
  if(g_json)
    printf("{\n");
  g_indent = g_json ? 1 : 0;
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
  std::vector<ActionRow> rows = Actions(ctrl);

  int shown = 0, events = 0;
  ArrayOpen("events");
  for(size_t i = 0; i < rows.size(); i++)
  {
    const ActionRow &r = rows[i];
    if(r.eid == 0)
      continue;                                    // a device-level chunk: not an event
    bool call = IsCall(r.name);
    events++;
    if(filter && *filter && strstr(r.name.c_str(), filter) == NULL)
      continue;
    if(!call && filter == NULL)
      continue;                                    // without a filter: calls and markers only
    if(maxRows > 0 && shown >= maxRows)
      continue;

    if(g_json)
    {
      Indent();
      printf("{\"eid\": %d, \"depth\": %d, \"chunkID\": %u, \"name\": \"%s\"}%s\n", r.eid, r.depth,
             r.chunkID, JsonEscape(r.name).c_str(), ",\n");
    }
    else
    {
      std::string pad(r.depth * 2, ' ');
      printf("%-7d %-5d %s%s\n", r.eid, r.depth, pad.c_str(), r.name.c_str());
    }
    shown++;
  }
  ArrayClose();
  g_indent = g_json ? 1 : 0;
  Field("totalChunks", (long long)rows.size());
  Field("totalEvents", events);
  Field("shown", shown, true);
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
  const PipeState &pipe = ctrl->GetPipelineState();

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("api", (long long)ctrl->GetAPIProperties().pipelineType);

  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();

  ArrayOpen("shaders");
  for(int i = 0; i < (int)ShaderStage::Count; i++)
  {
    ShaderStage stage = (ShaderStage)i;
    const D3D12Pipe::Shader *sh = StageShader(d3d12, stage);
    if(sh == NULL || sh->resourceId == ResourceId::Null())
      continue;
    Row(Fmt("%-3s res%-7s", StageName(stage), IdText(sh->resourceId).c_str()));
  }
  ArrayClose();

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
  ArrayClose();
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
  {
    fprintf(stderr, "error: no D3D12 pipeline state at eid %d\n", eid);
    return 1;
  }

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);

  for(int i = 0; i < (int)ShaderStage::Count; i++)
  {
    ShaderStage stage = (ShaderStage)i;
    ResourceId shader = StageShader(d3d12, stage) ? StageShader(d3d12, stage)->resourceId
                                                  : ResourceId::Null();
    if(shader == ResourceId::Null())
      continue;

    // An empty entry-point name means "the default one"; `ShaderEntryPoint` carries the stage too.
    const ShaderReflection *refl =
        ctrl->GetShader(d3d12->pipelineResourceId, shader, ShaderEntryPoint(rdcstr(), stage));
    if(refl == NULL)
    {
      printf("  %-3s res%-7s (no reflection)\n", StageName(stage), IdText(shader).c_str());
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
    ArrayClose();

    ArrayOpen("readOnlyResources");
    for(size_t r = 0; r < refl->readOnlyResources.size(); r++)
      Row(Fmt("%s t%d s%d n%d", refl->readOnlyResources[r].name.c_str(),
              refl->readOnlyResources[r].fixedBindNumber,
              refl->readOnlyResources[r].fixedBindSetOrSpace,
              (int)refl->readOnlyResources[r].bindArraySize));
    ArrayClose();

    ArrayOpen("readWriteResources");
    for(size_t r = 0; r < refl->readWriteResources.size(); r++)
      Row(Fmt("%s u%d s%d n%d", refl->readWriteResources[r].name.c_str(),
              refl->readWriteResources[r].fixedBindNumber,
              refl->readWriteResources[r].fixedBindSetOrSpace,
              (int)refl->readWriteResources[r].bindArraySize));
    ArrayClose();

    ArrayOpen("inputSignature");
    for(size_t s = 0; s < refl->inputSignature.size(); s++)
      Row(Fmt("%s%d reg%d", refl->inputSignature[s].semanticName.c_str(),
              refl->inputSignature[s].semanticIndex, refl->inputSignature[s].regIndex));
    ArrayClose();

    ArrayOpen("outputSignature");
    for(size_t s = 0; s < refl->outputSignature.size(); s++)
      Row(Fmt("%s%d reg%d", refl->outputSignature[s].semanticName.c_str(),
              refl->outputSignature[s].semanticIndex, refl->outputSignature[s].regIndex));
    ArrayClose(false);

    if(wantDisasm)
    {
      // The disassembly is not part of the reflection: the controller generates it on request, per
      // target, and an empty target means "the native one".
      rdcstr asmText = ctrl->DisassembleShader(d3d12->pipelineResourceId, refl, rdcstr());
      ArrayOpen("disassembly");
      // one entry per line so JSON consumers can diff it
      std::string text(asmText.c_str());
      size_t start = 0;
      while(start <= text.size())
      {
        size_t nl = text.find('\n', start);
        std::string line = text.substr(start, nl == std::string::npos ? std::string::npos : nl - start);
        Row(line);
        if(nl == std::string::npos)
          break;
        start = nl + 1;
      }
      ArrayClose();
    }

    if(g_json)
      printf(",\n");
  }

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
  {
    fprintf(stderr, "error: no %s shader is bound at eid %d\n", StageName(stage), eid);
    return 1;
  }

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
    if(g_json)
    {
      Indent();
      printf("{\"resource\": \"%s\", \"dimension\": %d, \"width\": %u, \"height\": %u, \"depth\": %u,"
             " \"mips\": %u, \"arraySize\": %u, \"samples\": %u, \"format\": \"%s\", \"bytes\": %llu}%s\n",
             id.c_str(), (int)t.dimension, t.width, t.height, t.depth, t.mips, t.arraysize, t.msSamp,
             t.format.Name().c_str(), (unsigned long long)t.byteSize, ",");
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
      std::string out = Fmt("%s\\tex_%s.png", saveDir, id.c_str());
      ResultDetails res = ctrl->SaveTexture(save, rdcstr(out.c_str()));
      if(!res.OK())
        fprintf(stderr, "  warning: could not save res%s: %s\n", id.c_str(),
                res.Message().c_str());
      else
        printf("  -> %s\n", out.c_str());
    }
  }
  ArrayClose();
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

  MeshFormat mesh = ctrl->GetPostVSData((uint32_t)instance, 0, MeshDataStage::VSOut);
  Field("topology", (long long)mesh.topology);
  Field("vertexResource", IdText(mesh.vertexResourceId));
  Field("vertexStride", (long long)mesh.vertexByteStride);
  Field("vertexBytes", (long long)mesh.vertexByteSize);
  Field("indexResource", IdText(mesh.indexResourceId));
  Field("indexBytes", (long long)mesh.indexByteSize);
  Field("baseVertex", (long long)mesh.baseVertex);

  if(mesh.vertexResourceId == ResourceId::Null() || mesh.vertexByteStride == 0)
  {
    if(g_json)
      printf("}\n");
    else
      printf("(no post-VS data for this event)\n");
    return 0;
  }

  // The data is the post-VS stream, so it is printed as the floats it is: `stride / 4` per vertex.
  // Which float is which attribute is the shader reflection's business (`shaders <eid>`), not
  // something this buffer can say.
  bytebuf data = ctrl->GetBufferData(mesh.vertexResourceId, mesh.vertexByteOffset,
                                     mesh.vertexByteSize);
  const uint32_t stride = mesh.vertexByteStride;
  const uint32_t count = (uint32_t)(data.size() / stride);
  const uint32_t comps = stride / 4;

  ArrayOpen("vertices");
  for(uint32_t v = 0; v < count && (maxRows <= 0 || (int)v < maxRows); v++)
  {
    std::string line = Fmt("[%u]", v);
    for(uint32_t c = 0; c < comps; c++)
    {
      float f = 0.0f;
      memcpy(&f, data.data() + v * stride + c * 4, 4);
      line += Fmt(" %g", f);
    }
    Row(line);
  }
  ArrayClose();
  g_indent = g_json ? 1 : 0;
  Field("vertexCount", (long long)count);
  Field("componentsPerVertex", (long long)comps, true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

//: A 24-bit BMP of the texture display at one event. BMP rather than PNG because it needs no
//: encoder: the pixels come back as RGBA and the header is 54 bytes.
static bool WriteBMP(const char *path, const bytebuf &rgba, int32_t width, int32_t height)
{
  if(width <= 0 || height <= 0 || rgba.size() < (size_t)(width * height * 4))
    return false;

  const int rowBytes = width * 3;
  const int pad = (4 - (rowBytes % 4)) % 4;
  const uint32_t imageSize = (uint32_t)((rowBytes + pad) * height);
  const uint32_t fileSize = 54 + imageSize;

  FILE *f = fopen(path, "wb");
  if(!f)
    return false;

  uint8_t header[54] = {};
  header[0] = 'B';
  header[1] = 'M';
  memcpy(header + 2, &fileSize, 4);
  uint32_t offset = 54;
  memcpy(header + 10, &offset, 4);
  uint32_t dibSize = 40;
  memcpy(header + 14, &dibSize, 4);
  int32_t w = width, h = height;
  memcpy(header + 18, &w, 4);
  memcpy(header + 22, &h, 4);
  uint16_t planes = 1, bpp = 24;
  memcpy(header + 26, &planes, 2);
  memcpy(header + 28, &bpp, 2);
  memcpy(header + 34, &imageSize, 4);

  fwrite(header, 1, 54, f);
  std::vector<uint8_t> row(rowBytes + pad, 0);
  for(int y = height - 1; y >= 0; y--)              // BMP rows are bottom-up
  {
    for(int x = 0; x < width; x++)
    {
      const uint8_t *px = rgba.data() + (y * width + x) * 4;
      row[x * 3 + 0] = px[2];
      row[x * 3 + 1] = px[1];
      row[x * 3 + 2] = px[0];
    }
    fwrite(row.data(), 1, row.size(), f);
  }
  fclose(f);
  return true;
}

static int CmdImage(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
                    const char *outPath)
{
  ctrl->SetFrameEvent(eid, true);
  const PipeState &pipe = ctrl->GetPipelineState();

  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  ResourceId rt = d3d12 && !d3d12->outputMerger.renderTargets.empty()
                      ? d3d12->outputMerger.renderTargets[0].resource
                      : ResourceId::Null();
  if(rt == ResourceId::Null())
  {
    fprintf(stderr, "error: nothing is bound to render target 0 at eid %d\n", eid);
    return 1;
  }

  IReplayOutput *out = ctrl->CreateOutput(CreateHeadlessWindowingData(256, 256),
                                         ReplayOutputType::Texture);
  TextureDisplay disp;
  disp.resourceId = rt;
  disp.typeCast = CompType::Typeless;
  disp.rangeMin = 0.0f;
  disp.rangeMax = 1.0f;
  out->SetTextureDisplay(disp);
  out->Display();

  rdcpair<int32_t, int32_t> dims = out->GetDimensions();
  bytebuf pixels = out->ReadbackOutputTexture();
  bool ok = WriteBMP(outPath, pixels, dims.first, dims.second);
  out->Shutdown();

  // The display readback can come back empty (the output is rendered by the engine and not every
  // target goes through it cleanly). `SaveTexture` is the engine's own encoder, so when the readback
  // gives nothing the target is saved directly -- same picture, no BMP.
  if(!ok)
  {
    TextureSave save;
    save.resourceId = rt;
    save.destType = FileType::PNG;
    std::string png = std::string(outPath) + ".png";
    ResultDetails res = ctrl->SaveTexture(save, rdcstr(png.c_str()));
    if(res.OK())
    {
      Field("saved", std::string("(readback empty, saved the target instead) ") + png);
      ok = true;
    }
  }

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("resource", IdText(rt));
  Field("width", (long long)dims.first);
  Field("height", (long long)dims.second);
  Field("file", std::string(outPath));
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
    Row(Fmt("eid %-7u %-18s = %f", results[i].eventId, ToStr(results[i].counter).c_str(),
            results[i].value.d));
  ArrayClose();
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
    Row(Fmt("eid %-6u %-8s %s", msgs[i].eventId, ToStr(msgs[i].severity).c_str(),
            msgs[i].description.c_str()));
  ArrayClose();
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
  {
    fprintf(stderr, "error: no resource with id or name '%s'\n", what);
    return 1;
  }

  rdcarray<EventUsage> usage = ctrl->GetUsage(id);

  ArrayOpen("usage");
  for(size_t i = 0; i < usage.size(); i++)
    Row(Fmt("eid %-7u %s", usage[i].eventId, ToStr(usage[i].usage).c_str()));
  ArrayClose();
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
  ArrayClose();
  g_indent = g_json ? 1 : 0;
  Field("scanned", (long long)maxEid);
  Field("withState", found, true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

// --------------------------------------------------------------------------- CLI

static void Usage()
{
  printf(
      "replay_dump - headless RenderDoc replay as a data source (ROADMAP 1)\n"
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
      "\n"
      "Event ids match the offline tool's chunk indices, which `draws` here confirms rather than\n"
      "assumes. $RDC_RENDERDOC_DLL overrides the renderdoc.dll to load (default: the installed one).\n"
      "$RDC_REPLAY_DEBUG=1 traces each step on stderr, for when the engine takes the process down.\n");
}

int main(int argc, char **argv)
{
  // Unbuffered: the replay engine is third-party code that can take the process down with it, and
  // losing the output that was already produced would hide exactly what happened.
  setvbuf(stdout, NULL, _IONBF, 0);
  setvbuf(stderr, NULL, _IONBF, 0);

  std::vector<std::string> args;
  bool wantDisasm = false;
  const char *saveDir = NULL;
  for(int i = 1; i < argc; i++)
  {
    if(!strcmp(argv[i], "--json"))
      g_json = true;
    else if(!strcmp(argv[i], "--disasm"))
      wantDisasm = true;
    else if(!strcmp(argv[i], "--save") && i + 1 < argc)
      saveDir = argv[++i];
    else
      args.push_back(argv[i]);
  }

  if(args.empty() || args[0] == "--help" || args[0] == "-h")
  {
    Usage();
    return args.empty() ? 2 : 0;
  }

  const char *cmd = args[0].c_str();
  if(args.size() < 2)
  {
    fprintf(stderr, "error: %s needs a capture path\n", cmd);
    return 2;
  }
  const char *path = args[1].c_str();

  Trace("loading renderdoc.dll");
  HMODULE dll = LoadReplayDLL();
  if(dll == NULL)
    return 1;

  Trace("RENDERDOC_InitialiseReplay");
  if(!InitialiseReplay(dll, argc, argv))
    return 1;

  Trace("RENDERDOC_OpenCaptureFile");
  ICaptureFile *file = OpenCaptureFile(dll);
  if(file == NULL)
    return 1;

  Trace("OpenFile");
  ResultDetails res = file->OpenFile(path, "rdc", NULL);
  if(!res.OK())
  {
    fprintf(stderr, "error: cannot open %s: %s\n", path, res.Message().c_str());
    return 1;
  }

  Trace("OpenCapture (replay)");
  // Every member of ReplayOptions is default-initialised in the header, but zeroing the whole struct
  // also rules out a layout disagreement with the DLL: all-zero means "no overrides" either way.
  ReplayOptions opts;
  memset(&opts, 0, sizeof(opts));
  rdcpair<ResultDetails, IReplayController *> opened = file->OpenCapture(opts, NULL);
  Trace("OpenCapture returned");
  if(!opened.first.OK())
  {
    fprintf(stderr, "error: cannot replay %s: %s\n", path, opened.first.Message().c_str());
    return 1;
  }
  IReplayController *ctrl = opened.second;

  Trace("running the command");
  int ret;
  if(!strcmp(cmd, "info"))
    ret = CmdInfo(ctrl, file, path);
  else if(!strcmp(cmd, "draws"))
    ret = CmdDraws(ctrl, file, path, args.size() > 2 ? atoi(args[2].c_str()) : 80,
                   args.size() > 3 ? args[3].c_str() : NULL);
  else if(!strcmp(cmd, "state") && args.size() > 2)
    ret = CmdState(ctrl, file, path, atoi(args[2].c_str()));
  else if(!strcmp(cmd, "shaders") && args.size() > 2)
    ret = CmdShaders(ctrl, file, path, atoi(args[2].c_str()), wantDisasm);
  else if(!strcmp(cmd, "cb") && args.size() > 4)
    ret = CmdCbuffer(ctrl, file, path, atoi(args[2].c_str()), StageFromName(args[3].c_str()),
                     atoi(args[4].c_str()));
  else if(!strcmp(cmd, "textures"))
    ret = CmdTextures(ctrl, file, path, args.size() > 2 ? args[2].c_str() : NULL, saveDir);
  else if(!strcmp(cmd, "mesh") && args.size() > 2)
    ret = CmdMesh(ctrl, file, path, atoi(args[2].c_str()), args.size() > 3 ? atoi(args[3].c_str()) : 0,
                  args.size() > 4 ? atoi(args[4].c_str()) : 16);
  else if(!strcmp(cmd, "image") && args.size() > 3)
    ret = CmdImage(ctrl, file, path, atoi(args[2].c_str()), args[3].c_str());
  else if(!strcmp(cmd, "counters"))
    ret = CmdCounters(ctrl, file, path);
  else if(!strcmp(cmd, "debug"))
    ret = CmdDebug(ctrl, file, path);
  else if(!strcmp(cmd, "usage") && args.size() > 2)
    ret = CmdUsage(ctrl, file, path, args[2].c_str());
  else if(!strcmp(cmd, "probe"))
    ret = CmdProbe(ctrl, file, path, args.size() > 2 ? atoi(args[2].c_str()) : 2000);
  else
  {
    fprintf(stderr, "error: unknown command '%s' (or missing arguments)\n", cmd);
    Usage();
    ret = 2;
  }

  Trace("shutting down");
  ctrl->Shutdown();
  file->Shutdown();
  if(g_ShutdownReplay)
    g_ShutdownReplay();
  // The DLL is deliberately not freed: the objects above are owned by it, and RenderDoc's own tools
  // let the process exit instead.
  return ret;
}
