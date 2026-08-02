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
        $deadline = [DateTime]::UtcNow.AddSeconds(120)
        $observedChild = $false
        $observedConnection = $false
        while (-not $process.HasExited -and [DateTime]::UtcNow -lt $deadline) {
            $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($process.Id)" -ErrorAction Stop)
            $observedChild = $observedChild -or $children.Count -gt 0
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
        $remainingChildren = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($process.Id)" -ErrorAction Stop)
        if ($observedChild -or $remainingChildren.Count -gt 0) {
            throw "$ProbeName created a child process"
        }
        if ($observedConnection) {
            throw "$ProbeName opened a TCP connection or listening port"
        }
    }
    finally {
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
    }
}

$savedBuildEnvironment = @{
    AMADEUS_PYINSTALLER_MODEL_DIR = $env:AMADEUS_PYINSTALLER_MODEL_DIR
    HF_HOME = $env:HF_HOME
    HUGGINGFACE_HUB_CACHE = $env:HUGGINGFACE_HUB_CACHE
    HF_HUB_OFFLINE = $env:HF_HUB_OFFLINE
    HF_HUB_DISABLE_XET = $env:HF_HUB_DISABLE_XET
    TRANSFORMERS_OFFLINE = $env:TRANSFORMERS_OFFLINE
}
try {
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
        $NoticePath = Join-Path $OutputPath '_internal\amadeus_desktop\resources\licenses\P5B_THIRD_PARTY_NOTICES.txt'
        $PackagedModelPath = Join-Path $OutputPath '_internal\amadeus_desktop\resources\embedding_model'
        if (-not (Test-Path -LiteralPath $ExecutablePath)) { throw 'onedir executable missing' }
        if (-not (Test-Path -LiteralPath $NoticePath)) { throw 'P5B notices missing from onedir' }
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
            for ($cycle = 1; $cycle -le $LifecycleCycles; $cycle++) {
                $instanceName = 'amadeus-acceptance-' + [Guid]::NewGuid().ToString('N')
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
