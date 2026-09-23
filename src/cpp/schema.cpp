//: The schema table (REFERENCE §4.12). Written and checked by replay_dump.cpp, which owns the plumbing
//: for errors and the log; this file is the contract itself, so adding a document is a one-file diff.

#include "schema.h"

#include <cstring>

const SchemaDoc kSchemas[] = {
    {"capture", "info, dump (capture.json)", R"sc({
  "title": "capture",
  "description": "A capture's own facts. `info` writes the short form; the bundle's capture.json adds the byte count, the absolute path and the pixel-history flag.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "pipelineType",
               "localRenderer", "remoteReplay", "vendor", "shaderDebugging", "chunks", "resources",
               "textures", "buffers", "debugMessages"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string", "description": "the path as given on the command line"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string", "description": "the capture's API, e.g. D3D12"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "pipelineType": {"type": "integer"},
    "localRenderer": {"type": "integer"},
    "remoteReplay": {"type": "integer"},
    "vendor": {"type": "integer"},
    "shaderDebugging": {"type": "integer"},
    "pixelHistory": {"type": "integer"},
    "chunks": {"type": "integer"},
    "resources": {"type": "integer"},
    "textures": {"type": "integer"},
    "buffers": {"type": "integer"},
    "debugMessages": {"type": "integer"},
    "captureBytes": {"type": "integer"},
    "absPath": {"type": "string"}
  },
  "additionalProperties": false
})sc"},

    {"events", "dump (events.json)", R"sc({
  "title": "events",
  "description": "Every id with bound state, and what was bound. One entry per state change plus the ids the scan was told to include. A state hash repeats when nothing changed, so a consumer can group without re-reading the state files.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "events", "total",
               "scanned", "scanFrom", "scanTo", "scanStopped", "stateFiles"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "events": {"type": "array", "items": {
      "type": "object",
      "required": ["eid", "marker", "pso", "psoKind", "shaders", "targets", "depth", "rootParameters", "state"],
      "properties": {
        "eid": {"type": "integer"},
        "marker": {"type": "string", "description": "the engine's marker path for this event, `A > B`, empty outside every marker"},
        "pso": {"type": "string", "description": "the pipeline state object's resource id"},
        "psoKind": {"enum": ["graphics", "compute"]},
        "shaders": {"type": "string", "description": "`vs=2348 ps=2349`, stages in a fixed order"},
        "targets": {"type": "array", "items": {"type": "string"},
                    "description": "`<id> <w>x<h>x<d> <FORMAT>`, the output-merge state at this id"},
        "depth": {"type": "string", "description": "the depth target's resource id, `0` for none"},
        "rootParameters": {"type": "integer"},
        "state": {"type": "string", "description": "hash of what the state file was written from"},
        "volume": {
          "type": "object",
          "description": "what the call asked the GPU to do, from the engine's action list; absent when the event is not a call, so a reader can tell `asked for nothing` from `not a call`. What is inside depends on `psoKind`: a **draw** has `vertices` (its index count, or its vertex count when the draw is not indexed), `instances`, and `triangles` -- `0` when the topology does not fix one (a patch list, a meshlet list); a **dispatch** has `groups` (workgroups), `threadsPerGroup` (the call's own override, else the bound shader's `[numthreads]`) and `threads` (`groups` x `threadsPerGroup`, `0` when neither published a size)",
          "properties": {
            "vertices": {"type": "integer"},
            "instances": {"type": "integer"},
            "triangles": {"type": "integer"},
            "groups": {"type": "array", "items": {"type": "integer"}},
            "threadsPerGroup": {"type": "array", "items": {"type": "integer"}},
            "threads": {"type": "integer"}
          },
          "additionalProperties": false
        }
      },
      "additionalProperties": false
    }},
    "total": {"type": "integer"},
    "scanned": {"type": "integer"},
    "scanFrom": {"type": "integer"},
    "scanTo": {"type": "integer"},
    "scanStopped": {"type": "string", "description": "why the sweep stopped, empty when it reached the end"},
    "stateFiles": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"resources", "dump (resources.json)", R"sc({
  "title": "resources",
  "description": "Every resource the engine knows, with the usage list it was gathered from. A texture carries its format and dimensions, a buffer its size in bytes; `usage` is what makes \"who touched this\" answerable.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "resources", "total"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "resources": {"type": "array", "items": {
      "type": "object",
      "required": ["resource", "name", "kind"],
      "properties": {
        "resource": {"type": "string"},
        "name": {"type": "string", "description": "the application's name, empty when it has none"},
        "kind": {"enum": ["texture", "buffer", "other"]},
        "format": {"type": "string"},
        "dimension": {"type": "integer"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "depth": {"type": "integer"},
        "mips": {"type": "integer"},
        "arraySize": {"type": "integer"},
        "samples": {"type": "integer"},
        "bytes": {"type": "integer"},
        "usage": {"type": ["array", "string"], "items": {
          "type": "object",
          "required": ["eid", "usage"],
          "properties": {"eid": {"type": "integer"}, "usage": {"type": "integer"}},
          "additionalProperties": false
        }, "description": "every event that touched this resource; a short string instead when the bundle was written with --no-usage, and the manifest's resourceUsage says why"},
        "usageCount": {"type": "integer"},
        "firstEvent": {"type": "integer", "description": "absent with --no-usage, like `usage`"},
        "lastEvent": {"type": "integer"}
      },
      "additionalProperties": false
    }},
    "total": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"manifest", "dump (manifest.json)", R"sc({
  "title": "manifest",
  "description": "What the bundle is and what is in it: the capture it was written from (with its hash), the flags it was written with, every file with its size and SHA-256, and the list of things the bundle deliberately does not contain. This is what a reader checks before trusting the rest.",
  "type": "object",
  "required": ["schemaVersion", "bundleVersion", "driver", "renderdoc", "capture", "captureAbsolute",
               "captureBytes", "captureSha256", "since", "until", "maxEvents", "withImages", "withCounters",
               "withTextures", "resourceUsage", "stateHashInputs", "statesRule", "notInThisBundle", "skipped",
               "files", "fileCount", "fileBytes"],
  "properties": {
    "schemaVersion": {"const": 1},
    "bundleVersion": {"type": "integer", "description": "the layout of the bundle itself"},
    "driver": {"type": "string"},
    "renderdoc": {"type": "string"},
    "capture": {"type": "string"},
    "captureAbsolute": {"type": "string"},
    "captureBytes": {"type": "integer"},
    "captureSha256": {"type": "string"},
    "since": {"type": "integer"},
    "until": {"type": "integer"},
    "maxEvents": {"type": "integer"},
    "withImages": {"type": "integer"},
    "withCounters": {"type": "integer"},
    "withTextures": {"type": "integer"},
    "resourceUsage": {"type": "string", "description": "`collected`, or why the usage lists are absent"},
    "stateHashInputs": {"type": "string", "description": "what the events' state hash is computed from"},
    "statesRule": {"type": "string", "description": "when a state file is written"},
    "notInThisBundle": {"type": "array", "items": {
      "type": "object",
      "required": ["what", "why"],
      "properties": {"what": {"type": "string"}, "why": {"type": "string"}},
      "additionalProperties": false
    }},
    "skipped": {"type": "array", "items": {"type": "string"}},
    "files": {"type": "array", "items": {
      "type": "object",
      "required": ["path", "bytes", "sha256"],
      "properties": {"path": {"type": "string"}, "bytes": {"type": "integer"}, "sha256": {"type": "string"}},
      "additionalProperties": false
    }},
    "fileCount": {"type": "integer"},
    "fileBytes": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"state", "state, dump (states/<eid>.state.json)", R"sc({
  "title": "state",
  "description": "One event's bound state: the capture header, then that event. The arrays are the driver's own rows, which are text by design -- they carry the engine's names verbatim.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "marker",
               "api", "shaders", "renderTargets", "depthTarget", "rootSignature", "rootParameters"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "marker": {"type": "string", "description": "the engine's marker path for this event, `A > B`, empty outside every marker"},
    "api": {"type": "integer"},
    "shaders": {"type": "array", "items": {"type": "string"},
                "description": "`vs  res2348`, one row per bound stage -- including the stages this call kind does not use"},
    "renderTargets": {"type": "array", "items": {"type": "string"}},
    "depthTarget": {"type": "string", "description": "a resource id, `0` for none"},
    "rootSignature": {"type": "string"},
    "rootParameters": {"type": "array", "items": {"type": "string"}},
    "viewports": {"type": "array", "items": {
      "type": "object",
      "required": ["x", "y", "width", "height", "minDepth", "maxDepth", "enabled"],
      "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "width": {"type": "number"},
                     "height": {"type": "number"}, "minDepth": {"type": "number"},
                     "maxDepth": {"type": "number"}, "enabled": {"type": "boolean"}},
      "additionalProperties": false},
      "description": "the bound viewports, in the engine's own terms; absent from a bundle written before the pipeline-state rows existed (the offline rules that read them say so rather than reporting clean)"},
    "scissors": {"type": "array", "items": {
      "type": "object",
      "required": ["x", "y", "width", "height", "enabled"],
      "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer"},
                     "height": {"type": "integer"}, "enabled": {"type": "boolean"}},
      "additionalProperties": false}},
    "outputMerger": {"type": "object",
      "required": ["depthEnable", "depthWrites", "depthFunction", "stencilEnable", "frontFace", "backFace",
                   "blends"],
      "properties": {
        "depthEnable": {"type": "boolean"},
        "depthWrites": {"type": "boolean"},
        "depthFunction": {"type": "string", "description": "the engine's `CompareFunction` name, e.g. LessEqual"},
        "stencilEnable": {"type": "boolean"},
        "stencilReadOnly": {"type": "boolean"},
        "alphaToCoverage": {"type": "boolean"},
        "independentBlend": {"type": "boolean"},
        "frontFace": {"type": "object", "description": "fail/depthFail/pass operations, the compare function and the two masks"},
        "backFace": {"type": "object"},
        "blends": {"type": "array", "items": {"type": "object", "description": "one per render target: enabled, writeMask, the two operations and their four multipliers"}}
      },
      "additionalProperties": false}
  },
  "additionalProperties": false
})sc"},

    {"shaders", "shaders, dump (states/<eid>.shaders.json)", R"sc({
  "title": "shaders",
  "description": "The reflection of every stage bound at one event, one object per stage -- an array, because two stages share every member name and a flat object would let a reader keep only the last.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "marker",
               "stages"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "marker": {"type": "string", "description": "the engine's marker path for this event, `A > B`, empty outside every marker"},
    "eid": {"type": "integer"},
    "stages": {"type": "array", "items": {
      "type": "object",
      "required": ["stage", "resource", "entry", "encoding", "bytes", "hash", "constantBlocks",
                   "readOnlyResources", "readWriteResources", "inputSignature", "outputSignature"],
      "properties": {
        "stage": {"type": "string", "description": "vs hs ds gs ps cs as ms"},
        "resource": {"type": "string"},
        "entry": {"type": "string"},
        "encoding": {"type": "integer"},
        "bytes": {"type": "integer"},
        "hash": {"type": "string", "description": "SHA-256 of the shader's bytes, 64 lowercase hex characters: the shader's identity, where `bytes` is only its size (empty when the OS had no SHA-256 provider)"},
        "constantBlocks": {"type": "array", "items": {"type": "string"}},
        "readOnlyResources": {"type": "array", "items": {"type": "string"}},
        "readWriteResources": {"type": "array", "items": {"type": "string"}},
        "inputSignature": {"type": "array", "items": {"type": "string"}},
        "outputSignature": {"type": "array", "items": {"type": "string"}}
      },
      "additionalProperties": false
    }}
  },
  "additionalProperties": false
})sc"},

    {"messages", "debug, dump (messages.json)", R"sc({
  "title": "messages",
  "description": "The engine's own messages, one row each (`eid <n>  <severity>  <text>`), plus the header. Rows are strings: they are the same text the terminal prints, so nothing is lost between the two forms. With `debug --group` a row is one *distinct* message (`eid <first>..<last>  <n>x  severity(n) category(n) source(n) id(n)  <text>`) and `total` counts the groups rather than the messages.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "messages", "total"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "messages": {"type": "array", "items": {"type": "string"}},
    "total": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"counters", "counters, dump (counters.json)", R"sc({
  "title": "counters",
  "description": "The driver's counter results, one row each (`eid <n>  <name> = <value>`), plus the header. Only written when the bundle was asked for them and the driver supports them.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "counters", "total"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "counters": {"type": "array", "items": {"type": "string"}},
    "total": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"counters-passes", "counters --per-pass", R"sc({
  "title": "counters-passes",
  "description": "One counter folded over each pass. `FetchCounters` answers per event and takes no range, so the per-event results are summed here between a pass's first and last event id; `measured` is how many of the pass's events the counter produced a value for, and `peak` is the largest single one. Which counter is the cost is the engine's choice (`EventGPUDuration` when this replay produced one) and it is named in `costCounter` with its `unit`, because a column headed `counter(7)` says nothing. A replay with no counter results carries `available` 0 and a `note` instead of a table of zeros: GPU counters are a driver feature and are not available everywhere.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "mode",
               "costCounter", "unit", "passesFrom", "available", "passes", "top", "topCount"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "mode": {"const": "per-pass"},
    "costCounter": {"type": "string", "description": "the counter the cost column folds, named by the engine when it offers a name"},
    "unit": {"type": "string", "description": "the engine's own unit for that counter, empty when the value is absolute"},
    "passesFrom": {"type": "string", "description": "where the pass list came from: `the frame's markers`, or the `--passes` file"},
    "available": {"type": "integer", "description": "how many counter results this frame produced; 0 means nothing was measured"},
    "passes": {"type": "array", "items": {
      "type": "object",
      "required": ["pass", "name", "firstEid", "lastEid", "events", "measured", "cost", "peak"],
      "properties": {
        "pass": {"type": "integer", "description": "1-based, matching the terminal's `pass 1`"},
        "name": {"type": "string", "description": "the marker path the pass's calls share, empty outside every marker"},
        "firstEid": {"type": "integer"},
        "lastEid": {"type": "integer"},
        "events": {"type": "integer", "description": "calls in the pass, from the action tree"},
        "measured": {"type": "integer", "description": "of those, how many produced a counter value"},
        "cost": {"type": "string", "description": "the counter summed over the pass, as text (the writer has no fractional field)"},
        "peak": {"type": "string", "description": "the largest single value in the pass"}
      },
      "additionalProperties": false
    }},
    "top": {"type": "array", "items": {
      "type": "object",
      "required": ["pass", "name", "cost"],
      "properties": {
        "pass": {"type": "integer", "description": "the 1-based pass number, an index into `passes`"},
        "name": {"type": "string"},
        "cost": {"type": "string"}
      },
      "additionalProperties": false
    }, "description": "the dearest passes, `topCount` of them, most expensive first"},
    "topCount": {"type": "integer"},
    "note": {"type": "string", "description": "why no cost could be folded, written when `available` is 0"}
  },
  "additionalProperties": false
})sc"},

    {"crosscheck", "crosscheck", R"sc({
  "title": "crosscheck",
  "description": "What the reflections say a shader wants, against what the state says it was given: the vertex shader's outputs against the pixel shader's inputs, each stage's bindings against the root signature's declared ranges, and the bound render targets' formats against the pixel shader's outputs. Every row names the event it applies to and quotes both sides, so `state <eid>` and `shaders <eid>` show the same two things. The three `...Checked` counts say how much was actually compared: both sides need shader reflection, so a capture with stripped shaders checks nothing, and an empty `findings` next to three zeros means nothing was checked rather than that the frame is clean.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "from",
               "to", "scanned", "linksChecked", "bindingsChecked", "bindingsUnmapped", "targetsChecked",
               "noRootParameters", "total", "findings", "shown", "stoppedEarly"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer", "description": "the event when one was named on the command line, 0 for a range"},
    "from": {"type": "integer", "description": "first event id checked, inclusive"},
    "to": {"type": "integer", "description": "last event id checked, inclusive"},
    "scanned": {"type": "integer", "description": "events in that range that had shaders bound"},
    "linksChecked": {"type": "integer", "description": "ps inputs matched against the vs outputs"},
    "bindingsChecked": {"type": "integer", "description": "shader bindings matched against the root signature"},
    "bindingsUnmapped": {"type": "integer", "description": "of those, the ones at a register class the root signature declares nothing for. Counted and not reported: a shader reading its resources bindlessly (SM 6.6 `ResourceDescriptorHeap`), or one HLSL binds wider than the code it compiled to uses, lists bindings the root signature need not declare, and that is indistinguishable from a range somebody forgot. Measured on the Unreal captures, where most compute passes are exactly that and reporting them put 4373 rows on a frame with nothing wrong with it (over the every-id sweep this command also replaced; over the frame's 144 calls the same rule is nearer 500). Only a binding *outside* a class the signature does declare is reported -- the `the table is too small` shape, which is decidable."},
    "targetsChecked": {"type": "integer", "description": "bound render targets whose format was compared with the ps output"},
    "noRootParameters": {"type": "integer", "description": "scanned events whose state carries no root parameters at all. Counted and not reported: on the Unreal captures every compute event answers `rootSignature 0`, which is a fact about what the engine reports rather than a defect, and one row per event put a thousand rows of it on a frame."},
    "total": {"type": "integer", "description": "findings, before --max"},
    "findings": {"type": "array", "items": {
      "type": "object",
      "required": ["eid", "marker", "check", "detail"],
      "properties": {
        "eid": {"type": "integer"},
        "marker": {"type": "string", "description": "the engine's marker path at that event, empty outside every marker"},
        "check": {"enum": ["vs-ps-link", "bindings", "rt-format"]},
        "detail": {"type": "string", "description": "what was found, naming both sides"}
      },
      "additionalProperties": false
    }},
    "shown": {"type": "integer", "description": "rows written, capped by --max"},
    "stoppedEarly": {"type": "boolean", "description": "true when --max-events ended the sweep before `to`"}
  },
  "additionalProperties": false
})sc"},

    {"textures", "textures", R"sc({
  "title": "textures",
  "description": "Every texture the engine knows, with the format and dimensions from the resource description.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "textures", "total",
               "shown"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "textures": {"type": "array", "items": {
      "type": "object",
      "required": ["resource", "dimension", "width", "height", "depth", "mips", "arraySize", "samples",
                   "format", "bytes"],
      "properties": {
        "resource": {"type": "string"},
        "dimension": {"type": "integer"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "depth": {"type": "integer"},
        "mips": {"type": "integer"},
        "arraySize": {"type": "integer"},
        "samples": {"type": "integer"},
        "format": {"type": "string"},
        "bytes": {"type": "integer"}
      },
      "additionalProperties": false
    }},
    "total": {"type": "integer"},
    "shown": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"draws", "draws", R"sc({
  "title": "draws",
  "description": "The engine's action list in frame order: markers and calls, with the engine's own event ids (`probe` lists the same ids, and `SetFrameEvent` takes them). The structured file's chunk numbering is *not* used here (REFERENCE §9): the two spaces are different, and the chunk-derived one is not an id the engine answers to.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "actions",
               "totalActions", "totalCalls", "shown"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "actions": {"type": "array", "items": {
      "type": "object",
      "required": ["eid", "depth", "call", "marker", "name", "path"],
      "properties": {
        "eid": {"type": "integer", "description": "ActionDescription::eventId"},
        "depth": {"type": "integer", "description": "how deep in the marker nest"},
        "call": {"type": "boolean", "description": "a draw/dispatch/copy rather than a marker"},
        "marker": {"type": "boolean", "description": "this row opens a marker"},
        "name": {"type": "string", "description": "the marker's custom name, or the call's own name"},
        "path": {"type": "string", "description": "the markers this row sits inside, `A > B`, empty at the root"}
      },
      "additionalProperties": false
    }},
    "totalActions": {"type": "integer"},
    "totalCalls": {"type": "integer"},
    "shown": {"type": "integer"},
    "truncated": {"type": "string", "description": "present only when the action tree was deeper than the recursion limit"}
  },
  "additionalProperties": false
})sc"},

    {"probe", "probe", R"sc({
  "title": "probe",
  "description": "Which ids in a range have pipeline state. Rows are strings (`eid <n>  shaders=.. rootSig=.. params=..`), the same text the terminal prints. `scanned` is how far the range went (the frame's own last event unless a cap was asked for), `lastEvent` is that bound, and `cached` says whether the sweep happened in this run or was read from the probe cache.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "events",
               "scanned", "withState", "lastEvent", "cached"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "events": {"type": "array", "items": {"type": "string"}},
    "scanned": {"type": "integer"},
    "withState": {"type": "integer"},
    "lastEvent": {"type": "integer"},
    "cached": {"type": "boolean"}
  },
  "additionalProperties": false
})sc"},

    {"watch", "watch", R"sc({
  "title": "watch",
  "description": "One reflection member's value at every event of a range: one row per change (`eid <n> <stage> b<slot> <path> = <value>`), plus what the run actually read. `valuesMatched` counts every reading of a matching path, so a range where nothing changed still shows it was read; `found` is false when the name matched nothing at all, which is not the same as 'it never changed'.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "name",
               "since", "until", "events", "scanned", "withState", "blocksRead", "valuesMatched",
               "changes", "truncated", "found"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "name": {"type": "string"},
    "since": {"type": "integer"},
    "until": {"type": "integer"},
    "events": {"type": "array", "items": {"type": "string"}},
    "scanned": {"type": "integer"},
    "withState": {"type": "integer"},
    "blocksRead": {"type": "integer"},
    "valuesMatched": {"type": "integer"},
    "changes": {"type": "integer"},
    "truncated": {"type": "boolean"},
    "found": {"type": "boolean"}
  },
  "additionalProperties": false
})sc"},

    {"cb", "cb", R"sc({
  "title": "cb",
  "description": "One constant buffer at one event: what it is bound to and one row per reflection variable, with the value read from the data.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "marker",
               "stage", "slot", "shader", "buffer", "variables"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "marker": {"type": "string", "description": "the engine's marker path for this event, `A > B`, empty outside every marker"},
    "stage": {"type": "string"},
    "slot": {"type": "integer"},
    "shader": {"type": "string"},
    "buffer": {"type": "string"},
    "variables": {"type": "array", "items": {"type": "string"}}
  },
  "additionalProperties": false
})sc"},

    {"mesh", "mesh", R"sc({
  "title": "mesh",
  "description": "One draw's geometry at one mesh stage: the stream the engine has for it, the vertices, and -- when they can be derived -- the primitive count and the position bounds. `stage` is what the data is (vsin is what the draw read, vsout what the vertex shader wrote), `primitives` is absent with `primitivesNote` in its place when the topology does not fix a count, and `obj`/`objVertices` are the --obj export (an empty `obj` means none was asked for).",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "instance",
               "stage", "topology", "vertexResource", "vertexStride", "vertexBytes", "indexResource",
               "indexBytes", "indexCount", "obj", "objVertices"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "instance": {"type": "integer"},
    "stage": {"type": "string"},
    "topology": {"type": "integer"},
    "vertexResource": {"type": "string"},
    "vertexStride": {"type": "integer"},
    "vertexBytes": {"type": "integer"},
    "indexResource": {"type": "string"},
    "indexBytes": {"type": "integer"},
    "indexCount": {"type": "integer"},
    "baseVertex": {"type": "integer"},
    "vertices": {"type": "array", "items": {"type": "string"}},
    "vertexCount": {"type": "integer"},
    "componentsPerVertex": {"type": "integer"},
    "primitives": {"type": "integer"},
    "primitivesNote": {"type": "string"},
    "boundsMin": {"type": "string"},
    "boundsMax": {"type": "string"},
    "boundsNote": {"type": "string"},
    "obj": {"type": "string"},
    "objVertices": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"image", "image", R"sc({
  "title": "image",
  "description": "One render target saved to a file, and whether the write succeeded: the subresource, the component type it was read as, and the display overlay drawn into it.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "resource",
               "width", "height", "file", "overlay", "mip", "slice", "sample", "cast", "written"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "resource": {"type": "string"},
    "width": {"type": "integer"},
    "height": {"type": "integer"},
    "file": {"type": "string"},
    "overlay": {"type": "string"},
    "mip": {"type": "integer"},
    "slice": {"type": "integer"},
    "sample": {"type": "integer"},
    "cast": {"type": "string"},
    "written": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"formats", "formats", R"sc({
  "title": "formats",
  "description": "The format coverage audit: every format in the frame's texture list, how many resources use it, what it is made of, and whether the engine can make a picture of it. `picture` is 'yes', 'with a cast' or 'no', and `why` carries the reason when it is not a plain yes -- so a format nothing can show is a row rather than a silent skip.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "formats",
               "textures", "bytes", "formatCount", "needCast", "noLayout"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "formats": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["format", "textures", "bytes", "components", "componentBits", "type", "srgb",
                     "blockCompressed", "special", "elementBytes", "picture", "why"],
        "properties": {
          "format": {"type": "string"},
          "textures": {"type": "integer"},
          "bytes": {"type": "integer"},
          "components": {"type": "integer"},
          "componentBits": {"type": "integer"},
          "type": {"type": "string"},
          "srgb": {"type": "integer"},
          "blockCompressed": {"type": "integer"},
          "special": {"type": "integer"},
          "elementBytes": {"type": "integer"},
          "picture": {"type": "string"},
          "why": {"type": "string"}
        },
        "additionalProperties": false
      }
    },
    "textures": {"type": "integer"},
    "bytes": {"type": "integer"},
    "formatCount": {"type": "integer"},
    "needCast": {"type": "integer"},
    "noLayout": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"cubemap", "cubemap", R"sc({
  "title": "cubemap",
  "description": "A cubemap written as six face pictures plus the engine's cruciform: the resource, the directory, the face size, and what was actually written (`faces` is 0..6, `cross` is empty when the cruciform could not be written).",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "resource",
               "name", "directory", "faceSize", "mips", "mip", "format", "faces", "cross"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "resource": {"type": "string"},
    "name": {"type": "string"},
    "directory": {"type": "string"},
    "faceSize": {"type": "integer"},
    "mips": {"type": "integer"},
    "mip": {"type": "integer"},
    "format": {"type": "string"},
    "faces": {"type": "integer"},
    "cross": {"type": "string"}
  },
  "additionalProperties": false
})sc"},

    {"bundle-verify", "bundle-verify", R"sc({
  "title": "bundle-verify",
  "description": "The result of re-hashing a bundle against its manifest: one row per file that is missing, short, long or whose hash differs, and the counts. `problems` is the exit code.",
  "type": "object",
  "required": ["schemaVersion", "manifest", "files", "checked", "problems", "fileCount"],
  "properties": {
    "schemaVersion": {"const": 1},
    "manifest": {"type": "string"},
    "files": {"type": "array", "items": {"type": "string"}},
    "checked": {"type": "integer"},
    "problems": {"type": "integer"},
    "fileCount": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"find", "find", R"sc({
  "title": "find",
  "description": "What the action list and the resource table hold that matches a substring: events whose call name or marker path matches, and resources whose name matches. Case-insensitive, and `where` says which field matched -- `View` hitting a marker and `View` hitting a resource are different answers.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "needle",
               "calls", "events", "resources", "matched"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "needle": {"type": "string", "description": "the substring as given"},
    "calls": {"type": "integer", "description": "how many calls the action list has, matching or not"},
    "events": {"type": "array", "items": {
      "type": "object",
      "required": ["eid", "kind", "name", "where", "marker"],
      "properties": {
        "eid": {"type": "integer", "description": "ActionDescription::eventId"},
        "kind": {"type": "string", "enum": ["call", "marker", "event"]},
        "name": {"type": "string"},
        "where": {"type": "string", "enum": ["name", "marker"]},
        "marker": {"type": "string", "description": "the markers the event sits inside, empty at the root"}
      },
      "additionalProperties": false
    }},
    "resources": {"type": "array", "items": {
      "type": "object",
      "required": ["resource", "name"],
      "properties": {
        "resource": {"type": "string", "description": "e.g. res1234"},
        "name": {"type": "string"}
      },
      "additionalProperties": false
    }},
    "matched": {"type": "integer", "description": "how many events and resources were printed"}
  },
  "additionalProperties": false
})sc"},

    {"statediff", "statediff", R"sc({
  "title": "statediff",
  "description": "One changed field per line between two events' pipeline states. Both sides are read by stepping onto the id from past it, so a difference is about the two events rather than about how far the replay had got (REFERENCE 9). Field names are the ones `state` uses.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eidA",
               "markerA", "eidB", "markerB", "fieldsA", "fieldsB", "changed", "fields"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eidA": {"type": "integer"},
    "markerA": {"type": "string", "description": "the marker path event A sits inside"},
    "eidB": {"type": "integer"},
    "markerB": {"type": "string"},
    "fieldsA": {"type": "integer", "description": "how many fields event A's state carries"},
    "fieldsB": {"type": "integer"},
    "changed": {"type": "integer"},
    "fields": {"type": "array", "items": {
      "type": "object",
      "required": ["field", "a", "b"],
      "properties": {
        "field": {"type": "string", "description": "the same key `state` writes, e.g. rootParameters.3"},
        "a": {"type": "string", "description": "event A's value, or `(absent)` when only B has the field"},
        "b": {"type": "string"}
      },
      "additionalProperties": false
    }}
  },
  "additionalProperties": false
})sc"},

    {"buffer", "buffer", R"sc({
  "title": "buffer",
  "description": "A buffer's contents, read through the engine at the current event. `values` holds the rows the terminal prints: one hex string for `hex` and `ascii`, one number per element for `u32` and `f32`.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "resource",
               "name", "length", "offset", "read", "as", "values"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "resource": {"type": "string", "description": "e.g. res1234"},
    "name": {"type": "string", "description": "the capture's own name for it, or the id"},
    "length": {"type": "integer", "description": "the buffer's size in bytes"},
    "offset": {"type": "integer", "description": "where the read started"},
    "read": {"type": "integer", "description": "how many bytes the engine returned"},
    "as": {"type": "string", "enum": ["hex", "u32", "f32", "ascii"]},
    "values": {"type": "array", "items": {"type": "string"}}
  },
  "additionalProperties": false
})sc"},

    {"sheet", "sheet", R"sc({
  "title": "sheet",
  "description": "One image per pass of the frame, a montage of them, and an index that names each tile. The image is taken at the pass's last call, from the first bound render target of that event. `--list` writes nothing and stops after `shown`, so the last three members are absent there.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "outDir",
               "passesInFrame", "every", "max", "tile", "shown", "passes"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "outDir": {"type": "string", "description": "where the images went, empty for --list"},
    "passesInFrame": {"type": "integer", "description": "markers containing at least one call"},
    "every": {"type": "integer"},
    "max": {"type": "integer"},
    "tile": {"type": "integer", "description": "thumbnail width in the montage"},
    "shown": {"type": "integer", "description": "passes this run selected"},
    "skipped": {"type": "integer", "description": "passes whose target could not be read or written"},
    "montage": {"type": "string", "description": "the montage's path, empty when no tile was readable"},
    "index": {"type": "string", "description": "the Markdown index's path"},
    "passes": {"type": "array", "items": {
      "type": "object",
      "required": ["markerEid", "imageEid", "depth", "calls", "name", "path"],
      "properties": {
        "markerEid": {"type": "integer", "description": "the marker's own event id"},
        "imageEid": {"type": "integer", "description": "the last call inside it: where the image is from"},
        "depth": {"type": "integer"},
        "calls": {"type": "integer"},
        "name": {"type": "string"},
        "path": {"type": "string", "description": "the full `A > B` marker path"},
        "file": {"type": "string", "description": "absent when this pass has no image"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "hash": {"type": "string", "description": "16 hex digits: the image's difference hash"},
        "note": {"type": "string", "description": "why this pass has no image, or that the image is the depth target because no colour target was bound"}
      },
      "additionalProperties": false
    }}
  },
  "additionalProperties": false
})sc"},

    {"imgdiff", "imgdiff", R"sc({
  "title": "imgdiff",
  "description": "How two images differ, exactly and perceptually: how many pixels changed, by how much, and the difference hash of each. The two must be the same size -- comparing different sizes is a different question, not a smaller difference.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "a", "b",
               "width", "height", "pixels", "differing", "percentDiffering", "meanDelta", "maxDelta",
               "hashA", "hashB", "hashDistance", "identical", "visuallySame", "heatMap"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string", "description": "the capture the command was run against (unused)"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "a": {"type": "string", "description": "the first image's path"},
    "b": {"type": "string"},
    "width": {"type": "integer"},
    "height": {"type": "integer"},
    "pixels": {"type": "integer"},
    "differing": {"type": "integer", "description": "pixels with any channel difference"},
    "percentDiffering": {"type": "string", "description": "a percentage with three decimals, as text: the writer has no fractional field"},
    "meanDelta": {"type": "string", "description": "average channel difference over changed pixels, three decimals"},
    "maxDelta": {"type": "integer", "description": "the largest single-channel difference"},
    "hashA": {"type": "string", "description": "16 hex digits: a 64-bit difference hash"},
    "hashB": {"type": "string"},
    "hashDistance": {"type": "integer", "description": "bits that differ, 0-64"},
    "identical": {"type": "boolean"},
    "visuallySame": {"type": "boolean", "description": "hash distance <= 2"},
    "heatMap": {"type": "string", "description": "the difference image written, empty when --out was not given"}
  },
  "additionalProperties": false
})sc"},

    {"patch", "patch", R"sc({
  "title": "patch",
  "description": "A shader built for this replay target and substituted for the capture's own. The document has three shapes: `--encodings` reports only what the target builds, `--dump` adds the disassembly it wrote, and `--from` adds the compile result -- plus, with `--compare`, the before/after images and their difference.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "stage": {"type": "string", "enum": ["vs", "hs", "ds", "gs", "ps", "cs", "as", "ms"]},
    "shader": {"type": "string", "description": "e.g. res1234: the capture's own shader"},
    "reflection": {"type": "string", "description": "whether the engine has a reflection for it"},
    "encodings": {"type": "string", "description": "what this replay target builds"},
    "entry": {"type": "string", "description": "the entry point the source was compiled with"},
    "encoding": {"type": "string", "enum": ["hlsl", "dxbc", "dxil", "glsl", "spirv", "spirv-asm"]},
    "flags": {"type": "integer", "description": "how many --flag name=value pairs were passed"},
    "built": {"type": "string", "description": "the new shader's id, e.g. res9001"},
    "compiled": {"type": "boolean"},
    "compiler": {"type": "string", "description": "the compiler's own message, when it had one"},
    "replaced": {"type": "boolean", "description": "the replacement was installed"},
    "dumped": {"type": "string", "description": "the disassembly file written, empty if none was"},
    "before": {"type": "string"},
    "after": {"type": "string"},
    "diff": {"type": "string", "description": "the difference map written"},
    "wroteImages": {"type": "boolean"},
    "pixels": {"type": "integer"},
    "differing": {"type": "integer"},
    "percentDiffering": {"type": "string", "description": "a percentage with three decimals, as text"},
    "maxDelta": {"type": "integer"},
    "hashBefore": {"type": "string", "description": "16 hex digits: a 64-bit difference hash"},
    "hashAfter": {"type": "string"},
    "hashDistance": {"type": "integer", "description": "bits that differ, 0-64"}
  },
  "additionalProperties": false
})sc"},

    {"pixelhistory", "pixelhistory", R"sc({
  "title": "pixelhistory",
  "description": "Why one pixel is the colour it is: every event up to the scope event that tried to write it, the test that rejected each attempt, and the value before, from and after it. `readAs` says how the numbers in `color` were read -- the texture's own format, or --cast, or raw 32-bit words when the capture says neither.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid",
               "marker", "texture", "name", "x", "y", "mip", "slice", "sample", "format", "dimension",
               "textureWidth", "textureHeight", "mips", "slices", "samples", "cast", "readAs",
               "usagesUpTo", "note", "modifications", "total", "passed", "rejected", "shown"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer", "description": "the scope: every write up to and including this event"},
    "marker": {"type": "string", "description": "the marker path of the scope event, empty outside every marker"},
    "texture": {"type": "string", "description": "e.g. res1234"},
    "name": {"type": "string", "description": "the capture's own name for the texture, or its id"},
    "x": {"type": "integer"},
    "y": {"type": "integer"},
    "mip": {"type": "integer"},
    "slice": {"type": "integer", "description": "the array slice, the cube face, or the 3D slice at this mip"},
    "sample": {"type": "integer"},
    "format": {"type": "string"},
    "dimension": {"type": "integer", "description": "1, 2 or 3: the texture's base dimension"},
    "textureWidth": {"type": "integer"},
    "textureHeight": {"type": "integer"},
    "mips": {"type": "integer"},
    "slices": {"type": "integer", "description": "how many slices this subresource has"},
    "samples": {"type": "integer"},
    "cast": {"type": "string", "enum": ["typeless", "float", "unorm", "snorm", "uint", "sint",
                                        "uscaled", "sscaled", "depth", "srgb"],
             "description": "what --cast asked for, `typeless` when it was not given"},
    "readAs": {"type": "string", "description": "how `color` is to be read: a component type, or `raw 32-bit words` when nothing in the capture says"},
    "usagesUpTo": {"type": "integer", "description": "how many events at or before the scope touch this texture, from the engine's own usage list: the evidence an empty `modifications` is read against"},
    "note": {"type": "string", "description": "empty, or why a verdict in this document is not a closed case by itself: on D3D12 the `sample masked` test is an instrumented re-draw, so a flagged fragment can still carry a changed post value"},
    "modifications": {"type": "array", "items": {
      "type": "object",
      "required": ["eid", "marker", "fragIndex", "primitiveID", "passed", "reasons",
                   "directShaderWrite", "unboundPS", "preMod", "shaderOut", "postMod"],
      "properties": {
        "eid": {"type": "integer"},
        "marker": {"type": "string", "description": "the marker path that event sits inside, empty outside every marker"},
        "fragIndex": {"type": "integer", "description": "which fragment of that event this was"},
        "primitiveID": {"type": "integer"},
        "passed": {"type": "boolean", "description": "every test passed: this fragment wrote the pixel"},
        "reasons": {"type": "string", "description": "the tests that rejected it, in the order `passed` reads them; empty when it passed"},
        "directShaderWrite": {"type": "boolean", "description": "an arbitrary shader write (a UAV or a copy), not a draw's fragment"},
        "unboundPS": {"type": "boolean", "description": "no usable pixel shader was bound at the event"},
        "preMod": {"type": "object", "required": ["valid", "color", "depth", "stencil"],
                   "properties": {
                     "valid": {"type": "boolean", "description": "false when the engine marked the value invalid: the other three are then `-`"},
                     "color": {"type": "string", "description": "four components, comma separated, read as `readAs`"},
                     "depth": {"type": "string"},
                     "stencil": {"type": "string", "description": "-1 not in use or unknown, -2 in use but unreadable"}
                   }, "additionalProperties": false},
        "shaderOut": {"type": "object", "required": ["valid", "color", "depth", "stencil"],
                      "properties": {
                        "valid": {"type": "boolean"},
                        "color": {"type": "string"},
                        "depth": {"type": "string"},
                        "stencil": {"type": "string"}
                      }, "additionalProperties": false},
        "postMod": {"type": "object", "required": ["valid", "color", "depth", "stencil"],
                    "properties": {
                      "valid": {"type": "boolean"},
                      "color": {"type": "string"},
                      "depth": {"type": "string"},
                      "stencil": {"type": "string"}
                    }, "additionalProperties": false}
      },
      "additionalProperties": false
    }},
    "total": {"type": "integer", "description": "modifications the engine reported"},
    "passed": {"type": "integer", "description": "how many of them wrote the pixel"},
    "rejected": {"type": "integer"},
    "shown": {"type": "integer", "description": "rows in `modifications`: total, or --max when there were more"}
  },
  "additionalProperties": false
})sc"},

    {"trace", "trace", R"sc({
  "title": "trace",
  "description": "One shader invocation, stepped: the values it started with, every step the engine's debugger ran with the variables that changed on it, and the variable list it ended with. `stage` is what the engine says it debugged and `stageAsked` what the selector implies, so a disagreement is visible rather than assumed away. `outputs` is the running variable list at the last step, which is the invocation's own answer only when `truncated` is false.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "event",
               "stage", "shader", "stageAsked", "invocation", "constantBlocks", "resources",
               "samplers", "sourceVariables", "inputs", "steps", "outputs", "stepCount", "variables",
               "truncated"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "event": {"type": "integer", "description": "the event the invocation ran at"},
    "stage": {"type": "string", "enum": ["vs", "hs", "ds", "gs", "ps", "cs", "as", "ms"],
              "description": "what the engine says it debugged"},
    "shader": {"type": "string", "description": "the bound shader's resource id, digits only"},
    "stageAsked": {"type": "string", "enum": ["vs", "hs", "ds", "gs", "ps", "cs", "as", "ms"],
                   "description": "the stage the selector implies: pixel=ps, vertex=vs, thread=cs, mesh-thread=ms"},
    "invocation": {"type": "string", "description": "the invocation as it was asked for, e.g. `pixel 640,360` or `vertex 0,0,0,0`"},
    "sample": {"type": "integer", "description": "--sample, only when it was given: `DebugPixelInputs`' field, whose default means `no preference`"},
    "primitive": {"type": "integer", "description": "--primitive, only when it was given"},
    "view": {"type": "integer", "description": "--view, only when it was given"},
    "debuggable": {"type": "boolean", "description": "the engine's verdict on this shader (ShaderDebugInfo), when it published a reflection"},
    "sourceDebugInfo": {"type": "boolean", "description": "whether the shader's debug data was found: a DXIL shader is stepped *through* it, so false on one of those means no trace at all, while a DXBC shader steps from its bytecode either way"},
    "debugStatus": {"type": "string", "description": "the engine's own sentence, only when it gave one"},
    "constantBlocks": {"type": "integer", "description": "how many constant blocks the trace carries (their values are `cb`'s answer, read through the reflection's names)"},
    "resources": {"type": "integer", "description": "read-only plus read-write resource variables the trace carries"},
    "samplers": {"type": "integer"},
    "sourceVariables": {"type": "integer", "description": "source-level variable mappings the trace carries: non-zero only with debug info"},
    "inputs": {"type": "array", "items": {"type": "string"},
               "description": "`name = value`, the values the invocation started with"},
    "steps": {"type": "array", "items": {
      "type": "object",
      "required": ["step", "instruction", "events", "source", "callstack", "changes"],
      "properties": {
        "step": {"type": "integer", "description": "1-based, and consecutive across `ContinueDebug` batches"},
        "instruction": {"type": "integer", "description": "the next instruction after this state: the engine's own program counter"},
        "events": {"type": "string", "description": "the ShaderEvents flags on this step: none, sample/load/gather, nan/inf, debugbreak, or an unknown bit as hex"},
        "source": {"type": "string", "description": "file:line[-line] covering this instruction from the trace's instInfo, empty without debug info or when no mapping covers it"},
        "callstack": {"type": "string", "description": "function names, oldest first, ` > ` separated"},
        "changes": {"type": "array", "items": {"type": "string"},
                    "description": "`name: before -> after`, `name = value (new)` for a variable that came into scope, `name left scope` for one that stopped existing"}
      },
      "additionalProperties": false
    }},
    "outputs": {"type": "array", "items": {"type": "string"},
                "description": "`name = value` after the last step: the variables the invocation changed, accumulated the way RenderDoc's own UI accumulates a debug state"},
    "stepCount": {"type": "integer", "description": "rows in `steps`: less than the invocation's length only when `truncated`"},
    "variables": {"type": "integer", "description": "rows in `outputs`"},
    "truncated": {"type": "boolean", "description": "true when --max-steps (or the hard cap under --all) stopped the run before the invocation finished, so `outputs` is the state at the cap rather than the invocation's end"}
  },
  "additionalProperties": false
})sc"},
};

const int kSchemaCount = (int)(sizeof(kSchemas) / sizeof(kSchemas[0]));

const SchemaDoc *FindSchema(std::string_view name)
{
  for(int i = 0; i < kSchemaCount; i++)
  {
    if(name == kSchemas[i].m_Name)
      return &kSchemas[i];
  }
  return NULL;
}
