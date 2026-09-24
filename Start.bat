@echo off
setlocal EnableExtensions
rem Media Toolkit: start from source. Runs setup.bat first if needed.
rem Arguments are passed on to run.py (for example --port 8765).
cd /d "%~dp0"
title Media Toolkit

if not exist ".venv\Scripts\python.exe" (
  echo   First run - starting setup...
  call "%~dp0setup.bat"
  if not exist ".venv\Scripts\python.exe" exit /b 1
)

".venv\Scripts\python.exe" "run.py" %*
if errorlevel 1 pause
