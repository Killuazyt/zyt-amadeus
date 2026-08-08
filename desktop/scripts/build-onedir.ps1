param(
    [ValidateSet('Degraded', 'Bundled')]
    [string]$Mode = 'Degraded',
    [string]$ModelPath = '',
    [string]$PythonPath = '',
    [ValidateRange(1, 20)]
    [int]$LifecycleCycles = 1
)

$ErrorActionPreference = 'Stop'

$DesktopRoot = Split-Path -Parent $PSScriptRoot
$RepositoryRoot = Split-Path -Parent $DesktopRoot
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $DesktopRoot '.venv\Scripts\python.exe'
}
$PythonPath = [IO.Path]::GetFullPath($PythonPath)
$SpecPath = Join-Path $DesktopRoot 'packaging\amadeus-desktop.spec'
$BuildRoot = [IO.Path]::GetFullPath((Join-Path $DesktopRoot 'build'))
$DistPath = [IO.Path]::GetFullPath((Join-Path $BuildRoot 'onedir-dist'))
$WorkPath = [IO.Path]::GetFullPath((Join-Path $BuildRoot 'onedir-work'))
$OutputPath = Join-Path $DistPath 'Amadeus'
$BuildCacheRoot = [IO.Path]::GetFullPath((Join-Path $BuildRoot 'isolated-cache'))
$PackagingBuildRoot = [IO.Path]::GetFullPath((Join-Path $BuildRoot 'packaging'))
$BuildInfoPath = Join-Path $PackagingBuildRoot 'build-info.json'
$IconPath = Join-Path $PackagingBuildRoot 'amadeus.ico'
$PayloadManifestScript = Join-Path $DesktopRoot 'scripts\payload_manifest.py'
$ExpectedVersion = '0.7.0.dev7'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Selected Python executable is missing. Run scripts\bootstrap.ps1 or pass -PythonPath.'
}

[void](New-Item -ItemType Directory -Path $BuildRoot -Force)
$buildPrefix = $BuildRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
foreach ($candidate in @($DistPath, $WorkPath)) {
    if (-not $candidate.StartsWith($buildPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'PyInstaller output path escaped the desktop build directory'
    }
}

function Get-ProjectVersion {
    $pyproject = Get-Content -LiteralPath (Join-Path $DesktopRoot 'pyproject.toml') -Raw -Encoding UTF8
    $match = [regex]::Match($pyproject, '(?m)^version = "([^"]+)"\s*$')
    if (-not $match.Success) {
        throw 'Project version could not be read from pyproject.toml'
    }
    $init = Get-Content -LiteralPath (Join-Path $DesktopRoot 'src\amadeus_desktop\__init__.py') -Raw -Encoding UTF8
    $initMatch = [regex]::Match($init, '(?m)^__version__ = "([^"]+)"\s*$')
    if (-not $initMatch.Success -or $initMatch.Groups[1].Value -ne $match.Groups[1].Value) {
        throw 'Project version metadata is inconsistent'
    }
    return $match.Groups[1].Value
}

function New-DeterministicPackagingResources {
    $projectVersion = Get-ProjectVersion
    if ($projectVersion -ne $ExpectedVersion) {
        throw "P7 onedir requires project version $ExpectedVersion"
    }
    $commitSha = (& git -C $RepositoryRoot rev-parse HEAD 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or $commitSha -notmatch '^[0-9a-f]{40}$') {
        throw 'Exact Git commit SHA could not be determined'
    }
    $commitTimestamp = (& git -C $RepositoryRoot show -s --format=%cI HEAD 2>$null).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'Git commit timestamp could not be determined'
    }
    try {
        $commitDate = [DateTimeOffset]::Parse(
            $commitTimestamp,
            [Globalization.CultureInfo]::InvariantCulture
        )
        $buildDate = $commitDate.UtcDateTime.ToString(
            'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    catch {
        throw 'Git commit timestamp was invalid'
    }

    [void](New-Item -ItemType Directory -Path $PackagingBuildRoot -Force)
    $buildInfo = [ordered]@{
        schema_version = 1
        version = $projectVersion
        commit_sha = $commitSha
        build_date_utc = $buildDate
    }
    $json = ($buildInfo | ConvertTo-Json -Compress) + "`n"
    [IO.File]::WriteAllText($BuildInfoPath, $json, [Text.UTF8Encoding]::new($false))
    $env:SOURCE_DATE_EPOCH = $commitDate.ToUnixTimeSeconds().ToString(
        [Globalization.CultureInfo]::InvariantCulture
    )
    $env:PYTHONHASHSEED = '0'

    $iconScript = Join-Path $DesktopRoot 'scripts\generate_app_icon.py'
    $iconSource = Join-Path $DesktopRoot 'src\amadeus_desktop\resources\app_icon\amadeus-kurisu.png'
    & $PythonPath $iconScript --source $iconSource --output $IconPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $IconPath)) {
        throw 'Pinned Kurisu application icon generation failed'
    }
}

function Convert-CimProcessCreationDateToUtc {
    param(
        [Parameter(Mandatory = $true)]$CreationDate
    )

    if ($CreationDate -is [DateTimeOffset]) {
        return $CreationDate.UtcDateTime
    }
    if ($CreationDate -is [DateTime]) {
        return $CreationDate.ToUniversalTime()
    }
    return [Management.ManagementDateTimeConverter]::ToDateTime(
        [string]$CreationDate
    ).ToUniversalTime()
}

function Get-VerifiedChildProcesses {
    param(
        [Parameter(Mandatory = $true)][int]$ParentProcessId,
        [Parameter(Mandatory = $true)][DateTime]$ParentCreationTimeUtc,
        [Parameter(Mandatory = $true)][DateTime]$ObservationEndTimeUtc
    )

    # Win32_Process documents that process IDs are reusable. A stale process can
    # therefore report the new probe PID as ParentProcessId even though it was
    # created before the probe. CreationDate disambiguates that case without
    # weakening detection of children actually created during the probe.
    $candidates = @(
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $ParentProcessId" -ErrorAction Stop
    )
    $verified = @()
    foreach ($candidate in $candidates) {
        try {
            $childCreationTimeUtc = Convert-CimProcessCreationDateToUtc $candidate.CreationDate
            if (
                $childCreationTimeUtc -ge $ParentCreationTimeUtc -and
                $childCreationTimeUtc -le $ObservationEndTimeUtc
            ) {
                $verified += $candidate
            }
        }
        catch {
            # A candidate whose identity cannot be disambiguated must fail closed.
            $verified += $candidate
        }
    }
    return $verified
}

function Invoke-PackagedProbe {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$ProbeName
    )

    $probeRoot = Join-Path ([IO.Path]::GetTempPath()) ('amadeus-p5b-probe-' + [Guid]::NewGuid().ToString('N'))
    $resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $resolvedProbeRoot = [IO.Path]::GetFullPath($probeRoot)
    if (-not $resolvedProbeRoot.StartsWith($resolvedTempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'probe directory escaped the system temporary directory'
    }
    [void](New-Item -ItemType Directory -Path $resolvedProbeRoot)
    $process = $null
    $savedEnvironment = @{
        LOCALAPPDATA = $env:LOCALAPPDATA
        HF_HOME = $env:HF_HOME
        HUGGINGFACE_HUB_CACHE = $env:HUGGINGFACE_HUB_CACHE
        HF_HUB_OFFLINE = $env:HF_HUB_OFFLINE
        TRANSFORMERS_OFFLINE = $env:TRANSFORMERS_OFFLINE
        QT_QPA_PLATFORM = $env:QT_QPA_PLATFORM
    }
    try {
        $env:LOCALAPPDATA = Join-Path $resolvedProbeRoot 'localappdata'
        $env:HF_HOME = Join-Path $resolvedProbeRoot 'empty-hf-home'
        $env:HUGGINGFACE_HUB_CACHE = Join-Path $resolvedProbeRoot 'empty-hf-cache'
        $env:HF_HUB_OFFLINE = '1'
        $env:TRANSFORMERS_OFFLINE = '1'
        $env:QT_QPA_PLATFORM = 'offscreen'
        $process = Start-Process -FilePath $ExecutablePath -ArgumentList $Arguments -PassThru -WindowStyle Hidden
        $processCreationTimeUtc = $process.StartTime.ToUniversalTime()
        $deadline = [DateTime]::UtcNow.AddSeconds(120)
        $observedChildren = @()
        $observedConnection = $false
        while (-not $process.HasExited -and [DateTime]::UtcNow -lt $deadline) {
            $children = @(
                Get-VerifiedChildProcesses `
                    -ParentProcessId $process.Id `
                    -ParentCreationTimeUtc $processCreationTimeUtc `
                    -ObservationEndTimeUtc ([DateTime]::MaxValue)
            )
            foreach ($child in $children) {
                $observedChildren += $child
            }
            $connections = @(
                Get-CimInstance -Namespace root/StandardCimv2 -ClassName MSFT_NetTCPConnection -Filter "OwningProcess = $($process.Id)" -ErrorAction Stop
            )
            $observedConnection = $observedConnection -or $connections.Count -gt 0
            Start-Sleep -Milliseconds 25
            $process.Refresh()
        }
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
            throw "$ProbeName timed out"
        }
        if ($process.ExitCode -ne 0) {
            throw "$ProbeName failed with exit code $($process.ExitCode)"
        }
        $processExitTimeUtc = $process.ExitTime.ToUniversalTime()
        $remainingChildren = @(
            Get-VerifiedChildProcesses `
                -ParentProcessId $process.Id `
                -ParentCreationTimeUtc $processCreationTimeUtc `
                -ObservationEndTimeUtc $processExitTimeUtc
        )
        $observedChildren += $remainingChildren
        $verifiedObservedChildren = @()
        foreach ($child in $observedChildren) {
            try {
                $childCreationTimeUtc = Convert-CimProcessCreationDateToUtc $child.CreationDate
                if (
                    $childCreationTimeUtc -ge $processCreationTimeUtc -and
                    $childCreationTimeUtc -le $processExitTimeUtc
                ) {
                    $verifiedObservedChildren += $child
                }
            }
            catch {
                # Preserve fail-closed handling for a candidate with no usable timestamp.
                $verifiedObservedChildren += $child
            }
        }
        if ($verifiedObservedChildren.Count -gt 0) {
            $childEvidence = @(
                $verifiedObservedChildren |
                    ForEach-Object { "$($_.Name)#$($_.ProcessId)" } |
                    Sort-Object -Unique
            ) -join ', '
            throw "$ProbeName created a child process: $childEvidence"
        }
        if ($observedConnection) {
            throw "$ProbeName opened a TCP connection or listening port"
        }
    }
    finally {
        $processCleanupFailed = $false
        if ($null -ne $process) {
            try {
                $process.Refresh()
                if (-not $process.HasExited) {
                    Stop-Process -Id $process.Id -Force -ErrorAction Stop
                    if (-not $process.WaitForExit(5000)) {
                        $processCleanupFailed = $true
                    }
                }
            }
            catch {
                $processCleanupFailed = $true
            }
        }
        foreach ($name in $savedEnvironment.Keys) {
            $value = $savedEnvironment[$name]
            if ($null -eq $value) {
                Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
            }
            else {
                Set-Item -LiteralPath ("Env:" + $name) -Value $value
            }
        }
        if (Test-Path -LiteralPath $resolvedProbeRoot) {
            Remove-Item -LiteralPath $resolvedProbeRoot -Recurse -Force
        }
        if ($processCleanupFailed) {
            throw "$ProbeName could not clean up its process"
        }
    }
}

$savedBuildEnvironment = @{
    AMADEUS_PYINSTALLER_MODEL_DIR = $env:AMADEUS_PYINSTALLER_MODEL_DIR
    AMADEUS_PYINSTALLER_BUILD_INFO = $env:AMADEUS_PYINSTALLER_BUILD_INFO
    AMADEUS_PYINSTALLER_ICON = $env:AMADEUS_PYINSTALLER_ICON
    SOURCE_DATE_EPOCH = $env:SOURCE_DATE_EPOCH
    PYTHONHASHSEED = $env:PYTHONHASHSEED
    HF_HOME = $env:HF_HOME
    HUGGINGFACE_HUB_CACHE = $env:HUGGINGFACE_HUB_CACHE
    HF_HUB_OFFLINE = $env:HF_HUB_OFFLINE
    HF_HUB_DISABLE_XET = $env:HF_HUB_DISABLE_XET
    TRANSFORMERS_OFFLINE = $env:TRANSFORMERS_OFFLINE
}
try {
    New-DeterministicPackagingResources
    $env:AMADEUS_PYINSTALLER_BUILD_INFO = $BuildInfoPath
    $env:AMADEUS_PYINSTALLER_ICON = $IconPath
    [void](New-Item -ItemType Directory -Path $BuildCacheRoot -Force)
    $env:HF_HOME = Join-Path $BuildCacheRoot 'hf-home'
    $env:HUGGINGFACE_HUB_CACHE = Join-Path $BuildCacheRoot 'hf-cache'
    $env:HF_HUB_OFFLINE = '1'
    $env:HF_HUB_DISABLE_XET = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    if ($Mode -eq 'Bundled') {
        if (-not $ModelPath) {
            $ModelPath = Join-Path $env:LOCALAPPDATA 'Amadeus\models\bge-small-zh-v1.5\46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59'
        }
        $resolvedModelPath = (Resolve-Path -LiteralPath $ModelPath -ErrorAction Stop).Path
        & $PythonPath -m amadeus_desktop.tools.model verify --path $resolvedModelPath
        if ($LASTEXITCODE -ne 0) { throw 'fixed model verification failed' }
        $env:AMADEUS_PYINSTALLER_MODEL_DIR = $resolvedModelPath
    }
    else {
        Remove-Item Env:AMADEUS_PYINSTALLER_MODEL_DIR -ErrorAction SilentlyContinue
    }

    Push-Location $DesktopRoot
    try {
        & $PythonPath -m PyInstaller --clean --noconfirm --distpath $DistPath --workpath $WorkPath $SpecPath
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller onedir build failed' }

        $ExecutablePath = Join-Path $OutputPath 'Amadeus.exe'
        $NoticePath = Join-Path $OutputPath '_internal\amadeus_desktop\resources\licenses\THIRD_PARTY_NOTICES.txt'
        $QtNoticePath = Join-Path $OutputPath '_internal\amadeus_desktop\resources\licenses\QT_LGPL_COMPLIANCE.txt'
        $BuildInfoOutputPath = Join-Path $OutputPath '_internal\amadeus_desktop\resources\build-info.json'
        $PackagedModelPath = Join-Path $OutputPath '_internal\amadeus_desktop\resources\embedding_model'
        $QtPlatformPluginPath = Join-Path $OutputPath '_internal\PySide6\plugins\platforms\qwindows.dll'
        $QtImagePluginRoot = Join-Path $OutputPath '_internal\PySide6\plugins\imageformats'
        $WinCredPath = Get-ChildItem -LiteralPath (Join-Path $OutputPath '_internal') -Filter 'win32cred.pyd' -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not (Test-Path -LiteralPath $ExecutablePath)) { throw 'onedir executable missing' }
        if (-not (Test-Path -LiteralPath $NoticePath)) { throw 'third-party notices missing from onedir' }
        if (-not (Test-Path -LiteralPath $QtNoticePath)) { throw 'Qt LGPL compliance notice missing from onedir' }
        if (-not (Test-Path -LiteralPath $BuildInfoOutputPath)) { throw 'build info missing from onedir' }
        if (-not (Test-Path -LiteralPath $QtPlatformPluginPath)) { throw 'Qt Windows platform plugin missing from onedir' }
        foreach ($plugin in @('qgif.dll', 'qico.dll', 'qjpeg.dll', 'qsvg.dll', 'qwebp.dll')) {
            if (-not (Test-Path -LiteralPath (Join-Path $QtImagePluginRoot $plugin))) {
                throw "Qt image plugin missing from onedir: $plugin"
            }
        }
        if ($null -eq $WinCredPath) { throw 'win32cred.pyd missing from onedir' }
        $expectedCommitSha = (& git -C $RepositoryRoot rev-parse HEAD 2>$null).Trim()
        $expectedCommitTimestamp = (& git -C $RepositoryRoot show -s --format=%cI HEAD 2>$null).Trim()
        if ($LASTEXITCODE -ne 0 -or $expectedCommitSha -notmatch '^[0-9a-f]{40}$') {
            throw 'Packaged identity could not be compared with the exact Git HEAD'
        }
        try {
            $expectedBuildDate = [DateTimeOffset]::Parse(
                $expectedCommitTimestamp,
                [Globalization.CultureInfo]::InvariantCulture
            ).UtcDateTime.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        }
        catch {
            throw 'Exact Git HEAD timestamp was invalid'
        }
        $packagedBuildInfo = Get-Content -LiteralPath $BuildInfoOutputPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if (
            $packagedBuildInfo.schema_version -ne 1 -or
            $packagedBuildInfo.version -ne $ExpectedVersion -or
            $packagedBuildInfo.commit_sha -ne $expectedCommitSha -or
            $packagedBuildInfo.build_date_utc -ne $expectedBuildDate
        ) {
            throw 'Packaged build information does not exactly match the current Git HEAD'
        }
        $executableVersionInfo = (Get-Item -LiteralPath $ExecutablePath).VersionInfo
        if (
            $executableVersionInfo.CompanyName.Trim() -ne 'Killuazyt' -or
            $executableVersionInfo.FileDescription.Trim() -ne 'Amadeus Desktop Pet' -or
            $executableVersionInfo.FileVersion.Trim() -ne $ExpectedVersion -or
            $executableVersionInfo.ProductName.Trim() -ne 'Amadeus Desktop Pet' -or
            $executableVersionInfo.ProductVersion.Trim() -ne $ExpectedVersion
        ) {
            throw 'Frozen executable Windows version information is invalid'
        }
        & (Join-Path $DesktopRoot 'scripts\check-release-licenses.ps1') -OnedirPath $OutputPath
        if ($LASTEXITCODE -ne 0) { throw 'release license closure check failed' }
        & $PythonPath $PayloadManifestScript create --root $OutputPath
        if ($LASTEXITCODE -ne 0) { throw 'payload SHA-256 manifest generation failed' }
        & $PythonPath $PayloadManifestScript verify --root $OutputPath
        if ($LASTEXITCODE -ne 0) { throw 'payload SHA-256 manifest verification failed' }
        if ($Mode -eq 'Degraded') {
            if (Test-Path -LiteralPath $PackagedModelPath) {
                throw 'degraded onedir unexpectedly contains the embedding model'
            }
            Invoke-PackagedProbe -ExecutablePath $ExecutablePath -Arguments @('--fts-degraded-probe') -ProbeName 'packaged FTS degradation probe'
            $instanceName = 'amadeus-acceptance-' + [Guid]::NewGuid().ToString('N')
            Invoke-PackagedProbe -ExecutablePath $ExecutablePath -Arguments @('--embedding-lifecycle-probe=degraded', "--acceptance-instance-name=$instanceName") -ProbeName 'packaged degraded lifecycle probe'
            & $PythonPath (Join-Path $DesktopRoot 'scripts\scan_artifacts.py') --path $OutputPath
            if ($LASTEXITCODE -ne 0) { throw 'degraded onedir artifact scan failed' }
        }
        else {
            if (-not (Test-Path -LiteralPath $PackagedModelPath)) {
                throw 'bundled onedir embedding model missing'
            }
            Invoke-PackagedProbe -ExecutablePath $ExecutablePath -Arguments @('--embedding-model-probe') -ProbeName 'packaged embedding inference probe'
            $credentialProbeId = [Guid]::NewGuid().ToString('N')
            Invoke-PackagedProbe -ExecutablePath $ExecutablePath -Arguments @("--wincred-acceptance-probe=$credentialProbeId") -ProbeName 'packaged WinCred write/read/replace/delete probe'
            # Reuse one endpoint across cycles so a leaked QLocalServer endpoint fails
            # the next launch instead of being hidden by a fresh acceptance name.
            $instanceName = 'amadeus-acceptance-' + [Guid]::NewGuid().ToString('N')
            for ($cycle = 1; $cycle -le $LifecycleCycles; $cycle++) {
                Invoke-PackagedProbe -ExecutablePath $ExecutablePath -Arguments @('--embedding-lifecycle-probe=ready', "--acceptance-instance-name=$instanceName") -ProbeName "packaged model lifecycle probe $cycle/$LifecycleCycles"
            }
            Write-Output "Bundled lifecycle probes passed: $LifecycleCycles/$LifecycleCycles"
            & $PythonPath (Join-Path $DesktopRoot 'scripts\scan_artifacts.py') --allow-model --path $OutputPath
            if ($LASTEXITCODE -ne 0) { throw 'bundled onedir artifact scan failed' }
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    foreach ($name in $savedBuildEnvironment.Keys) {
        $value = $savedBuildEnvironment[$name]
        if ($null -eq $value) {
            Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
        }
        else {
            Set-Item -LiteralPath ("Env:" + $name) -Value $value
        }
    }
    $resolvedBuildCache = [IO.Path]::GetFullPath($BuildCacheRoot)
    if (-not $resolvedBuildCache.StartsWith($buildPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'isolated build cache escaped the desktop build directory'
    }
    if (Test-Path -LiteralPath $resolvedBuildCache) {
        Remove-Item -LiteralPath $resolvedBuildCache -Recurse -Force
    }
}
