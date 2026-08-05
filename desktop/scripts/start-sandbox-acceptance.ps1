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
$DefenderHelper = Join-Path $PSScriptRoot 'invoke-sandbox-defender.ps1'
$BuildInfoPath = Join-Path $BuildRoot 'packaging\build-info.json'
$resolvedInstaller = (Resolve-Path -LiteralPath $InstallerPath -ErrorAction Stop).Path
$resolvedBaseline = (Resolve-Path -LiteralPath $BaselineInstallerPath -ErrorAction Stop).Path

foreach ($candidate in @(
    $resolvedInstaller,
    $resolvedBaseline,
    $TemplatePath,
    $AcceptanceScript,
    $DefenderHelper,
    $BuildInfoPath
)) {
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
if ((Get-Item -LiteralPath $BuildInfoPath).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'sandbox_acceptance_build_info_is_reparse_point'
}

try {
    $expectedBuildInfo = Get-Content -LiteralPath $BuildInfoPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw 'sandbox_acceptance_build_info_invalid'
}
$buildInfoProperties = @($expectedBuildInfo.PSObject.Properties.Name | Sort-Object)
if (
    $buildInfoProperties.Count -ne 4 -or
    ($buildInfoProperties -join ',') -cne 'build_date_utc,commit_sha,schema_version,version' -or
    [int]$expectedBuildInfo.schema_version -ne 1 -or
    [string]$expectedBuildInfo.version -cne '0.7.0.dev7' -or
    [string]$expectedBuildInfo.commit_sha -cnotmatch '^[0-9a-f]{40}$' -or
    [string]$expectedBuildInfo.build_date_utc -cnotmatch '^\d{4}-\d{2}-\d{2}$'
) {
    throw 'sandbox_acceptance_build_info_invalid'
}
$repositoryRoot = Split-Path -Parent $DesktopRoot
$worktreeStatus = @(& git -C $repositoryRoot status --porcelain=v1 --untracked-files=all 2>$null)
if ($LASTEXITCODE -ne 0) {
    throw 'sandbox_acceptance_git_status_unavailable'
}
if ($worktreeStatus.Count -ne 0) {
    throw 'sandbox_acceptance_git_worktree_dirty'
}
$headSha = (& git -C $repositoryRoot rev-parse HEAD 2>$null).Trim()
$headTimestamp = (& git -C $repositoryRoot show -s --format=%cI HEAD 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $headSha -notmatch '^[0-9a-f]{40}$' -or -not $headTimestamp) {
    throw 'sandbox_acceptance_git_identity_unavailable'
}
try {
    $headBuildDate = [DateTimeOffset]::Parse(
        $headTimestamp,
        [Globalization.CultureInfo]::InvariantCulture
    ).UtcDateTime.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
}
catch {
    throw 'sandbox_acceptance_git_identity_invalid'
}
if (
    [string]$expectedBuildInfo.commit_sha -cne $headSha -or
    [string]$expectedBuildInfo.build_date_utc -cne $headBuildDate
) {
    throw 'sandbox_acceptance_build_info_not_current_head'
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
Copy-Item -LiteralPath $DefenderHelper -Destination (Join-Path $InputRoot 'invoke-sandbox-defender.ps1')
Copy-Item -LiteralPath $BuildInfoPath -Destination (Join-Path $InputRoot 'expected-build-info.json')

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
if (-not $sandbox.WaitForExit(3600000)) {
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
if ($summary.schema -ne 'amadeus-p7-sandbox-acceptance/v2' -or $summary.status -ne 'passed') {
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
$defenderEvidencePath = Join-Path $OutputRoot 'defender-evidence.json'
if (-not (Test-Path -LiteralPath $defenderEvidencePath -PathType Leaf)) {
    throw 'sandbox_acceptance_defender_evidence_missing'
}
$defenderEvidenceItem = Get-Item -LiteralPath $defenderEvidencePath -ErrorAction Stop
if (
    $defenderEvidenceItem.Attributes -band [IO.FileAttributes]::ReparsePoint -or
    $defenderEvidenceItem.Length -le 0 -or
    $defenderEvidenceItem.Length -gt 65536
) {
    throw 'sandbox_acceptance_defender_evidence_file_invalid'
}
try {
    $rawDefenderEvidence = Get-Content -LiteralPath $defenderEvidencePath -Raw -Encoding UTF8 |
        ConvertFrom-Json
}
catch {
    throw 'sandbox_acceptance_defender_evidence_json_invalid'
}
if (
    [string]$rawDefenderEvidence.schema -cne [string]$summary.defender.schema -or
    [string]$rawDefenderEvidence.status -cne [string]$summary.defender.status -or
    [string]$rawDefenderEvidence.execution_identity.integrity_level -cne
        [string]$summary.defender.execution_identity.integrity_level -or
    [string]$rawDefenderEvidence.current_installer.target_sha256 -cne
        [string]$summary.defender.current_installer.target_sha256 -or
    [string]$rawDefenderEvidence.installed_directory.target_sha256 -cne
        [string]$summary.defender.installed_directory.target_sha256
) {
    throw 'sandbox_acceptance_defender_evidence_copy_mismatch'
}
$defenderEvidenceHash = (
    Get-FileHash -LiteralPath $defenderEvidencePath -Algorithm SHA256
).Hash.ToLowerInvariant()
if (
    [bool]$summary.execution_identity.is_elevated -or
    [bool]$summary.execution_identity.is_administrator_role -or
    [string]$summary.execution_identity.integrity_level -cne 'medium' -or
    [int]$summary.non_elevated_mutation_count -lt 1
) {
    throw 'sandbox_acceptance_execution_identity_invalid'
}
if (
    [int]$summary.clean_runtime.forbidden_runtime_detection_count -ne 0 -or
    [int]$summary.forbidden_runtime_product_count -ne 0
) {
    throw 'sandbox_acceptance_runtime_evidence_invalid'
}
if (
    [string]$summary.build_identity.version -cne [string]$expectedBuildInfo.version -or
    [string]$summary.build_identity.commit_sha -cne [string]$expectedBuildInfo.commit_sha -or
    [string]$summary.build_identity.build_date_utc -cne [string]$expectedBuildInfo.build_date_utc
) {
    throw 'sandbox_acceptance_build_identity_mismatch'
}
if (
    [string]$summary.defender.schema -cne 'amadeus-p7-sandbox-defender/v1' -or
    [string]$summary.defender.status -cne 'passed' -or
    [bool]$summary.defender.execution_identity.is_elevated -ne $true -or
    [bool]$summary.defender.execution_identity.is_administrator_role -ne $true -or
    [string]$summary.defender.execution_identity.integrity_level -cne 'high' -or
    [string]$summary.defender.product_status.scanner_signature_status -cne 'Valid' -or
    [string]$summary.defender.product_status.scanner_publisher -cne 'Microsoft' -or
    [string]$summary.defender.current_installer.status -cne 'passed' -or
    [string]$summary.defender.current_installer.target_sha256 -cne $currentHash -or
    [string]$summary.defender.installed_directory.status -cne 'passed' -or
    [string]$summary.defender.installed_directory.target_kind -cne 'directory_payload' -or
    [string]$summary.defender.installed_directory.target_sha256 -cnotmatch '^[0-9a-f]{64}$'
) {
    throw 'sandbox_acceptance_defender_evidence_invalid'
}
foreach ($scan in @($summary.defender.current_installer, $summary.defender.installed_directory)) {
    if (
        [int]$scan.exit_code -ne 0 -or
        -not [bool]$scan.target_digest_verified_after_scan -or
        -not [bool]$scan.remediation_disabled -or
        [int]$scan.new_detection_count -ne 0 -or
        -not [string]$scan.started_utc -or
        -not [string]$scan.finished_utc -or
        -not [string]$scan.antimalware_product_version -or
        -not [string]$scan.engine_version -or
        -not [string]$scan.antivirus_signature_version -or
        -not [string]$scan.scanner_file_version
    ) {
        throw 'sandbox_acceptance_defender_scan_metadata_invalid'
    }
}
if (
    [string]$summary.installed_payload_scan.status -cne 'passed' -or
    [int]$summary.installed_payload_scan.files_scanned -le 0 -or
    [int]$summary.installed_payload_scan.model_file_count -ne 6 -or
    [string]$summary.installed_payload_scan.payload_sha256 -cne [string]$summary.defender.installed_directory.target_sha256
) {
    throw 'sandbox_acceptance_installed_payload_scan_invalid'
}
if (
    [int]$summary.sqlite_fts_fixture.fixture_row_count -ne 1 -or
    [int]$summary.sqlite_fts_fixture.fts_match_count -ne 1 -or
    [int]$summary.sqlite_fts_fixture.verification_count -lt 8 -or
    [string]$summary.sqlite_fts_fixture.runtime_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
    [string]$summary.sqlite_fts_fixture.dependency_sha256 -cnotmatch '^[0-9a-f]{64}$'
) {
    throw 'sandbox_acceptance_sqlite_fixture_invalid'
}
if (
    [int]$summary.start_menu_shortcuts.application_shortcut_count -lt 1 -or
    [int]$summary.start_menu_shortcuts.uninstall_shortcut_count -lt 1
) {
    throw 'sandbox_acceptance_shortcut_evidence_invalid'
}

$hostSummary = [ordered]@{
    schema = 'amadeus-p7-sandbox-host/v2'
    status = 'passed'
    installer_sha256 = $currentHash
    baseline_installer_sha256 = $baselineHash
    commit_sha = [string]$summary.build_identity.commit_sha
    build_date_utc = [string]$summary.build_identity.build_date_utc
    installed_payload_sha256 = [string]$summary.installed_payload_scan.payload_sha256
    defender_evidence_sha256 = $defenderEvidenceHash
    scenario_count = @($summary.scenarios).Count
    result_directory = "build/sandbox-acceptance/$runId/output"
}
$hostSummaryJson = $hostSummary | ConvertTo-Json -Compress
$resolvedOutputRoot = (Resolve-Path -LiteralPath $OutputRoot -ErrorAction Stop).Path
if ((Get-Item -LiteralPath $resolvedOutputRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'sandbox_acceptance_host_output_is_reparse_point'
}
$hostSummaryPath = [IO.Path]::GetFullPath((Join-Path $resolvedOutputRoot 'sandbox-host-summary.json'))
$hostSummaryTemporaryPath = [IO.Path]::GetFullPath((Join-Path $resolvedOutputRoot 'sandbox-host-summary.json.tmp'))
$outputPrefix = $resolvedOutputRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (
    -not $hostSummaryPath.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    -not $hostSummaryTemporaryPath.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    (Test-Path -LiteralPath $hostSummaryPath) -or
    (Test-Path -LiteralPath $hostSummaryTemporaryPath)
) {
    throw 'sandbox_acceptance_host_summary_boundary_invalid'
}
[IO.File]::WriteAllText(
    $hostSummaryTemporaryPath,
    $hostSummaryJson,
    [Text.UTF8Encoding]::new($false)
)
[IO.File]::Move($hostSummaryTemporaryPath, $hostSummaryPath)
Write-Output $hostSummaryJson
