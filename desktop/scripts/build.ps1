$ErrorActionPreference = 'Stop'

$DesktopRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $DesktopRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Desktop virtual environment is missing. Run scripts\bootstrap.ps1 first.'
}

Push-Location $DesktopRoot
try {
    & $PythonPath -m build --no-isolation
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
