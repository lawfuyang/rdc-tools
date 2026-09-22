// z.commands_patch — building a shader for the target, substituting it, and looking at what changed
//
// Part of replay_dump; the internal API is declared in common.h.
//
// The "what if" half of the tool: `BuildTargetShader` compiles a shader for *this* replay device,
// `ReplaceResource` substitutes it for the capture's own, and the next `SetFrameEvent` re-runs the
// frame with it -- so "is this branch the problem" becomes an experiment rather than a re-capture.
//
// What can be built is the target's business, not ours: `--encodings` prints what this replay instance
// accepts (`GetTargetShaderEncodings`), and for D3D12 that is source or bytecode encodings -- HLSL,
// DXBC, DXIL -- but *not* the disassembly `--dump` writes, which is text for a reader rather than a
// program for a compiler. Saying that here is cheaper than a user editing assembly for an hour.

#include "common.h"

namespace
{
const char *EncodingName(ShaderEncoding enc)
{
  switch(enc)
  {
    case ShaderEncoding::HLSL: return "hlsl";
    case ShaderEncoding::DXBC: return "dxbc";
    case ShaderEncoding::DXIL: return "dxil";
    case ShaderEncoding::GLSL: return "glsl";
    case ShaderEncoding::SPIRV: return "spirv";
    case ShaderEncoding::SPIRVAsm: return "spirv-asm";
    case ShaderEncoding::OpenGLSPIRV: return "opengl-spirv";
    case ShaderEncoding::OpenGLSPIRVAsm: return "opengl-spirv-asm";
    case ShaderEncoding::Slang: return "slang";
    default: return "unknown";
  }
}

bool EncodingFromName(const std::string &name, ShaderEncoding &out)
{
  if(name == "hlsl")
    out = ShaderEncoding::HLSL;
  else if(name == "dxbc")
    out = ShaderEncoding::DXBC;
  else if(name == "dxil")
    out = ShaderEncoding::DXIL;
  else if(name == "glsl")
    out = ShaderEncoding::GLSL;
  else if(name == "spirv")
    out = ShaderEncoding::SPIRV;
  else if(name == "spirv-asm")
    out = ShaderEncoding::SPIRVAsm;
  else
    return false;
  return true;
}

std::string EncodingList(IReplayController *ctrl)
{
  const rdcarray<ShaderEncoding> target = ctrl->GetTargetShaderEncodings();
  std::string out;
  for(size_t i = 0; i < target.size(); i++)
    out += Fmt("%s%s", out.empty() ? "" : ", ", EncodingName(target[i]));
  if(out.empty())
    out = "(none reported)";
  return out;
}

//: Frees the built shader and drops the replacement on every path out of the scope.
//:
//: In a `--repl` or `batch` session the process outlives the command, and a replacement left behind
//: would change what every later command answers -- so this is not tidiness, it is the difference
//: between one experiment and a session that quietly lies.
struct PatchGuard
{
  PatchGuard(IReplayController *ctrl, ResourceId original)
      : m_Ctrl(ctrl), m_Original(original), m_Built(ResourceId::Null())
  {
  }
  ~PatchGuard()
  {
    if(m_Ctrl == NULL)
      return;
    if(m_Original != ResourceId::Null())
      m_Ctrl->RemoveReplacement(m_Original);
    if(m_Built != ResourceId::Null())
      m_Ctrl->FreeTargetResource(m_Built);
  }
  void SetBuilt(ResourceId id) { m_Built = id; }
  PatchGuard(const PatchGuard &) = delete;
  PatchGuard &operator=(const PatchGuard &) = delete;

private:
  IReplayController *m_Ctrl;
  ResourceId m_Original;
  ResourceId m_Built;
};

//: `name=value` or a bare `name`, the way the API's own `ShaderCompileFlag` is shaped.
void AddCompileFlag(ShaderCompileFlags &flags, const std::string &text)
{
  ShaderCompileFlag flag;
  const size_t eq = text.find('=');
  if(eq == std::string::npos)
  {
    flag.name = rdcstr(text.c_str());
  }
  else
  {
    flag.name = rdcstr(text.substr(0, eq).c_str());
    flag.value = rdcstr(text.substr(eq + 1).c_str());
  }
  flags.flags.push_back(flag);
}
}    // namespace

int CmdPatch(IReplayController *ctrl, ICaptureFile *file, const char *path,
             const std::vector<std::string> &args)
{
  if(args.size() < 3)
    return Fail(2,
                "patch needs an event id and a stage: patch <rdc> <eid> <stage> [--from <file>]");

  bool bListEncodings = false, bCompare = false;
  std::string dumpPath, fromPath, encName = "hlsl", entry = "main", outDir;
  std::vector<std::string> flagDefs;
  for(size_t i = 3; i < args.size(); i++)
  {
    if(args[i] == "--encodings")
      bListEncodings = true;
    else if(args[i] == "--compare")
      bCompare = true;
    else if(args[i] == "--dump" && i + 1 < args.size())
      dumpPath = args[++i];
    else if(args[i] == "--from" && i + 1 < args.size())
      fromPath = args[++i];
    else if(args[i] == "--enc" && i + 1 < args.size())
      encName = args[++i];
    else if(args[i] == "--entry" && i + 1 < args.size())
      entry = args[++i];
    else if(args[i] == "--flag" && i + 1 < args.size())
      flagDefs.push_back(args[++i]);
    else if(args[i].size() > 2 && args[i].compare(0, 2, "--") != 0 && outDir.empty())
      outDir = args[i];    // the output directory, positionally: `--out` belongs to `schema`
    else
      return Fail(2, "patch does not know '%s'", args[i].c_str());
  }

  const int eid = ToInt(args[1], 0);
  const ShaderStage stage = StageFromName(args[2].c_str());
  if(stage == ShaderStage::Invalid)
    return Fail(2, "'%s' is not a shader stage (vs hs ds gs ps cs as ms)", args[2].c_str());

  if(bListEncodings)
  {
    PrintCaptureHeader(file, path);
    Field("encodings", EncodingList(ctrl), true);
    if(g_bJson)
      printf("}\n");
    Log("patch: this replay target builds %s; `--dump` writes disassembly, which is readable but "
        "is "
        "not one of them",
        EncodingList(ctrl).c_str());
    return 0;
  }

  if(dumpPath.empty() && fromPath.empty())
    return Fail(2,
                "give --dump <file> to write the shader out, or --from <file> to replace it "
                "(with --compare to see the difference)");

  ShaderEncoding encoding = ShaderEncoding::HLSL;
  if(!EncodingFromName(encName, encoding))
    return Fail(
        2, "'%s' is not an encoding this tool names (hlsl, dxbc, dxil, glsl, spirv, spirv-asm)",
        encName.c_str());

  MoveToEvent(ctrl, eid);
  const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
  const D3D12Pipe::Shader *shader = StageShader(d3d12, stage);
  if(shader == NULL || shader->resourceId == ResourceId::Null())
    return Fail(1, "no %s shader is bound at eid %d", StageName(stage), eid);
  const ResourceId original = shader->resourceId;
  const ResourceId pipeline = d3d12 == NULL ? ResourceId::Null() : d3d12->pipelineResourceId;

  const ShaderReflection *refl =
      ctrl->GetShader(pipeline, original, ShaderEntryPoint(rdcstr(), stage));

  PrintCaptureHeader(file, path);
  Field("eid", (long long)eid);
  // `std::string(...)` around a `const char *`: the writer's `string_view` and `rdcstr` overloads
  // are both one user-defined conversion away from it, which is ambiguous (the rest of the driver
  // wraps for the same reason).
  Field("stage", std::string(StageName(stage)));
  Field("shader", Fmt("res%s", IdText(original).c_str()));
  Field("reflection",
        refl == NULL ? std::string("(the engine has none for this shader)") : std::string("ok"));
  Field("encodings", EncodingList(ctrl));

  std::string dumped;
  if(!dumpPath.empty())
  {
    if(refl == NULL)
      return Fail(1, "the engine has no reflection for res%s, so it cannot be disassembled",
                  IdText(original).c_str());
    const rdcstr asmText = ctrl->DisassembleShader(pipeline, refl, rdcstr());
    const std::string text(asmText.c_str(), asmText.size());
    FILE *f = FileOpen(dumpPath, "wb");
    if(f == NULL)
      return Fail(1, "cannot write %s", dumpPath.c_str());
    const bool bOk = fwrite(text.data(), 1, text.size(), f) == text.size();
    fclose(f);
    if(!bOk)
      return Fail(1, "cannot write %s", dumpPath.c_str());
    dumped = dumpPath;
    Log("patch: wrote %d byte(s) of disassembly to %s (readable, not a buildable encoding -- see "
        "`patch --encodings`)",
        (int)text.size(), dumpPath.c_str());
  }

  // The "before" picture is read *before* the replacement, which is the only order that means
  // anything: `ReplaceResource` applies from the next replay onwards.
  ImageData before;
  ResourceId target = ResourceId::Null();
  std::string beforeWhy;
  if(bCompare)
  {
    target = FirstRenderTarget(d3d12);
    if(!ReadTargetImage(ctrl, target, before, beforeWhy))
      return Fail(1, "cannot read the render target to compare: %s", beforeWhy.c_str());
  }

  bool bReplaced = false;
  std::string compileLog;
  if(!fromPath.empty())
  {
    std::string text;
    if(!ReadWholeFile(fromPath.c_str(), text))
      return Fail(1, "cannot read %s", fromPath.c_str());

    ShaderCompileFlags flags;
    for(size_t i = 0; i < flagDefs.size(); i++)
      AddCompileFlag(flags, flagDefs[i]);

    // `bytebuf` is RenderDoc's own `rdcarray<byte>`, which has no range `assign`: this is the copy.
    bytebuf source;
    source.reserve(text.size());
    for(size_t i = 0; i < text.size(); i++)
      source.push_back((byte)text[i]);
    const rdcpair<ResourceId, rdcstr> built =
        ctrl->BuildTargetShader(rdcstr(entry.c_str()), encoding, source, flags, stage);
    compileLog = std::string(built.second.c_str(), built.second.size());
    Field("entry", entry);
    Field("encoding", std::string(EncodingName(encoding)));
    Field("flags", (long long)flagDefs.size());

    if(built.first == ResourceId::Null())
    {
      Flag("compiled", false);
      Field("compiler", compileLog, true);
      if(g_bJson)
        printf("}\n");
      // The compiler's own message is the answer here, not a paraphrase of it.
      return Fail(1, "the shader did not compile -- the compiler said: %s",
                  compileLog.empty() ? "(nothing)" : compileLog.c_str());
    }

    PatchGuard guard(ctrl, original);
    guard.SetBuilt(built.first);
    Field("built", Fmt("res%s", IdText(built.first).c_str()));
    Flag("compiled", true);
    if(!compileLog.empty())
      Log("patch: the compiler said: %s", compileLog.c_str());

    ctrl->ReplaceResource(original, built.first);
    bReplaced = true;
    // The replacement applies from the next replay -- but a replay that reuses cached pipeline objects
    // keeps the *old* shader, which is what `ClearReplayCache` exists for ("ensure subsequent replays
    // fully re-initialise any data"). Measured the hard way: without it, a replacement pixel shader
    // that discards every pixel rendered byte-identically to the original, which is a change no
    // bookkeeping could fake and the reason this call is here rather than discovered later.
    ctrl->ClearReplayCache();
    MoveToEvent(ctrl, eid);
    Flag("replaced", true);

    if(bCompare)
    {
      ImageData after;
      std::string why;
      if(!ReadTargetImage(ctrl, target, after, why))
        return Fail(1, "the frame replayed but its render target cannot be read: %s", why.c_str());

      const std::string dir = outDir.empty() ? std::string("patch") : outDir;
      if(!MakeDir(dir))
        return Fail(1, "cannot create %s", dir.c_str());
      const std::string beforePath = (std::filesystem::path(dir) / "before.bmp").string();
      const std::string afterPath = (std::filesystem::path(dir) / "after.bmp").string();
      const std::string heatPath = (std::filesystem::path(dir) / "diff.bmp").string();
      ImageData heat;
      int maxDelta = 0;
      long long sumDelta = 0;
      const long long differing = ImagePixelDelta(before, after, maxDelta, sumDelta, &heat);
      const bool bWrote = WriteBMPImage(beforePath.c_str(), before) &&
                          WriteBMPImage(afterPath.c_str(), after) &&
                          WriteBMPImage(heatPath.c_str(), heat);

      const uint64_t hashBefore = DifferenceHash(before), hashAfter = DifferenceHash(after);
      int hamming = 0;
      for(int bit = 0; bit < 64; bit++)
      {
        if(((hashBefore >> bit) & 1u) != ((hashAfter >> bit) & 1u))
          hamming++;
      }
      const long long pixels = (long long)before.m_Width * (long long)before.m_Height;

      Field("before", beforePath);
      Field("after", afterPath);
      Field("diff", heatPath);
      Flag("wroteImages", bWrote);
      Field("pixels", pixels);
      Field("differing", differing);
      // A string, because the writer has no fractional field (see `Field` in common.h): three
      // decimals, so two runs are comparable by eye and by a consumer that parses it.
      Field("percentDiffering",
            Fmt("%.3f", pixels == 0 ? 0.0 : 100.0 * (double)differing / (double)pixels));
      Field("maxDelta", (long long)maxDelta);
      Field("hashBefore", Fmt("%016llx", (unsigned long long)hashBefore));
      Field("hashAfter", Fmt("%016llx", (unsigned long long)hashAfter));
      Field("hashDistance", (long long)hamming);

      if(differing == 0)
        Log("patch: the frame rendered identically -- the patched shader changed nothing in this "
            "draw");
      else
        Log("patch: %lld of %lld pixel(s) changed (%.3f%%), hash distance %d of 64 -- "
            "before/after/diff "
            "in %s",
            differing, pixels, 100.0 * (double)differing / (double)pixels, hamming, dir.c_str());
      if(!bWrote)
        Log("patch: one of the three images could not be written to %s", dir.c_str());
    }
  }

  Field("dumped", dumped, true);
  if(g_bJson)
    printf("}\n");
  if(!bReplaced)
    Log("patch: nothing was replaced (--from was not given); %s",
        dumped.empty() ? "nothing was written" : "the disassembly is above");
  return 0;
}
