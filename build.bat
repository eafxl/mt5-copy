@echo off
rem ============================================================================
rem  MetaCT - build MetaCT.exe with Nuitka
rem
rem  Double-click to build. Output is a single dist\MetaCT.exe. From a
rem  shell:
rem      build.bat                 build dist\MetaCT.exe
rem      build.bat --console       keep the console window, for debugging
rem      build.bat --clean         delete the previous exe/log/scratch dirs
rem      build.bat --check         print the Nuitka command, build nothing
rem
rem  Uses venv\ when install.bat has been run, otherwise finds a system Python
rem  and builds its own venv-build\ so the build never pollutes global site-
rem  packages.
rem
rem  Every step routes through :run and returns here, so the window ALWAYS
rem  pauses - a failure is readable instead of a flash of red. Nuitka's output
rem  outruns the console buffer, so build.py also tees it to dist\build.log.
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

title MetaCT - Build EXE
color 0B

call :run %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    color 0A
) else (
    color 0C
    echo ============================================
    echo    BUILD FAILED  ^(exit code %RC%^)
    echo ============================================
    echo.
    if exist "%~dp0dist\build.log" (
        echo Full compiler output: dist\build.log
        echo Open it with:  notepad dist\build.log
    )
    echo.
    echo Common causes:
    echo   - Nuitka does not support this Python yet. It trails new releases
    echo     by months; a 3.12 venv is the safe target.
    echo   - Building on a shared/network folder ^(C:\Mac\... is a Parallels
    echo     share^). Copy the repo to a local disk such as C:\mt5copy first -
    echo     Nuitka writes thousands of intermediate files.
    echo   - Antivirus quarantined the compiler output. Exclude this folder.
    echo   - Out of disk space in %TEMP%.
    echo   - A previous MetaCT.exe still running and holding the file.
)

echo.
pause
endlocal
exit /b %RC%


rem ============================================================================
rem  :run - the actual work. Returns a code; never pauses, never closes.
rem ============================================================================
:run

echo ============================================
echo    MetaCT - Build EXE
echo ============================================
echo.

rem --- [1/4] Pick a Python -----------------------------------------------------
echo [1/4] Locating Python...

set "PY="

rem Prefer the app's own venv: it already has PyQt6 / MetaTrader5 / PyYAML,
rem and Nuitka must compile against the SAME packages the app runs on.
if exist "%~dp0venv\Scripts\python.exe" (
    set "PY=%~dp0venv\Scripts\python.exe"
    echo    Using app venv: !PY!
    goto :have_python
)

rem Second choice: a build-only venv from a previous run of this script.
if exist "%~dp0venv-build\Scripts\python.exe" (
    set "PY=%~dp0venv-build\Scripts\python.exe"
    echo    Using build venv: !PY!
    goto :have_python
)

rem Otherwise find a system Python and make venv-build\ from it. The py
rem launcher is the reliable one - bare "python" on Windows often resolves to
rem the Microsoft Store stub, which cannot create venvs.
set "SYSPY="
where py >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=*" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "SYSPY=%%i"
)
if not defined SYSPY (
    for %%p in (
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
        "%PROGRAMFILES%\Python312\python.exe"
        "%PROGRAMFILES%\Python311\python.exe"
    ) do (
        if exist %%p (
            set "SYSPY=%%~p"
            goto :got_syspy
        )
    )
)
:got_syspy
if not defined SYSPY (
    echo.
    echo ERROR: Python not found.
    echo.
    echo Install Python 3.12 from https://www.python.org/downloads/
    echo and tick "Add Python to PATH".
    echo.
    echo If Python IS installed, turn off the Store aliases:
    echo   Settings ^> Apps ^> Advanced app settings ^> App execution aliases
    echo   Turn OFF "python.exe" and "python3.exe"
    exit /b 1
)

echo    Found system Python: !SYSPY!
echo    Creating build venv (venv-build\)...
"!SYSPY!" -m venv "%~dp0venv-build"
if not exist "%~dp0venv-build\Scripts\python.exe" (
    echo ERROR: failed to create venv-build
    exit /b 1
)
set "PY=%~dp0venv-build\Scripts\python.exe"

rem A fresh build venv has none of the app's dependencies. Nuitka has to be
rem able to import every one of them to trace what goes in the binary.
echo    Installing app dependencies into build venv...
"!PY!" -m pip install --upgrade pip
"!PY!" -m pip install MetaTrader5 PyYAML PyQt6
if errorlevel 1 (
    echo ERROR: failed to install app dependencies
    exit /b 1
)

:have_python

rem --- [2/4] Sanity-check the interpreter -------------------------------------
echo.
echo [2/4] Checking interpreter...

for /f "tokens=*" %%i in ('"!PY!" --version 2^>^&1') do echo    %%i

rem A 32-bit Python builds a 32-bit EXE, which cannot talk to a 64-bit MT5.
rem Checked by exit code, NOT by capturing output: a `for /f` around a quoted
rem interpreter path plus an inner -c "..." loses the inner quotes to cmd's
rem quote stripping, and the result comes back empty.
"!PY!" -c "import struct,sys; sys.exit(0 if struct.calcsize(chr(80))*8 == 64 else 1)"
if errorlevel 1 (
    echo.
    echo ERROR: this Python is 32-bit. MetaTrader5 is 64-bit only.
    echo Install 64-bit Python, then delete venv-build\ before retrying.
    exit /b 1
)
echo    64-bit OK

rem --- [3/4] Nuitka ------------------------------------------------------------
echo.
echo [3/4] Ensuring Nuitka is installed...
echo.

rem Nuitka is pure Python, so this is a quick wheel download. Installed on its
rem own and REQUIRED: without it there is no build.
rem No --quiet: pip with no output is indistinguishable from a hang, and on a
rem Python version too new for prebuilt wheels pip silently compiles from
rem source for minutes.
"!PY!" -m pip install --upgrade nuitka
if errorlevel 1 (
    echo.
    echo ERROR: could not install Nuitka.
    echo If pip reported no matching distribution, Nuitka has no release for
    echo this Python yet. Build with a 3.12 venv instead.
    exit /b 1
)

rem ordered-set speeds up Nuitka's own passes; zstandard shrinks the onefile
rem payload. Both are OPTIONAL - Nuitka only nags without them. --only-binary
rem keeps pip from spending ten minutes compiling zstandard's C extension when
rem no wheel exists for this Python, and a failure here is not fatal.
echo.
echo    Optional accelerators (safe to fail)...
"!PY!" -m pip install --only-binary=:all: ordered-set zstandard
if errorlevel 1 (
    echo    No wheels for this Python - continuing without them.
)

rem Nuitka's own --version is NOT run here. It crashes on some layouts (a
rem Parallels share, for one) with a raw traceback that reads like the build
rem already failed. build.py's preflight probes it and reports the cause in
rem one line instead.

rem --- [4/4] Build -------------------------------------------------------------
echo.
echo [4/4] Building...
rem Nuitka needs a C compiler. With none installed it offers to download a
rem private MinGW64; --assume-yes-for-downloads in build.py accepts that. If
rem MSVC (Visual Studio Build Tools) is present, Nuitka prefers it.
rem First build compiles the C sources: expect 5-15 minutes. Later builds
rem reuse the ccache and are much faster.
echo.

"!PY!" build.py %*
if errorlevel 1 exit /b 1

rem --check builds nothing, so there is no folder to offer.
echo %* | find /I "--check" >nul
if not errorlevel 1 exit /b 0

if exist "%~dp0dist\MetaCT.exe" (
    echo.
    choice /C YN /N /T 15 /D N /M "Open the output folder? [Y/N] "
    if !errorlevel! equ 1 explorer "%~dp0dist"
)

exit /b 0
