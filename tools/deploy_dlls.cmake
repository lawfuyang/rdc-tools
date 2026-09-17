# Copy the engine DLLs the exe loads at run time next to it (bin/), tolerating a locked file.
#
# The import library makes renderdoc.dll a *load-time* dependency, and Windows looks next to the exe rather
# than in Program Files — so this copy is what makes `bin\replay_dump.exe` runnable on its own. The engine also
# spawns `<its own directory>\renderdoccmd.exe crashhandle`; without it every run stalls ~400 ms waiting for a
# crash-handle server that never arrives, and then runs with no handler at all. d3dcompiler does the shader
# work, dbghelp/symsrv the symbol handling.
#
# Any of these can be *in use* by a running instance of the tool, and a locked file is not a build failure:
# the copy already there is the same file. That tolerance is the reason this is a script rather than a plain
# `copy_if_different`, which would fail the build instead.
#
# Run by CMakeLists.txt's POST_BUILD step; runnable by hand too:
#
#   cmake -DDll="C:/Program Files/RenderDoc/renderdoc.dll" -DDest=bin -P tools/deploy_dlls.cmake

if(NOT DEFINED Dll OR NOT DEFINED Dest)
  message(FATAL_ERROR "usage: cmake -DDll=<renderdoc.dll> -DDest=<dir> -P tools/deploy_dlls.cmake")
endif()
if(NOT EXISTS "${Dll}")
  message(FATAL_ERROR "renderdoc.dll not found at '${Dll}'")
endif()

file(MAKE_DIRECTORY "${Dest}")
get_filename_component(_dllDir "${Dll}" DIRECTORY)

# Everything the engine or its shader path loads. renderdoccmd is first because its absence is silent.
set(_runtimeDeps renderdoccmd.exe d3dcompiler_47.dll dbghelp.dll symsrv.dll symsrv.yes)

function(deploy_one source)
  if(NOT EXISTS "${source}")
    return()
  endif()
  get_filename_component(name "${source}" NAME)
  set(target "${Dest}/${name}")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E copy_if_different "${source}" "${target}"
                  RESULT_VARIABLE result OUTPUT_QUIET ERROR_QUIET)
  if(result EQUAL 0)
    message(STATUS "deployed ${name}")
  elseif(EXISTS "${target}")
    message(STATUS "note: keeping the existing ${name} (in use by a running instance)")
  else()
    message(FATAL_ERROR "cannot deploy ${name} to ${Dest}")
  endif()
endfunction()

deploy_one("${Dll}")
foreach(dep IN LISTS _runtimeDeps)
  deploy_one("${_dllDir}/${dep}")
endforeach()
