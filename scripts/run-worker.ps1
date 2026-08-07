$ErrorActionPreference = 'Stop'

$workerRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $workerRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonPath)) {
  throw "Readji TTS virtual environment is missing: $pythonPath"
}

Set-Location -LiteralPath $workerRoot
& $pythonPath -m readji_tts.worker
