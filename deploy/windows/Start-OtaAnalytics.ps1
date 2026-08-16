# Launch InTouch OTA Analytics for the office network.
#
# Reads its settings from local-env.ps1 beside this file (gitignored), then starts the dashboard
# bound to every interface so colleagues can reach it. Registered with Task Scheduler it starts
# at boot and restarts if it stops — see docs/DEPLOY.md.
#
#   .\deploy\windows\Start-OtaAnalytics.ps1
#   .\deploy\windows\Start-OtaAnalytics.ps1 -Host 127.0.0.1     # local only

[CmdletBinding()]
param(
    # 0.0.0.0 means "every interface", which is what lets other machines connect. main.py
    # refuses this unless a dashboard password is configured.
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No virtual environment at $python. Create it with: python -m venv .venv"
}

# Secrets come from a file that is never committed. Dot-sourcing rather than parsing keeps it a
# plain PowerShell file with no format of its own to get wrong.
$envFile = Join-Path $PSScriptRoot "local-env.ps1"
if (Test-Path $envFile) {
    . $envFile
} else {
    Write-Warning "No local-env.ps1 found. Copy local-env.example.ps1 and set the passwords."
}

# The platform password normally stays in Windows Credential Manager and never appears here.
# Task Scheduler jobs sometimes cannot reach the credential vault in a non-interactive session,
# and OTA_PLATFORM_PASSWORD in local-env.ps1 is the fallback for exactly that case.
if (-not $env:OTA_ADMIN_PASSWORD_HASH) {
    Write-Warning "OTA_ADMIN_PASSWORD_HASH is not set - the dashboard has no password."
    Write-Warning "Generate one with: .\.venv\Scripts\python.exe -m ota_analytics.cli passwd --role admin"
}

Write-Host "Starting on ${BindHost}:${Port} ..." -ForegroundColor Cyan
& $python main.py --host $BindHost --port $Port --no-browser
exit $LASTEXITCODE
