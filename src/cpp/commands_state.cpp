// z.commands_state — the per-event and per-resource commands (state, statediff, buffer, shaders,
// cb, pixelhistory)
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

int CmdState(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid)
{
  ctrl->SetFrameEvent(eid, true);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  // Where in the frame this event sits, in the engine's own marker names: the same path `draws`
  // prints and a bundle stores, so a state document says which pass it belongs to without a second
  // command.
  Field("marker", MarkerPathAt(ctrl, eid));
  Field("api", (long long)ctrl->GetAPIProperties().pipelineType);

  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();

  ArrayOpen("shaders");
  for(const ShaderStage stage : ReportedStages())
  {
    const D3D12Pipe::Shader *sh = StageShader(d3d12, stage);
    if(sh == NULL || sh->resourceId == ResourceId::Null())
      continue;
    Row(Fmt("%-3s res%-7s", StageName(stage), IdText(sh->resourceId).c_str()));
  }
  ArrayClose(false);    // renderTargets follows

  ArrayOpen("renderTargets");
  if(d3d12)
  {
    for(int i = 0; i < (int)d3d12->outputMerger.renderTargets.size(); i++)
    {
      if(d3d12->outputMerger.renderTargets[i].resource == ResourceId::Null())
        continue;
      Row(Fmt("slot %d  res%s", i, IdText(d3d12->outputMerger.renderTargets[i].resource).c_str()));
    }
  }
  ArrayClose(d3d12 == NULL);    // depthTarget/rootSignature follow if there is state
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
      Row(Fmt("rp%-3d reg=%u space=%u vis=%s %s", i, rp.reg, rp.space,
              VisibilityText(rp.visibility).c_str(), detail.c_str()));

      // A *set* table's contents, slot by slot: what the heap holds where each declared range
      // points, so the offline side can compare a slot against the register the reflection says the
      // shader reads. Nothing in the pipe state carries a descriptor index and the command payloads
      // live in a different numbering than the engine's event ids -- but the engine can resolve its
      // own table, which is both simpler and exact: `GetDescriptors` looks slots up by the same
      // descriptor offsets the ranges are expressed in (measured: `heapByteOffset` is the root
      // element's offset and `tableByteOffset` is D3D12's `OffsetInDescriptorsFromTableStart`, both
      // in descriptors, not bytes).
      //
      // Only when the parameter was set: a table that was never set prints its parameter row with
      // no detail, and inventing `none` for every register it declares would bury the stronger fact
      // -- that the parameter itself is unset -- under a hundred null slots.
      if(rp.heap != ResourceId::Null())
      {
        for(const D3D12Pipe::RootTableRange &range : rp.tableRanges)
        {
          rdcarray<DescriptorRange> request;
          DescriptorRange ask;
          ask.offset = rp.heapByteOffset + range.tableByteOffset;
          ask.descriptorSize = 32;    // D3D12 descriptors are 32 bytes (the heap path ignores this)
          ask.count = range.count;
          request.push_back(ask);

          const rdcarray<Descriptor> contents = ctrl->GetDescriptors(rp.heap, request);
          for(uint32_t k = 0; k < range.count; k++)
          {
            const bool bPresent = k < contents.size() && contents[k].resource != ResourceId::Null();
            // Two kinds per row, and they are different facts: `cat(N)` is the range's *category*
            // from the root signature (what the binding declares), `type(N)` is the heap slot's own
            // `DescriptorType` (what was actually written there). The engine has
            // `CategoryForDescriptorType` to relate them, so a disagreement -- a CBV range whose
            // slot holds an SRV descriptor -- is decidable offline rather than a guess.
            const DescriptorType kind =
                k < contents.size() ? contents[k].type : DescriptorType::Unknown;
            Row(Fmt("rp%-3d %c%-2u s%-3u cat(%u) type(%u) %s", i, RegisterLetter(range.category),
                    range.baseRegister + k, range.space, (unsigned)range.category, (unsigned)kind,
                    bPresent ? Fmt("res%s", IdText(contents[k].resource).c_str()).c_str() : "none"));
          }
        }
      }
    }
    ArrayClose(false);    // the pipeline-state blocks follow

    // -----------------------------------------------------------------------
    // The state a capture's *draws* are judged by, which earlier bundles did not carry: viewport
    // and scissor for "draws that can only produce nothing", depth and stencil for "depth logic"
    // and "stencil without a writer", blend for "blend in an opaque pass".
    //
    // Written as numbers, booleans and names rather than as rows, which is the opposite of the
    // binding rows above -- and for that reason: nothing here is matched against a reflection row,
    // so the offline rules want to compare *values*. The names come from the local switches above
    // (`LessEqual`, `Replace`, `InvSrcAlpha`), because a finding has to read like the state a
    // person set.
    ArrayOpen("viewports");
    for(const Viewport &vp : d3d12->rasterizer.viewports)
      ObjectRow(Fmt(
          "{\"x\": %.2f, \"y\": %.2f, \"width\": %.2f, \"height\": %.2f, \"minDepth\": %.3f, "
          "\"maxDepth\": %.3f, \"enabled\": %s}",
          vp.x, vp.y, vp.width, vp.height, vp.minDepth, vp.maxDepth, vp.enabled ? "true" : "false"));
    ArrayClose(false);

    ArrayOpen("scissors");
    for(const Scissor &sc : d3d12->rasterizer.scissors)
      ObjectRow(Fmt("{\"x\": %d, \"y\": %d, \"width\": %d, \"height\": %d, \"enabled\": %s}",
                    (int)sc.x, (int)sc.y, (int)sc.width, (int)sc.height,
                    sc.enabled ? "true" : "false"));
    ArrayClose(false);

    ObjectOpenKey("outputMerger");
    {
      const D3D12Pipe::DepthStencilState &ds = d3d12->outputMerger.depthStencilState;
      const D3D12Pipe::BlendState &bs = d3d12->outputMerger.blendState;
      Flag("depthEnable", ds.depthEnable);
      Flag("depthWrites", ds.depthWrites);
      Field("depthFunction", std::string(CompareFunctionText(ds.depthFunction)));
      Flag("stencilEnable", ds.stencilEnable);
      Flag("stencilReadOnly", d3d12->outputMerger.stencilReadOnly);
      Flag("alphaToCoverage", bs.alphaToCoverage);
      Flag("independentBlend", bs.independentBlend);

      // A face's three operations plus its compare and write mask: `Keep` on all three is a face
      // that only *tests* stencil, which is exactly what "stencil test enabled where nothing wrote
      // stencil" turns on.
      const auto face = [](const char *key, const StencilFace &f, bool bLastInParent) {
        ObjectOpenKey(key);
        Field("fail", std::string(StencilOperationText(f.failOperation)));
        Field("depthFail", std::string(StencilOperationText(f.depthFailOperation)));
        Field("pass", std::string(StencilOperationText(f.passOperation)));
        Field("function", std::string(CompareFunctionText(f.function)));
        Field("compareMask", (long long)f.compareMask);
        Field("writeMask", (long long)f.writeMask, true);
        ObjectClose(bLastInParent);
      };
      face("frontFace", ds.frontFace, false);
      face("backFace", ds.backFace, false);

      ArrayOpen("blends");
      for(const ColorBlend &blend : bs.blends)
        ObjectRow(Fmt(
            "{\"enabled\": %s, \"writeMask\": %u, \"colorOperation\": \"%s\", \"alphaOperation\": "
            "\"%s\", \"srcColor\": \"%s\", \"dstColor\": \"%s\", \"srcAlpha\": \"%s\", "
            "\"dstAlpha\": \"%s\"}",
            blend.enabled ? "true" : "false", (unsigned)blend.writeMask,
            BlendOperationText(blend.colorBlend.operation),
            BlendOperationText(blend.alphaBlend.operation),
            BlendMultiplierText(blend.colorBlend.source),
            BlendMultiplierText(blend.colorBlend.destination),
            BlendMultiplierText(blend.alphaBlend.source),
            BlendMultiplierText(blend.alphaBlend.destination)));
      ArrayClose();
      ObjectClose();
    }
  }

  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return 0;
}

//: Every field `statediff` compares, keyed the way `state` names them: `shaders.vs`,
//: `renderTargets.0`, `rootParameters.3`, `viewports.0`, `outputMerger.depthFunction`. Same
//: vocabulary as the state document on purpose -- a row in a diff can be found in the state it came
//: from without a second table to keep in sync.
static void StateDiffRows(const D3D12Pipe::State *st, std::map<std::string, std::string> &rows)
{
  rows.clear();
  if(st == NULL)
    return;

  for(const ShaderStage stage : ReportedStages())
  {
    const D3D12Pipe::Shader *sh = StageShader(st, stage);
    if(sh == NULL || sh->resourceId == ResourceId::Null())
      continue;
    rows[Fmt("shaders.%s", StageName(stage))] = IdText(sh->resourceId);
  }
  for(int i = 0; i < (int)st->outputMerger.renderTargets.size(); i++)
  {
    const ResourceId rt = st->outputMerger.renderTargets[i].resource;
    if(rt == ResourceId::Null())
      continue;
    rows[Fmt("renderTargets.%d", i)] = IdText(rt);
  }
  rows["depthTarget"] = IdText(st->outputMerger.depthTarget.resource);
  rows["rootSignature"] = IdText(st->rootSignature.resourceId);
  rows["rootSignature.parameters"] = Fmt("%d", (int)st->rootSignature.parameters.size());
  for(int i = 0; i < (int)st->rootSignature.parameters.size(); i++)
  {
    const D3D12Pipe::RootParam &rp = st->rootSignature.parameters[i];
    std::string detail = "none";
    if(rp.descriptor.resource != ResourceId::Null())
      detail = Fmt("res%s", IdText(rp.descriptor.resource).c_str());
    else if(rp.heap != ResourceId::Null())
      detail = Fmt("heap%s+0x%x", IdText(rp.heap).c_str(), rp.heapByteOffset);
    else if(!rp.constants.empty())
      detail = Fmt("%d words", (int)rp.constants.size() / 4);
    rows[Fmt("rootParameters.%d", i)] = Fmt("reg=%u space=%u vis=%s %s", rp.reg, rp.space,
                                            VisibilityText(rp.visibility).c_str(), detail.c_str());
  }
  for(size_t i = 0; i < st->rasterizer.viewports.size(); i++)
  {
    const Viewport &vp = st->rasterizer.viewports[i];
    rows[Fmt("viewports.%d", (int)i)] =
        Fmt("%.2fx%.2f at %.2f,%.2f z %.3f..%.3f %s", vp.width, vp.height, vp.x, vp.y, vp.minDepth,
            vp.maxDepth, vp.enabled ? "enabled" : "disabled");
  }
  for(size_t i = 0; i < st->rasterizer.scissors.size(); i++)
  {
    const Scissor &sc = st->rasterizer.scissors[i];
    rows[Fmt("scissors.%d", (int)i)] = Fmt("%dx%d at %d,%d %s", (int)sc.width, (int)sc.height,
                                           (int)sc.x, (int)sc.y, sc.enabled ? "enabled" : "disabled");
  }
  const D3D12Pipe::DepthStencilState &ds = st->outputMerger.depthStencilState;
  const D3D12Pipe::BlendState &bs = st->outputMerger.blendState;
  rows["outputMerger.depthEnable"] = ds.depthEnable ? "true" : "false";
  rows["outputMerger.depthWrites"] = ds.depthWrites ? "true" : "false";
  rows["outputMerger.depthFunction"] = CompareFunctionText(ds.depthFunction);
  rows["outputMerger.stencilEnable"] = ds.stencilEnable ? "true" : "false";
  rows["outputMerger.stencilReadOnly"] = st->outputMerger.stencilReadOnly ? "true" : "false";
  rows["outputMerger.frontFace"] = Fmt(
      "%s/%s/%s %s", StencilOperationText(ds.frontFace.failOperation),
      StencilOperationText(ds.frontFace.depthFailOperation),
      StencilOperationText(ds.frontFace.passOperation), CompareFunctionText(ds.frontFace.function));
  rows["outputMerger.backFace"] = Fmt("%s/%s/%s %s", StencilOperationText(ds.backFace.failOperation),
                                      StencilOperationText(ds.backFace.depthFailOperation),
                                      StencilOperationText(ds.backFace.passOperation),
                                      CompareFunctionText(ds.backFace.function));
  rows["outputMerger.alphaToCoverage"] = bs.alphaToCoverage ? "true" : "false";
  rows["outputMerger.independentBlend"] = bs.independentBlend ? "true" : "false";
  for(size_t i = 0; i < bs.blends.size(); i++)
  {
    const ColorBlend &blend = bs.blends[i];
    rows[Fmt("outputMerger.blends.%d", (int)i)] = Fmt(
        "%s mask=%u %s(%s+%s) alpha %s(%s+%s)", blend.enabled ? "enabled" : "disabled",
        (unsigned)blend.writeMask, BlendOperationText(blend.colorBlend.operation),
        BlendMultiplierText(blend.colorBlend.source),
        BlendMultiplierText(blend.colorBlend.destination),
        BlendOperationText(blend.alphaBlend.operation), BlendMultiplierText(blend.alphaBlend.source),
        BlendMultiplierText(blend.alphaBlend.destination));
  }
}

//: The state of `eid`, read so that it is the *complete* one.
//:
//: A forward `SetFrameEvent` hands back what the engine has accumulated so far; a backward one
//: makes it replay the frame from its start (REFERENCE 9). Two events read forward would therefore
//: differ in ways that are only about how far the replay had got -- `shaders: "cs=11388 "` against
//: the complete
//: `"ps=11402 cs=11388 ms=11368 "` -- so both sides are read the same way: step onto the id from
//: *past* it. That is one extra refresh (~50 ms), and an id past the last event clamps to it, so
//: the last event is read the same way as any other.
static const D3D12Pipe::State *StateAtComplete(IReplayController *ctrl, int eid)
{
  ctrl->SetFrameEvent((uint32_t)(eid + 1), true);
  ctrl->SetFrameEvent((uint32_t)eid, true);
  return ctrl->GetD3D12PipelineState();
}

int CmdStateDiff(IReplayController *ctrl, ICaptureFile *file, const char *path, int eidA, int eidB)
{
  // The marker paths come from the action list, which is cached and needs no engine call.
  const std::string markerA = MarkerPathAt(ctrl, eidA);
  const std::string markerB = MarkerPathAt(ctrl, eidB);

  std::map<std::string, std::string> a, b;
  StateDiffRows(StateAtComplete(ctrl, eidA), a);
  StateDiffRows(StateAtComplete(ctrl, eidB), b);

  // A field only one side has counts as changed, and says which side it came from: a render target
  // that stopped being bound is not "the same field, empty".
  struct Change
  {
    std::string m_Field, m_A, m_B;
  };
  std::vector<Change> changed;
  std::map<std::string, std::string>::const_iterator ia = a.begin(), ib = b.begin();
  while(ia != a.end() || ib != b.end())
  {
    const bool bAFirst = ib == b.end() || (ia != a.end() && ia->first < ib->first);
    const std::string key = bAFirst ? ia->first : ib->first;
    const std::map<std::string, std::string>::const_iterator fa = a.find(key), fb = b.find(key);
    const std::string va = fa == a.end() ? std::string("(absent)") : fa->second;
    const std::string vb = fb == b.end() ? std::string("(absent)") : fb->second;
    if(va != vb)
      changed.push_back(Change{key, va, vb});
    if(fa != a.end())
      ++ia;
    if(fb != b.end())
      ++ib;
  }

  PrintCaptureHeader(file, path);
  Field("eidA", (long long)eidA);
  Field("markerA", markerA);
  Field("eidB", (long long)eidB);
  Field("markerB", markerB);
  Field("fieldsA", (long long)a.size());
  Field("fieldsB", (long long)b.size());
  Field("changed", (long long)changed.size());
  if(g_bJson)
  {
    ArrayOpen("fields");
    for(size_t i = 0; i < changed.size(); i++)
      ObjectRow(Fmt("{\"field\": \"%s\", \"a\": \"%s\", \"b\": \"%s\"}",
                    JsonEscape(changed[i].m_Field).c_str(), JsonEscape(changed[i].m_A).c_str(),
                    JsonEscape(changed[i].m_B).c_str()));
    ArrayClose(true);
    printf("}\n");
    return 0;
  }
  printf("--- changed ---\n");
  if(changed.empty())
    printf("(no field differs)\n");
  for(size_t i = 0; i < changed.size(); i++)
    printf("%-28s %-26s -> %s\n", changed[i].m_Field.c_str(), changed[i].m_A.c_str(),
           changed[i].m_B.c_str());
  return 0;
}

//: The resource an argument names: `res123`, a bare `123`, or a name -- exact, then a case-insensitive
//: substring, the same way `usage` matches. `why` receives what went wrong when nothing matched.
static ResourceId ResolveResourceArg(IReplayController *ctrl, const char *what, std::string &name,
                                     std::string &why)
{
  std::string want(what == NULL ? "" : what);
  // The tool writes ids as `res1234` and `IdText` returns the bare number, so both spellings are
  // accepted here rather than making the caller remember which command wants which.
  if(want.size() > 3 && want.compare(0, 3, "res") == 0)
    want = want.substr(3);
  const rdcarray<ResourceDescription> &res = ctrl->GetResources();
  for(size_t i = 0; i < res.size(); i++)
  {
    if(IdText(res[i].resourceId) == want ||
       (!res[i].name.empty() && std::string(res[i].name.c_str()) == want))
    {
      name = res[i].name.empty() ? want : std::string(res[i].name.c_str());
      return res[i].resourceId;
    }
  }
  for(size_t i = 0; i < res.size(); i++)
  {
    if(!res[i].name.empty() && std::string(res[i].name.c_str()).find(want) != std::string::npos)
    {
      name = std::string(res[i].name.c_str());
      return res[i].resourceId;
    }
  }
  why = Fmt("no resource with id or name '%s'", want.c_str());
  return ResourceId::Null();
}

//: `buffer <resId|name> [offset] [len] [--as u32|f32|hex|ascii]`: what is actually in a buffer,
//: read through the engine. The offline tool prints a buffer's size and name from the file; only
//: replay can show its contents, and this is the command that does -- a float row for a uniform
//: block, a hexdump for a structure, an ASCII row for a string table.
//:
//: `offset` and `len` accept decimal or `0x` hex, and `len` defaults to 256 bytes (a screenful)
//: rather than the whole buffer: reads go through the engine, and a 64 MB buffer is not a hexdump
//: anyone reads. `--as` defaults to `hex`.
int CmdBuffer(IReplayController *ctrl, ICaptureFile *file, const char *path, const char *what,
              unsigned long long offset, unsigned long long length, const char *asMode)
{
  const std::string mode(asMode == NULL ? "hex" : asMode);
  if(mode != "hex" && mode != "u32" && mode != "f32" && mode != "ascii")
    return Fail(2, "--as '%s' is not one of u32, f32, hex, ascii", mode.c_str());

  std::string name, why;
  const ResourceId id = ResolveResourceArg(ctrl, what, name, why);
  if(id == ResourceId::Null())
    return Fail(1, "%s", why.c_str());

  const rdcarray<BufferDescription> &buffers = ctrl->GetBuffers();
  const BufferDescription *desc = NULL;
  for(size_t i = 0; i < buffers.size(); i++)
  {
    if(buffers[i].resourceId == id)
    {
      desc = &buffers[i];
      break;
    }
  }
  if(desc == NULL)
    return Fail(1, "%s is not a buffer (a texture, a heap or an acceleration structure)",
                name.c_str());

  const unsigned long long total = (unsigned long long)desc->length;
  if(offset > total)
    return Fail(1, "offset 0x%llx is past the end of %s (0x%llx bytes)", offset, name.c_str(), total);
  unsigned long long want = length == 0 ? 256 : length;
  if(offset + want > total)
    want = total - offset;

  const bytebuf data = ctrl->GetBufferData(id, offset, want);

  PrintCaptureHeader(file, path);
  Field("resource", Fmt("res%s", IdText(id).c_str()));
  Field("name", name);
  Field("length", (long long)total);
  Field("offset", (long long)offset);
  Field("read", (long long)data.size());
  Field("as", mode);

  if(g_bJson)
  {
    ArrayOpen("values");
    if(mode == "hex" || mode == "ascii")
    {
      std::string hex;
      for(size_t i = 0; i < data.size(); i++)
        hex += Fmt("%02x", (unsigned)data[i]);
      Row(hex);
    }
    else if(mode == "u32")
    {
      for(size_t i = 0; i + 4 <= data.size(); i += 4)
      {
        uint32_t value = 0;
        memcpy(&value, &data[i], 4);
        Row(Fmt("%u", (unsigned)value));
      }
    }
    else
    {
      for(size_t i = 0; i + 4 <= data.size(); i += 4)
      {
        float value = 0.0f;
        memcpy(&value, &data[i], 4);
        Row(Fmt("%.6g", (double)value));
      }
    }
    ArrayClose(true);
    printf("}\n");
    return 0;
  }

  // Text: rows addressed by their offset in the buffer, so a value can be read back as a byte range.
  if(mode == "hex")
  {
    for(size_t i = 0; i < data.size(); i += 16)
    {
      std::string hex, ascii;
      for(size_t k = 0; k < 16; k++)
      {
        if(i + k < data.size())
        {
          const unsigned char c = (unsigned char)data[i + k];
          hex += Fmt("%02x ", (unsigned)c);
          ascii += (c >= 32 && c < 127) ? (char)c : '.';
        }
        else
        {
          hex += "   ";
        }
      }
      printf("  %08llx  %s |%s|\n", offset + (unsigned long long)i, hex.c_str(), ascii.c_str());
    }
  }
  else if(mode == "ascii")
  {
    for(size_t i = 0; i < data.size(); i += 32)
    {
      std::string text;
      for(size_t k = 0; k < 32 && i + k < data.size(); k++)
      {
        const unsigned char c = (unsigned char)data[i + k];
        text += (c >= 32 && c < 127) ? (char)c : '.';
      }
      printf("  %08llx  %s\n", offset + (unsigned long long)i, text.c_str());
    }
  }
  else
  {
    const int perRow = 4;
    for(size_t i = 0; i + 4 <= data.size(); i += 4 * perRow)
    {
      std::string line;
      for(int k = 0; k < perRow && i + 4 * (size_t)k + 4 <= data.size(); k++)
      {
        if(mode == "u32")
        {
          uint32_t value = 0;
          memcpy(&value, &data[i + 4 * (size_t)k], 4);
          line += Fmt("%-12u", (unsigned)value);
        }
        else
        {
          float value = 0.0f;
          memcpy(&value, &data[i + 4 * (size_t)k], 4);
          line += Fmt("%-12.6g", (double)value);
        }
      }
      printf("  %08llx  %s\n", offset + (unsigned long long)i, line.c_str());
    }
  }
  if(data.size() < want)
    printf(
        "  (the engine returned %d of the %llu byte(s) asked for: the buffer's contents are not "
        "fully initialised)\n",
        (int)data.size(), want);
  return 0;
}

//: One signature element as a row: the semantic with its index, the register it starts at, `c<N>`
//: -- the component count the engine reports for it (`SigParameter::compCount`) -- and the
//: component type
//: (`SigParameter::varType`), which `VarTypeText` names.
//:
//: Both are here for the offline side's sake. The *count* is what a link comparison needs: a pixel
//: shader that reads more components of a semantic than the stage before it writes is a mismatch
//: that the semantic name alone does not show. The *type* is what a format rule needs: a `float4`
//: written into an 8-bit UNORM target is a different thing from a `uint4` written into the same
//: target, and the target's format is already in the bundle.
int CmdShaders(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
               bool bWantDisasm)
{
  ctrl->SetFrameEvent(eid, true);
  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  if(d3d12 == NULL)
    return Fail(1, "no D3D12 pipeline state at eid %d", eid);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("marker", MarkerPathAt(ctrl, eid));

  // One object per bound stage, in a `stages` array. They cannot be members of one flat object:
  // two bound stages then repeat every key (`stage`, `resource`, `constantBlocks`, ...) and every
  // JSON reader keeps only the last value of a repeated key, so the vertex shader's whole
  // reflection was silently dropped on any draw that had one. The array also makes the empty case
  // (no stage bound -- a copy, a marker, or an event with no pipeline state) a valid document,
  // where a flat object used to end on `eid`'s separator with nothing after it.
  ArrayOpen("stages");
  for(const ShaderStage stage : ReportedStages())
  {
    const D3D12Pipe::Shader *stageState = StageShader(d3d12, stage);
    const ResourceId shader = stageState ? stageState->resourceId : ResourceId::Null();
    if(shader == ResourceId::Null())
      continue;

    // An empty entry-point name means "the default one"; `ShaderEntryPoint` carries the stage too.
    const ShaderReflection *refl =
        ctrl->GetShader(d3d12->pipelineResourceId, shader, ShaderEntryPoint(rdcstr(), stage));

    ObjectOpen();

    if(refl == NULL)
    {
      if(IsJson())
      {
        Field(Fmt("%s (no reflection)", StageName(stage)).c_str(), IdText(shader));
      }
      else
      {
        printf("  %-3s res%-7s (no reflection)\n", StageName(stage), IdText(shader).c_str());
      }
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
              cb.fixedBindNumber, cb.fixedBindSetOrSpace, (int)cb.byteSize, (int)cb.variables.size()));
    }
    ArrayClose(false);    // more members of this stage follow

    ArrayOpen("readOnlyResources");
    for(size_t r = 0; r < refl->readOnlyResources.size(); r++)
      Row(Fmt("%s t%d s%d n%d", refl->readOnlyResources[r].name.c_str(),
              refl->readOnlyResources[r].fixedBindNumber,
              refl->readOnlyResources[r].fixedBindSetOrSpace,
              (int)refl->readOnlyResources[r].bindArraySize));
    ArrayClose(false);    // more members of this stage follow

    ArrayOpen("readWriteResources");
    for(size_t r = 0; r < refl->readWriteResources.size(); r++)
      Row(Fmt("%s u%d s%d n%d", refl->readWriteResources[r].name.c_str(),
              refl->readWriteResources[r].fixedBindNumber,
              refl->readWriteResources[r].fixedBindSetOrSpace,
              (int)refl->readWriteResources[r].bindArraySize));
    ArrayClose(false);    // more members of this stage follow

    ArrayOpen("inputSignature");
    for(size_t s = 0; s < refl->inputSignature.size(); s++)
      Row(SignatureText(refl->inputSignature[s]));
    ArrayClose(false);    // outputSignature follows

    ArrayOpen("outputSignature");
    for(size_t s = 0; s < refl->outputSignature.size(); s++)
      Row(SignatureText(refl->outputSignature[s]));
    ArrayClose(!bWantDisasm);

    if(bWantDisasm)
    {
      // The disassembly is not part of the reflection: the controller generates it on request, per
      // target, and an empty target means "the native one".
      rdcstr asmText = ctrl->DisassembleShader(d3d12->pipelineResourceId, refl, rdcstr());
      ArrayOpen("disassembly");
      // one entry per line so JSON consumers can diff it
      const std::string text(asmText.c_str());
      size_t start = 0;
      while(start <= text.size())
      {
        const size_t nl = text.find('\n', start);
        Row(text.substr(start, nl == std::string::npos ? std::string::npos : nl - start));
        if(nl == std::string::npos)
          break;
        start = nl + 1;
      }
      ArrayClose();
    }

    ObjectClose();    // this stage's object
  }
  ArrayClose(true);    // `stages` is the object's last member

  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return 0;
}

//: The named values of one constant buffer at one event: the answer to "what is actually in the
//: buffer bound at rp7", which the offline tool cannot give at all.
int CmdCbuffer(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
               ShaderStage stage, int slot)
{
  ctrl->SetFrameEvent(eid, true);
  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  const D3D12Pipe::Shader *sh = StageShader(d3d12, stage);
  if(sh == NULL || sh->resourceId == ResourceId::Null())
    return Fail(1, "no %s shader is bound at eid %d", StageName(stage), eid);

  // The bound shader's own reflection first: the pipe state hands it over
  // (`D3D12Pipe::Shader::reflection`), so it is the reflection of the entry point that is *actually
  // bound*. Asking `GetShader` with an empty entry-point name instead returns the shader's default
  // entry, whose constant-block *layout* need not be the bound one's -- which lays a block's
  // members out against the wrong offsets and prints the right bytes under the wrong names.
  // `GetShader` stays as the fallback for a state the engine did not fill in.
  const ShaderReflection *refl = sh->reflection;
  if(refl == NULL)
    refl = ctrl->GetShader(d3d12->pipelineResourceId, sh->resourceId,
                           ShaderEntryPoint(rdcstr(), stage));
  rdcstr entry = refl ? refl->entryPoint : rdcstr();

  // Which buffer to read: the engine wants the resource explicitly, and a constant block is bound
  // either as a root descriptor or as a slot of a descriptor table -- a bindless engine (UE) binds
  // its cbuffers through tables, and asking only for the root descriptor reported "values will be
  // zero" for *every* one of them (measured: all 312 cbuffer documents of the PC capture came back
  // zeroed). The root signature cannot answer this: for a table it holds a heap and an offset, not
  // a resource.
  //
  // `GetDescriptorAccess` is the engine's own list of what this event's shaders read -- stage,
  // binding *index* (the reflection's own constant-block index), array element, store and offset --
  // built for the event by the replay device. The RenderDoc GUI resolves its cbuffers through
  // exactly this list
  // (`PipeState::GetConstantBlock`), and `DescriptorRange(access)` is the matching way to ask for
  // the descriptor itself, so this is the same resolution and not an imitation of it.
  ResourceId buffer = ResourceId::Null();
  uint64_t bufferOffset = 0;
  uint64_t bufferLength = 0;
  const rdcarray<DescriptorAccess> &access = ctrl->GetDescriptorAccess();
  for(size_t i = 0; i < access.size(); i++)
  {
    const DescriptorAccess &a = access[i];
    if(a.stage != stage || a.index != (uint32_t)slot || a.arrayElement != 0)
      continue;
    if(CategoryForDescriptorType(a.type) != DescriptorCategory::ConstantBlock)
      continue;
    if(a.descriptorStore == ResourceId::Null())
      continue;

    rdcarray<DescriptorRange> request;
    DescriptorRange ask = DescriptorRange(a);
    ask.count = 1;
    request.push_back(ask);
    const rdcarray<Descriptor> contents = ctrl->GetDescriptors(a.descriptorStore, request);
    if(!contents.empty() && contents[0].resource != ResourceId::Null())
    {
      buffer = contents[0].resource;
      bufferOffset = contents[0].byteOffset;
      bufferLength = contents[0].byteSize;
    }
    // Whether or not the slot held anything, this is the row the reflection means: a second row for
    // the same index would be a different array element, which is not what the slot asked for.
    break;
  }

  // A read that comes back zeroed has two very different meanings -- "the engine did not record a
  // binding for this block here" and "the binding is real and the bytes are zero" -- and only the
  // first is a fact about this tool. `$RDC_REPLAY_DEBUG=1` prints what the engine offered, so the
  // two can be told apart without a rebuild.
  if(getenv("RDC_REPLAY_DEBUG") != NULL)
  {
    Log("cb: %d descriptor access row(s) at eid %d, %d root parameter(s), %d block(s) in the "
        "reflection",
        (int)access.size(), eid, (int)d3d12->rootSignature.parameters.size(),
        refl != NULL ? (int)refl->constantBlocks.size() : -1);
    for(size_t i = 0; i < access.size(); i++)
      Log("cb:   %s type=%u cat=%u index=%u elem=%u store=%s off=%llu size=%llu",
          StageName(access[i].stage), (unsigned)access[i].type,
          (unsigned)CategoryForDescriptorType(access[i].type), access[i].index,
          access[i].arrayElement, IdText(access[i].descriptorStore).c_str(),
          (unsigned long long)access[i].byteOffset, (unsigned long long)access[i].byteSize);
  }

  // The root-descriptor half, for the case the access list does not cover (an API or a driver that
  // does not publish one): a CBV bound directly on a root parameter *is* in the pipe state as a
  // resource. The lookup key is the block's own bind point -- the register and space the reflection
  // declares -- not the block's index, which only happens to equal the register in the common case.
  if(buffer == ResourceId::Null() && refl != NULL && (size_t)slot < refl->constantBlocks.size())
  {
    const uint32_t reg = refl->constantBlocks[(size_t)slot].fixedBindNumber;
    const uint32_t space = refl->constantBlocks[(size_t)slot].fixedBindSetOrSpace;
    for(size_t i = 0; i < d3d12->rootSignature.parameters.size(); i++)
    {
      const D3D12Pipe::RootParam &rp = d3d12->rootSignature.parameters[i];
      if(rp.space == space && rp.reg == reg && rp.descriptor.resource != ResourceId::Null())
      {
        buffer = rp.descriptor.resource;
        bufferOffset = rp.descriptor.byteOffset;
        bufferLength = rp.descriptor.byteSize;
        break;
      }
    }
  }

  // `length` is not optional: the engine fetches the bytes only when it is non-zero
  // (`ReplayController:: GetCBufferVariableContents` reads `if(length > 0) GetBufferData(...)`),
  // and a zero length left every variable at its *default* of zero -- which is how every cbuffer
  // document this tool ever wrote came out zeroed, for a block that was bound and full. A CBV's
  // declared size is the right answer, and the reflection's block size is the fallback for a
  // binding whose descriptor does not carry one.
  if(bufferLength == 0 && refl != NULL && (size_t)slot < refl->constantBlocks.size())
    bufferLength = (uint64_t)refl->constantBlocks[(size_t)slot].byteSize;

  rdcarray<ShaderVariable> vars =
      ctrl->GetCBufferVariableContents(d3d12->pipelineResourceId, sh->resourceId, stage, entry,
                                       (uint32_t)slot, buffer, bufferOffset, bufferLength);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("marker", MarkerPathAt(ctrl, eid));
  Field("stage", std::string(StageName(stage)));
  Field("slot", (long long)slot);
  Field("shader", IdText(sh->resourceId));
  Field(
      "buffer",
      buffer == ResourceId::Null()
          ? std::string("(no root descriptor or table slot binds this block: values will be zero)")
          : Fmt("res%s+0x%llx", IdText(buffer).c_str(), (unsigned long long)bufferOffset));

  ArrayOpen("variables");
  PrintVariables(vars, 0);
  ArrayClose();
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");
  return 0;
}

// --------------------------------------------------------------------------- pixel history

//: The `--cast` vocabulary: the component type a name means, and its name. `typeless` is "no cast",
//: which is the engine reading the texture as its own format says. One spelling per type and an
//: unknown one is refused by the caller rather than defaulted -- a typo here asks the engine a
//: different question (a colour read as unsigned integers) and the answer would still look like one.
bool CastFromName(std::string_view name, CompType &type)
{
  static const struct
  {
    const char *name;
    CompType type;
  } kCasts[] = {
      {"typeless", CompType::Typeless}, {"float", CompType::Float},     {"unorm", CompType::UNorm},
      {"snorm", CompType::SNorm},       {"uint", CompType::UInt},       {"sint", CompType::SInt},
      {"uscaled", CompType::UScaled},   {"sscaled", CompType::SScaled}, {"depth", CompType::Depth},
      {"srgb", CompType::UNormSRGB},
  };
  for(const auto &cast : kCasts)
  {
    if(name == cast.name)
    {
      type = cast.type;
      return true;
    }
  }
  return false;
}

const char *CastText(CompType type)
{
  switch(type)
  {
    case CompType::Typeless: return "typeless";
    case CompType::Float: return "float";
    case CompType::UNorm: return "unorm";
    case CompType::SNorm: return "snorm";
    case CompType::UInt: return "uint";
    case CompType::SInt: return "sint";
    case CompType::UScaled: return "uscaled";
    case CompType::SScaled: return "sscaled";
    case CompType::Depth: return "depth";
    case CompType::UNormSRGB: return "srgb";
  }
  return "typeless";
}

//: One pixel value, read the way its component type says to read it. `PixelValue` is a union of the
//: same four 32-bit words seen three ways, so which member is meaningful is the *format's*
//: business: a float format read as `uintValue` prints `1065353216` where `1.0` belongs, and
//: nothing about the number says it is the wrong reading. `Typeless` prints the raw words, because
//: a typeless format is the capture saying it does not know either.
std::string PixelValueText(const PixelValue &value, CompType type)
{
  if(type == CompType::UInt || type == CompType::Typeless)
    return Fmt("%u,%u,%u,%u", value.uintValue[0], value.uintValue[1], value.uintValue[2],
               value.uintValue[3]);
  if(type == CompType::SInt)
    return Fmt("%d,%d,%d,%d", value.intValue[0], value.intValue[1], value.intValue[2],
               value.intValue[3]);
  return Fmt("%.6f,%.6f,%.6f,%.6f", value.floatValue[0], value.floatValue[1], value.floatValue[2],
             value.floatValue[3]);
}

//: The three parts of a modification value, each `-` when the engine marked the value invalid. One
//: decision, used by the terminal line *and* by the JSON writer, because a sentinel that leaks is a
//: fabricated colour in both: an invalid value's union holds `0xdeadbeef`/`0xdeadf00d`
//: (`ModificationValue::SetInvalid`) and printing that is a plausible-looking number that never
//: existed. The depth and stencil sentinels are the API's own (-1 "not in use or unknown", -2 "in
//: use, but the pixel history used stencil for itself"), so they are printed as they came back --
//: `-` there would hide which of the two it is.
std::string ModificationColorText(const ModificationValue &value, CompType type)
{
  return value.IsValid() ? PixelValueText(value.col, type) : std::string("-");
}

std::string ModificationDepthText(const ModificationValue &value)
{
  return value.IsValid() ? Fmt("%.6f", value.depth) : std::string("-");
}

std::string ModificationStencilText(const ModificationValue &value)
{
  return value.IsValid() ? Fmt("%d", value.stencil) : std::string("-");
}

//: A whole value as the one line the terminal shows, built from those three.
std::string ModificationValueText(const ModificationValue &value, CompType type)
{
  if(!value.IsValid())
    return std::string("-");
  return Fmt("col(%s) depth(%s) stencil(%s)", ModificationColorText(value, type).c_str(),
             ModificationDepthText(value).c_str(), ModificationStencilText(value).c_str());
}

//: Why a fragment did not write the pixel, in words, in the order `PixelModification::Passed` reads
//: the flags -- so the reasons and the verdict cannot disagree and an empty answer means every test
//: passed. The API's prose about pixel history names eight reasons; `Passed` counts ten, including
//: `depthClipped` and `predicationSkipped`, and a fragment that fails one of those did not write
//: the pixel whatever the prose calls it.
std::string RejectionText(const PixelModification &mod)
{
  const struct
  {
    bool bFailed;
    const char *name;
  } kTests[] = {
      {mod.sampleMasked, "sample masked"},
      {mod.backfaceCulled, "backface culled"},
      {mod.depthClipped, "depth clipped"},
      {mod.depthBoundsFailed, "depth bounds failed"},
      {mod.viewClipped, "view clipped"},
      {mod.scissorClipped, "scissor clipped"},
      {mod.shaderDiscarded, "shader discarded"},
      {mod.depthTestFailed, "depth test failed"},
      {mod.stencilTestFailed, "stencil test failed"},
      {mod.predicationSkipped, "predication skipped"},
  };
  std::string reasons;
  for(const auto &test : kTests)
  {
    if(!test.bFailed)
      continue;
    if(!reasons.empty())
      reasons += ", ";
    reasons += test.name;
  }
  return reasons;
}

//: One of the three values of a modification as a JSON object. Every member is a string, the
//: numbers included: this writer has no fractional field (see `Field` in common.h), and `-` for an
//: invalid value has to be expressible -- so a reader gets `valid` to check, and the same text the
//: terminal shows. Only called in JSON mode: it opens an object, which the text writer does not.
static void WriteModificationValue(const char *key, const ModificationValue &value, CompType type,
                                   bool bLast)
{
  ObjectOpenKey(key);
  Flag("valid", value.IsValid());
  Field("color", ModificationColorText(value, type));
  Field("depth", ModificationDepthText(value));
  Field("stencil", ModificationStencilText(value), true);
  ObjectClose(bLast);
}

//: `pixelhistory <rdc> <eid> <resId|name> <x> <y> [--mip N] [--slice N] [--sample N] [--cast <type>]
//: [--max N]`: why this pixel is this colour -- every event that tried to write it, the test that
//: rejected each attempt, and the value before, from and after it.
//:
//: The scope is the *event*: the engine's pixel history covers every write up to the one the replay
//: is positioned at (`ReplayController::PixelHistory` filters the usage list by it), so `last` is the
//: whole frame's answer and a pass's last eid is the answer at the end of that pass.
//:
//: Two things this command refuses to fake, both because an empty answer reads as a fact:
//: the driver must say it supports pixel history at all (`APIProperties.pixelHistory`), and the pixel
//: must be inside the texture -- the engine answers an out-of-range pixel with an *empty* list, which
//: is exactly what "nothing wrote it" looks like. An empty list that is a real answer is reported
//: with the counts that make it one, and an empty list the engine's own source explains (a texture
//: whose format it does not know: `D3D12Replay::PixelHistory` returns before doing anything) is an
//: error rather than an answer.
int CmdPixelHistory(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid,
                    const char *what, unsigned x, unsigned y, const Subresource &sub,
                    CompType typeCast, int maxRows)
{
  const APIProperties props = ctrl->GetAPIProperties();
  if(!props.pixelHistory)
    return Fail(1,
                "this capture's driver does not support pixel history (`info` prints the API "
                "properties): the engine can say what was bound at an event, but not why a pixel "
                "is the colour it is");

  std::string name, why;
  const ResourceId id = ResolveResourceArg(ctrl, what, name, why);
  if(id == ResourceId::Null())
    return Fail(1, "%s", why.c_str());

  // A texture, and the engine's own description of it: every number below is read out of this rather
  // than assumed, and a buffer is refused with what it is (`buffer` is the command that reads one).
  const rdcarray<TextureDescription> &textures = ctrl->GetTextures();
  const TextureDescription *tex = NULL;
  for(size_t i = 0; i < textures.size(); i++)
  {
    if(textures[i].resourceId == id)
    {
      tex = &textures[i];
      break;
    }
  }
  if(tex == NULL)
    return Fail(1,
                "res%s (%s) is not a texture: pixel history is about a pixel of a colour or depth "
                "target (`buffer %s` reads a buffer's bytes)",
                IdText(id).c_str(), name.c_str(), what);

  // The slice count of the *subresource* asked about, the way RenderDoc's own texture viewer works
  // it out: a 3D texture has `depth >> mip` slices at mip N, and everything else has one per array
  // element -- which for a cubemap is one per *face* (`arraysize` counts faces, and the viewer
  // labels them `[cube] face`).
  const uint32_t slices = tex->dimension == 3 ? (tex->depth >> sub.mip) : tex->arraysize;
  if(x >= tex->width || y >= tex->height)
    return Fail(1,
                "(%u,%u) is outside res%s (%s, %ux%u): the engine answers an out-of-range pixel "
                "with an empty history, so it is refused here instead",
                x, y, IdText(id).c_str(), tex->format.Name().c_str(), tex->width, tex->height);
  if(sub.mip >= tex->mips)
    return Fail(1, "res%s has %u mip level(s), so --mip %u is outside it", IdText(id).c_str(),
                tex->mips, sub.mip);
  if(sub.slice >= slices)
    return Fail(1, "res%s has %u slice(s) at mip %u, so --slice %u is outside it",
                IdText(id).c_str(), slices, sub.mip, sub.slice);
  if(sub.sample >= tex->msSamp)
    return Fail(1,
                "res%s has %u sample(s), so --sample %u is outside it (a non-multisampled texture "
                "has one sample, index 0)",
                IdText(id).c_str(), tex->msSamp, sub.sample);

  // What the numbers are read as: `--cast` when it was given, the texture's own format otherwise --
  // and a component type of `Typeless` means nothing in the capture says how to read them.
  const CompType readAs = typeCast == CompType::Typeless ? tex->format.compType : typeCast;
  const std::string formatName(tex->format.Name().c_str());
  const bool bFormatKnown = LowerAscii(formatName).find("unknown") == std::string::npos;

  // The replay is positioned at the scope event before the engine is asked: the history covers
  // every write *up to* the event the replay is on, so this is what makes the answer the frame's
  // (eid = the last event) rather than wherever the replay happened to be.
  ctrl->SetFrameEvent((uint32_t)eid, true);

  // One engine call, and not a cheap one: the driver re-runs the frame's draws with instrumented
  // shaders to catch this pixel's fragments, which is seconds rather than milliseconds.
  Log("pixelhistory: asking the engine for pixel (%u,%u) on res%s as of eid %d -- it re-runs the "
      "frame's draws with instrumented shaders",
      x, y, IdText(id).c_str(), eid);
  const ULONGLONG started = Millis();
  const rdcarray<PixelModification> history = ctrl->PixelHistory(id, x, y, sub, typeCast);
  ProfileAdd(kProfilePixelHistory, started);

  // How many events up to the scope touch this texture at all, from the engine's own usage list (the
  // one `usage` prints). It is the number that turns an empty answer into evidence: "no modification"
  // with 300 events that touch the target and "no modification" with none are different statements.
  int usagesUpTo = 0;
  const rdcarray<EventUsage> usage = ctrl->GetUsage(id);
  for(size_t i = 0; i < usage.size(); i++)
  {
    if((int)usage[i].eventId <= eid)
      usagesUpTo++;
  }

  if(history.empty() && !bFormatKnown)
    return Fail(1,
                "the engine cannot run pixel history on res%s: its format (%s) is one it does not "
                "know, and it declines rather than guessing",
                IdText(id).c_str(), formatName.c_str());

  int passed = 0;
  bool bAnyMasked = false;
  for(size_t i = 0; i < history.size(); i++)
  {
    if(history[i].Passed())
      passed++;
    bAnyMasked = bAnyMasked || history[i].sampleMasked;
  }

  // The one verdict here that is not a fact on its own. On D3D12 the sample-mask test is an
  // instrumented re-draw of the event -- RenderDoc's own source carries `TODO: figure out if we
  // always need to check this` over the flag that enables it -- and measured on the hobby capture's
  // 1-sample targets every base-pass fragment comes back flagged while one of them carries a
  // *changed* `postMod` value in the same row. So the rows print the values next to the verdict,
  // and this says which two to compare rather than letting `sample masked` read as a closed case.
  const std::string note =
      bAnyMasked
          ? std::string(
                "at least one fragment is flagged `sample masked`: on D3D12 that test is an "
                "instrumented re-draw of the event (RenderDoc's source marks it a TODO), so a "
                "flagged fragment can still carry a changed post value -- read `pre` against "
                "`post` here rather than the verdict alone")
          : std::string();
  const int shown = maxRows > 0 && (int)history.size() > maxRows ? maxRows : (int)history.size();

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  Field("marker", MarkerPathAt(ctrl, eid));
  Field("texture", Fmt("res%s", IdText(id).c_str()));
  Field("name", name);
  Field("x", (long long)x);
  Field("y", (long long)y);
  Field("mip", (long long)sub.mip);
  Field("slice", (long long)sub.slice);
  Field("sample", (long long)sub.sample);
  Field("format", formatName);
  Field("dimension", (long long)tex->dimension);
  Field("textureWidth", (long long)tex->width);
  Field("textureHeight", (long long)tex->height);
  Field("mips", (long long)tex->mips);
  Field("slices", (long long)slices);
  Field("samples", (long long)tex->msSamp);
  Field("cast", std::string(CastText(typeCast)));
  Field("readAs", readAs == CompType::Typeless ? std::string("raw 32-bit words")
                                               : std::string(CastText(readAs)));
  Field("usagesUpTo", (long long)usagesUpTo);
  Field("note", note);

  if(IsJson())
  {
    ArrayOpen("modifications");
    for(int i = 0; i < shown; i++)
    {
      const PixelModification &mod = history[(size_t)i];
      ObjectOpen();
      Field("eid", (long long)mod.eventId);
      Field("marker", MarkerPathAt(ctrl, (int)mod.eventId));
      Field("fragIndex", (long long)mod.fragIndex);
      Field("primitiveID", (long long)mod.primitiveID);
      Flag("passed", mod.Passed());
      Field("reasons", RejectionText(mod));
      Flag("directShaderWrite", mod.directShaderWrite);
      Flag("unboundPS", mod.unboundPS);
      WriteModificationValue("preMod", mod.preMod, readAs, false);
      WriteModificationValue("shaderOut", mod.shaderOut, readAs, false);
      WriteModificationValue("postMod", mod.postMod, readAs, true);
      ObjectClose(true);
    }
    ArrayClose(false);    // the counting fields follow
    g_Indent = 1;
  }
  else
  {
    for(int i = 0; i < shown; i++)
    {
      const PixelModification &mod = history[(size_t)i];
      const std::string where = MarkerPathAt(ctrl, (int)mod.eventId);
      printf("#%-3d eid %-7u frag %-3u prim %-7u %s\n", i + 1, mod.eventId, mod.fragIndex,
             mod.primitiveID, where.empty() ? "-" : where.c_str());
      if(mod.Passed())
        printf("      passed     every test: this fragment wrote the pixel\n");
      else
        printf("      rejected   %s\n", RejectionText(mod).c_str());
      if(mod.directShaderWrite)
        printf(
            "      note       an arbitrary shader write (a UAV or a copy), not this draw's "
            "fragment\n");
      if(mod.unboundPS)
        printf(
            "      note       no pixel shader was bound: on D3D this may also mean one is bound "
            "that declares no output for this target\n");
      printf("      pre        %s\n", ModificationValueText(mod.preMod, readAs).c_str());
      printf("      ps         %s\n", ModificationValueText(mod.shaderOut, readAs).c_str());
      printf("      post       %s\n", ModificationValueText(mod.postMod, readAs).c_str());
    }
    if(shown < (int)history.size())
      printf("... %d more (--max %d)\n", (int)history.size() - shown, maxRows);
  }

  Field("total", (long long)history.size());
  Field("passed", (long long)passed);
  Field("rejected", (long long)(history.size() - (size_t)passed));
  Field("shown", (long long)shown, true);
  g_Indent = 0;
  if(IsJson())
    printf("}\n");

  // The one-line verdict, in both formats: what the table says, said once, so a batch run's log
  // reads as answers rather than as rows.
  if(history.empty())
  {
    const std::string whyEmpty =
        usagesUpTo == 0
            ? std::string(
                  "not one event up to it touches the texture at all (`usage` shows where it "
                  "is written, and a later eid widens the scope)")
            : Fmt("%d event(s) up to it touch the texture, but none modified this pixel", usagesUpTo);
    Log("pixelhistory: the engine reports no modification of pixel (%u,%u) on res%s up to eid %d: "
        "%s",
        x, y, IdText(id).c_str(), eid, whyEmpty.c_str());
  }
  else
  {
    Log("pixelhistory: %d modification(s) up to eid %d -- %d passed every test, %d rejected; "
        "values "
        "read as %s",
        (int)history.size(), eid, passed, (int)history.size() - passed,
        readAs == CompType::Typeless ? "raw 32-bit words" : CastText(readAs));
  }
  return 0;
}

// --------------------------------------------------------------------------- crosscheck
//
// Three checks, each comparing two things the capture *states* against each other rather than
// inferring either of them:
//
//   vs-ps-link  the vertex shader's output signature against the pixel shader's input signature
//   bindings    what a stage's reflection says it binds, against what the root signature declares
//   rt-format   the bound render targets' formats, against what the pixel shader writes
//
// Deterministic by construction, which is the point (ROADMAP 2): every finding names an event and
// quotes both sides, so `state <eid>` and `shaders <eid>` show the reader the same two things.
//
// All three need shader reflection, and a capture whose shaders were stripped has none -- on those
// the three `...Checked` counts are 0 and an empty `findings` means *nothing was checked*, not
// *nothing is wrong*. That is why the counts are in the document: a silent run has to say so.

//: One line of the checklist. `check` says which of the three produced it, `detail` says what was
//: found in the capture's own words, and `eid`/`marker` say where to look with `state`.
struct CrossCheckFinding
{
  int m_Eid = 0;
  std::string m_Marker;
  std::string m_Check;
  std::string m_Detail;
};

//: What a signature element is called, in the only form a link comparison can use: HLSL matches a
//: semantic by name and index, and both are case-insensitive (`TEXCOORD0` and `texcoord0` are the
//: same link). An empty name -- a stripped reflection has no semantics at all -- matches nothing.
static std::string SemanticKey(const SigParameter &sig)
{
  return LowerAscii(sig.semanticName.c_str()) + Fmt("#%d", (int)sig.semanticIndex);
}

std::string SignatureLinkText(const SigParameter &written, const SigParameter &read)
{
  // Reading *fewer* components than the writer produced is the normal case (a `float4` output read
  // as a `float2`), so only reading more is reported: those components were never written.
  if(read.compCount > written.compCount)
    return Fmt("written with %u component(s), read as %u", (unsigned)written.compCount,
               (unsigned)read.compCount);

  // Compared as component *families*, by the header's own `VarTypeCompType`: `float` and `half` are
  // the same family, `float` and `uint` are not, and a `Typeless` side means the reflection does
  // not say -- which is not a mismatch anyone can act on.
  const CompType writtenType = ComponentClass(VarTypeCompType(written.varType));
  const CompType readType = ComponentClass(VarTypeCompType(read.varType));
  if(writtenType != readType && writtenType != CompType::Typeless && readType != CompType::Typeless)
    return Fmt("written as %s, read as %s", CastText(writtenType), CastText(readType));

  return std::string();
}

static bool StageVisibleTo(ShaderStageMask mask, ShaderStage stage)
{
  if(mask == ShaderStageMask::All)
    return true;
  return ((uint32_t)mask & (1u << (uint32_t)stage)) != 0;
}

//: Whether any root parameter visible to `stage` declares `category` at `space`/`reg`.
static bool RootDeclares(const D3D12Pipe::RootSignature &root, DescriptorCategory category,
                         uint32_t space, uint32_t reg, ShaderStage stage)
{
  for(int p = 0; p < (int)root.parameters.size(); p++)
  {
    const D3D12Pipe::RootParam &param = root.parameters[p];
    if(!StageVisibleTo(param.visibility, stage))
      continue;

    if(!param.tableRanges.empty())
    {
      // A table declares itself by its ranges and by nothing else: `reg`/`space` on a *table*
      // parameter are not the ranges' registers (they read 0 on every table measured), so matching
      // them here would call every binding at b0 s0 covered by every table.
      for(int r = 0; r < (int)param.tableRanges.size(); r++)
      {
        const D3D12Pipe::RootTableRange &range = param.tableRanges[r];
        if(range.category != category || range.space != space)
          continue;
        // An unbounded range is written as 0 or as UINT32_MAX; both mean "everything from here on".
        const bool bUnbounded = (range.count == 0 || range.count == ~0u);
        if(reg >= range.baseRegister && (bUnbounded || reg < range.baseRegister + range.count))
          return true;
      }
      continue;
    }

    // A root constant or a root descriptor: the register and the space are in the state, but which
    // of the three categories the parameter is *for* is not -- so a match here covers the binding
    // rather than proving the categories agree, and it is not turned into a finding either way.
    // `state` prints the row this was decided from, which is the honest place to look.
    if(param.space == space && param.reg == reg)
      return true;
  }
  return false;
}

//: The span of registers the root signature declares for one register class -- `b`, `t` or `u` at
//: one space -- or "it declares none at all".
//:
//: Which is the question that decides whether an uncovered binding can be reported. A shader that
//: reads its resources bindlessly (SM 6.6 `ResourceDescriptorHeap`) lists bindings the root
//: signature need not declare, and that is indistinguishable from a range somebody forgot: measured
//: on the UE captures, where most compute passes are exactly that and a per-binding "not declared"
//: line put 4373 rows on a frame with nothing wrong with it -- over the every-id sweep this command
//: also replaced, so the same rule over the frame's 144 calls is nearer 500. So a class the
//: signature never mentions is counted and left alone, and only a binding *outside* a class it does
//: declare is reported -- the "the table is too small" shape, which is decidable.
static bool DeclaredSpan(const D3D12Pipe::RootSignature &root, DescriptorCategory category,
                         uint32_t space, ShaderStage stage, uint32_t &lo, uint32_t &hi)
{
  bool bAny = false;
  lo = ~0u;
  hi = 0;
  for(int p = 0; p < (int)root.parameters.size(); p++)
  {
    const D3D12Pipe::RootParam &param = root.parameters[p];
    if(!StageVisibleTo(param.visibility, stage))
      continue;
    for(int r = 0; r < (int)param.tableRanges.size(); r++)
    {
      const D3D12Pipe::RootTableRange &range = param.tableRanges[r];
      if(range.category != category || range.space != space)
        continue;
      // An unbounded range is written as 0 or as UINT32_MAX; both mean "everything from here on",
      // and then no binding can be outside it.
      const uint32_t last =
          (range.count == 0 || range.count == ~0u) ? ~0u : range.baseRegister + range.count - 1;
      lo = std::min(lo, range.baseRegister);
      hi = std::max(hi, last);
      bAny = true;
    }
  }
  return bAny;
}

//: One binding against the root signature. One line per binding and not per register: an array's
//: uncovered tail says nothing the first uncovered register did not.
static void CheckBindingList(int eid, const std::string &marker, ShaderStage stage,
                             const D3D12Pipe::RootSignature &root, DescriptorCategory category,
                             const char *name, uint32_t bindNumber, uint32_t space,
                             uint32_t arraySize, std::vector<CrossCheckFinding> &out, int &checked,
                             int &unmapped)
{
  checked++;

  uint32_t lo = 0, hi = 0;
  if(!DeclaredSpan(root, category, space, stage, lo, hi))
  {
    unmapped++;    // the signature says nothing about this register class: counted, not reported
    return;
  }

  const uint32_t count = arraySize == 0 ? 1 : arraySize;
  for(uint32_t i = 0; i < count; i++)
  {
    if(RootDeclares(root, category, space, bindNumber + i, stage))
      continue;
    out.push_back(
        {eid, marker, "bindings",
         Fmt("%s binds %s at %c%u s%u (%u register(s)) which no root parameter visible to "
             "%s declares: the %c ranges it does declare at s%u cover %c%u-%c%u",
             StageName(stage), name, RegisterLetter(category), bindNumber, space, count,
             StageName(stage), RegisterLetter(category), space, RegisterLetter(category), lo,
             RegisterLetter(category), hi)});
    return;
  }
}

static void CheckBindings(int eid, const std::string &marker, ShaderStage stage,
                          const ShaderReflection *refl, const D3D12Pipe::RootSignature &root,
                          std::vector<CrossCheckFinding> &out, int &checked, int &unmapped)
{
  if(refl == NULL)
    return;

  for(size_t i = 0; i < refl->constantBlocks.size(); i++)
  {
    const ConstantBlock &block = refl->constantBlocks[i];
    CheckBindingList(eid, marker, stage, root, DescriptorCategory::ConstantBlock,
                     block.name.empty() ? "(unnamed)" : block.name.c_str(), block.fixedBindNumber,
                     block.fixedBindSetOrSpace, block.bindArraySize, out, checked, unmapped);
  }
  for(size_t i = 0; i < refl->readOnlyResources.size(); i++)
  {
    const ShaderResource &res = refl->readOnlyResources[i];
    CheckBindingList(eid, marker, stage, root, DescriptorCategory::ReadOnlyResource,
                     res.name.empty() ? "(unnamed)" : res.name.c_str(), res.fixedBindNumber,
                     res.fixedBindSetOrSpace, res.bindArraySize, out, checked, unmapped);
  }
  for(size_t i = 0; i < refl->readWriteResources.size(); i++)
  {
    const ShaderResource &res = refl->readWriteResources[i];
    CheckBindingList(eid, marker, stage, root, DescriptorCategory::ReadWriteResource,
                     res.name.empty() ? "(unnamed)" : res.name.c_str(), res.fixedBindNumber,
                     res.fixedBindSetOrSpace, res.bindArraySize, out, checked, unmapped);
  }
}

static void CheckLink(int eid, const std::string &marker, const ShaderReflection *vs,
                      const ShaderReflection *ps, std::vector<CrossCheckFinding> &out, int &checked)
{
  if(vs == NULL || ps == NULL)
    return;

  for(size_t i = 0; i < ps->inputSignature.size(); i++)
  {
    const SigParameter &read = ps->inputSignature[i];
    // SV_Position and friends are the pipeline's, not a shader-to-shader link: the rasteriser fills
    // them in and no stage has to write them.
    if(read.systemValue != ShaderBuiltin::Undefined)
      continue;
    if(read.semanticName.empty())
      continue;

    const std::string key = SemanticKey(read);
    const SigParameter *written = NULL;
    for(size_t j = 0; j < vs->outputSignature.size(); j++)
    {
      if(SemanticKey(vs->outputSignature[j]) == key)
      {
        written = &vs->outputSignature[j];
        break;
      }
    }

    checked++;
    if(written == NULL)
    {
      out.push_back({eid, marker, "vs-ps-link",
                     Fmt("ps reads %s which vs does not write", SignatureText(read).c_str())});
      continue;
    }
    const std::string why = SignatureLinkText(*written, read);
    if(!why.empty())
      out.push_back({eid, marker, "vs-ps-link",
                     Fmt("ps reads %s: %s", SignatureText(read).c_str(), why.c_str())});
  }
}

//: Whether a pixel-shader output is a colour target, and which slot it is. The compiler picks the
//: spelling and both occur: `SV_Target` carries the slot in the semantic index, `SV_Target2` in the
//: name. An output that is neither is not a colour target (SV_Depth, SV_Coverage, ...).
static bool TargetSlot(const SigParameter &sig, int &slot)
{
  if(sig.systemValue == ShaderBuiltin::ColorOutput)
  {
    slot = (int)sig.semanticIndex;
    return true;
  }
  const std::string name = LowerAscii(sig.semanticName.c_str());
  if(name.compare(0, 9, "sv_target") != 0)
    return false;
  if(name == "sv_target")
  {
    slot = (int)sig.semanticIndex;
    return true;
  }
  return ParseInt(name.c_str() + 9, slot) && slot >= 0;
}

static void CheckTargets(int eid, const std::string &marker, const D3D12Pipe::State *d3d12,
                         const ShaderReflection *ps, std::vector<CrossCheckFinding> &out,
                         int &checked)
{
  if(ps == NULL)
    return;

  const rdcarray<Descriptor> &targets = d3d12->outputMerger.renderTargets;
  int bound = 0;
  for(size_t i = 0; i < targets.size(); i++)
    if(targets[i].resource != ResourceId::Null())
      bound++;

  std::map<int, const SigParameter *> outputs;
  for(size_t i = 0; i < ps->outputSignature.size(); i++)
  {
    int slot = 0;
    if(TargetSlot(ps->outputSignature[i], slot))
      outputs[slot] = &ps->outputSignature[i];
  }
  const int declared = outputs.empty() ? 0 : (int)outputs.rbegin()->first + 1;

  // A fact, not a verdict: the write to an unbound target is dropped, which is legal and is also
  // what a pass that lost a colour target looks like. Both readings are the reader's to make.
  if(declared > bound)
    out.push_back({eid, marker, "rt-format",
                   Fmt("ps writes %d colour target(s) (SV_Target0..%d) but %d render target(s) are "
                       "bound",
                       declared, declared - 1, bound)});

  for(size_t i = 0; i < targets.size(); i++)
  {
    if(targets[i].resource == ResourceId::Null())
      continue;
    const std::map<int, const SigParameter *>::const_iterator it = outputs.find((int)i);
    if(it == outputs.end())
      continue;    // no ps output for this slot: nothing to compare the format against

    checked++;
    const CompType rtType = ComponentClass(targets[i].format.compType);
    const CompType shaderType = ComponentClass(VarTypeCompType(it->second->varType));
    // Typeless on either side is "the capture does not say", not a mismatch.
    if(rtType == CompType::Typeless || shaderType == CompType::Typeless || rtType == shaderType)
      continue;
    out.push_back({eid, marker, "rt-format",
                   Fmt("rt%u is %s but ps writes it as %s", (unsigned)i,
                       FormatText(targets[i].format).c_str(), CastText(shaderType))});
  }
}

static const ShaderReflection *ReflectionFor(IReplayController *ctrl, const D3D12Pipe::State *d3d12,
                                             ShaderStage stage)
{
  const D3D12Pipe::Shader *shader = StageShader(d3d12, stage);
  if(shader == NULL || shader->resourceId == ResourceId::Null())
    return NULL;
  // The pipe state carries the reflection when the engine already has it; `GetShader` is the same
  // answer the long way round.
  if(shader->reflection != NULL)
    return shader->reflection;
  return ctrl->GetShader(d3d12->pipelineResourceId, shader->resourceId,
                         ShaderEntryPoint(rdcstr(), stage));
}

static bool AnyStageBound(const D3D12Pipe::State *d3d12)
{
  for(const ShaderStage stage : ReportedStages())
  {
    const D3D12Pipe::Shader *shader = StageShader(d3d12, stage);
    if(shader != NULL && shader->resourceId != ResourceId::Null())
      return true;
  }
  return false;
}

//: The three checks at one event. Returns false when the event has no shaders bound, which is a
//: call with nothing to compare (a copy, a barrier, a clear) rather than a failure.
static bool CheckEvent(IReplayController *ctrl, int eid, std::vector<CrossCheckFinding> &findings,
                       int &linksChecked, int &bindingsChecked, int &bindingsUnmapped,
                       int &targetsChecked, int &noRootParameters)
{
  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  if(d3d12 == NULL || !AnyStageBound(d3d12))
    return false;

  const std::string marker = MarkerPathAt(ctrl, eid);
  const ShaderReflection *vs = ReflectionFor(ctrl, d3d12, ShaderStage::Vertex);
  const ShaderReflection *ps = ReflectionFor(ctrl, d3d12, ShaderStage::Pixel);

  CheckLink(eid, marker, vs, ps, findings, linksChecked);
  if(d3d12->rootSignature.parameters.empty())
  {
    // Counted, not reported: an empty root signature is `rootSignature 0` in the state, which on
    // the UE captures is how a compute event comes back and is not a defect anyone can act on --
    // and one row per event put a thousand rows of it on a frame. It is a fact about what could be
    // checked, which is what `bindingsChecked` is for.
    noRootParameters++;
  }
  else
  {
    for(const ShaderStage stage : ReportedStages())
      CheckBindings(eid, marker, stage, ReflectionFor(ctrl, d3d12, stage), d3d12->rootSignature,
                    findings, bindingsChecked, bindingsUnmapped);
  }
  CheckTargets(eid, marker, d3d12, ps, findings, targetsChecked);
  return true;
}

int CmdCrosscheck(IReplayController *ctrl, ICaptureFile *file, const char *path, int eid, int since,
                  int until, int maxEvents, int maxRows)
{
  // The action tree is the authority for the last event id (`chunks` numbering is a different one,
  // REFERENCE 9), and the sweep needs it whether or not an eid was given.
  int calls = 0;
  bool bTruncated = false;
  const std::vector<ActionNode> rows = ActionTree(ctrl, calls, bTruncated);
  int last = 0;
  for(size_t i = 0; i < rows.size(); i++)
    if(rows[i].m_Eid > last)
      last = rows[i].m_Eid;

  const int from = eid > 0 ? eid : (since > 0 ? since : 1);
  const int to = eid > 0 ? eid : (until > 0 ? until : last);
  if(from <= 0 || to < from)
    return Fail(3,
                "crosscheck: nothing to check: the range %d..%d is empty (the capture's last "
                "event id is %d)",
                from, to, last);

  // The sweep walks the frame's *calls*, not every id. `dump` walks every id and stops after 256 in
  // a row with no state, which is right for a scan that is looking for state changes; here it is
  // wrong twice over -- an id that is not a call has no state of its own to check (`SetFrameEvent`
  // answers at any id with whatever is still bound, so the same state comes back dozens of times),
  // and a stop-after-256 rule on a capture where 119 of 1736 ids are calls ends the walk inside the
  // first marker. Measured on the hobby capture: 1736 ids, 119 calls.
  std::vector<int> ids;
  if(eid > 0)
  {
    ids.push_back(eid);    // asked for by hand: checked whether or not the tree calls it a call
  }
  else
  {
    for(size_t i = 0; i < rows.size(); i++)
      if(rows[i].m_bCall && rows[i].m_Eid >= from && rows[i].m_Eid <= to)
        ids.push_back(rows[i].m_Eid);
  }

  std::vector<CrossCheckFinding> findings;
  int linksChecked = 0, bindingsChecked = 0, bindingsUnmapped = 0, targetsChecked = 0;
  int noRootParameters = 0;
  int scanned = 0;
  int visited = 0;    // ids the sweep positioned the replay on, which is not `scanned`
  bool bStoppedEarly = false;

  Progress progress;
  progress.Begin("crosscheck", (int)ids.size());

  for(size_t n = 0; n < ids.size(); n++)
  {
    visited++;
    const int id = ids[(size_t)n];
    const ULONGLONG sinceEvent = Millis();
    // `force` so a repeated id is not skipped: the answer has to be the state at *this* id, not
    // "already there".
    ctrl->SetFrameEvent((uint32_t)id, true);
    ProfileAdd(kProfileSetFrameEvent, sinceEvent);

    const ULONGLONG sinceChecks = Millis();
    const bool bHasState = CheckEvent(ctrl, id, findings, linksChecked, bindingsChecked,
                                      bindingsUnmapped, targetsChecked, noRootParameters);
    ProfileAdd(kProfileCrosscheck, sinceChecks);

    if(!bHasState)
      continue;    // a call with nothing bound: no reflection, so nothing to compare

    scanned++;
    progress.Tick((int)n + 1);

    if(maxEvents > 0 && scanned >= maxEvents)
    {
      bStoppedEarly = true;
      break;
    }
  }
  // What was *visited*, not the size of the list: `--max-events` can end the sweep early, and a
  // progress line reading `119/119 done` after five events have been checked is a false statement
  // in the log, which is the one place a stopped run has to say so.
  progress.Done(visited);

  PrintCaptureHeader(file, path);
  Field("eid", (long long)(eid > 0 ? eid : 0));
  Field("from", (long long)from);
  Field("to", (long long)to);
  Field("scanned", (long long)scanned);
  // How much was actually compared: an empty `findings` next to three zeros means the capture has
  // no reflection to check, which is a different answer from "everything links".
  Field("linksChecked", (long long)linksChecked);
  Field("bindingsChecked", (long long)bindingsChecked);
  Field("bindingsUnmapped", (long long)bindingsUnmapped);
  Field("targetsChecked", (long long)targetsChecked);
  Field("noRootParameters", (long long)noRootParameters);
  Field("total", (long long)findings.size());

  const int shown = maxRows > 0 ? std::min<int>(maxRows, (int)findings.size()) : (int)findings.size();
  ArrayOpen("findings");
  for(int i = 0; i < shown; i++)
  {
    const CrossCheckFinding &f = findings[i];
    if(IsJson())
    {
      ObjectOpen();
      Field("eid", (long long)f.m_Eid);
      Field("marker", f.m_Marker);
      Field("check", f.m_Check);
      Field("detail", f.m_Detail, true);
      ObjectClose();
    }
    else
    {
      const std::string where = f.m_Marker.empty() ? std::string() : "  [" + f.m_Marker + "]";
      Row(Fmt("eid %-7d %-11s %s%s", f.m_Eid, f.m_Check.c_str(), f.m_Detail.c_str(), where.c_str()));
    }
  }
  if(!IsJson() && shown < (int)findings.size())
    printf("... %d more (--max %d)\n", (int)findings.size() - shown, maxRows);
  ArrayClose(false);
  Field("shown", (long long)shown);
  Flag("stoppedEarly", bStoppedEarly, true);
  g_Indent = 0;
  if(IsJson())
    printf("}\n");

  const int all = (int)findings.size();
  if(all == 0)
  {
    Log("crosscheck: %d finding(s) in %d event(s) with state (eid %d..%d); %d link(s), %d "
        "binding(s) "
        "(%d at a register class the root signature never declares) and %d target(s) checked; %d "
        "event(s) with no root parameters",
        all, scanned, from, to, linksChecked, bindingsChecked, bindingsUnmapped, targetsChecked,
        noRootParameters);
  }
  else
  {
    Log("crosscheck: %d finding(s) in %d event(s) with state (eid %d..%d); %d link(s), %d "
        "binding(s) "
        "(%d at a register class the root signature never declares) and %d target(s) checked; %d "
        "event(s) with no root parameters -- first: eid %d %s: %s",
        all, scanned, from, to, linksChecked, bindingsChecked, bindingsUnmapped, targetsChecked,
        noRootParameters, findings[0].m_Eid, findings[0].m_Check.c_str(),
        findings[0].m_Detail.c_str());
  }
  return 0;
}
