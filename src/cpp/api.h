// z.api — the driver as a library: `bin/rdc_replay.dll`, called in-process (REFERENCE §9).
//
// A *thin* ABI over the same code `replay_dump.exe` runs, for a caller that already has a process: the
// offline tool through `ctypes` (`src/py/rdc_replay.py`), or anything else that would rather ask twenty
// questions of one open capture than start twenty programs -- each of which pays the engine's own
// standup, ~4 s on a small capture and ~11 s on a 1.4 GB one.
//
// The rules a C ABI needs, and what each means here:
//
//  * **Opaque handles.** `RdcReplayOpen` returns a `void *`, and nothing about a session is visible
//    through it. No C++ type crosses: no `rdcarray`, no `rdcstr`, no engine type. A caller needs this
//    header and the DLL, and nothing else.
//  * **Errors are a return value plus a sentence.** Every entry point that can fail says so with its
//    return value and writes a NUL-terminated message into the caller's `err` buffer, truncated if it
//    does not fit. `err` may be NULL, and `errLen` 0 is legal, for a caller that wants only the code.
//  * **The caller frees what the library allocated.** Text returned through an `out` parameter is the
//    library's `malloc` and is released with `RdcReplayFree`. Nothing else is ever the caller's.
//  * **The command surface is the command line's.** `RdcReplayCommand` takes one line in exactly the
//    syntax a batch file uses (`state 270 --json`, `textures --save out`), so one question has one
//    spelling everywhere and everything the CLI documents about a command holds here too.
//  * **stdout belongs to the command, stderr to the run.** A command's document comes back in `out`
//    rather than being printed; its own failure messages and progress lines still go to the process's
//    stderr, exactly as they do on the command line, and the exit code says which happened.
//
// What the ABI deliberately does not have: no callbacks, no threads of its own (one session per handle
// and the caller decides what to do with it), no buffering of documents beyond one command, and no
// version negotiation beyond `RdcReplayAbi` -- a caller that finds an ABI it does not know refuses it
// rather than guessing at the shape of what it gets back.

#ifndef RDC_REPLAY_API_H
#define RDC_REPLAY_API_H

//: The ABI this build implements. A caller compares it and refuses a mismatch, because an ABI is a
//: contract with the *caller* rather than with the capture: `schemaVersion` is the same idea for a
//: document, and neither one is negotiable at run time.
#define RDC_REPLAY_ABI "rdc_replay/1"

#define RDC_API extern "C" __declspec(dllexport)

//: The ABI's own name and version, e.g. `rdc_replay/1`. Never NULL.
RDC_API const char *RdcReplayAbi(void);

//: Open a session. `capturePath` may be NULL or empty for a session with **no capture**, which is
//: what the capture-free commands need (`schema`): no device is created and no capture is read.
//: `logPath` names the log file for the **process** -- NULL means a per-run name beside this DLL
//: (`rdc_replay_<date>_<time>.log.txt`), which is the same log the exe writes and the thing to read
//: when a run looks stuck -- and only the call that brings the engine up uses it: a second session
//: logs into the running file rather than opening a second one.
//:
//: **The replay system is the process's, not the session's.** RenderDoc initialises it once per
//: process and shuts it down once, so the first `RdcReplayOpen` does that and the shutdown happens
//: when this DLL unloads. A session owns a capture and its controller, which is what makes a handle
//: something a caller can hold across many commands -- and what makes a second session possible at
//: all. (Building it the other way round is not a design choice that was weighed: the second
//: session faulted inside the teardown, which is the engine enforcing its rule.)
//:
//: Returns NULL and fills `err` when the session cannot be opened: the engine cannot be loaded, the
//: capture was recorded by a newer RenderDoc than the engine (the version guard refuses it), the
//: file cannot be opened, or the replay device cannot be created.
RDC_API void *RdcReplayOpen(const char *capturePath, const char *logPath, char *err, int errLen);

//: Run one command line against the session. Returns the command's own exit code (0 = answered, 1 =
//: failed, 2 = bad arguments -- the codes the CLI documents) and, when `out` is not NULL, stores
//: the command's **stdout** there as a NUL-terminated buffer the caller releases with
//: `RdcReplayFree`. With `out` NULL the command's stdout goes to the caller's own stdout instead.
//:
//: A session opened without a capture answers the capture-free commands (`schema`) and refuses the
//: rest with a message in `err`, rather than answering from a device it never created.
RDC_API int RdcReplayCommand(void *session, const char *line, char **out, char *err, int errLen);

//: Close a session: the controller, the capture file and the replay system, in the engine's own
//: order, and the log file. A NULL handle is legal and does nothing.
RDC_API void RdcReplayClose(void *session);

//: Release a buffer `RdcReplayCommand` returned. NULL is legal and does nothing.
RDC_API void RdcReplayFree(void *block);

#endif
