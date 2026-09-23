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
  // The one API property a command is *gated* on, and the gate says so in its own message: a capture
  // whose driver cannot answer pixel history is refused rather than answered with an empty list.
  Field("pixelHistory", (long long)props.pixelHistory);
  Field("chunks", (long long)sd.chunks.size());
  Field("resources", (long long)ctrl->GetResources().size());
  Field("textures", (long long)ctrl->GetTextures().size());
  Field("buffers", (long long)ctrl->GetBuffers().size());
  Field("debugMessages", (long long)ctrl->GetDebugMessages().size(), true);

  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return 0;
}

int CmdDraws(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxRows,
             const char *filter)
{
  PrintCaptureHeader(file, path);
  int calls = 0;
  bool bTruncated = false;
  const std::vector<ActionNode> rows = ActionTree(ctrl, calls, bTruncated);

  int shown = 0;
  ArrayOpen("actions");
  for(size_t i = 0; i < rows.size(); i++)
  {
    const ActionNode &row = rows[i];
    // The filter matches the call's own name *or* the marker path it sits inside, which is what
    // makes a marker path a handle for a set of events (`--filter BasePass` finds the pass, not a
    // call that spells it).
    const bool bFiltered = filter != NULL && *filter != '\0';
    const bool bMatches = !bFiltered || strstr(row.m_Name.c_str(), filter) != NULL ||
                          strstr(row.m_Path.c_str(), filter) != NULL;
    if(!bMatches)
      continue;
    if(!bFiltered && !row.m_bCall && !row.m_bMarker)
      continue;    // without a filter: calls and markers only -- the state setters between them are not the tree
    if(maxRows > 0 && shown >= maxRows)
      continue;

    if(g_bJson)
    {
      ObjectRow(Fmt(
          "{\"eid\": %d, \"depth\": %d, \"call\": %s, \"marker\": %s, \"name\": \"%s\", "
          "\"path\": \"%s\"}",
          row.m_Eid, row.m_Depth, row.m_bCall ? "true" : "false", row.m_bMarker ? "true" : "false",
          JsonEscape(row.m_Name).c_str(), JsonEscape(row.m_Path).c_str()));
    }
    else
    {
      const std::string pad((size_t)row.m_Depth * 2, ' ');
      printf("%-7d %-5d %s%s\n", row.m_Eid, row.m_Depth, pad.c_str(), row.m_Name.c_str());
    }
    shown++;
  }
  ArrayClose(false);    // totalActions/totalCalls/shown follow
  g_Indent = g_bJson ? 1 : 0;
  Field("totalActions", (long long)rows.size());
  Field("totalCalls", (long long)calls);
  Field("shown", shown, !bTruncated);
  if(bTruncated)
  {
    // Only reachable on a capture whose action tree is deeper than the recursion limit: say so
    // rather than presenting a partial tree as the whole one.
    Field("truncated", std::string("action tree deeper than the recursion limit"), true);
  }
  g_Indent = 0;
  if(g_bJson)
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
                const char *saveDir, const PictureOptions &opts)
{
  // Before the header, so a failure to make the destination prints nothing rather than half a
  // document. The folder is one the tool makes for the same reason `dump`'s, `sheet`'s and
  // `patch`'s are: a caller names a destination, and `textures --save shots/frame12` used to warn
  // once per texture instead ("could not save res271") and exit 0 -- the files that were asked for
  // are not there, and the run says it succeeded, which is the one combination a script cannot see.
  if(saveDir != NULL && *saveDir != '\0' && !MakeDir(saveDir))
    return Fail(1, "cannot create %s", saveDir);

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
      // The name carries the subresource when one was asked for, so a directory of pictures says
      // which mip and which slice each file is: `tex_270.png` is mip 0 slice 0, and `--mip 2`
      // writes `tex_270_m2.png` rather than quietly overwriting it. A cubemap face is slice 0..5,
      // so `--slice 3` is a face.
      std::string stem = Fmt("tex_%s", id.c_str());
      if(opts.m_Sub.mip != 0)
        stem += Fmt("_m%u", opts.m_Sub.mip);
      if(opts.m_Sub.slice != 0)
        stem += Fmt("_s%u", opts.m_Sub.slice);
      if(opts.m_Sub.sample != 0)
        stem += Fmt("_x%u", opts.m_Sub.sample);

      if(opts.m_bRaw)
      {
        // The engine's own bytes for one subresource, undecoded: the answer for a format the
        // display path will not take, and the only way to have exactly what the capture holds
        // rather than a picture of it. What the bytes *are* is a fact about the format, so the line
        // names the format and the size -- a `.bin` nobody can interpret would be a file that only
        // looks like data.
        const bytebuf raw = ctrl->GetTextureData(t.resourceId, opts.m_Sub);
        const std::string out = (std::filesystem::path(saveDir) / (stem + ".bin")).string();
        FILE *f = FileOpen(std::filesystem::path(out), "wb");
        const bool bWritten =
            f != NULL &&
            (raw.size() == 0 || fwrite(raw.data(), 1, (size_t)raw.size(), f) == raw.size());
        if(f != NULL)
          fclose(f);
        if(!bWritten)
        {
          fprintf(stderr, "  warning: could not write %s\n", out.c_str());
        }
        else
        {
          const std::string summary = Fmt("  -> %s (%llu B of %s)", out.c_str(),
                                          (unsigned long long)raw.size(), t.format.Name().c_str());
          if(IsJson())
            fprintf(stderr, "%s\n", summary.c_str());
          else
            printf("%s\n", summary.c_str());
        }
      }
      else
      {
        TextureSave save;
        save.resourceId = t.resourceId;
        save.destType = FileType::PNG;
        ApplySaveOptions(save, opts);
        const std::string out = (std::filesystem::path(saveDir) / (stem + ".png")).string();
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
  }
  ArrayClose(false);    // total/shown follow
  g_Indent = g_bJson ? 1 : 0;
  Field("total", (long long)texs.size());
  Field("shown", shown, true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return 0;
}

//: Post-VS geometry for one instance: what the vertex shader actually emitted, per vertex. This is
//: the "which instance SH reached the pixel shader" question the offline tool cannot answer.
// --------------------------------------------------------------------------- mesh, and its stages

long long PrimitiveCount(Topology topology, long long count)
{
  if(count <= 0)
    return 0;
  switch(topology)
  {
    case Topology::PointList: return count;
    case Topology::LineList: return count / 2;
    case Topology::LineStrip: return count - 1;
    case Topology::LineLoop: return count;    // the closing segment draws no vertex of its own
    case Topology::TriangleList: return count / 3;
    case Topology::TriangleStrip: return count > 1 ? count - 2 : 0;
    case Topology::TriangleFan: return count > 1 ? count - 2 : 0;
    case Topology::LineList_Adj: return count / 4;    // four per segment, two of them adjacency
    case Topology::TriangleList_Adj:
      return count / 6;    // six per triangle, three of them adjacency
    default: break;
  }

  // `PatchList_1CPs` is where the patch list starts and each following value adds a control point,
  // so a patch's vertex count is the difference from that base. `Topology::Count` and the meshlet
  // list sit above the last patch size and fall out of the range on purpose: 0 is this function's
  // "not derived".
  const int patchBase = (int)Topology::PatchList_1CPs;
  const int perPatch = (int)topology - patchBase + 1;
  if((int)topology >= patchBase && perPatch <= 16)
    return count / perPatch;

  // A strip with adjacency, and a meshlet list: the pattern is in the index buffer or in the
  // meshlets, and the count alone does not say how many primitives there are. The command prints
  // that in words rather than printing this zero as a count.
  return 0;
}

Bounds3 VertexBounds(const bytebuf &data, size_t stride, size_t count)
{
  Bounds3 bounds;
  for(size_t v = 0; v < count; v++)
  {
    // The offset is computed in `size_t` and checked before it is used: `v * stride` can wrap, and
    // a wrapped offset would read outside the buffer ([expr.add]).
    const size_t offset = v * stride;
    if(stride == 0 || offset + 3u * sizeof(float) > data.size())
      break;
    float xyz[3];
    memcpy(xyz, data.data() + offset, sizeof(xyz));
    // A `NaN` or an infinity is skipped rather than compared: one of them in the stream poisons
    // every later min and max, and `nan nan nan` is not a bounds line. `m_bAny` then says whether
    // anything was finite.
    if(!std::isfinite(xyz[0]) || !std::isfinite(xyz[1]) || !std::isfinite(xyz[2]))
      continue;
    if(!bounds.m_bAny)
    {
      for(size_t c = 0; c < 3; c++)
        bounds.m_Min[c] = bounds.m_Max[c] = xyz[c];
      bounds.m_bAny = true;
      continue;
    }
    for(size_t c = 0; c < 3; c++)
    {
      if(xyz[c] < bounds.m_Min[c])
        bounds.m_Min[c] = xyz[c];
      if(xyz[c] > bounds.m_Max[c])
        bounds.m_Max[c] = xyz[c];
    }
  }
  return bounds;
}

long long WriteObj(const std::filesystem::path &path, const bytebuf &positions, size_t stride,
                   size_t count, const std::vector<uint32_t> &indices, Topology topology,
                   std::string &why)
{
  FILE *f = FileOpen(path, "wb");
  if(f == NULL)
  {
    why = Fmt("cannot write %s", path.string().c_str());
    return -1;
  }

  // What an OBJ carries, and what it cannot: the `v` lines are positions only (the rest of an
  // interleaved vertex has nowhere to go in the format), and faces are spelled out only where the
  // vertex order *is* the primitive's. A triangle list is that case; a strip's order is not a
  // face's, and an index buffer's values are not vertex numbers when the draw has a `baseVertex`
  // (the caller leaves the indices out then). The header says which of the two this file is -- a
  // file that silently loses its faces is worse than one that says why, because it still opens.
  const bool bFaces = topology == Topology::TriangleList;
  fprintf(f, "# replay_dump mesh --obj: %llu vertex/vertices, topology %d\n",
          (unsigned long long)count, (int)topology);
  if(!bFaces)
    fprintf(
        f,
        "# no faces: this writer spells them out for a triangle list, and this draw is not one\n");

  long long written = 0;
  for(size_t v = 0; v < count; v++)
  {
    const size_t offset = v * stride;
    if(stride == 0 || offset + 3u * sizeof(float) > positions.size())
      break;
    float xyz[3];
    memcpy(xyz, positions.data() + offset, sizeof(xyz));
    fprintf(f, "v %g %g %g\n", (double)xyz[0], (double)xyz[1], (double)xyz[2]);
    written++;
  }

  if(bFaces && written > 0)
  {
    if(indices.empty())
    {
      // No index buffer: the vertices are the triangles, in threes.
      for(long long i = 0; i + 2 < written; i += 3)
        fprintf(f, "f %lld %lld %lld\n", i + 1, i + 2, i + 3);
    }
    else
    {
      for(size_t i = 0; i + 2 < indices.size(); i += 3)
      {
        // One-based, and only when all three are inside the stream that was read: an index past the end is a
        // hole in the buffer, and a face naming it would claim a vertex this file does not have.
        if((long long)indices[i] >= written || (long long)indices[i + 1] >= written ||
           (long long)indices[i + 2] >= written)
          continue;
        fprintf(f, "f %u %u %u\n", (unsigned)indices[i] + 1u, (unsigned)indices[i + 1] + 1u,
                (unsigned)indices[i + 2] + 1u);
      }
    }
  }
  fclose(f);
  return written;
}

int CmdMesh(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid, int instance,
            int maxRows, MeshDataStage stage, const char *objPath)
{
  MoveToEvent(ctrl, eid);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("instance", (long long)instance);
  // Which stage this is, in words: `vsout` is the default and `vsin` is the stream the draw *read*,
  // and the two differ by the shader -- a reader comparing two runs has to know which one produced
  // the numbers.
  Field("stage", std::string(MeshStageText(stage)));

  const MeshFormat mesh = ctrl->GetPostVSData((uint32_t)instance, 0, stage);
  const bool bHasData = mesh.vertexResourceId != ResourceId::Null() && mesh.vertexByteStride != 0;

  Field("topology", (long long)mesh.topology);
  Field("vertexResource", IdText(mesh.vertexResourceId));
  Field("vertexStride", (long long)mesh.vertexByteStride);
  Field("vertexBytes", (long long)mesh.vertexByteSize);
  Field("indexResource", IdText(mesh.indexResourceId));
  Field("indexBytes", (long long)mesh.indexByteSize);
  // Whether this is the last member of the object depends on whether the stream follows, and the
  // separator has to agree with that: `last` is the one thing the writer cannot work out alone.
  Field("baseVertex", (long long)mesh.baseVertex, !bHasData);

  if(!bHasData)
  {
    if(IsJson())
      printf("}\n");
    else
      printf("(no %s data for this event: nothing was recorded for that stage)\n",
             MeshStageText(stage));
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
  ArrayClose(false);    // the counts, the bounds and the export follow
  g_Indent = g_bJson ? 1 : 0;
  Field("vertexCount", (long long)count);
  Field("componentsPerVertex", (long long)comps);

  // The index stream, read for its *count*: how many primitives the draw covered is the sanity line
  // a mesh shader's output needs (a meshlet list read as positions gives a count that is right and
  // bounds that are not), and the count is what `PrimitiveCount` folds the topology over.
  std::vector<uint32_t> indices;
  long long indexCount = 0;
  if(mesh.indexResourceId != ResourceId::Null() && mesh.indexByteStride != 0)
  {
    indexCount = (long long)(mesh.indexByteSize / mesh.indexByteStride);
    Field("indexCount", indexCount);
    // Kept for `--obj` only, and only when the values are vertex numbers: with a `baseVertex` the fetched
    // vertex is `baseVertex + index`, so those numbers name nothing in the stream this reads.
    if(objPath != NULL && mesh.baseVertex == 0)
    {
      const bytebuf raw =
          ctrl->GetBufferData(mesh.indexResourceId, mesh.indexByteOffset, mesh.indexByteSize);
      const size_t element = mesh.indexByteStride;
      for(size_t i = 0; i + element <= raw.size(); i += element)
      {
        uint32_t value = 0;
        if(element == 2)
        {
          uint16_t small = 0;
          memcpy(&small, raw.data() + i, sizeof(small));
          value = small;
        }
        else
        {
          memcpy(&value, raw.data() + i, sizeof(value));
        }
        indices.push_back(value);
      }
    }
  }
  else
  {
    Field("indexCount", 0);
  }

  const long long primitives =
      PrimitiveCount(mesh.topology, indexCount > 0 ? indexCount : (long long)count);
  if(primitives > 0)
  {
    Field("primitives", primitives);
  }
  else
  {
    // Not derived, and said so: a strip with adjacency and a meshlet list do not say their primitive count
    // in the vertex count, and a `0` in this field would read as "nothing was drawn".
    Field("primitivesNote",
          Fmt("not derived: topology %d does not fix a primitive count from %lld %s",
              (int)mesh.topology, indexCount > 0 ? indexCount : (long long)count,
              indexCount > 0 ? "index/indices" : "vertex/vertices"));
  }

  const Bounds3 bounds = VertexBounds(data, stride, count);
  if(bounds.m_bAny)
  {
    Field("boundsMin", Fmt("%g %g %g", (double)bounds.m_Min[0], (double)bounds.m_Min[1],
                           (double)bounds.m_Min[2]));
    Field("boundsMax", Fmt("%g %g %g", (double)bounds.m_Max[0], (double)bounds.m_Max[1],
                           (double)bounds.m_Max[2]));
  }
  else
  {
    Field("boundsNote",
          std::string("no finite position in the stream: every vertex had a NaN or an "
                      "infinity in its first three components"));
  }

  // The export is the last member whether or not `--obj` was given: a document whose *shape*
  // depends on a flag is one every consumer has to special-case, and an empty `obj` is the "not
  // asked for" answer that keeps one schema for one command. `last` is the one thing the writer
  // cannot work out for itself.
  long long objVertices = 0;
  std::string objWhy;
  if(objPath != NULL)
    objVertices = WriteObj(std::filesystem::path(objPath), data, stride, count, indices,
                           mesh.topology, objWhy);
  Field("obj", std::string(objPath != NULL ? objPath : ""));
  if(objVertices < 0)
  {
    fprintf(stderr, "warning: %s\n", objWhy.c_str());
    objVertices = 0;
  }
  Field("objVertices", objVertices, true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return objWhy.empty() ? 0 : 1;
}

// ------------------------------------------------------------------- the format coverage audit

//: What one format is made of, as one line: the components and their width, the type the engine
//: reads them as (`CastText` -- the same vocabulary `--cast` takes, so `formats` and `image --cast`
//: cannot disagree about what a type is called), and the two flags that change what a picture of it
//: means.
std::string FormatShape(const ResourceFormat &format)
{
  return Fmt("%u x %u-bit %s%s%s", (unsigned)format.compCount, (unsigned)format.compByteWidth * 8u,
             CastText(format.compType), format.SRGBCorrected() ? " srgb" : "",
             format.BlockFormat() ? " block" : "");
}

//: The engine's own answer to "can this be a picture?", with the reason when the answer is not a
//: plain yes. It is a *rule* about the format, not a promise about the frame: the display path is
//: what actually decodes one, and it has a measured failure the caller covers with
//: `SaveTargetImage`'s engine-encoder fallback (one texture in the Android capture comes back empty
//: through the display path).
//:
//: Three answers, and the middle one is why this is not a bool: a **typeless** format has no type
//: of its own to read the bits as, so a picture needs `--cast` -- and `compType == Typeless` is
//: exactly the case `TextureDisplay::typeCast` exists for. A format the engine has no layout for
//: (`Special`, or no components at all) cannot be shown at all, and that is worth a row rather than
//: a silent skip.
bool FormatPicture(const ResourceFormat &format, bool &bNeedsCast, std::string &why)
{
  bNeedsCast = false;
  why.clear();
  if(format.compCount == 0 || format.compByteWidth == 0)
  {
    why = "the engine's format table has no layout for it (no components)";
    return false;
  }
  if(format.compType == CompType::Typeless)
  {
    bNeedsCast = true;
    why = "typeless: `--cast <type>` says how to read the bits";
    return false;
  }
  if(format.Special())
    why = "a special layout (multi-plane or packed): the display path does not take it";
  return !format.Special();
}

int CmdFormats(IReplayController *ctrl, ICaptureFile *file, const char *path)
{
  PrintCaptureHeader(file, path);

  const rdcarray<TextureDescription> &texs = ctrl->GetTextures();

  // Grouped by the format's own *name*, which is what one format is: the enum has one value per
  // layout and the engine names each. A name is also what a reader can match against the capture's
  // own tables, and it is what `--cast` is phrased in -- the numeric id never appears in a picture
  // command, so it is not the grouping key here.
  struct Group
  {
    std::string m_Key;
    ResourceFormat m_Format;
    long long m_Count = 0;
    unsigned long long m_Bytes = 0;
    long long m_NeedCast = 0;
  };
  std::vector<Group> groups;
  unsigned long long bytes = 0;

  for(size_t i = 0; i < texs.size(); i++)
  {
    const TextureDescription &t = texs[i];
    const std::string key(t.format.Name().c_str());
    Group *group = NULL;
    for(size_t g = 0; g < groups.size(); g++)
    {
      if(groups[g].m_Key == key)
      {
        group = &groups[g];
        break;
      }
    }
    if(group == NULL)
    {
      groups.push_back(Group());
      group = &groups.back();
      group->m_Key = key;
      group->m_Format = t.format;
    }
    group->m_Count++;
    group->m_Bytes += (unsigned long long)t.byteSize;
    bytes += (unsigned long long)t.byteSize;
  }

  // The classes the totals are about, counted once: how many *textures* need a cast, and how many the engine
  // has no layout for. A total is what a summary can say without listing every row again.
  long long needsCast = 0, noLayout = 0, total = 0;
  for(size_t g = 0; g < groups.size(); g++)
  {
    bool bNeedsCast = false;
    std::string why;
    if(!FormatPicture(groups[g].m_Format, bNeedsCast, why))
    {
      if(bNeedsCast)
        needsCast += groups[g].m_Count;
      else
        noLayout += groups[g].m_Count;
    }
    total += groups[g].m_Count;
  }

  if(!IsJson())
  {
    printf("%-28s %5s %13s  %-26s %-12s %s\n", "format", "count", "bytes", "shape", "picture",
           "why");
  }

  ArrayOpen("formats");
  for(size_t g = 0; g < groups.size(); g++)
  {
    const ResourceFormat &format = groups[g].m_Format;
    bool bNeedsCast = false;
    std::string why;
    const bool bPicture = FormatPicture(format, bNeedsCast, why);
    const char *picture = bPicture ? "yes" : (bNeedsCast ? "with a cast" : "no");

    if(IsJson())
    {
      ObjectRow(Fmt(
          "{\"format\": \"%s\", \"textures\": %lld, \"bytes\": %llu, \"components\": %u,"
          " \"componentBits\": %u, \"type\": \"%s\", \"srgb\": %d, \"blockCompressed\": %d,"
          " \"special\": %d, \"elementBytes\": %u, \"picture\": \"%s\", \"why\": \"%s\"}",
          JsonEscape(groups[g].m_Key.c_str()).c_str(), groups[g].m_Count, groups[g].m_Bytes,
          (unsigned)format.compCount, (unsigned)format.compByteWidth * 8u, CastText(format.compType),
          format.SRGBCorrected() ? 1 : 0, format.BlockFormat() ? 1 : 0, format.Special() ? 1 : 0,
          (unsigned)format.ElementSize(), picture, JsonEscape(why.c_str()).c_str()));
    }
    else
    {
      printf("%-28s %5lld %10.2f MB  %-26s %-12s %s\n", groups[g].m_Key.c_str(), groups[g].m_Count,
             (double)groups[g].m_Bytes / 1048576.0, FormatShape(format).c_str(), picture,
             why.c_str());
    }
  }
  ArrayClose(false);    // the totals follow

  g_Indent = g_bJson ? 1 : 0;
  Field("textures", total);
  Field("bytes", (long long)bytes);
  // `formatCount`, not `formats`: the array above already owns that name, and two members with one
  // key is a document no parser can read.
  Field("formatCount", (long long)groups.size());
  // The two sentences this command exists to be able to say, counted rather than left for the reader to add
  // up: a texture the engine cannot show without being told how, and one it has no layout for at all.
  Field("needCast", needsCast);
  Field("noLayout", noLayout, true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return 0;
}

//: Defined with the bundle (REFERENCE §9), used here too: `image` and the bundle's `rt/` images
//: save a target through the same code so the two cannot drift apart.
bool SaveTargetImage(IReplayController *ctrl, ResourceId target, const char *outBase,
                     const PictureOptions &opts, std::string &written, int32_t &width,
                     int32_t &height);

int CmdImage(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
             const char *outPath, const PictureOptions &opts)
{
  MoveToEvent(ctrl, eid);

  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  ResourceId rt = d3d12 && !d3d12->outputMerger.renderTargets.empty()
                      ? d3d12->outputMerger.renderTargets[0].resource
                      : ResourceId::Null();
  if(rt == ResourceId::Null())
    return Fail(1, "nothing is bound to render target 0 at eid %d", eid);

  int32_t width = 0, height = 0;
  std::string used;
  const bool bOk = SaveTargetImage(ctrl, rt, outPath, opts, used, width, height);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("resource", IdText(rt));
  Field("width", (long long)width);
  Field("height", (long long)height);
  Field("file", used);
  // What was asked for, so a picture can be read back later knowing which overlay and which
  // subresource it shows: `none` and `0` are the defaults, and a document that leaves them out
  // cannot be told from one that was made with `--overlay wireframe`.
  Field("overlay", std::string(OverlayText(opts.m_Overlay)));
  Field("mip", (long long)opts.m_Sub.mip);
  Field("slice", (long long)opts.m_Sub.slice);
  Field("sample", (long long)opts.m_Sub.sample);
  Field("cast", std::string(CastText(opts.m_bCastGiven ? opts.m_Cast : CompType::Typeless)));
  Field("written", bOk ? 1 : 0, true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return bOk ? 0 : 1;
}

// --------------------------------------------------------------------------- per-pass counters
//
// `FetchCounters` answers per event and takes no range, so a pass's cost is folded here out of the
// per-event list (REFERENCE §9). Three things about that are worth saying rather than assuming:
//
// * Which counter *is* the cost is the engine's choice, not ours: `EventGPUDuration` when this
//   replay produced one, and the first counter it did produce otherwise. It is named in the
//   document either way, because a cost column headed `counter(7)` says nothing.
// * A pass is folded over `[firstEid, lastEid]`, both inclusive, and `measured` says how many of
//   the pass's events actually produced a value -- a pass the counter skipped is not a free pass.
// * A run with no counter results is *not* a run where every pass costs zero. GPU counters are a
//   driver feature and are off, or unsupported, on plenty of machines: `available` and `note` carry
//   that, and no table of zeros is printed to suggest otherwise.

//: One counter folded over each pass. `FetchCounters` answers per event and takes no range, so a
//: pass's cost is the sum of the values at the events in `[first, last]`, both inclusive -- and
//: `measured` is how many of the pass's events produced one, because a pass the counter skipped is
//: not a pass that cost nothing.
//:
//: Free of the controller on purpose: this machine's replays publish 16 counters and produce no
//: results for either real capture, so the arithmetic is the one part of `--per-pass` that a check
//: can reach.
//: A counter result as a double, reading whichever member of the union the counter's own
//: `resultType` says is meaningful. Reading `.d` for every counter -- which is what a first cut did
//: -- prints a `u64` as a double, and a table of those is worse than no table.
static double CounterValueAsDouble(const CounterResult &result, CompType resultType)
{
  switch(ComponentClass(resultType))
  {
    case CompType::UInt: return (double)result.value.u64;
    case CompType::SInt: return (double)(long long)result.value.u64;
    default: return result.value.d;
  }
}

std::vector<PassCost> FoldPassCosts(const rdcarray<CounterResult> &results, GPUCounter cost,
                                    CompType resultType, const std::vector<PassRange> &passes,
                                    const std::vector<ActionNode> &rows)
{
  // One pass over the calls and one over the results, with each row *bucketed* into its pass,
  // rather than a scan of every call and every result per pass: the old shape was O(passes x (calls
  // + results)) and a frame with a few hundred passes and five figures of counter results is where
  // that shows.
  //
  // The bucket holds *indices* in the array's own order, so each pass sums the same values in the
  // same order it used to -- floating-point addition is not associative, and a tidier-looking sort
  // by event id would have changed the last digits of a total that a reader compares between
  // captures. The passes are ordered and do not overlap (`PassesFromActions`), which is what makes
  // `upper_bound` on `m_First` the right question.
  std::vector<std::vector<size_t>> resultBuckets(passes.size());
  std::vector<int> callCounts(passes.size(), 0);
  for(size_t i = 0; i < results.size(); i++)
  {
    if(results[i].counter != cost)
      continue;
    const int id = (int)results[i].eventId;
    const size_t at =
        (size_t)(std::upper_bound(passes.begin(), passes.end(), id,
                                  [](int eid, const PassRange &r) { return eid < r.m_First; }) -
                 passes.begin());
    if(at > 0 && id <= passes[at - 1].m_Last)
      resultBuckets[at - 1].push_back(i);
  }
  for(size_t i = 0; i < rows.size(); i++)
  {
    if(!rows[i].m_bCall)
      continue;
    const size_t at =
        (size_t)(std::upper_bound(passes.begin(), passes.end(), rows[i].m_Eid,
                                  [](int eid, const PassRange &r) { return eid < r.m_First; }) -
                 passes.begin());
    if(at > 0 && rows[i].m_Eid <= passes[at - 1].m_Last)
      callCounts[at - 1]++;
  }

  std::vector<PassCost> costs;
  costs.reserve(passes.size());
  for(size_t p = 0; p < passes.size(); p++)
  {
    PassCost pc;
    pc.m_Index = (int)p + 1;
    pc.m_Name = passes[p].m_Name;
    pc.m_First = passes[p].m_First;
    pc.m_Last = passes[p].m_Last;
    pc.m_Events = callCounts[p];
    for(size_t k = 0; k < resultBuckets[p].size(); k++)
    {
      const double value = CounterValueAsDouble(results[resultBuckets[p][k]], resultType);
      pc.m_Sum += value;
      if(pc.m_Measured == 0 || value > pc.m_Max)
        pc.m_Max = value;
      pc.m_Measured++;
    }
    costs.push_back(pc);
  }
  return costs;
}

std::vector<PassRange> PassesFromActions(const std::vector<ActionNode> &rows)
{
  std::vector<PassRange> passes;
  for(size_t i = 0; i < rows.size(); i++)
  {
    if(!rows[i].m_bCall)
      continue;
    const std::string path(rows[i].m_Path.c_str(), rows[i].m_Path.size());
    if(!passes.empty() && passes.back().m_Name == path)
    {
      // Same marker path as the call before it: still the same pass, which is what makes a pass the
      // maximal run of consecutive calls sharing one and not merely one marker's own rows.
      passes.back().m_Last = rows[i].m_Eid;
      continue;
    }
    PassRange pass;
    pass.m_Name = path;
    pass.m_First = rows[i].m_Eid;
    pass.m_Last = rows[i].m_Eid;
    passes.push_back(pass);
  }
  return passes;
}

//: Reads the next integer in `line` from `pos`, and leaves `pos` just past it. Deliberately not
//: `ParseInt` on a token: this is the front of a line whose tail is a free-text name, so there is
//: nothing to split on and nothing to escape.
static bool ReadRangeInt(const std::string &line, size_t &pos, int &value)
{
  pos = line.find_first_not_of(" \t", pos);
  if(pos == std::string::npos)
    return false;
  const size_t start = pos;
  while(pos < line.size() && line[pos] >= '0' && line[pos] <= '9')
    pos++;
  if(pos == start)
    return false;
  int parsed = 0;
  if(!ParseInt(line.substr(start, pos - start).c_str(), parsed))
    return false;
  value = parsed;
  return true;
}

bool ReadPassRanges(const char *path, std::vector<PassRange> &ranges, std::string &why)
{
  std::string text;
  if(!ReadWholeFile(path, text))
  {
    why = Fmt("cannot read '%s'", path);
    return false;
  }

  ranges.clear();
  size_t at = 0;
  int lineNo = 0;
  while(at < text.size())
  {
    size_t end = text.find('\n', at);
    if(end == std::string::npos)
      end = text.size();
    std::string line = text.substr(at, end - at);
    at = end + 1;
    lineNo++;
    while(!line.empty() && (line.back() == '\r' || line.back() == '\n'))
      line.pop_back();

    const size_t first = line.find_first_not_of(" \t");
    if(first == std::string::npos || line[first] == '#')
      continue;

    size_t pos = first;
    int firstEid = 0, lastEid = 0;
    if(!ReadRangeInt(line, pos, firstEid) || !ReadRangeInt(line, pos, lastEid))
    {
      why = Fmt("line %d: expected '<first eid> <last eid> [<name>]', got \"%s\"", lineNo,
                line.c_str());
      return false;
    }
    if(firstEid <= 0 || lastEid < firstEid)
    {
      why = Fmt("line %d: %d..%d is not a range of event ids", lineNo, firstEid, lastEid);
      return false;
    }

    PassRange pass;
    pass.m_First = firstEid;
    pass.m_Last = lastEid;
    const size_t nameStart = line.find_first_not_of(" \t", pos);
    // The name is optional and free text, so a pass nobody named is named by its range rather than
    // by an invented one.
    pass.m_Name = nameStart == std::string::npos ? Fmt("eid %d-%d", firstEid, lastEid)
                                                 : line.substr(nameStart);
    ranges.push_back(pass);
  }

  if(ranges.empty())
  {
    why = Fmt("'%s' names no passes", path);
    return false;
  }
  return true;
}

//: The engine's own unit for a counter, which is the only authority for it: `EventGPUDuration`
//: being milliseconds is a fact about the counter, not about the number.
static const char *CounterUnitText(CounterUnit unit)
{
  switch(unit)
  {
    case CounterUnit::Seconds: return "s";
    case CounterUnit::Percentage: return "%";
    case CounterUnit::Ratio: return "x";
    case CounterUnit::Bytes: return " bytes";
    case CounterUnit::Cycles: return " cycles";
    case CounterUnit::Hertz: return " Hz";
    case CounterUnit::Volt: return " V";
    case CounterUnit::Celsius: return " C";
    default: return "";    // Absolute: the value is the value
  }
}

int CmdCounters(IReplayController *ctrl, ICaptureFile *file, const char *path, bool bPerPass,
                const char *passesPath, int topN)
{
  rdcarray<CounterResult> results = ctrl->FetchCounters(rdcarray<GPUCounter>());

  if(!bPerPass)
  {
    PrintCaptureHeader(file, path);

    // RenderDoc's own counter names live in its (unexported) stringise.cpp, so the enum value is
    // what gets printed here; `GPUCounter`'s numbering is in the API headers.
    ArrayOpen("counters");
    for(size_t i = 0; i < results.size(); i++)
      Row(Fmt("eid %-7u %-18s = %f", (unsigned)results[i].eventId,
              CounterText(results[i].counter).c_str(), results[i].value.d));
    ArrayClose(false);    // total follows
    g_Indent = g_bJson ? 1 : 0;
    Field("total", (long long)results.size(), true);
    g_Indent = 0;
    if(g_bJson)
      printf("}\n");
    return 0;
  }

  // ---- one row per pass --------------------------------------------------------------------
  PrintCaptureHeader(file, path);

  // The action tree is where both the passes and the per-pass event counts come from, so it is
  // fetched whether or not `--passes` supplies the ranges.
  int calls = 0;
  bool bTruncated = false;
  const std::vector<ActionNode> rows = ActionTree(ctrl, calls, bTruncated);

  std::vector<PassRange> passes;
  std::string passesSource = "the frame's markers";
  if(passesPath != NULL && passesPath[0] != '\0')
  {
    std::string why;
    if(!ReadPassRanges(passesPath, passes, why))
      return Fail(1, "counters: --passes: %s", why.c_str());
    passesSource = passesPath;
  }
  else
  {
    passes = PassesFromActions(rows);
  }

  // Which counters this replay even has: the descriptions name them and give the unit and the
  // result type, all of which are the engine's answer and not ours.
  const rdcarray<GPUCounter> available = ctrl->EnumerateCounters();
  std::map<GPUCounter, CounterDescription> descriptions;
  for(size_t i = 0; i < available.size(); i++)
    descriptions[available[i]] = ctrl->DescribeCounter(available[i]);

  GPUCounter cost = GPUCounter::EventGPUDuration;
  bool bHaveCost = false;
  for(size_t i = 0; i < results.size() && !bHaveCost; i++)
  {
    if(results[i].counter == GPUCounter::EventGPUDuration)
    {
      cost = results[i].counter;
      bHaveCost = true;
    }
  }
  if(!bHaveCost && !results.empty())
  {
    cost = results[0].counter;
    bHaveCost = true;
  }

  std::string costName = CounterText(cost);
  std::string unit;
  CompType resultType = CompType::Float;
  const std::map<GPUCounter, CounterDescription>::const_iterator dit = descriptions.find(cost);
  if(dit != descriptions.end())
  {
    if(!dit->second.name.empty())
      costName = dit->second.name.c_str();
    unit = CounterUnitText(dit->second.unit);
    resultType = dit->second.resultType;
  }

  // Wrapped: `Field` has a `rdcstr` and a `string_view` overload and a bare literal is ambiguous
  // between them.
  Field("mode", std::string("per-pass"));
  Field("costCounter", costName);
  Field("unit", unit);
  Field("passesFrom", passesSource);
  Field("available", (long long)results.size());

  if(!bHaveCost)
  {
    // Not "every pass costs zero": a table of zeros is a fact this machine did not give.
    const std::string note =
        available.empty()
            ? std::string(
                  "this replay publishes no counters at all -- GPU counters are a driver "
                  "feature and are not available here, so no pass cost can be folded")
            : Fmt("this replay publishes %d counter(s) but produced no results for this frame -- "
                  "nothing was measured, so no pass cost can be folded",
                  (int)available.size());
    // The arrays are written empty rather than left out: a document whose members come and go with
    // the answer is one a consumer cannot validate against one schema.
    ArrayOpen("passes");
    ArrayClose(false);
    ArrayOpen("top");
    ArrayClose(false);
    Field("topCount", (long long)0);
    Field("note", note, true);
    g_Indent = 0;
    if(g_bJson)
      printf("}\n");
    Log("counters: %s", note.c_str());
    return 0;
  }

  const std::vector<PassCost> costs = FoldPassCosts(results, cost, resultType, passes, rows);

  std::vector<int> order;
  order.reserve(costs.size());
  for(size_t i = 0; i < costs.size(); i++)
    order.push_back((int)i);
  std::stable_sort(order.begin(), order.end(), [&costs](int a, int b) {
    if(costs[(size_t)a].m_Sum != costs[(size_t)b].m_Sum)
      return costs[(size_t)a].m_Sum > costs[(size_t)b].m_Sum;
    return a < b;    // a tie keeps frame order, so the same run prints the same thing twice
  });
  const int top = topN > 0 ? std::min<int>(topN, (int)order.size()) : (int)order.size();

  ArrayOpen("passes");
  for(size_t i = 0; i < costs.size(); i++)
  {
    const PassCost &pc = costs[i];
    if(g_bJson)
    {
      ObjectOpen();
      Field("pass", (long long)pc.m_Index);
      Field("name", pc.m_Name);
      Field("firstEid", (long long)pc.m_First);
      Field("lastEid", (long long)pc.m_Last);
      Field("events", (long long)pc.m_Events);
      Field("measured", (long long)pc.m_Measured);
      Field("cost", Fmt("%.3f", pc.m_Sum));
      Field("peak", Fmt("%.3f", pc.m_Max), true);
      ObjectClose();
    }
    else
    {
      const std::string name = pc.m_Name.empty() ? std::string("(no marker)") : pc.m_Name;
      Row(Fmt("pass %-4d eid %-7d - %-7d %5d event(s), %5d measured   %-28s %s%s", pc.m_Index,
              pc.m_First, pc.m_Last, pc.m_Events, pc.m_Measured, name.c_str(),
              Fmt("%.3f", pc.m_Sum).c_str(), unit.c_str()));
    }
  }
  ArrayClose(false);    // top follows

  ArrayOpen("top");
  for(int i = 0; i < top; i++)
  {
    const PassCost &pc = costs[(size_t)order[(size_t)i]];
    if(g_bJson)
    {
      ObjectOpen();
      Field("pass", (long long)pc.m_Index);
      Field("name", pc.m_Name);
      Field("cost", Fmt("%.3f", pc.m_Sum), true);
      ObjectClose();
    }
    else
    {
      const std::string name = pc.m_Name.empty() ? std::string("(no marker)") : pc.m_Name;
      Row(Fmt("%2d. pass %-4d %-44s %s%s", i + 1, pc.m_Index, name.c_str(),
              Fmt("%.3f", pc.m_Sum).c_str(), unit.c_str()));
    }
  }
  ArrayClose(false);
  Field("topCount", (long long)top, true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");

  const std::string headline =
      costs.empty()
          ? std::string("no passes to fold: the frame's calls carry no marker to group them by")
          : Fmt("%d pass(es) folded over %s; dearest: %s (eid %d-%d) at %.3f%s -- %d of %d "
                "event(s) with a value",
                (int)costs.size(), passesSource.c_str(),
                costs[(size_t)order[0]].m_Name.empty() ? "(no marker)"
                                                       : costs[(size_t)order[0]].m_Name.c_str(),
                costs[(size_t)order[0]].m_First, costs[(size_t)order[0]].m_Last,
                costs[(size_t)order[0]].m_Sum, unit.c_str(), costs[(size_t)order[0]].m_Measured,
                costs[(size_t)order[0]].m_Events);
  Log("counters: %s", headline.c_str());
  return 0;
}

std::vector<DebugGroup> GroupDebugMessages(const rdcarray<DebugMessage> &messages)
{
  std::vector<DebugGroup> groups;
  for(size_t i = 0; i < messages.size(); i++)
  {
    const DebugMessage &message = messages[i];
    DebugGroup *found = NULL;
    for(size_t g = 0; g < groups.size(); g++)
    {
      if(groups[g].messageID == message.messageID && groups[g].severity == message.severity &&
         groups[g].category == message.category && groups[g].source == message.source)
      {
        found = &groups[g];
        break;
      }
    }

    if(found == NULL)
    {
      DebugGroup group;
      group.severity = message.severity;
      group.category = message.category;
      group.source = message.source;
      group.messageID = message.messageID;
      group.firstEid = message.eventId;
      group.lastEid = message.eventId;
      group.count = 1;
      group.text = message.description.c_str();
      groups.push_back(group);
      continue;
    }

    found->count++;
    found->firstEid = std::min(found->firstEid, message.eventId);
    found->lastEid = std::max(found->lastEid, message.eventId);
  }

  std::sort(groups.begin(), groups.end(), [](const DebugGroup &a, const DebugGroup &b) {
    if(a.severity != b.severity)
      return (unsigned)a.severity < (unsigned)b.severity;
    if(a.count != b.count)
      return a.count > b.count;
    return a.firstEid < b.firstEid;
  });
  return groups;
}

//: `debug <rdc> [--group] [--fail-on high|medium|low|info]`: the engine's own complaints.
//:
//: Two shapes of the same answer. Without `--group` it is one row per message, which is what a reader
//: wants at ten messages; with it, one row per *distinct* message with its count and its eid range,
//: which is what a reader wants at ten thousand. Rows are strings in both, so `--json` stays the
//: document the `messages` schema describes (REFERENCE 4.12) and the grouped form needs no new kind.
//:
//: `--fail-on` is the driver-side sanity gate: with a threshold, a run exits **1** when anything at or
//: above it was reported -- High is the most severe, so `--fail-on medium` means High or Medium. Nothing
//: else in this program fails on a *finding* rather than on a failure, and the exit code is the point:
//: "did the engine complain" becomes a line in a script instead of a paragraph a reader has to judge.
int CmdDebug(IReplayController *ctrl, ICaptureFile *file, const char *path,
             const std::vector<std::string> &args)
{
  bool bGroup = false;
  bool bFailOn = false;
  MessageSeverity failOn = MessageSeverity::Info;
  for(size_t i = 1; i < args.size(); i++)
  {
    const std::string &a = args[i];
    if(a == "--group")
      bGroup = true;
    else if(a == "--fail-on" && i + 1 < args.size())
    {
      i++;
      if(!SeverityFromName(args[i].c_str(), failOn))
        return Fail(2, "--fail-on takes high, medium, low or info, not '%s'", args[i].c_str());
      bFailOn = true;
    }
    else if(a.size() > 2 && a[0] == '-' && a[1] == '-')
      return Fail(2, "unknown option '%s' for debug", a.c_str());
    // Anything else is the capture path, which the dispatcher already took apart.
  }

  PrintCaptureHeader(file, path);
  rdcarray<DebugMessage> msgs = ctrl->GetDebugMessages();

  // The worst severity present decides the exit code, and it is read from the messages themselves
  // rather than from what is printed: a threshold must not depend on which form was asked for.
  unsigned worst = (unsigned)MessageSeverity::Info + 1;
  for(size_t i = 0; i < msgs.size(); i++)
    worst = std::min(worst, (unsigned)msgs[i].severity);

  ArrayOpen("messages");
  const std::vector<DebugGroup> groups =
      bGroup ? GroupDebugMessages(msgs) : std::vector<DebugGroup>();
  if(bGroup)
  {
    if(groups.empty())
      Row("(no messages)");
    for(size_t g = 0; g < groups.size(); g++)
    {
      const DebugGroup &group = groups[g];
      Row(Fmt("eid %u..%u  %ux  %s category(%u) source(%u) id(%u)  %s", group.firstEid,
              group.lastEid, group.count, SeverityText(group.severity).c_str(),
              (unsigned)group.category, (unsigned)group.source, group.messageID, group.text.c_str()));
    }
  }
  else
  {
    for(size_t i = 0; i < msgs.size(); i++)
      Row(Fmt("eid %-6u %-8s %s", (unsigned)msgs[i].eventId, SeverityText(msgs[i].severity).c_str(),
              msgs[i].description.c_str()));
  }
  ArrayClose(false);    // total follows
  g_Indent = g_bJson ? 1 : 0;
  // `total` counts what the rows are: messages by default, groups with `--group`. The number of
  // messages is still worth having in the grouped form, so it goes to the log rather than nowhere.
  Field("total", (long long)(bGroup ? groups.size() : msgs.size()), true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");

  if(bGroup)
    Log("debug: %d message(s) in %d group(s)", (int)msgs.size(), (int)groups.size());

  if(bFailOn && worst <= (unsigned)failOn)
  {
    // To stderr, so stdout stays a document under `--json`, and to the log, because the exit code
    // is the part a script reads and the log is where a reader finds out why.
    const int count = (int)std::count_if(msgs.begin(), msgs.end(), [failOn](const DebugMessage &m) {
      return (unsigned)m.severity <= (unsigned)failOn;
    });
    Log("failed: %d of %d message(s) at or above %s (--fail-on)", count, (int)msgs.size(),
        SeverityText(failOn).c_str());
    return 1;
  }
  return 0;
}

//: `find <substring> [max]`: which events' calls or markers, and which resources, mention something.
//:
//: Everything here is something the engine already publishes -- the action list gives every call its
//: name and the marker path it sits inside, the resource table gives every resource its name -- so
//: nothing is inferred from a state or a binding. It is the cheap half of the marker plumbing:
//: `--at-marker` and a path written where an event id is expected both resolve through
//: `ResolveMarkerPath`, and this is how a reader finds out which paths there are to resolve.
//:
//: Matching is case-insensitive, and each row says *where* it matched, because `View` hitting a marker
//: and `View` hitting a resource are different answers. Resources are listed rather than chased: which
//: events touch one is `usage <resId>`'s answer, from the engine's own usage chain, and deriving it
//: here would be a second implementation of the same thing.
int CmdFind(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *needle,
            int maxRows)
{
  const std::string want(needle == NULL ? "" : needle);
  if(want.empty())
    return Fail(2, "find needs a substring to look for");
  const std::string wantLower = LowerAscii(want);

  int calls = 0;
  bool bTruncated = false;
  const std::vector<ActionNode> rows = ActionTree(ctrl, calls, bTruncated);
  const std::map<int, std::string> &paths = MarkerPaths(ctrl);

  PrintCaptureHeader(file, path);
  Field("needle", want);
  Field("calls", (long long)calls);

  int found = 0;
  ArrayOpen("events");
  for(size_t i = 0; i < rows.size() && found < maxRows; i++)
  {
    const std::string name = std::string(rows[i].m_Name.c_str());
    const std::map<int, std::string>::const_iterator pathIt = paths.find(rows[i].m_Eid);
    const std::string markerPath = pathIt == paths.end() ? std::string() : pathIt->second;

    const bool bNameHit = LowerAscii(name).find(wantLower) != std::string::npos;
    const bool bPathHit =
        !markerPath.empty() && LowerAscii(markerPath).find(wantLower) != std::string::npos;
    if(!bNameHit && !bPathHit)
      continue;

    found++;
    const char *kind = rows[i].m_bMarker ? "marker" : (rows[i].m_bCall ? "call" : "event");
    if(g_bJson)
      ObjectRow(
          Fmt("{\"eid\": %d, \"kind\": \"%s\", \"name\": \"%s\", \"where\": \"%s\", "
              "\"marker\": \"%s\"}",
              rows[i].m_Eid, kind, JsonEscape(name).c_str(), bNameHit ? "name" : "marker",
              JsonEscape(markerPath).c_str()));
    else
      printf("#%-6d %-7s %-34s %s%s\n", rows[i].m_Eid, kind, name.c_str(),
             bPathHit && !bNameHit ? "in " : "", bPathHit && !bNameHit ? markerPath.c_str() : "");
  }
  ArrayClose(false);

  int resFound = 0;
  const rdcarray<ResourceDescription> &res = ctrl->GetResources();
  ArrayOpen("resources");
  for(size_t i = 0; i < res.size() && resFound < maxRows; i++)
  {
    const std::string name = res[i].name.empty() ? std::string() : std::string(res[i].name.c_str());
    if(name.empty() || LowerAscii(name).find(wantLower) == std::string::npos)
      continue;
    resFound++;
    const std::string idText = Fmt("res%s", IdText(res[i].resourceId).c_str());
    if(g_bJson)
      ObjectRow(Fmt("{\"resource\": \"%s\", \"name\": \"%s\"}", idText.c_str(),
                    JsonEscape(name).c_str()));
    else
      printf("%-9s %s\n", idText.c_str(), name.c_str());
  }
  ArrayClose(false);

  Field("matched", (long long)(found + resFound), true);
  if(g_bJson)
    printf("}\n");
  if(found == 0 && resFound == 0)
    Log("find: nothing in this capture's action list or resource table contains '%s'", want.c_str());
  if(found >= maxRows || resFound >= maxRows)
    Log("find: %d row(s) printed per list -- narrow the substring, or pass a larger max", maxRows);
  return 0;
}

//: Which events touch a resource: the way to answer "where does this buffer come from". The
//: argument is an id (as `textures` prints it) or a resource name -- `ResourceId` cannot be
//: constructed from a number outside the DLL, so the match is made against what the engine itself
//: reports.
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
  g_Indent = g_bJson ? 1 : 0;
  Field("total", (long long)usage.size(), true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return 0;
}

//: The three fields of one probe row, from the engine's state at that id: how many stages have a
//: shader, the root signature, and how many parameters it declares. Extracted so the same reading
//: produces a row, a cache line and a JSON row.
bool ProbeRowAt(IReplayController *ctrl, int eid, ProbeRow &row)
{
  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  if(d3d12 == NULL)
    return false;

  int shaders = 0;
  for(int i = 0; i < (int)ShaderStage::Count; i++)
  {
    const D3D12Pipe::Shader *sh = StageShader(d3d12, (ShaderStage)i);
    if(sh != NULL && sh->resourceId != ResourceId::Null())
      shaders++;
  }
  if(shaders == 0 && d3d12->rootSignature.resourceId == ResourceId::Null())
    return false;

  row.m_Eid = eid;
  row.m_Shaders = shaders;
  row.m_RootSig = IdText(d3d12->rootSignature.resourceId);
  row.m_Params = (int)d3d12->rootSignature.parameters.size();
  return true;
}

void ProbePrintRow(const ProbeRow &row)
{
  Row(Fmt("eid %-7d shaders=%d rootSig=%s params=%d", row.m_Eid, row.m_Shaders,
          row.m_RootSig.c_str(), row.m_Params));
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
//: `AnyEventReplayed()` is that rule made checkable: a run that is not first says so on stderr, and
//: only a first run's answer is written to the cache (a cached answer is someone's *first* run).
//:
//: The scan is bounded by the frame's own last event as well as by the caller's cap (`ProbeUntil`),
//: because a cap alone is a guess: on the 1.4 GB capture, whose frame runs to five figures, the old
//: default of 2000 ids swept for a minute and then answered "nothing has state", which is a property
//: of the range and not of the frame. `probe <rdc> last` asks for the whole frame, and the answer is
//: cached (bundle.cpp's probe cache) -- a repeat is then seconds, and a scan killed part-way keeps the
//: prefix it had established.
int CmdProbe(IReplayController *ctrl, ICaptureFile *file, const char *path, int maxEid)
{
  PrintCaptureHeader(file, path);
  const int lastEvent = LastEventId(ctrl);
  const int until = ProbeUntil(maxEid, lastEvent);
  const bool bCold = !AnyEventReplayed();
  if(!bCold)
    Log("warning: `probe` is not the first command in this session; the state it reads is the "
        "leftover "
        "of the last event another command replayed, which is why the ids below can name events "
        "that "
        "have nothing bound (run `probe` first)");

  const std::filesystem::path cachePath = ProbeCachePath(path);
  ProbeCache cache;
  const bool bHit = ReadProbeCache(cachePath, path, lastEvent, cache);
  if(bHit && cache.m_Scanned < until)
  {
    // A longer range than the cache holds means sweeping from id 1 again: arriving at the cache's
    // prefix end by a cold jump instead of through it loses the state just after it (bundle.cpp).
    Log("probe: the cache holds ids 1..%d and this run asks for 1..%d, so the range is swept again",
        cache.m_Scanned, until);
    cache.m_Rows.clear();
    cache.m_Scanned = 0;
  }

  if(cache.m_Scanned < until)
  {
    Progress sweep;
    sweep.Begin("probe: sweep", until);
    for(int eid = cache.m_Scanned + 1; eid <= until; eid++)
    {
      MoveToEvent(ctrl, eid);
      ProbeRow row;
      if(ProbeRowAt(ctrl, eid, row))
        cache.m_Rows.push_back(row);
      sweep.Tick(eid);
      // Only a cold engine's answer is worth keeping: warm, the state left over from another
      // command's replay is what these rows would record.
      if(bCold && eid % kProbeFlushEvery == 0)
      {
        cache.m_Scanned = eid;
        WriteProbeCache(cachePath, path, lastEvent, cache);
      }
    }
    cache.m_Scanned = until;
    if(bCold)
      WriteProbeCache(cachePath, path, lastEvent, cache);
    sweep.Done(until);
  }

  // The rows the cache holds may reach past what this run asked for (`probe 200` against a cached
  // whole frame): the file keeps them -- that is what makes it reusable -- and the *answer* stops
  // at the range this run was asked about.
  ArrayOpen("events");
  int shown = 0;
  for(size_t i = 0; i < cache.m_Rows.size() && cache.m_Rows[i].m_Eid <= until; i++)
  {
    ProbePrintRow(cache.m_Rows[i]);
    shown++;
  }
  ArrayClose(false);    // scanned/withState follow
  g_Indent = g_bJson ? 1 : 0;
  Field("scanned", (long long)until);
  Field("withState", (long long)shown);
  Field("lastEvent", (long long)lastEvent);
  // `Flag`, not `Field`: the schema says boolean and a `1` there is a document that does not
  // validate against its own schema -- which is exactly what the offline validator exists to catch.
  Flag("cached", bHit, true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return 0;
}

// --------------------------------------------------------------------------- the bundle (REFERENCE §9)

//: Runs a block with stdout pointing at a file, so a command written to print to the terminal
//: writes a file instead. A file-descriptor swap rather than a `FILE *` threaded through the
//: writers, because the helpers (`Field`, `Row`, `ArrayOpen`, ...) and the commands that use them
//: print in a dozen places each: one missed call site would silently corrupt a document, and the
//: descriptor is the only version of this that cannot miss one.
//:
//: `stdout` is unbuffered (setvbuf in `main`), so nothing has to be flushed before the swap, and
//: the guard restores the descriptor on every path out of the scope, early returns included.
bool SaveTargetImage(IReplayController *ctrl, ResourceId target, const char *outBase,
                     const PictureOptions &opts, std::string &written, int32_t &width,
                     int32_t &height)
{
  // The pixels come from `ReadTargetImage`, which is the same display-and-readback the contact sheet
  // uses: one implementation, so a pass image and a tile cannot be two interpretations of a target.
  ImageData img;
  std::string why;
  bool bOk = ReadTargetImage(ctrl, target, opts, img, why);
  width = img.m_Width;
  height = img.m_Height;
  written = std::string(outBase);
  if(bOk)
    bOk = WriteBMPImage(outBase, img);

  if(!bOk)
  {
    // The engine's own encoder, for the cases the display readback cannot serve: a target it will
    // not show (measured: one texture in the Android capture came back empty through the display
    // path). The options go with it, or a `--cast`/`--mip` picture would silently come back as the
    // texture's own format at mip 0 on the fallback path only.
    TextureSave save;
    save.resourceId = target;
    save.destType = FileType::PNG;
    ApplySaveOptions(save, opts);
    const std::string png = std::string(outBase) + ".png";
    const ResultDetails res = ctrl->SaveTexture(save, rdcstr(png.c_str()));
    if(res.OK())
    {
      written = png;
      bOk = true;
    }
    else if(!why.empty())
    {
      fprintf(stderr, "warning: %s\n", why.c_str());
    }
  }
  return bOk;
}

//: The per-event documents: the state, the reflection, and one file per constant block of every bound
//: stage. Each is written through the command that already produces it (the descriptor swap), so a file
//: is exactly what that command prints -- there is no second writer to drift away from the first.
