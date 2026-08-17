@echo off
setlocal
set "WORKER_ROOT=%~dp0"
set "PYTHONW=%WORKER_ROOT%.venv\Scripts\pythonw.exe"

if not exist "%PYTHONW%" (
  echo TTSCore virtual environment is missing: %PYTHONW%
  pause
  exit /b 1
)

cd /d "%WORKER_ROOT%"
start "" "%PYTHONW%" -m readji_tts.gui
