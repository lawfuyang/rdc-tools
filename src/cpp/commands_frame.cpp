// z.commands_frame — the frame-inspection commands (info, draws, probe, counters, debug, textures,
// mesh, image)
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

int CmdInfo(IReplayController *ctrl, ICaptureFile *file, const char *path)
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

int CmdDraws(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxRows,
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
      continue;    // a device-level chunk: not an event
    const bool call = IsCall(r.name);
    events++;
    if(filter != NULL && *filter != '\0' && strstr(r.name.c_str(), filter) == NULL)
      continue;
    if(!call && filter == NULL)
      continue;    // without a filter: calls and markers only
    if(maxRows > 0 && shown >= maxRows)
      continue;

    if(g_json)
    {
      ObjectRow(Fmt("{\"eid\": %d, \"depth\": %d, \"chunkID\": %u, \"name\": \"%s\"}", r.eid,
                    r.depth, r.chunkID, JsonEscape(r.name).c_str()));
    }
    else
    {
      const std::string pad((size_t)r.depth * 2, ' ');
      printf("%-7d %-5d %s%s\n", r.eid, r.depth, pad.c_str(), r.name.c_str());
    }
    shown++;
  }
  ArrayClose(false);    // totalChunks/totalEvents/shown follow
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

//: Names for the pipeline-state enums the offline rules compare and print. The engine's own
//: stringisers are in its unexported stringise.cpp (see the note at the top of this file), so these
//: are local switches in the same shape as `StageName` and `RegisterLetter` -- and named by hand on
//: purpose: a finding should read `LessEqual`, not `2`.
const char *CompareFunctionText(CompareFunction fn)
{
  switch(fn)
  {
    case CompareFunction::Never: return "Never";
    case CompareFunction::AlwaysTrue: return "AlwaysTrue";
    case CompareFunction::Less: return "Less";
    case CompareFunction::LessEqual: return "LessEqual";
    case CompareFunction::Greater: return "Greater";
    case CompareFunction::GreaterEqual: return "GreaterEqual";
    case CompareFunction::Equal: return "Equal";
    case CompareFunction::NotEqual: return "NotEqual";
    default: break;
  }
  return "?";
}

const char *StencilOperationText(StencilOperation op)
{
  switch(op)
  {
    case StencilOperation::Keep: return "Keep";
    case StencilOperation::Zero: return "Zero";
    case StencilOperation::Replace: return "Replace";
    case StencilOperation::IncSat: return "IncSat";
    case StencilOperation::DecSat: return "DecSat";
    case StencilOperation::IncWrap: return "IncWrap";
    case StencilOperation::DecWrap: return "DecWrap";
    case StencilOperation::Invert: return "Invert";
    default: break;
  }
  return "?";
}

const char *BlendMultiplierText(BlendMultiplier m)
{
  switch(m)
  {
    case BlendMultiplier::Zero: return "Zero";
    case BlendMultiplier::One: return "One";
    case BlendMultiplier::SrcCol: return "SrcCol";
    case BlendMultiplier::InvSrcCol: return "InvSrcCol";
    case BlendMultiplier::DstCol: return "DstCol";
    case BlendMultiplier::InvDstCol: return "InvDstCol";
    case BlendMultiplier::SrcAlpha: return "SrcAlpha";
    case BlendMultiplier::InvSrcAlpha: return "InvSrcAlpha";
    case BlendMultiplier::DstAlpha: return "DstAlpha";
    case BlendMultiplier::InvDstAlpha: return "InvDstAlpha";
    case BlendMultiplier::SrcAlphaSat: return "SrcAlphaSat";
    case BlendMultiplier::FactorRGB: return "FactorRGB";
    case BlendMultiplier::InvFactorRGB: return "InvFactorRGB";
    case BlendMultiplier::FactorAlpha: return "FactorAlpha";
    case BlendMultiplier::InvFactorAlpha: return "InvFactorAlpha";
    case BlendMultiplier::Src1Col: return "Src1Col";
    case BlendMultiplier::InvSrc1Col: return "InvSrc1Col";
    case BlendMultiplier::Src1Alpha: return "Src1Alpha";
    case BlendMultiplier::InvSrc1Alpha: return "InvSrc1Alpha";
    default: break;
  }
  return "?";
}

const char *BlendOperationText(BlendOperation op)
{
  switch(op)
  {
    case BlendOperation::Add: return "Add";
    case BlendOperation::Subtract: return "Subtract";
    case BlendOperation::ReversedSubtract: return "ReversedSubtract";
    case BlendOperation::Minimum: return "Minimum";
    case BlendOperation::Maximum: return "Maximum";
    default: break;
  }
  return "?";
}

//: `VarType` names the *component* type of a signature element; `SignatureText` writes it next to
//: the component count so a finding can say `float4` or `uint2` without looking an enum number up.
const char *VarTypeText(VarType t)
{
  switch(t)
  {
    case VarType::Float: return "float";
    case VarType::Double: return "double";
    case VarType::Half: return "half";
    case VarType::SInt: return "int";
    case VarType::UInt: return "uint";
    case VarType::SShort: return "short";
    case VarType::UShort: return "ushort";
    case VarType::SLong: return "long";
    case VarType::ULong: return "ulong";
    case VarType::SByte: return "byte";
    case VarType::UByte: return "ubyte";
    case VarType::Bool: return "bool";
    case VarType::Enum: return "enum";
    case VarType::Struct: return "struct";
    case VarType::GPUPointer: return "pointer";
    case VarType::ConstantBlock: return "cbuffer";
    case VarType::ReadOnlyResource: return "srv";
    case VarType::ReadWriteResource: return "uav";
    case VarType::Sampler: return "sampler";
    case VarType::Unknown: return "unknown";
    default: break;
  }
  return "?";
}

//: The pipeline state at one event: which shaders are bound (with their entry points), the input
//: assembler, the outputs, and -- for D3D12 -- the root signature and every root parameter that is
//: set. This is the "what is bound, exactly" answer the offline tool can only approximate.
int CmdTextures(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *filter,
                const char *saveDir)
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
      ObjectRow(Fmt(
          "{\"resource\": \"%s\", \"dimension\": %d, \"width\": %u, \"height\": %u,"
          " \"depth\": %u, \"mips\": %u, \"arraySize\": %u, \"samples\": %u,"
          " \"format\": \"%s\", \"bytes\": %llu}",
          id.c_str(), (int)t.dimension, t.width, t.height, t.depth, t.mips, t.arraysize, t.msSamp,
          JsonEscape(t.format.Name().c_str()).c_str(), (unsigned long long)t.byteSize));
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
        fprintf(stderr, "  warning: could not save res%s: %s\n", id.c_str(), ResultText(res).c_str());
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
  ArrayClose(false);    // total/shown follow
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
int CmdMesh(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid, int instance,
            int maxRows)
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
  // A binary32 has no trap representations, so every bit pattern that comes back is a value that
  // can be printed; the assert keeps that assumption attached to the code that relies on it.
  static_assert(std::numeric_limits<float>::is_iec559,
                "the vertex stream is read as IEEE-754 binary32");
  static_assert(sizeof(float) == 4, "a vertex component is four bytes");

  const bytebuf data =
      ctrl->GetBufferData(mesh.vertexResourceId, mesh.vertexByteOffset, mesh.vertexByteSize);
  const size_t stride = mesh.vertexByteStride;    // non-zero: checked above (no division by zero)
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
  ArrayClose(false);    // vertexCount/componentsPerVertex follow
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
void PutLE16(uint8_t *dst, uint16_t value)
{
  dst[0] = (uint8_t)(value & 0xffu);
  dst[1] = (uint8_t)((value >> 8) & 0xffu);
}

void PutLE32(uint8_t *dst, uint32_t value)
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
bool WriteBMP(const char *path, const bytebuf &rgba, int32_t width, int32_t height)
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
    return false;    // the header's size fields are 32-bit

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
    const size_t y = h - 1 - line;    // BMP rows are bottom-up
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
bool SaveTargetImage(IReplayController *ctrl, ResourceId target, const char *outBase,
                     std::string &written, int32_t &width, int32_t &height);

int CmdImage(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
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

int CmdCounters(IReplayController *ctrl, ICaptureFile *file, const char *path)
{
  PrintCaptureHeader(file, path);
  rdcarray<CounterResult> results = ctrl->FetchCounters(rdcarray<GPUCounter>());

  // RenderDoc's own counter names live in its (unexported) stringise.cpp, so the enum value is what
  // gets printed here; `GPUCounter`'s numbering is in the API headers.
  ArrayOpen("counters");
  for(size_t i = 0; i < results.size(); i++)
    Row(Fmt("eid %-7u %-18s = %f", (unsigned)results[i].eventId,
            CounterText(results[i].counter).c_str(), results[i].value.d));
  ArrayClose(false);    // total follows
  g_indent = g_json ? 1 : 0;
  Field("total", (long long)results.size(), true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

int CmdDebug(IReplayController *ctrl, ICaptureFile *file, const char *path)
{
  PrintCaptureHeader(file, path);
  rdcarray<DebugMessage> msgs = ctrl->GetDebugMessages();

  ArrayOpen("messages");
  for(size_t i = 0; i < msgs.size(); i++)
    Row(Fmt("eid %-6u %-8s %s", (unsigned)msgs[i].eventId, SeverityText(msgs[i].severity).c_str(),
            msgs[i].description.c_str()));
  ArrayClose(false);    // total follows
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
int CmdUsage(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *what)
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
  ArrayClose(false);    // total follows
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
int CmdProbe(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxEid)
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
  ArrayClose(false);    // scanned/withState follow
  g_indent = g_json ? 1 : 0;
  Field("scanned", (long long)maxEid);
  Field("withState", found, true);
  g_indent = 0;
  if(g_json)
    printf("}\n");
  return 0;
}

// --------------------------------------------------------------------------- the bundle (ROADMAP §2)

//: Runs a block with stdout pointing at a file, so a command written to print to the terminal
//: writes a file instead. A file-descriptor swap rather than a `FILE *` threaded through the
//: writers, because the helpers (`Field`, `Row`, `ArrayOpen`, ...) and the commands that use them
//: print in a dozen places each: one missed call site would silently corrupt a document, and the
//: descriptor is the only version of this that cannot miss one.
//:
//: `stdout` is unbuffered (setvbuf in `main`), so nothing has to be flushed before the swap, and
//: the guard restores the descriptor on every path out of the scope, early returns included.
bool SaveTargetImage(IReplayController *ctrl, ResourceId target, const char *outBase,
                     std::string &written, int32_t &width, int32_t &height)
{
  IReplayOutput *out =
      ctrl->CreateOutput(CreateHeadlessWindowingData(256, 256), ReplayOutputType::Texture);
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
