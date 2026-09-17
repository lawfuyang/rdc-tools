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
      "required": ["eid", "pso", "psoKind", "shaders", "targets", "depth", "rootParameters", "state"],
      "properties": {
        "eid": {"type": "integer"},
        "pso": {"type": "string", "description": "the pipeline state object's resource id"},
        "psoKind": {"enum": ["graphics", "compute"]},
        "shaders": {"type": "string", "description": "`vs=2348 ps=2349`, stages in a fixed order"},
        "targets": {"type": "array", "items": {"type": "string"},
                    "description": "`<id> <w>x<h>x<d> <FORMAT>`, the output-merge state at this id"},
        "depth": {"type": "string", "description": "the depth target's resource id, `0` for none"},
        "rootParameters": {"type": "integer"},
        "state": {"type": "string", "description": "hash of what the state file was written from"}
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
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "api",
               "shaders", "renderTargets", "depthTarget", "rootSignature", "rootParameters"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
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
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "stages"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "stages": {"type": "array", "items": {
      "type": "object",
      "required": ["stage", "resource", "entry", "encoding", "bytes", "constantBlocks",
                   "readOnlyResources", "readWriteResources", "inputSignature", "outputSignature"],
      "properties": {
        "stage": {"type": "string", "description": "vs hs ds gs ps cs as ms"},
        "resource": {"type": "string"},
        "entry": {"type": "string"},
        "encoding": {"type": "integer"},
        "bytes": {"type": "integer"},
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
  "description": "The engine's own messages, one row each (`eid <n>  <severity>  <text>`), plus the header. Rows are strings: they are the same text the terminal prints, so nothing is lost between the two forms.",
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
  "description": "The structured file's draw-like chunks in order. `eid` is the *engine's* id where one is known and the chunk index where it is not: the two spaces are not the same (REFERENCE §9), and `probe` lists the ids that have state.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "events",
               "totalChunks", "totalEvents", "shown"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "events": {"type": "array", "items": {
      "type": "object",
      "required": ["eid", "depth", "chunkID", "name"],
      "properties": {
        "eid": {"type": "integer"},
        "depth": {"type": "integer"},
        "chunkID": {"type": "integer"},
        "name": {"type": "string"}
      },
      "additionalProperties": false
    }},
    "totalChunks": {"type": "integer"},
    "totalEvents": {"type": "integer"},
    "shown": {"type": "integer"},
    "truncated": {"type": "string", "description": "present only when the action tree was deeper than the recursion limit"}
  },
  "additionalProperties": false
})sc"},

    {"probe", "probe", R"sc({
  "title": "probe",
  "description": "Which ids in a range have pipeline state. Rows are strings (`eid <n>  shaders=.. rootSig=.. params=..`), the same text the terminal prints.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "events",
               "scanned", "withState"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "events": {"type": "array", "items": {"type": "string"}},
    "scanned": {"type": "integer"},
    "withState": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"cb", "cb", R"sc({
  "title": "cb",
  "description": "One constant buffer at one event: what it is bound to and one row per reflection variable, with the value read from the data.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "stage",
               "slot", "shader", "buffer", "variables"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
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
  "description": "One draw's mesh: the state that feeds it, and -- when the capture has post-VS data -- the vertices the vertex shader emitted.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "instance",
               "topology", "vertexResource", "vertexStride", "vertexBytes", "indexResource", "indexBytes"],
  "properties": {
    "schemaVersion": {"const": 1},
    "capture": {"type": "string"},
    "renderdoc": {"type": "string"},
    "driver": {"type": "string"},
    "localReplay": {"type": "integer"},
    "machine": {"type": "string"},
    "eid": {"type": "integer"},
    "instance": {"type": "integer"},
    "topology": {"type": "integer"},
    "vertexResource": {"type": "string"},
    "vertexStride": {"type": "integer"},
    "vertexBytes": {"type": "integer"},
    "indexResource": {"type": "string"},
    "indexBytes": {"type": "integer"},
    "baseVertex": {"type": "integer"},
    "vertices": {"type": "array", "items": {"type": "string"}},
    "vertexCount": {"type": "integer"},
    "componentsPerVertex": {"type": "integer"}
  },
  "additionalProperties": false
})sc"},

    {"image", "image", R"sc({
  "title": "image",
  "description": "One render target saved to a file, and whether the write succeeded.",
  "type": "object",
  "required": ["schemaVersion", "capture", "renderdoc", "driver", "localReplay", "machine", "eid", "resource",
               "width", "height", "file", "written"],
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
    "written": {"type": "integer"}
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
};

const int kSchemaCount = (int)(sizeof(kSchemas) / sizeof(kSchemas[0]));

const SchemaDoc *FindSchema(std::string_view name)
{
  for(int i = 0; i < kSchemaCount; i++)
  {
    if(name == kSchemas[i].name)
      return &kSchemas[i];
  }
  return NULL;
}
