# Keeps a temporary Mini App tunnel in this window. Start the bot with
# .\scripts\run.ps1 -Tunnel in another window: it takes this address.
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
        throw "Сначала выполните .\scripts\setup.ps1."
    }
    & ".\.venv\Scripts\python.exe" -m labdefender.tools tunnel
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
