# replay_dump.exe — headless RenderDoc replay as a data source (REFERENCE §9).
#
#   cmake -S . -B build             # configure
#   cmake --build build --parallel  # compile (on top of /MP, so every core is busy)
#
# The executable and the engine DLLs it loads at run time go to bin/ (gitignored); the build tree stays in
# build/ (gitignored). Both are separate on purpose: `bin/replay_dump.exe` is what every command in the docs
# and scripts calls, and it does not move when the build system or its flags change.

cmake_minimum_required(VERSION 3.20)

project(rdc_tools LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# The replay API is C++ and the installed renderdoc.dll exports C entry points, so the build needs an import
# library — which the RenderDoc installer does not ship, and which is made from the DLL's export table below.
set(RENDERDOC_DLL "C:/Program Files/RenderDoc/renderdoc.dll" CACHE FILEPATH
    "the installed renderdoc.dll to link against and deploy beside the exe")
set(RENDERDOC_SRC "${CMAKE_CURRENT_SOURCE_DIR}/renderdoc-src" CACHE PATH
    "the RenderDoc source tree the replay headers come from (see ROADMAP.md)")

if(NOT EXISTS "${RENDERDOC_DLL}")
  message(FATAL_ERROR "renderdoc.dll not found at '${RENDERDOC_DLL}' — configure with -DRENDERDOC_DLL=<path>")
endif()
if(NOT EXISTS "${RENDERDOC_SRC}/renderdoc/api/replay/renderdoc_replay.h")
  message(FATAL_ERROR "renderdoc-src not found under '${RENDERDOC_SRC}' (ROADMAP.md's environment convention)")
endif()

# bin/ holds the exe and the DLLs beside it; the build tree stays out of the way. A multi-config generator
# (the Visual Studio one) appends the configuration to the output directory, so every configuration is set
# explicitly: `bin\replay_dump.exe` is what the docs and scripts call, and it must not become
# `bin\Release\replay_dump.exe` when the generator changes.
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/bin")
foreach(_config IN ITEMS DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)
  set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${_config} "${CMAKE_CURRENT_SOURCE_DIR}/bin")
endforeach()

# Globbed so that splitting a file never means editing this list; CONFIGURE_DEPENDS makes the build re-glob.
file(GLOB REPLAY_DUMP_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/cpp/*.cpp")
file(GLOB REPLAY_DUMP_HEADERS CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/cpp/*.h")

add_executable(replay_dump ${REPLAY_DUMP_SOURCES})

# --- the same code as a library, for a caller that already has a process ---------------------------
# `bin/rdc_replay.dll` exports the ABI declared in `src/cpp/api.h`: one open capture, many commands,
# called through `ctypes` by the offline tool (`src/py/rdc_replay.py`, REFERENCE §9). The sources are the
# executable's own, so a library line and a batch line are the same command language and the same
# dispatcher -- the only difference in this file is that the second target is a DLL, which makes `main`
# dead code in it.
#
# It is a *separate* artefact rather than an export from the exe because that is what a caller can load:
# `ctypes.CDLL('bin/rdc_replay.dll')` needs a DLL, and nothing about the exe's one-run-one-command shape
# has to change to provide it.
add_library(rdc_replay SHARED ${REPLAY_DUMP_SOURCES})

# Everything both targets share, in one place: a flag that reached only one of them would make the
# library's answers differ from the executable's on the same capture, which is the one thing this pair
# exists not to do.
foreach(_target IN ITEMS replay_dump rdc_replay)
  target_include_directories(${_target} PRIVATE "${RENDERDOC_SRC}/renderdoc/api/replay")
  target_compile_definitions(${_target} PRIVATE RENDERDOC_PLATFORM_WIN32 _CRT_SECURE_NO_WARNINGS)
  # bcrypt is SHA-256 for the bundle manifest; the source also carries the pragma, and this is explicit.
  target_link_libraries(${_target} PRIVATE bcrypt)

  if(MSVC)
    # /MP compiles the translation units in parallel. With one target, the build tool's own --parallel
    # has nothing to overlap, so without /MP a split into modules would only cost wall-clock. /external:*
    # keeps RenderDoc's own headers out of the warnings this project treats as its own.
    target_compile_options(${_target} PRIVATE
      /W4 /permissive- /Zc:__cplusplus /Zc:preprocessor /utf-8 /MP
      /external:W0 /external:anglebrackets)
    # A warning is a finding: this is a tool whose whole job is to report what it cannot prove, so an
    # unhandled case should not scroll past. (Debug keeps it a warning for a single-file iteration.)
    target_compile_options(${_target} PRIVATE $<$<CONFIG:Release>:/WX>)
  endif()
endforeach()

# --- the import library, from the DLL's exports --------------------------------------------------------
# Only the entry points this tool calls are needed: everything else is virtual calls on the objects they
# return, which need no imports.
set(RENDERDOC_DEF "${CMAKE_CURRENT_BINARY_DIR}/renderdoc.def")
set(RENDERDOC_IMPLIB "${CMAKE_CURRENT_BINARY_DIR}/renderdoc.lib")
file(WRITE "${RENDERDOC_DEF}"
"LIBRARY renderdoc.dll
EXPORTS
    RENDERDOC_OpenCaptureFile
    RENDERDOC_GetVersionString
    RENDERDOC_InitialiseReplay
    RENDERDOC_ShutdownReplay
    RENDERDOC_AllocArrayMem
    RENDERDOC_FreeArrayMem
    RENDERDOC_ResourceFormatName
    RENDERDOC_HalfToFloat
")
# No quotes of our own inside the arguments: VERBATIM quotes each token that needs it, and embedded
# quotes would be passed through literally to lib.exe.
add_custom_command(OUTPUT "${RENDERDOC_IMPLIB}"
  COMMAND "${CMAKE_AR}" /nologo /def:${RENDERDOC_DEF} /out:${RENDERDOC_IMPLIB} /machine:x64
  DEPENDS "${RENDERDOC_DEF}" COMMENT "import library from renderdoc.dll's exports" VERBATIM)
add_custom_target(renderdoc_implib DEPENDS "${RENDERDOC_IMPLIB}")
add_dependencies(replay_dump renderdoc_implib)
add_dependencies(rdc_replay renderdoc_implib)
target_link_libraries(replay_dump PRIVATE "${RENDERDOC_IMPLIB}")
target_link_libraries(rdc_replay PRIVATE "${RENDERDOC_IMPLIB}")

# --- the engine's DLLs, beside the exe -----------------------------------------------------------------
# The import library makes renderdoc.dll a load-time dependency and the loader looks next to the exe, not in
# Program Files. renderdoccmd.exe is not optional either: the engine spawns `<its own directory>\renderdoccmd.exe
# crashhandle`, and without it every run stalls 400 ms and then carries on with no crash handler. A copy can
# fail while a running instance has the DLL loaded — not a build failure, so the script tolerates it.
add_custom_command(TARGET replay_dump POST_BUILD
  COMMAND "${CMAKE_COMMAND}" -DDll=${RENDERDOC_DLL} -DDest=$<TARGET_FILE_DIR:replay_dump>
          -P "${CMAKE_CURRENT_SOURCE_DIR}/tools/deploy_dlls.cmake"
  COMMENT "deploying the engine's DLLs next to the exe" VERBATIM)

# The library loads the same engine and lives in the same directory, so it needs the same neighbours --
# and this makes it so when the library is the only target that was built.
add_custom_command(TARGET rdc_replay POST_BUILD
  COMMAND "${CMAKE_COMMAND}" -DDll=${RENDERDOC_DLL} -DDest=$<TARGET_FILE_DIR:rdc_replay>
          -P "${CMAKE_CURRENT_SOURCE_DIR}/tools/deploy_dlls.cmake"
  COMMENT "deploying the engine's DLLs next to the library" VERBATIM)

# --- the LZ4 decoder the offline tool loads with ctypes ------------------------------------------------
# A capture's frame-capture section is one LZ4 stream of 1 MB pages, and this is what the offline tool decodes
# it with: `rdc_stream.decompress_lz4` looks for a library exporting `LZ4_decompress_safe_usingDict` —
# `bin/rdc_lz4.dll` first, then whatever the system has (`$RDC_LZ4_DLL` names one) — and raises if there is
# none, because the tool has no decoder of its own and a stream decoded by anything laxer is one every command
# downstream would read as fact. Measured: 0.22 s for `desktop-2`'s 1.47 GB, 0.5 s end to end with the
# container's own body copy (REFERENCE §3.2).
#
# The source is the decoder the replay engine itself vendors (`renderdoc-src/renderdoc/3rdparty/lz4`, LZ4
# v1.9.2, BSD-2-Clause, LICENSE beside it), copied into `src/cpp/third_party` so that building this does not
# depend on a fetched tree. `/W3` and no `/WX`: it is not this project's code to hold to its own warning
# rules. `LZ4_DLL_EXPORT` is what makes `LZ4LIB_API` `__declspec(dllexport)` — without it the DLL exports
# nothing at all and the tool finds no function in it.
enable_language(C)
add_library(rdc_lz4 SHARED "${CMAKE_CURRENT_SOURCE_DIR}/src/cpp/third_party/lz4/lz4.c")
target_include_directories(rdc_lz4 PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src/cpp/third_party/lz4")
target_compile_definitions(rdc_lz4 PRIVATE LZ4_DLL_EXPORT=1)
set_target_properties(rdc_lz4 PROPERTIES PREFIX "")
if(MSVC)
  target_compile_options(rdc_lz4 PRIVATE /W3 /utf-8)
endif()

# --- formatting ----------------------------------------------------------------------------------------
# The style is RenderDoc's own (.clang-format, copied from the tree): the driver is a RenderDoc client, and a
# reader should not have to switch conventions between the two. `clang-format` rewrites, `clang-format-check`
# is the gate — it prints the diff and fails, which is what a commit hook or CI would run.
find_program(CLANG_FORMAT NAMES clang-format clang-format.exe
  HINTS "$ENV{ProgramFiles}/Microsoft Visual Studio/18/Community/VC/Tools/Llvm/x64/bin"
        "$ENV{ProgramFiles}/Microsoft Visual Studio/2022/Community/VC/Tools/Llvm/x64/bin")
if(CLANG_FORMAT)
  add_custom_target(clang-format
    COMMAND "${CLANG_FORMAT}" -i ${REPLAY_DUMP_SOURCES} ${REPLAY_DUMP_HEADERS}
    COMMENT "clang-format over src/cpp" VERBATIM)
  add_custom_target(clang-format-check
    COMMAND "${CLANG_FORMAT}" --dry-run --Werror ${REPLAY_DUMP_SOURCES} ${REPLAY_DUMP_HEADERS}
    COMMENT "checking src/cpp against .clang-format" VERBATIM)
else()
  message(STATUS "clang-format not found — the format targets are unavailable")
endif()
