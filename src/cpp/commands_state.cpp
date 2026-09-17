// z.commands_state — the per-event and per-resource commands (state, shaders, cb, usage)
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
