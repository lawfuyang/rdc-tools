// z.commands_watch — `watch`: one reflection member's value at every event of a range
//
// Part of replay_dump; the internal API is declared in common.h.
//
// The question this answers is the one a `cb` per event answers badly: "this uniform is right at
// one draw and wrong at the next". Forty `cb` calls are forty commands and forty documents to read;
// this is one table of the values that *changed*, with the event id each change happened at.
//
// It is slow by nature -- a constant-buffer read per event, which is the engine fetching that
// buffer's bytes and reflecting it -- so it takes a range and reports how much it actually read,
// and a run that matched nothing says so rather than printing an empty table. What it reads, it
// reads the way `cb` does: `BlockRead` is the same resolution (commands_state.cpp), so a `watch`
// row and a `cb` document cannot disagree about which buffer a block is.

#include "common.h"

namespace
{
//: One watched value: its full dotted path inside the block, and the text `FormatValue` gives it.
struct Watched
{
  std::string m_Path;
  std::string m_Value;
};

}    // namespace

//: Case-insensitive equality for a *name*: a shader's reflection spells `Light.intensity` and a
//: reader types `light.intensity`, and the two are the same variable. ASCII only, which is what a
//: shader variable name is; anything else is compared byte for byte.
bool WatchSameName(const std::string &a, const std::string &b)
{
  if(a.size() != b.size())
    return false;
  for(size_t i = 0; i < a.size(); i++)
  {
    char left = a[i], right = b[i];
    if(left >= 'A' && left <= 'Z')
      left = (char)(left - 'A' + 'a');
    if(right >= 'A' && right <= 'Z')
      right = (char)(right - 'A' + 'a');
    if(left != right)
      return false;
  }
  return true;
}

//: Does `path` answer to `want`? The whole path first (`Light.intensity`), then -- only when the
//: request has no dot in it -- the last component (`intensity`), which is how a reader who
//: remembers the member but not the struct that holds it asks. Both case-insensitively; anything
//: else is not a match, because a substring rule would silently watch `intensityScale` alongside
//: `Light.intensity`.
//:
//: Split out of the collector (and declared in common.h) so the selftest can pin the two rules that
//: are wrong quietly: a substring match, and a match that ignores the case a reader types.
bool WatchNameMatches(const std::string &path, const std::string &want)
{
  if(WatchSameName(path, want))
    return true;
  if(want.find('.') != std::string::npos)
    return false;
  const size_t dot = path.rfind('.');
  return WatchSameName(dot == std::string::npos ? path : path.substr(dot + 1), want);
}

namespace
{
//: Every leaf under a matched struct, in the order `PrintVariables` would print them: what a
//: request for a *struct* means, since `FormatValue` prints a struct as `-` and that is not an
//: answer to "watch Light" while its members are. Recursion stops at `kMaxValueDepth`, the depth
//: `PrintVariables` stops at.
void CollectLeaves(const rdcarray<ShaderVariable> &vars, const std::string &prefix,
                   std::vector<Watched> &out, int depth)
{
  for(size_t i = 0; i < vars.size(); i++)
  {
    const ShaderVariable &v = vars[i];
    const std::string path = prefix + "." + std::string(v.name.c_str());
    if(v.members.empty() || depth >= kMaxValueDepth)
      out.push_back({path, FormatValue(v)});
    else
      CollectLeaves(v.members, path, out, depth + 1);
  }
}

//: Every value under `vars` whose path answers to `want`, with the members of a matched struct
//: underneath it. The walk descends through everything, so a request for `Light.intensity` finds it
//: wherever in the tree it sits.
void CollectWatched(const rdcarray<ShaderVariable> &vars, const std::string &prefix,
                    const std::string &want, std::vector<Watched> &out)
{
  for(size_t i = 0; i < vars.size(); i++)
  {
    const ShaderVariable &v = vars[i];
    const std::string name(v.name.c_str());
    const std::string path = prefix.empty() ? name : prefix + "." + name;
    if(WatchNameMatches(path, want))
    {
      if(v.members.empty())
        out.push_back({path, FormatValue(v)});
      else
        CollectLeaves(v.members, path, out, 0);
    }
    else if(!v.members.empty())
      CollectWatched(v.members, path, want, out);
  }
}
}    // namespace

std::vector<std::string> WatchPaths(const rdcarray<ShaderVariable> &vars, const std::string &want)
{
  std::vector<Watched> found;
  CollectWatched(vars, std::string(), want, found);
  std::vector<std::string> paths;
  for(size_t i = 0; i < found.size(); i++)
    paths.push_back(found[i].m_Path);
  return paths;
}

namespace
{
//: One parsed command line, so the loop below reads as the question it answers.
struct WatchArgs
{
  std::string m_Name;
  int m_Since = 1;
  int m_Until = 0;        // 0 = the frame's own last event
  int m_MaxEvents = 0;    // 0 = no cap on the ids visited
  bool m_bAll = false;    // print every event, not only the changes
  int m_Stage = -1;       // -1 = every stage with a bound shader
};

//: The non-option arguments and the options, in one pass: `--since`, `--until`, `--max-events`,
//: `--stage`, `--all`. An unknown option is refused rather than ignored -- a typo that silently
//: watched the whole frame would be a minute of sweeping to answer a question nobody asked.
bool ParseWatchArgs(const std::vector<std::string> &args, WatchArgs &out, std::string &why)
{
  for(size_t i = 1; i < args.size(); i++)
  {
    const std::string &arg = args[i];
    const bool bHasValue = i + 1 < args.size();
    if(arg == "--since" && bHasValue)
      out.m_Since = ToInt(args[++i], 1);
    else if(arg == "--until" && bHasValue)
      out.m_Until = ToInt(args[++i], 0);
    else if(arg == "--max-events" && bHasValue)
      out.m_MaxEvents = ToInt(args[++i], 0);
    else if(arg == "--stage" && bHasValue)
    {
      const std::string &want = args[++i];
      for(int s = 0; s < (int)ShaderStage::Count; s++)
      {
        if(want == StageName((ShaderStage)s))
        {
          out.m_Stage = s;
          break;
        }
      }
      if(out.m_Stage < 0)
      {
        why = Fmt("'%s' is not a shader stage", want.c_str());
        return false;
      }
    }
    else if(arg == "--all")
      out.m_bAll = true;
    else if(!arg.empty() && arg[0] == '-')
    {
      why =
          Fmt("unknown option '%s' (`watch <name> [--since A] [--until B] [--max-events N] "
              "[--stage <stage>] [--all]`)",
              arg.c_str());
      return false;
    }
    else if(out.m_Name.empty())
      out.m_Name = arg;
    else
    {
      why = Fmt("one name at a time: '%s' and '%s' both look like one", out.m_Name.c_str(),
                arg.c_str());
      return false;
    }
  }
  if(out.m_Name.empty())
  {
    why = "watch needs a name: `watch <rdc> Light.intensity` (a member path, or a bare member)";
    return false;
  }
  return true;
}
}    // namespace

int CmdWatch(IReplayController *ctrl, ICaptureFile *file, const char *path,
             const std::vector<std::string> &args)
{
  WatchArgs opts;
  std::string why;
  if(!ParseWatchArgs(args, opts, why))
    return Fail(2, "%s", why.c_str());

  const int lastEvent = LastEventId(ctrl);
  int until = opts.m_Until > 0 ? opts.m_Until : lastEvent;
  if(until <= 0)
    return Fail(1, "cannot tell how far the frame goes (no action list came back): pass `--until`");

  PrintCaptureHeader(file, path);
  Field("name", opts.m_Name);
  Field("since", (long long)opts.m_Since);
  Field("until", (long long)until);
  ArrayOpen("events");

  //: One row per (stage, block, path) *change*, which is the whole point: a value that holds for 900
  //: events prints once. `--all` prints every event instead, for a reader who wants the sampling.
  std::map<std::string, std::string> seen;
  std::vector<std::string> rows;
  int scanned = 0, withState = 0, blocksRead = 0, matched = 0, changes = 0;
  bool bTruncated = false;

  Progress bar;
  bar.Begin("watch: sweep", (size_t)(until - opts.m_Since + 1));
  for(int eid = opts.m_Since; eid <= until; eid++)
  {
    if(opts.m_MaxEvents > 0 && scanned >= opts.m_MaxEvents)
    {
      bTruncated = true;
      break;
    }
    scanned++;
    bar.Tick((size_t)scanned);
    MoveToEvent(ctrl, eid);
    const D3D12Pipe::State *d3d12 = ctrl->GetD3D12PipelineState();
    if(d3d12 == NULL)
      continue;

    bool bAny = false;
    for(int s = 0; s < (int)ShaderStage::Count; s++)
    {
      if(opts.m_Stage >= 0 && s != opts.m_Stage)
        continue;
      const ShaderStage stage = (ShaderStage)s;
      const D3D12Pipe::Shader *sh = StageShader(d3d12, stage);
      if(sh == NULL || sh->resourceId == ResourceId::Null())
        continue;
      const ShaderReflection *refl = BoundReflection(ctrl, d3d12, stage, sh);
      if(refl == NULL)
        continue;
      bAny = true;

      for(size_t b = 0; b < refl->constantBlocks.size() && b < 64; b++)
      {
        BlockValues block;
        BlockRead(ctrl, d3d12, refl, stage, sh, (int)b, block);
        blocksRead++;

        std::vector<Watched> found;
        CollectWatched(block.m_Variables, std::string(), opts.m_Name, found);
        for(size_t i = 0; i < found.size(); i++)
        {
          matched++;
          const std::string key = Fmt("%s/%d/%s", StageName(stage), (int)b, found[i].m_Path.c_str());
          const std::string &was = seen[key];
          const bool bChanged = seen.find(key) == seen.end() || was != found[i].m_Value;
          seen[key] = found[i].m_Value;
          if(!bChanged && !opts.m_bAll)
            continue;
          changes += bChanged ? 1 : 0;
          rows.push_back(Fmt("eid %-7d %-3s b%-2d %-42s = %s", eid, StageName(stage), (int)b,
                             found[i].m_Path.c_str(), found[i].m_Value.c_str()));
        }
      }
    }
    if(bAny)
      withState++;
  }
  bar.Done((size_t)scanned);

  for(size_t i = 0; i < rows.size(); i++)
    Row(rows[i]);
  ArrayClose(false);    // the counters follow

  // What the run could *not* do is part of its answer: a name that matched nothing is a fact about
  // the question (or about a frame whose blocks were never read), and not the same as "the value
  // never changed".
  const bool bFound = matched > 0;
  g_Indent = g_bJson ? 1 : 0;
  Field("scanned", (long long)scanned);
  Field("withState", (long long)withState);
  Field("blocksRead", (long long)blocksRead);
  Field("valuesMatched", (long long)matched);
  Field("changes", (long long)changes);
  Flag("truncated", bTruncated);
  Flag("found", bFound, true);
  g_Indent = 0;
  if(g_bJson)
    printf("}\n");

  if(!bFound)
    Log("watch: no variable or member named '%s' in any constant block of the %d event(s) with "
        "state "
        "in %d..%d -- `shaders <rdc> <eid>` lists the blocks a shader declares, and `cb <rdc> "
        "<eid> "
        "<stage> <slot>` prints one block's names",
        opts.m_Name.c_str(), withState, opts.m_Since, until);
  return 0;
}
