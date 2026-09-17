// z.actions — the capture's action tree: which events are calls, and which are dispatches
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

//: An action's kind comes from the engine's own `ActionFlags` (`Dispatch`, `Drawcall`, `PushMarker`, ...), the
//: way its name comes from `ActionDescription::GetName`. Nothing here classifies by *name* any more: the old
//: text test (`"Draw"` at the head of the chunk name) existed only for the structured-file walk, which this
//: file no longer does -- and the structured file's *numbering* is not the engine's either (its chunks are
//: numbered by index, and on `PC Renderer.rdc` those run to millions where the event ids run to 2132).
//:
//: Whether an action opens a marker: `PushMarker` and `SetMarker` both start one; `PopMarker` closes it and
//: carries no name of its own.
bool IsMarkerPush(ActionFlags flags)
{
  return (flags & (ActionFlags::PushMarker | ActionFlags::SetMarker)) != ActionFlags::NoFlags;
}

bool IsCallFlags(ActionFlags flags)
{
  return (flags & (ActionFlags::Drawcall | ActionFlags::Dispatch | ActionFlags::MeshDispatch |
                   ActionFlags::Copy | ActionFlags::Resolve | ActionFlags::Clear | ActionFlags::Present |
                   ActionFlags::DispatchRay | ActionFlags::BuildAccStruct | ActionFlags::GenMips)) !=
         ActionFlags::NoFlags;
}

void CollectActionNodes(const rdcarray<ActionDescription> &actions, const SDFile &file, const rdcstr &prefix,
                        int depth, std::vector<ActionNode> &rows, bool &truncated)
{
  if(depth >= kMaxActionDepth)
  {
    truncated = true;
    return;
  }
  for(size_t i = 0; i < actions.size(); i++)
  {
    const ActionDescription &action = actions[i];
    const bool push = IsMarkerPush(action.flags);

    ActionNode node;
    node.eid = (int)action.eventId;
    node.depth = depth;
    node.call = IsCallFlags(action.flags);
    node.marker = push;
    node.name = action.GetName(file);
    node.path = prefix;
    rows.push_back(node);

    // A push becomes part of the path for everything below it; the marker's own row keeps the path it opens
    // *from*, which is what makes "this marker sits inside that one" readable.
    rdcstr nested = prefix;
    if(push)
    {
      if(!nested.empty())
        nested += " > ";
      nested += node.name;
    }
    CollectActionNodes(action.children, file, nested, depth + (push ? 1 : 0), rows, truncated);
  }
}

std::vector<ActionNode> ActionTree(IReplayController *ctrl, int &calls, bool &truncated)
{
  const SDFile &file = ctrl->GetStructuredFile();
  std::vector<ActionNode> rows;
  truncated = false;
  CollectActionNodes(ctrl->GetRootActions(), file, rdcstr(), 0, rows, truncated);
  calls = 0;
  for(size_t i = 0; i < rows.size(); i++)
  {
    if(rows[i].call)
      calls++;
  }
  return rows;
}

std::map<int, std::string> MarkerPaths(IReplayController *ctrl)
{
  // The action tree is a property of the *capture*, not of the event: walking it once and keeping the result
  // is what stops a bundle, which asks for the path of every event it writes (`state`, `shaders` and each
  // `cb`), from walking the same 560 actions 440 times. Measured: 200 s of a 350 s dump. The driver opens one
  // capture per process, so a cache that lives for the process cannot go stale.
  static std::map<int, std::string> cached;
  static bool walked = false;
  if(walked)
    return cached;

  int ignored = 0;
  bool ignored_truncated = false;
  const std::vector<ActionNode> rows = ActionTree(ctrl, ignored, ignored_truncated);
  std::map<int, std::string> paths;
  for(size_t i = 0; i < rows.size(); i++)
  {
    rdcstr path = rows[i].path;
    if(rows[i].marker)
    {
      if(!path.empty())
        path += " > ";
      path += rows[i].name;
    }
    if(!path.empty())
      paths[rows[i].eid] = std::string(path.c_str());
  }
  cached = paths;
  walked = true;
  return cached;
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

// --------------------------------------------------------------------------- value formatting

//: How deep a struct-of-structs is expanded before the rest is elided. The tree comes from the
