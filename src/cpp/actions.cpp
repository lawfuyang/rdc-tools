// z.actions — the capture's action tree: which events are calls, and which are dispatches
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

//: One chunk of the structured file. `eid` is the depth-first index over *chunks* (parameters are
//: descended through without numbering), which is exactly the event id `SetFrameEvent` takes and the
//: UI shows -- and it is the same number the offline tool prints as its chunk index.
//: Whether a chunk is an *event*. RenderDoc's event ids number what the command list recorded --
//: draws, dispatches, copies, markers -- while device-level calls (resource and PSO creation,
//: `SetName`, descriptor writes) are in the structured file but are not events. So the event id is
//: **not** the chunk index the offline tool prints: on one capture those agreed (its command-list
//: chunks start early), on another they were off by tens of thousands. `probe` shows which ids the
//: engine really has.
bool IsAction(const rdcstr &name)
{
  return name.beginsWith("ID3D12GraphicsCommandList") || name.beginsWith("ID3D12CommandList") ||
         name.beginsWith("ID3D12VideoCommandList") ||
         name.beginsWith("ID3D12VideoEncodeCommandList");
}

//: Walks one object and everything below it, numbering every *structured-data object* -- chunks and
//: their parameters alike -- which is what makes the ids line up with the engine's (`probe` is how
//: that was established). `truncated` records whether the depth cap was ever reached, so a command
//: can say so rather than present a partial tree as the whole one.
void Flatten(const SDObject *obj, int depth, int &next, std::vector<ActionRow> &rows, bool &truncated)
{
  const int id = next++;
  if(obj->type.basetype == SDBasic::Chunk)
  {
    // The tag says this object is a chunk; the cast says which kind. `static_cast` rather than a
    // C-style cast, so only the derived-to-base relationship can be involved ([expr.cast]).
    const SDChunk *chunk = static_cast<const SDChunk *>(obj);
    ActionRow row;
    row.eid = IsAction(chunk->name) ? id : 0;
    row.depth = depth;
    row.name = chunk->name;
    row.chunkID = chunk->metadata.chunkID;
    rows.push_back(row);
    depth++;
  }

  if(depth >= kMaxTreeDepth)
  {
    truncated = true;
    return;
  }

  const size_t children = obj->NumChildren();
  for(size_t i = 0; i < children; i++)
    Flatten(obj->GetChild(i), depth, next, rows, truncated);
}

//: A draw, dispatch or copy: what a frame is *read* through, as opposed to the state and marker
//: chunks that also carry an event id.
bool IsCall(const rdcstr &name)
{
  const char *n = strstr(name.c_str(), "::");
  n = (n != NULL) ? n + 2 : name.c_str();
  return strncmp(n, "Draw", 4) == 0 || strncmp(n, "Dispatch", 8) == 0 ||
         strncmp(n, "ExecuteIndirect", 15) == 0 || strncmp(n, "Copy", 4) == 0 ||
         strncmp(n, "Clear", 5) == 0 || strncmp(n, "Present", 7) == 0 ||
         strncmp(n, "ResolveSubresource", 18) == 0 || strncmp(n, "BeginRenderPass", 15) == 0;
}

std::vector<ActionRow> Actions(IReplayController *ctrl, bool &truncated)
{
  const SDFile &sd = ctrl->GetStructuredFile();
  std::vector<ActionRow> rows;
  int next = 1;
  truncated = false;
  for(size_t i = 0; i < sd.chunks.size(); i++)
    Flatten(sd.chunks[i], 0, next, rows, truncated);
  return rows;
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
