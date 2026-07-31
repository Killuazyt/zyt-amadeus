param(
    [string]$Path,
    [string]$Output
)

$ErrorActionPreference = 'Stop'
$DesktopRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $DesktopRoot '.venv\Scripts\python.exe'
$Arguments = @('-m', 'amadeus_desktop.tools.pet_acceptance')
if ($Path) { $Arguments += @('--source', $Path) }
if ($Output) { $Arguments += @('--output', $Output) }

Push-Location $DesktopRoot
try {
    & $PythonPath @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
