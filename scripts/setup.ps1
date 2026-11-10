param([string]$Python = "python", [switch]$Dev)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & $Python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) { throw "Нужен Python 3.11+. Укажите путь через -Python." }
    if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Не удалось создать .venv." }
    }
    $package = if ($Dev) { ".[dev]" } else { "." }
    & ".\.venv\Scripts\python.exe" -m pip install -e $package
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости." }
    if (-not (Test-Path -LiteralPath ".env")) {
        Copy-Item -LiteralPath ".env.example" -Destination ".env"
    }
    Write-Host "Готово. Заполните BOT_TOKEN в .env, затем выполните .\scripts\run.ps1."
} finally {
    Pop-Location
}
