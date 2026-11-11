param([switch]$Tunnel)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
        throw "Сначала выполните .\scripts\setup.ps1."
    }
    & ".\.venv\Scripts\python.exe" -m labdefender.tools doctor
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $runArgs = @("-m", "labdefender")
    if ($Tunnel) { $runArgs += "--tunnel" }
    & ".\.venv\Scripts\python.exe" @runArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
