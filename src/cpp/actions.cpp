// z.actions — the capture's action tree: which events are calls, and which are dispatches
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

//: An action's kind comes from the engine's own `ActionFlags` (`Dispatch`, `Drawcall`,
//: `PushMarker`, ...), the way its name comes from `ActionDescription::GetName`. Nothing here
//: classifies by *name* any more: the old text test (`"Draw"` at the head of the chunk name)
//: existed only for the structured-file walk, which this file no longer does -- and the structured
//: file's *numbering* is not the engine's either (its chunks are numbered by index, and on `PC
//: Renderer.rdc` those run to millions where the event ids run to 2132).
//:
//: Whether an action opens a marker: `PushMarker` and `SetMarker` both start one; `PopMarker`
//: closes it and carries no name of its own.
bool IsMarkerPush(ActionFlags flags)
{
  return (flags & (ActionFlags::PushMarker | ActionFlags::SetMarker)) != ActionFlags::NoFlags;
}

bool IsCallFlags(ActionFlags flags)
{
  return (flags & (ActionFlags::Drawcall | ActionFlags::Dispatch | ActionFlags::MeshDispatch |
                   ActionFlags::Copy | ActionFlags::Resolve | ActionFlags::Clear |
                   ActionFlags::Present | ActionFlags::DispatchRay | ActionFlags::BuildAccStruct |
                   ActionFlags::GenMips)) != ActionFlags::NoFlags;
}

void CollectActionNodes(const rdcarray<ActionDescription> &actions, const SDFile &file,
                        const rdcstr &prefix, int depth, std::vector<ActionNode> &rows,
                        bool &bTruncated)
{
  if(depth >= kMaxActionDepth)
  {
    bTruncated = true;
    return;
  }
  for(size_t i = 0; i < actions.size(); i++)
  {
    const ActionDescription &action = actions[i];
    const bool bPush = IsMarkerPush(action.flags);

    ActionNode node;
    node.m_Eid = (int)action.eventId;
    node.m_Depth = depth;
    node.m_bCall = IsCallFlags(action.flags);
    node.m_bMarker = bPush;
    node.m_Name = action.GetName(file);
    node.m_Path = prefix;
    rows.push_back(node);

    // A push becomes part of the path for everything below it; the marker's own row keeps the path
    // it opens *from*, which is what makes "this marker sits inside that one" readable.
    rdcstr nested = prefix;
    if(bPush)
    {
      if(!nested.empty())
        nested += " > ";
      nested += node.m_Name;
    }
    CollectActionNodes(action.children, file, nested, depth + (bPush ? 1 : 0), rows, bTruncated);
  }
}

std::vector<ActionNode> ActionTree(IReplayController *ctrl, int &calls, bool &bTruncated)
{
  const SDFile &file = ctrl->GetStructuredFile();
  std::vector<ActionNode> rows;
  bTruncated = false;
  CollectActionNodes(ctrl->GetRootActions(), file, rdcstr(), 0, rows, bTruncated);
  calls = 0;
  for(size_t i = 0; i < rows.size(); i++)
  {
    if(rows[i].m_bCall)
      calls++;
  }
  return rows;
}

std::map<int, std::string> MarkerPaths(IReplayController *ctrl)
{
  // The action tree is a property of the *capture*, not of the event: walking it once and keeping
  // the result is what stops a bundle, which asks for the path of every event it writes (`state`,
  // `shaders` and each `cb`), from walking the same 560 actions 440 times. Measured: 200 s of a 350
  // s dump. The driver opens one capture per process, so a cache that lives for the process cannot
  // go stale.
  static std::map<int, std::string> gs_CachedMarkerPaths;
  static bool gs_bWalkedMarkers = false;
  if(gs_bWalkedMarkers)
    return gs_CachedMarkerPaths;

  int ignored = 0;
  bool bIgnoredTruncated = false;
  const std::vector<ActionNode> rows = ActionTree(ctrl, ignored, bIgnoredTruncated);
  std::map<int, std::string> paths;
  for(size_t i = 0; i < rows.size(); i++)
  {
    rdcstr path = rows[i].m_Path;
    if(rows[i].m_bMarker)
    {
      if(!path.empty())
        path += " > ";
      path += rows[i].m_Name;
    }
    if(!path.empty())
      paths[rows[i].m_Eid] = std::string(path.c_str());
  }
  gs_CachedMarkerPaths = paths;
  gs_bWalkedMarkers = true;
  return gs_CachedMarkerPaths;
}

std::string MarkerPathAt(IReplayController *ctrl, int eid)
{
  const std::map<int, std::string> paths = MarkerPaths(ctrl);
  const std::map<int, std::string>::const_iterator found = paths.find(eid);
  return found == paths.end() ? std::string() : found->second;
}

//: Whether each event id is a *dispatch* rather than a draw, from the engine's own action list.
//:
//: Two wrong ways to get this, both measured. The bound shaders: `PC Renderer.rdc` has a compute
//: shader bound at every one of its 2132 events, so "a cs is bound" called a frame of draws compute
//: -- which is what this field did before, and the offline tool then grouped draws into compute
//: passes. The structured file's own numbering: the action tree numbers its objects by *chunk
//: index*, and on that capture those run to millions while the engine's event ids run to 2132 --
//: two numberings that never meet, so a map built from them matched nothing.
//: `ActionDescription::eventId` is the id `SetFrameEvent` takes, and `flags` is the engine's own
//: classification, so nothing here is inferred from a name or a binding.
//:
//: Only the *calls* go into the map. An event the list does not name -- a state setter, a marker, a
//: barrier -- takes the kind of the call it follows, which is what the caller does with this: a
//: root-parameter change between two draws belongs to the pass those draws are in, and classifying
//: it on its own would cut a graphics run in two. Mesh dispatches are `Drawcall`-side on purpose:
//: they render to targets like a draw.
void CollectDispatchKinds(const rdcarray<ActionDescription> &actions, std::map<int, bool> &kinds)
{
  for(size_t i = 0; i < actions.size(); i++)
  {
    const ActionDescription &action = actions[i];
    if((action.flags & (ActionFlags::Dispatch | ActionFlags::DispatchRay |
                        ActionFlags::BuildAccStruct)) != ActionFlags::NoFlags)
      kinds[(int)action.eventId] = true;
    else if((action.flags & (ActionFlags::Drawcall | ActionFlags::MeshDispatch)) !=
            ActionFlags::NoFlags)
      kinds[(int)action.eventId] = false;
    CollectDispatchKinds(action.children, kinds);
  }
}

std::map<int, bool> DispatchByEid(IReplayController *ctrl, int &calls)
{
  std::map<int, bool> kinds;
  CollectDispatchKinds(ctrl->GetRootActions(), kinds);
  calls = (int)kinds.size();
  return kinds;
}

//: Recursive half of `LastEventId`: the maximum `ActionDescription::eventId` in the tree. Events
//: are numbered in the order the capture recorded them, so the maximum is the last one -- and the
//: last one is always the "End of Capture" action every driver appends while loading (an
//: `AddEvent()` and `AddAction()` pair per driver; d3d12_commands.cpp has D3D12's), which is what
//: makes a *maximum over actions* the frame's event boundary rather than a guess that non-action
//: events could sit past.
void CollectLastEventId(const rdcarray<ActionDescription> &actions, int &last)
{
  for(size_t i = 0; i < actions.size(); i++)
  {
    if((int)actions[i].eventId > last)
      last = (int)actions[i].eventId;
    CollectLastEventId(actions[i].children, last);
  }
}

int LastEventId(IReplayController *ctrl)
{
  // `GetRootActions` is a read of the list built while the capture was loaded (replay_controller.cpp
  // returns `m_FrameRecord.actionList`), so asking this before anything has replayed is free and
  // leaves the engine exactly as it was -- the property the id sweep depends on.
  int last = 0;
  CollectLastEventId(ctrl->GetRootActions(), last);
  return last;
}

//: `text` with its ASCII letters lowercased, for the case-insensitive halves of a search. A local
//: helper rather than a locale call: what is being matched is the engine's names, which are ASCII.
std::string LowerAscii(std::string_view text)
{
  std::string out(text);
  for(size_t i = 0; i < out.size(); i++)
  {
    const char c = out[i];
    if(c >= 'A' && c <= 'Z')
      out[i] = (char)(c - 'A' + 'a');
  }
  return out;
}

namespace
{
//: True when `want` is one component of the path, case-insensitively: paths are `A > B > C`.
bool ComponentEquals(const std::string &pathLower, const std::string &wantLower)
{
  size_t start = 0;
  for(;;)
  {
    const size_t end = pathLower.find(" > ", start);
    const std::string component =
        pathLower.substr(start, end == std::string::npos ? std::string::npos : end - start);
    if(component == wantLower)
      return true;
    if(end == std::string::npos)
      return false;
    start = end + 3;
  }
}
}    // namespace

int ResolveMarkerPath(IReplayController *ctrl, const char *text, std::string &matched)
{
  matched.clear();
  const std::string want(text == NULL ? "" : text);
  if(want.empty())
    return -1;
  const std::string wantLower = LowerAscii(want);

  std::map<int, std::string> paths = MarkerPaths(ctrl);

  // Three passes over the same table, strongest match first, so a full path can never be beaten by
  // a component that happens to say the same thing, and a component never by a substring.
  for(std::map<int, std::string>::const_iterator it = paths.begin(); it != paths.end(); ++it)
  {
    if(it->second == want)
    {
      matched = it->second;
      return it->first;
    }
  }
  for(std::map<int, std::string>::const_iterator it = paths.begin(); it != paths.end(); ++it)
  {
    if(LowerAscii(it->second) == wantLower)
    {
      matched = it->second;
      return it->first;
    }
  }
  for(std::map<int, std::string>::const_iterator it = paths.begin(); it != paths.end(); ++it)
  {
    if(ComponentEquals(LowerAscii(it->second), wantLower))
    {
      matched = it->second;
      return it->first;
    }
  }
  for(std::map<int, std::string>::const_iterator it = paths.begin(); it != paths.end(); ++it)
  {
    if(LowerAscii(it->second).find(wantLower) != std::string::npos)
    {
      matched = it->second;
      return it->first;
    }
  }
  return -1;
}

// --------------------------------------------------------------------------- value formatting

//: How deep a struct-of-structs is expanded before the rest is elided. The tree comes from the
