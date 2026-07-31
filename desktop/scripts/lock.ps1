$ErrorActionPreference = 'Stop'

$DesktopRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $DesktopRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Desktop virtual environment is missing. Create it and install pip-tools first.'
}

Push-Location $DesktopRoot
try {
    & $PythonPath -m piptools compile --cache-dir .pip-tools-cache --strip-extras --output-file requirements.lock pyproject.toml
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $PythonPath -m piptools compile --cache-dir .pip-tools-cache --allow-unsafe --extra dev --strip-extras --output-file requirements-dev.lock pyproject.toml
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
