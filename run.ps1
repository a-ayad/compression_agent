param(
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1",
    [switch]$Dev    # pass -Dev to enable --reload (auto-restart on file change)
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Also fix pip-warnings about 'amend pip via python -m pip'
$pythonExe = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
}

Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $pythonExe -m pip install --quiet --upgrade pip
& $pythonExe -m pip install --quiet -r requirements.txt

Write-Host "Starting server on http://${BindHost}:${Port}" -ForegroundColor Green

# --reload is opt-in via -Dev: on Windows, the reloader's worker can spawn
# without the ProactorEventLoop policy, which breaks asyncio subprocesses
# (every encode would raise NotImplementedError). Production-style start
# without --reload always keeps the policy that main.py sets at import time.
$args = @("-m", "uvicorn", "app.main:app", "--host", $BindHost, "--port", $Port)
if ($Dev) { $args += "--reload" }
& $pythonExe @args
