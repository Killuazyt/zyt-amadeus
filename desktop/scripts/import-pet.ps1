param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [switch]$Replace
)

$ErrorActionPreference = 'Stop'
$DesktopRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $DesktopRoot '.venv\Scripts\python.exe'
$Arguments = @('-m', 'amadeus_desktop.tools.pet_import', $Path)
if ($Replace) { $Arguments += '--replace' }

Push-Location $DesktopRoot
try {
    & $PythonPath @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
