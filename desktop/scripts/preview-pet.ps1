param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [string]$ExportDir
)

$ErrorActionPreference = 'Stop'
$DesktopRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $DesktopRoot '.venv\Scripts\python.exe'
$Arguments = @('-m', 'amadeus_desktop.tools.pet_preview', $Path)
if ($ExportDir) { $Arguments += @('--export-dir', $ExportDir) }

Push-Location $DesktopRoot
try {
    & $PythonPath @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
