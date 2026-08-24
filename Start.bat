@echo off
setlocal
cd /d "%~dp0"
title Media Toolkit

if not exist ".venv\Scripts\python.exe" (
  echo   First run - starting setup...
  call setup.bat
  if not exist ".venv\Scripts\python.exe" exit /b 1
)

".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause
