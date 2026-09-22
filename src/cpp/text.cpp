// z.text — how the engine's names and values are written as text
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

// --------------------------------------------------------------------------- enum text
//
// The enums below have fixed underlying types, so every value of the underlying type is a valid
// value of the enum and printing one as a number is exact -- no static_cast can be out of range
// ([dcl.enum], [ub:expr.static.cast.enum.outside.range] applies only to enums without one).

const char *ResultCodeName(ResultCode code)
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
  return NULL;    // unnamed: the caller prints the number
}

//: A failed operation as one line. `internal_msg`, when the engine supplied one, is the same text
//: `ResultDetails::Message()` would return and already names the code, so it is used verbatim; its
//: storage belongs to the engine and is only valid until `RENDERDOC_ShutdownReplay` (the header
//: says so), which is why this copies into a `std::string` straight away.
std::string ResultText(const ResultDetails &res)
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
std::string UsageText(ResourceUsage usage)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "usage(%u)", (unsigned)usage);
  return buf;
}

std::string SeverityText(MessageSeverity severity)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "severity(%u)", (unsigned)severity);
  return buf;
}

//: The names `--fail-on` takes, lowercase and exact like the stage names. `high` is the *most*
//: severe (the enum's 0), so "fail on medium" means High or Medium: the threshold is a floor of
//: severity, not a number to compare downwards. A table rather than a chain of ifs, because the
//: accepted spellings are part of a command line and belong in one place.
bool SeverityFromName(const char *name, MessageSeverity &out)
{
  static const struct
  {
    const char *m_Name;
    MessageSeverity m_Severity;
  } kSeverities[] = {
      {"high", MessageSeverity::High},
      {"medium", MessageSeverity::Medium},
      {"low", MessageSeverity::Low},
      {"info", MessageSeverity::Info},
  };

  for(size_t i = 0; i < sizeof(kSeverities) / sizeof(kSeverities[0]); i++)
  {
    if(name != NULL && strcmp(name, kSeverities[i].m_Name) == 0)
    {
      out = kSeverities[i].m_Severity;
      return true;
    }
  }
  return false;
}

std::string CounterText(GPUCounter counter)
{
  char buf[32];
  snprintf(buf, sizeof(buf), "counter(%u)", (unsigned)counter);
  return buf;
}

// --------------------------------------------------------------------------- output helpers

std::string IdText(ResourceId id)
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
const char *StageName(ShaderStage stage)
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

//: The register letter D3D12 and the reflection both use for a descriptor category: `b` for a
//: constant block, `s` for a sampler, `t` for a read-only resource, `u` for a read-write one. The
//: table-slot rows carry it so they can be read -- and matched by the offline side -- against the
//: reflection's own rows
//: (`cbuffer[0] $Globals b0 s0 ...`, `SkyViewLut t3 s0 n1`).
char RegisterLetter(DescriptorCategory category)
{
  switch(category)
  {
    case DescriptorCategory::ConstantBlock: return 'b';
    case DescriptorCategory::Sampler: return 's';
    case DescriptorCategory::ReadOnlyResource: return 't';
    case DescriptorCategory::ReadWriteResource: return 'u';
    default: return '?';
  }
}

//: The stages a root parameter is visible to, in the stage letters the rest of the output uses (`vs`, `ps`,
//: ...), or `all`. It is on the parameter row because the same register can mean different things to
//: different stages -- measured on `desktop-1` at eid 640, whose vertex and pixel shaders *both*
//: declare t0..t4, each served by its own table -- so a row without this cannot be matched against the
//: reflection, and an offline rule that guessed would compare a pixel binding against a vertex table.
std::string VisibilityText(ShaderStageMask mask)
{
  if(mask == ShaderStageMask::All)
    return "all";

  static const ShaderStage gs_Stages[] = {
      ShaderStage::Vertex, ShaderStage::Hull,    ShaderStage::Domain, ShaderStage::Geometry,
      ShaderStage::Pixel,  ShaderStage::Compute, ShaderStage::Task,   ShaderStage::Mesh};
  const uint32_t bits = (uint32_t)mask;
  std::string text;
  for(ShaderStage stage : gs_Stages)
  {
    if((bits & (1u << (uint32_t)stage)) == 0)
      continue;
    if(!text.empty())
      text += '+';
    text += StageName(stage);
  }
  return text.empty() ? "?" : text;
}
//: accessors exist but are not exported from the DLL, so the state struct is the way in -- and the
//: reflection is then fetched through the virtual controller API.
const D3D12Pipe::Shader *StageShader(const D3D12Pipe::State *d3d12, ShaderStage stage)
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

ShaderStage StageFromName(const char *name)
{
  // The stages this tool reports and their command-line spellings, in one place, so `StageName` and
  // the command line cannot drift apart (the order is also the order the commands print them in).
  static const struct
  {
    const char *m_Name;
    ShaderStage m_Stage;
  } kStages[] = {
      {"vs", ShaderStage::Vertex},        {"hs", ShaderStage::Hull},  {"ds", ShaderStage::Domain},
      {"gs", ShaderStage::Geometry},      {"ps", ShaderStage::Pixel}, {"cs", ShaderStage::Compute},
      {"as", ShaderStage::Amplification}, {"ms", ShaderStage::Mesh},
  };

  for(size_t i = 0; i < sizeof(kStages) / sizeof(kStages[0]); i++)
    if(strcmp(name, kStages[i].m_Name) == 0)
      return kStages[i].m_Stage;

  // `ShaderStage::Invalid` is the enum's own sentinel (`Invalid = Count`), so "no such stage" has a
  // defined spelling rather than an out-of-range value.
  return ShaderStage::Invalid;
}

//: The stages the commands report, in reporting order. Enumerating the enum instead would also walk
//: the eight ray-tracing stages, which have no D3D12 pipeline state and can never be bound here.
const ShaderStage kReportedStages[] = {
    ShaderStage::Vertex, ShaderStage::Hull,    ShaderStage::Domain,        ShaderStage::Geometry,
    ShaderStage::Pixel,  ShaderStage::Compute, ShaderStage::Amplification, ShaderStage::Mesh,
};

//: The reported stages as a collection: `kReportedStages` is an array so a state document prints the stages
//: in a fixed order, and every command that iterates it wants a range rather than a `sizeof` dance.
const rdcarray<ShaderStage> &ReportedStages()
{
  static rdcarray<ShaderStage> gs_Stages;
  if(gs_Stages.empty())
  {
    for(size_t i = 0; i < sizeof(kReportedStages) / sizeof(kReportedStages[0]); i++)
      gs_Stages.push_back(kReportedStages[i]);
  }
  return gs_Stages;
}

// --------------------------------------------------------------------------- the picture vocabulary

//: The `--overlay` vocabulary: RenderDoc's `DebugOverlay`, one spelling per member, in the engine's
//: own order with `none` first (it is the default). One table read by `OverlayFromName`,
//: `OverlayText` and `OverlayNames`, so the parse, the document and the error message cannot
//: disagree about what exists.
//:
//: The names are the engine's own idea spelled the way a command line can type it: `backface` is
//: `BackfaceCull`, `viewport` is `ViewportScissor`, and the four cost overlays keep the
//: `...Pass`/`...Draw` distinction because it is the whole point of having both (one measures over
//: a pass, one over a draw).
static const struct
{
  const char *m_Name;
  DebugOverlay m_Overlay;
} kOverlays[] = {
    {"none", DebugOverlay::NoOverlay},
    {"drawcall", DebugOverlay::Drawcall},
    {"wireframe", DebugOverlay::Wireframe},
    {"depth", DebugOverlay::Depth},
    {"stencil", DebugOverlay::Stencil},
    {"backface", DebugOverlay::BackfaceCull},
    {"viewport", DebugOverlay::ViewportScissor},
    {"nan", DebugOverlay::NaN},
    {"clipping", DebugOverlay::Clipping},
    {"clear-before-pass", DebugOverlay::ClearBeforePass},
    {"clear-before-draw", DebugOverlay::ClearBeforeDraw},
    {"quad-pass", DebugOverlay::QuadOverdrawPass},
    {"quad-draw", DebugOverlay::QuadOverdrawDraw},
    {"triangle-size-pass", DebugOverlay::TriangleSizePass},
    {"triangle-size-draw", DebugOverlay::TriangleSizeDraw},
};

bool OverlayFromName(std::string_view name, DebugOverlay &overlay)
{
  for(size_t i = 0; i < sizeof(kOverlays) / sizeof(kOverlays[0]); i++)
  {
    if(name == kOverlays[i].m_Name)
    {
      overlay = kOverlays[i].m_Overlay;
      return true;
    }
  }
  return false;
}

const char *OverlayText(DebugOverlay overlay)
{
  for(size_t i = 0; i < sizeof(kOverlays) / sizeof(kOverlays[0]); i++)
  {
    if(overlay == kOverlays[i].m_Overlay)
      return kOverlays[i].m_Name;
  }
  return "none";
}

std::string OverlayNames(const char *separator)
{
  std::string text;
  for(size_t i = 0; i < sizeof(kOverlays) / sizeof(kOverlays[0]); i++)
  {
    if(!text.empty())
      text += separator;
    text += kOverlays[i].m_Name;
  }
  return text;
}

//: The `--stage` vocabulary for `mesh`, in the engine's order. `AmpOut` is an alias of `TaskOut`
//: (an older name for the same stage), so it is parsed and never printed: two spellings of one
//: stage would otherwise look like two stages, and the document is the thing a reader compares
//: between runs.
static const struct
{
  const char *m_Name;
  MeshDataStage m_Stage;
} kMeshStages[] = {
    {"vsin", MeshDataStage::VSIn},     {"vsout", MeshDataStage::VSOut},
    {"gsout", MeshDataStage::GSOut},   {"taskout", MeshDataStage::TaskOut},
    {"ampout", MeshDataStage::AmpOut}, {"meshout", MeshDataStage::MeshOut},
};

bool MeshStageFromName(std::string_view name, MeshDataStage &stage)
{
  for(size_t i = 0; i < sizeof(kMeshStages) / sizeof(kMeshStages[0]); i++)
  {
    if(name == kMeshStages[i].m_Name)
    {
      stage = kMeshStages[i].m_Stage;
      return true;
    }
  }
  return false;
}

const char *MeshStageText(MeshDataStage stage)
{
  // `AmpOut` and `TaskOut` are the same value, so the first match is the one printed -- and
  // `taskout` is listed before `ampout` for exactly that reason.
  for(size_t i = 0; i < sizeof(kMeshStages) / sizeof(kMeshStages[0]); i++)
  {
    if(stage == kMeshStages[i].m_Stage)
      return kMeshStages[i].m_Name;
  }
  return "vsout";
}

std::string MeshStageNames(const char *separator)
{
  std::string text;
  for(size_t i = 0; i < sizeof(kMeshStages) / sizeof(kMeshStages[0]); i++)
  {
    if(!text.empty())
      text += separator;
    text += kMeshStages[i].m_Name;
  }
  return text;
}

// --------------------------------------------------------------------------- renderdoc.dll loading
//: a matrix by eye without drowning in noise.
std::string FormatValue(const ShaderVariable &v, int depth)
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

std::string FormatValue(const ShaderVariable &v)
{
  return FormatValue(v, 0);
}

void PrintVariables(const rdcarray<ShaderVariable> &vars, int depth)
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

std::string SignatureText(const SigParameter &sig)
{
  return Fmt("%s%d reg%d c%d %s", sig.semanticName.c_str(), sig.semanticIndex, sig.regIndex,
             sig.compCount, VarTypeText(sig.varType));
}

std::string FormatText(const ResourceFormat &fmt)
{
  return Fmt("%s%d", CastText(fmt.compType), fmt.compCount);
}

CompType ComponentClass(CompType type)
{
  switch(type)
  {
    case CompType::UInt: return CompType::UInt;
    case CompType::SInt: return CompType::SInt;
    case CompType::Typeless: return CompType::Typeless;
    default: return CompType::Float;
  }
}

//: The shader reflection: constant blocks with their names and bind points, the resource bindings,
//: the input/output signatures, and the disassembly. This is what names a root parameter, and what
//: the offline tool's removed `sig`/`dxbc` harvest was trying to guess at.
