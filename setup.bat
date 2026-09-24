@echo off
setlocal EnableExtensions
rem Media Toolkit: one-time setup for running from source.
rem Creates .venv next to this file, installs requirements.txt and fetches the
rem pinned ffmpeg build into bin\. Safe to run again: it repairs a broken or
rem moved environment and only downloads what is missing.
cd /d "%~dp0"
title Media Toolkit - Setup

echo.
echo   Media Toolkit setup
echo   ===================
echo.

rem --- Find Python 3.10+ (64-bit). The py launcher first, newest tested first,
rem --- then whatever "python" is on PATH, then any Python 3 the launcher knows.
set "PY="
for %%V in (3.13 3.12 3.11) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
)
if not defined PY goto nopython

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto oldpython
%PY% -c "import struct; raise SystemExit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 goto bitness

for /f "delims=" %%I in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%I"
echo   Using Python %PYVER% (%PY%)

rem --- A .venv that no longer runs (moved folder, Python uninstalled) is rebuilt.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo   The existing environment is broken; rebuilding it...
    rmdir /s /q ".venv"
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo   Creating a private Python environment...
  %PY% -m venv ".venv" || goto fail
)

echo   Installing packages ^(a few minutes the first time^)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --disable-pip-version-check
rem The versions a release was tested with, where this Python can use them;
rem otherwise the newest compatible ones.
".venv\Scripts\python.exe" -m pip install -r "requirements.txt" -c "requirements-lock.txt" --disable-pip-version-check
if errorlevel 1 (
  echo   Retrying without the tested versions list...
  ".venv\Scripts\python.exe" -m pip install -r "requirements.txt" --disable-pip-version-check || goto fail
)

echo.
echo   Fetching ffmpeg...
".venv\Scripts\python.exe" "tools\fetch_ffmpeg.py" || goto fail

echo.
echo   Done. Double-click Start.bat to launch the app.
echo   GPU transcription on an NVIDIA card: turn it on in Settings, or install
echo   requirements-gpu.txt with pip.
echo.
pause
exit /b 0

:nopython
echo   Python 3.10 or newer ^(64-bit^) is required, and none was found.
echo   Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
goto stop

:oldpython
echo   %PY% is older than Python 3.10. Install a newer Python from
echo   https://www.python.org/downloads/ and run setup.bat again.
goto stop

:bitness
echo   %PY% is a 32-bit Python. Media Toolkit needs the 64-bit version from
echo   https://www.python.org/downloads/
goto stop

:fail
echo.
echo   Setup failed. Scroll up for the error.
:stop
echo.
pause
exit /b 1
