$ErrorActionPreference = 'Stop'

$DesktopRoot = Split-Path -Parent $PSScriptRoot
$VenvPath = Join-Path $DesktopRoot '.venv'
$PythonPath = Join-Path $VenvPath 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    py -3.11 -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $PythonPath -m pip install 'pip>=24,<26'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PythonPath -m pip install -r (Join-Path $DesktopRoot 'requirements-dev.lock')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PythonPath -m pip install --no-deps --editable $DesktopRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Output "Amadeus desktop environment ready: $PythonPath"
