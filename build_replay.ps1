# Build replay_dump.exe against the installed RenderDoc.
#
# The replay API is C++ (renderdoc/api/replay/renderdoc_replay.h in the renderdoc-src tree) and the
# installed renderdoc.dll exports two C entry points, so this needs MSVC plus an import library --
# which the RenderDoc installer does not ship. The script makes one from the DLL's export table.
#
#   .\build_replay.ps1                 # build replay_dump.exe
#   .\build_replay.ps1 -Clean          # delete the build folder first
#
# Everything lands in .\build\ (gitignored). Override the DLL with -Dll <path>.

param(
    [string]$Dll = "C:\Program Files\RenderDoc\renderdoc.dll",
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$build = Join-Path $root 'build'

if ($Clean -and (Test-Path $build)) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Force -Path $build | Out-Null

if (-not (Test-Path $Dll)) { throw "renderdoc.dll not found at $Dll (pass -Dll)" }

# --- locate MSVC -------------------------------------------------------------------------------
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw 'vswhere.exe not found; is Visual Studio installed?' }
$vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsPath) { throw 'no MSVC toolset found (install the C++ workload)' }
$vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "vcvars64.bat not found at $vcvars" }

# --- import library from the DLL's exports -----------------------------------------------------
# Only the two entry points this tool calls are needed; everything else is virtual calls on the
# objects they return, which need no imports.
$def = Join-Path $build 'renderdoc.def'
@"
LIBRARY renderdoc.dll
EXPORTS
    RENDERDOC_OpenCaptureFile
    RENDERDOC_GetVersionString
    RENDERDOC_InitialiseReplay
    RENDERDOC_ShutdownReplay
    RENDERDOC_AllocArrayMem
    RENDERDOC_FreeArrayMem
    RENDERDOC_ResourceFormatName
    RENDERDOC_HalfToFloat
"@ | Set-Content -Encoding ASCII $def

$lib = Join-Path $build 'renderdoc.lib'
$src = Join-Path $root 'replay_dump.cpp'
$out = Join-Path $build 'replay_dump.exe'

$bat = Join-Path $build 'build.bat'
@"
@echo off
call "$vcvars" >nul || exit /b 1
cd /d "$build" || exit /b 1
lib /nologo /def:"$def" /out:"$lib" /machine:x64 || exit /b 1
cl /nologo /std:c++17 /EHsc /O2 /W3 /D_CRT_SECURE_NO_WARNINGS /I "$root\renderdoc-src\renderdoc\api\replay" /DRENDERDOC_PLATFORM_WIN32 /Fe:"$out" "$src" "$lib" || exit /b 1
"@ | Set-Content -Encoding ASCII $bat

& cmd /c $bat
if ($LASTEXITCODE -ne 0) { throw "build failed (exit $LASTEXITCODE)" }

# The import library makes renderdoc.dll a *load-time* dependency, and the loader looks next to the
# exe (not in Program Files), so put a copy there -- along with the DLLs it loads at runtime
# (d3dcompiler for shader work, dbghelp/symsrv for symbol handling). Everything here is gitignored.
Copy-Item -Force $Dll $build
$dllDir = Split-Path -Parent $Dll
foreach($dep in 'd3dcompiler_47.dll', 'dbghelp.dll', 'symsrv.dll', 'symsrv.yes')
{
    $src = Join-Path $dllDir $dep
    if(Test-Path $src) { Copy-Item -Force $src $build }
}

Write-Host "built $out"
& $out --help | Select-Object -First 3
