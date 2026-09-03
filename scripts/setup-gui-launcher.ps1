$ErrorActionPreference = 'Stop'

$workerRoot = Split-Path -Parent $PSScriptRoot
$pythonwPath = Join-Path $workerRoot '.venv\Scripts\pythonw.exe'

if (-not (Test-Path -LiteralPath $pythonwPath)) {
  throw "Readji TTS virtual environment is missing: $pythonwPath"
}

# Stop auto-starting a console-only worker at every login. Disable (not
# unregister) so this is trivially reversible via Task Scheduler if needed.
$taskName = 'ReadjiTtsWorker'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
  Disable-ScheduledTask -TaskName $taskName | Out-Null
  Write-Output "Disabled scheduled task '$taskName'."
} else {
  Write-Output "Scheduled task '$taskName' was not found; nothing to disable."
}

# A .bat right in the TTSCore folder (where the work happens), still
# double-clickable like a normal program. `start ""` detaches pythonw.exe
# into its own process instead of leaving cmd.exe waiting on it, so the
# brief console flash closes itself immediately rather than staying open for
# as long as the GUI runs. The empty "" after start is required whenever the
# target path can contain spaces, so cmd doesn't mistake the path for the
# window-title argument.
$batPath = Join-Path $workerRoot 'Readji TTS Worker.bat'
$lines = @(
  '@echo off',
  'setlocal',
  'set "WORKER_ROOT=%~dp0"',
  'set "PYTHONW=%WORKER_ROOT%.venv\Scripts\pythonw.exe"',
  '',
  'if not exist "%PYTHONW%" (',
  '  echo TTSCore virtual environment is missing: %PYTHONW%',
  '  pause',
  '  exit /b 1',
  ')',
  '',
  'cd /d "%WORKER_ROOT%"',
  'start "" "%PYTHONW%" -m readji_tts.gui'
)
Set-Content -LiteralPath $batPath -Value $lines -Encoding ASCII

Write-Output "Created: $batPath"
Write-Output 'Double-click that file from now on instead of waiting for the worker to start at logon.'
