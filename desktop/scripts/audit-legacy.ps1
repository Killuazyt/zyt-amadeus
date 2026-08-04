[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$DesktopRoot = Split-Path -Parent $PSScriptRoot
$RepositoryRoot = Split-Path -Parent $DesktopRoot
$violations = [ordered]@{}

function Add-Violation {
    param([Parameter(Mandatory = $true)][string]$Category)

    if (-not $violations.Contains($Category)) {
        $violations[$Category] = 0
    }
    $violations[$Category] = [int]$violations[$Category] + 1
}

function Invoke-GitLines {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = @(& git -C $RepositoryRoot @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'git_audit_command_failed'
    }
    return @($output | ForEach-Object { [string]$_ })
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $RepositoryRoot '.git'))) {
        throw 'repository_metadata_missing'
    }

    $tracked = @(Invoke-GitLines -Arguments @('ls-files'))
    if ($tracked.Count -eq 0) {
        Add-Violation -Category 'tracked_tree_empty'
    }

    $legacyRootPatterns = @(
        '^(electron|public|service|src)/',
        '^\.env(?:\.|$)',
        '^(?:index\.html|package(?:-lock)?\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?)$',
        '^(?:eslint|postcss|tailwind|tsconfig|vite)\..+$'
    )
    foreach ($path in $tracked) {
        $normalized = $path.Replace('\', '/')
        if ($legacyRootPatterns | Where-Object { $normalized -match $_ }) {
            Add-Violation -Category 'legacy_runtime_path_tracked'
        }
    }

    $dependencyFiles = @(
        (Join-Path $DesktopRoot 'pyproject.toml'),
        (Join-Path $DesktopRoot 'requirements.lock'),
        (Join-Path $DesktopRoot 'requirements-dev.lock')
    )
    $legacyDependencyPattern = '(?im)^\s*(?:fastapi|fastrtc|aiortc|webrtcvad|uvicorn|nodejs|electron)(?:\[[^\]]+\])?\s*(?:==|>=|<=|~=|>|<)'
    foreach ($file in $dependencyFiles) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            Add-Violation -Category 'dependency_manifest_missing'
            continue
        }
        $content = Get-Content -LiteralPath $file -Raw -Encoding UTF8
        if ($content -match $legacyDependencyPattern) {
            Add-Violation -Category 'legacy_runtime_dependency'
        }
    }

    $expectedTags = [ordered]@{
        'archive/legacy-main-44d1270' = '44d1270c655b3ae1f68c655e16e7333df2411076'
        'archive/legacy-web-voice-handoff-3ff9486' = '3ff948655b48a3b5049910e174f083ec2087f450'
    }
    $legacySentinels = @(
        'package.json',
        'electron/main.mjs',
        'service/webrtc/server.py',
        'src/App.tsx',
        'src/store/chatStore.tsx'
    )
    foreach ($entry in $expectedTags.GetEnumerator()) {
        $tagRef = 'refs/tags/' + $entry.Key
        $tagType = @(Invoke-GitLines -Arguments @('cat-file', '-t', $tagRef))
        if ($tagType.Count -ne 1 -or $tagType[0] -ne 'tag') {
            Add-Violation -Category 'archive_tag_not_annotated'
            continue
        }
        $peeledRef = $tagRef + '^{}'
        $target = @(Invoke-GitLines -Arguments @('rev-parse', $peeledRef))
        if ($target.Count -ne 1 -or $target[0] -ne $entry.Value) {
            Add-Violation -Category 'archive_tag_target_mismatch'
            continue
        }
        $tree = @(Invoke-GitLines -Arguments @('ls-tree', '-r', '--name-only', $peeledRef))
        foreach ($sentinel in $legacySentinels) {
            if ($tree -notcontains $sentinel) {
                Add-Violation -Category 'archive_tag_incomplete'
            }
        }
    }

    $handoffRef = 'refs/tags/archive/legacy-web-voice-handoff-3ff9486^{}'
    $storeSource = @(Invoke-GitLines -Arguments @('show', "${handoffRef}:src/store/chatStore.tsx")) -join "`n"
    if ($storeSource -notmatch 'localStorage\.(?:getItem|setItem)') {
        Add-Violation -Category 'legacy_persistence_shape_changed'
    }
    $legacyTree = @(Invoke-GitLines -Arguments @('ls-tree', '-r', '--name-only', $handoffRef))
    $unexpectedDurableStores = @(
        $legacyTree | Where-Object {
            $_ -match '(?i)(?:^|/)(?:[^/]+\.(?:sqlite3?|db)|leveldb)(?:$|/)'
        }
    )
    if ($unexpectedDurableStores.Count -gt 0) {
        Add-Violation -Category 'unexpected_legacy_durable_store'
    }
}
catch {
    Add-Violation -Category ([string]$_.Exception.Message)
}

$result = [ordered]@{
    schema = 'amadeus-p7-legacy-audit/v1'
    status = if ($violations.Count -eq 0) { 'passed' } else { 'failed' }
    tracked_file_count = if (Get-Variable tracked -ErrorAction SilentlyContinue) { $tracked.Count } else { 0 }
    archive_tag_count = 2
    legacy_persistence = 'chromium_localstorage_and_local_env_only'
    automatic_migration_required = $false
    violations_by_category = $violations
}
$result | ConvertTo-Json -Depth 4 -Compress
if ($violations.Count -ne 0) {
    exit 1
}
