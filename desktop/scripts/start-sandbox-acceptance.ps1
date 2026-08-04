[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$BaselineInstallerPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$DesktopRoot = Split-Path -Parent $PSScriptRoot
$BuildRoot = [IO.Path]::GetFullPath((Join-Path $DesktopRoot 'build'))
$TemplatePath = Join-Path $DesktopRoot 'packaging\windows-sandbox\p7-acceptance.wsb.template'
$AcceptanceScript = Join-Path $PSScriptRoot 'accept-installer-sandbox.ps1'
$resolvedInstaller = (Resolve-Path -LiteralPath $InstallerPath -ErrorAction Stop).Path
$resolvedBaseline = (Resolve-Path -LiteralPath $BaselineInstallerPath -ErrorAction Stop).Path

foreach ($candidate in @($resolvedInstaller, $resolvedBaseline, $TemplatePath, $AcceptanceScript)) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw 'sandbox_acceptance_input_missing'
    }
}
if ($resolvedInstaller -eq $resolvedBaseline) {
    throw 'sandbox_acceptance_installers_must_differ'
}
if ((Get-Item -LiteralPath $resolvedInstaller).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'sandbox_acceptance_current_installer_is_reparse_point'
}
if ((Get-Item -LiteralPath $resolvedBaseline).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'sandbox_acceptance_baseline_installer_is_reparse_point'
}

$sandboxExecutable = Join-Path $env:SystemRoot 'System32\WindowsSandbox.exe'
if (-not (Test-Path -LiteralPath $sandboxExecutable -PathType Leaf)) {
    throw 'windows_sandbox_is_not_enabled'
}

$runId = [Guid]::NewGuid().ToString('N')
$StageRoot = [IO.Path]::GetFullPath((Join-Path $BuildRoot "sandbox-acceptance\$runId"))
$InputRoot = Join-Path $StageRoot 'input'
$OutputRoot = Join-Path $StageRoot 'output'
$buildPrefix = $BuildRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $StageRoot.StartsWith($buildPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'sandbox_acceptance_stage_escaped_build_root'
}

[void](New-Item -ItemType Directory -Path $InputRoot -Force)
[void](New-Item -ItemType Directory -Path $OutputRoot -Force)
Copy-Item -LiteralPath $resolvedInstaller -Destination (Join-Path $InputRoot 'current-setup.exe')
Copy-Item -LiteralPath $resolvedBaseline -Destination (Join-Path $InputRoot 'baseline-setup.exe')
Copy-Item -LiteralPath $AcceptanceScript -Destination (Join-Path $InputRoot 'accept-installer-sandbox.ps1')

$template = Get-Content -LiteralPath $TemplatePath -Raw -Encoding UTF8
$escapedInput = [Security.SecurityElement]::Escape($InputRoot)
$escapedOutput = [Security.SecurityElement]::Escape($OutputRoot)
$configuration = $template.Replace('__AMADEUS_P7_INPUT__', $escapedInput).Replace(
    '__AMADEUS_P7_OUTPUT__',
    $escapedOutput
)
if ($configuration -match '__AMADEUS_P7_(?:INPUT|OUTPUT)__') {
    throw 'sandbox_acceptance_template_expansion_failed'
}
$WsbPath = Join-Path $StageRoot 'p7-acceptance.wsb'
[IO.File]::WriteAllText($WsbPath, $configuration, [Text.UTF8Encoding]::new($false))

$sandbox = Start-Process -FilePath $sandboxExecutable -ArgumentList @("`"$WsbPath`"") -PassThru
if (-not $sandbox.WaitForExit(1800000)) {
    Stop-Process -Id $sandbox.Id -Force -ErrorAction SilentlyContinue
    throw 'sandbox_acceptance_timed_out'
}

$summaryPath = Join-Path $OutputRoot 'acceptance-summary.json'
if (-not (Test-Path -LiteralPath $summaryPath -PathType Leaf)) {
    throw 'sandbox_acceptance_summary_missing'
}
try {
    $summary = Get-Content -LiteralPath $summaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw 'sandbox_acceptance_summary_invalid'
}
if ($summary.schema -ne 'amadeus-p7-sandbox-acceptance/v1' -or $summary.status -ne 'passed') {
    throw 'sandbox_acceptance_failed'
}
$expectedScenarios = @(
    'clean_windows_runtime',
    'install_and_upgrade_preserve_state',
    'running_application_mutex',
    'default_uninstall_preserves_user_data',
    'reparse_cleanup_fails_closed',
    'explicit_delete_data_uninstall',
    'unicode_space_custom_install_path'
)
$actualScenarios = @($summary.scenarios | ForEach-Object {
    if ($_.status -ne 'passed') { throw 'sandbox_acceptance_scenario_failed' }
    [string]$_.name
})
if (
    [int]$summary.scenario_count -ne $expectedScenarios.Count -or
    $actualScenarios.Count -ne $expectedScenarios.Count -or
    @(Compare-Object -ReferenceObject $expectedScenarios -DifferenceObject $actualScenarios).Count -ne 0
) {
    throw 'sandbox_acceptance_scenario_set_invalid'
}
$currentHash = (Get-FileHash -LiteralPath $resolvedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
$baselineHash = (Get-FileHash -LiteralPath $resolvedBaseline -Algorithm SHA256).Hash.ToLowerInvariant()
if (
    [string]$summary.current_installer_sha256 -ne $currentHash -or
    [string]$summary.baseline_installer_sha256 -ne $baselineHash
) {
    throw 'sandbox_acceptance_hash_mismatch'
}

[ordered]@{
    schema = 'amadeus-p7-sandbox-host/v1'
    status = 'passed'
    installer_sha256 = $currentHash
    baseline_installer_sha256 = $baselineHash
    scenario_count = @($summary.scenarios).Count
    result_directory = "build/sandbox-acceptance/$runId/output"
} | ConvertTo-Json -Compress
