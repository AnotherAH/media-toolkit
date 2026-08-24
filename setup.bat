@echo off
setlocal
cd /d "%~dp0"
title Media Toolkit - Setup

echo.
echo   Media Toolkit setup
echo   ===================
echo.

set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY ( python --version >nul 2>&1 && set PY=python )
if not defined PY (
  echo   Python 3.10 or newer is required.
  echo   Get it from https://www.python.org/downloads/  ^(tick "Add Python to PATH"^)
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo   Creating a private Python environment...
  %PY% -m venv .venv || goto fail
)

echo   Installing packages ^(a few minutes the first time^)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto fail

echo.
echo   Fetching ffmpeg...
".venv\Scripts\python.exe" tools\fetch_ffmpeg.py

echo.
echo   Done. Double-click Start.bat to launch the app.
echo.
pause
exit /b 0

:fail
echo.
echo   Setup failed. Scroll up for the error.
pause
exit /b 1
