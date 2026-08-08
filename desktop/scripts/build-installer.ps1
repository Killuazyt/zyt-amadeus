[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ModelPath,
    [string]$PythonPath = '',
    [string]$InnoSetupPath = '',
    [ValidateRange(1, 20)]
    [int]$LifecycleCycles = 20,
    [switch]$BuildAcceptanceBaseline,
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'
$ExpectedProjectVersion = '0.7.0.dev7'
$ExpectedInnoVersion = '6.7.3'
$DesktopRoot = Split-Path -Parent $PSScriptRoot
$RepositoryRoot = Split-Path -Parent $DesktopRoot
$BuildRoot = [IO.Path]::GetFullPath((Join-Path $DesktopRoot 'build'))
$OnedirPath = [IO.Path]::GetFullPath((Join-Path $BuildRoot 'onedir-dist\Amadeus'))
$InstallerRoot = [IO.Path]::GetFullPath((Join-Path $BuildRoot 'installer'))
$PackagingBuildRoot = [IO.Path]::GetFullPath((Join-Path $BuildRoot 'packaging'))
$IconPath = Join-Path $PackagingBuildRoot 'amadeus.ico'
$InstallerScript = Join-Path $DesktopRoot 'packaging\amadeus.iss'
$ProjectLicense = Join-Path $RepositoryRoot 'LICENSE'
$ManifestPath = Join-Path $InstallerRoot 'SHA256SUMS.txt'

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $DesktopRoot '.venv\Scripts\python.exe'
}
$PythonPath = [IO.Path]::GetFullPath($PythonPath)
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Selected Python executable is missing. Run scripts\bootstrap.ps1 or pass -PythonPath.'
}

$buildPrefix = $BuildRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
foreach ($candidate in @($OnedirPath, $InstallerRoot, $PackagingBuildRoot)) {
    if (-not $candidate.StartsWith($buildPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installer build output escaped the desktop build directory'
    }
}

$resolvedModelPath = (Resolve-Path -LiteralPath $ModelPath -ErrorAction Stop).Path
$modelItem = Get-Item -LiteralPath $resolvedModelPath -Force
if (-not $modelItem.PSIsContainer -or ($modelItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'ModelPath must be a real, non-reparse directory'
}

$gitTopLevel = (& git -C $RepositoryRoot rev-parse --show-toplevel 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($gitTopLevel) -ne [IO.Path]::GetFullPath($RepositoryRoot)) {
    throw 'Installer build must run from the canonical repository checkout'
}
$gitStatus = @(& git -C $RepositoryRoot status --porcelain=v1 --untracked-files=all)
if ($LASTEXITCODE -ne 0) { throw 'Git working-tree state could not be inspected' }
if ($gitStatus.Count -ne 0 -and -not $AllowDirty) {
    throw 'Installer build requires a clean Git working tree'
}
if ($gitStatus.Count -ne 0 -and $AllowDirty) {
    Write-Warning 'Building an uncommitted local acceptance artifact; never publish this output.'
}
$commitSha = (& git -C $RepositoryRoot rev-parse HEAD 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $commitSha -notmatch '^[0-9a-f]{40}$') {
    throw 'Installer build requires an exact Git commit SHA'
}

$pyproject = Get-Content -LiteralPath (Join-Path $DesktopRoot 'pyproject.toml') -Raw -Encoding UTF8
$versionMatch = [regex]::Match($pyproject, '(?m)^version = "([^"]+)"\s*$')
if (-not $versionMatch.Success -or $versionMatch.Groups[1].Value -ne $ExpectedProjectVersion) {
    throw "Installer build requires project version $ExpectedProjectVersion"
}

if ($BuildAcceptanceBaseline) {
    $installerVersion = '0.6.0.dev6'
    $numericVersion = '0.6.0.6'
    $outputBaseFilename = 'Amadeus-0.6.0.dev6-win64-acceptance-baseline-setup'
}
else {
    $installerVersion = $ExpectedProjectVersion
    $numericVersion = '0.7.0.7'
    $outputBaseFilename = 'Amadeus-0.7.0.dev7-win64-setup'
}
$expectedInstallerPath = Join-Path $InstallerRoot ($outputBaseFilename + '.exe')

function Resolve-InnoCompiler {
    if (-not [string]::IsNullOrWhiteSpace($InnoSetupPath)) {
        $candidate = [IO.Path]::GetFullPath($InnoSetupPath)
    }
    else {
        $installations = @(
            Get-ItemProperty `
                'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', `
                'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', `
                'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' `
                -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.DisplayName -eq 'Inno Setup version 6.7.3' -and
                    $_.DisplayVersion -eq $ExpectedInnoVersion -and
                    -not [string]::IsNullOrWhiteSpace($_.InstallLocation)
                }
        )
        $compilerPaths = @(
            $installations |
                ForEach-Object { Join-Path $_.InstallLocation 'ISCC.exe' } |
                Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
                Select-Object -Unique
        )
        if ($compilerPaths.Count -ne 1) {
            throw 'Exactly one registered Inno Setup 6.7.3 compiler is required; pass -InnoSetupPath to disambiguate'
        }
        $candidate = [IO.Path]::GetFullPath($compilerPaths[0])
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw 'Inno Setup compiler is missing'
    }

    $matchingRegistration = @(
        Get-ItemProperty `
            'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', `
            'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', `
            'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' `
            -ErrorAction SilentlyContinue |
            Where-Object {
                $_.DisplayName -eq 'Inno Setup version 6.7.3' -and
                $_.DisplayVersion -eq $ExpectedInnoVersion -and
                -not [string]::IsNullOrWhiteSpace($_.InstallLocation) -and
                [IO.Path]::GetFullPath((Join-Path $_.InstallLocation 'ISCC.exe')) -eq $candidate
            }
    )
    if ($matchingRegistration.Count -eq 0) {
        throw 'Selected Inno Setup compiler is not the registered 6.7.3 installation'
    }
    $versionInfo = (Get-Item -LiteralPath $candidate).VersionInfo
    if (
        $versionInfo.ProductName.Trim() -ne 'Inno Setup' -or
        $versionInfo.CompanyName.Trim() -ne 'Jordan Russell' -or
        $versionInfo.ProductVersion.Trim() -ne '0.0.0.0'
    ) {
        throw 'Selected compiler has unexpected Inno Setup product metadata'
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $candidate
    if (
        $signature.Status -ne [Management.Automation.SignatureStatus]::Valid -or
        $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch '(^|, )O=Pyrsys B\.V\.(,|$)'
    ) {
        throw 'Selected Inno Setup compiler does not have the expected valid publisher signature'
    }
    return $candidate
}

$InnoCompiler = Resolve-InnoCompiler
& (Join-Path $DesktopRoot 'scripts\check-release-licenses.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Release license source closure check failed' }

& (Join-Path $DesktopRoot 'scripts\build-onedir.ps1') `
    -Mode Bundled `
    -ModelPath $resolvedModelPath `
    -PythonPath $PythonPath `
    -LifecycleCycles $LifecycleCycles
if ($LASTEXITCODE -ne 0) { throw 'Bundled onedir build failed' }
if (-not (Test-Path -LiteralPath (Join-Path $OnedirPath 'Amadeus.exe') -PathType Leaf)) {
    throw 'Verified bundled onedir is missing'
}
& $PythonPath (Join-Path $DesktopRoot 'scripts\payload_manifest.py') verify --root $OnedirPath
if ($LASTEXITCODE -ne 0) { throw 'Bundled onedir payload manifest verification failed' }
if (-not (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
    throw 'Generated Kurisu application icon is missing'
}
if (-not (Test-Path -LiteralPath $ProjectLicense -PathType Leaf)) {
    throw 'Project MIT license is missing'
}

[void](New-Item -ItemType Directory -Path $InstallerRoot -Force)
if (Test-Path -LiteralPath $expectedInstallerPath) {
    Remove-Item -LiteralPath $expectedInstallerPath -Force
}

$compilerArguments = @(
    "/DSourceDir=$OnedirPath",
    "/DOutputDir=$InstallerRoot",
    "/DAppVersion=$installerVersion",
    "/DNumericVersion=$numericVersion",
    "/DOutputBaseFilename=$outputBaseFilename",
    "/DAppIcon=$IconPath",
    "/DProjectLicense=$ProjectLicense",
    $InstallerScript
)
$compilerOutput = @(& $InnoCompiler @compilerArguments 2>&1)
$compilerExitCode = $LASTEXITCODE
$compilerOutput | ForEach-Object { Write-Output $_ }
if ($compilerExitCode -ne 0) { throw 'Inno Setup installer build failed' }
if (($compilerOutput -join "`n") -notmatch 'Compiler engine version: Inno Setup 6\.7\.3') {
    throw 'Inno Setup compiler did not report the fixed 6.7.3 engine version'
}
if (-not (Test-Path -LiteralPath $expectedInstallerPath -PathType Leaf)) {
    throw 'Expected installer output is missing'
}
$installerVersionInfo = (Get-Item -LiteralPath $expectedInstallerPath).VersionInfo
if (
    $installerVersionInfo.CompanyName.Trim() -ne 'Killuazyt' -or
    $installerVersionInfo.ProductName.Trim() -ne 'Amadeus Desktop Pet' -or
    $installerVersionInfo.ProductVersion.Trim() -ne $numericVersion
) {
    throw 'Installer Windows version information is invalid'
}
$installerSignature = Get-AuthenticodeSignature -LiteralPath $expectedInstallerPath
if ($installerSignature.Status -ne [Management.Automation.SignatureStatus]::NotSigned) {
    throw 'P7 test installer must be unsigned; signing is reserved for a later release decision'
}

& $PythonPath (Join-Path $DesktopRoot 'scripts\scan_artifacts.py') --allow-model --path $expectedInstallerPath
if ($LASTEXITCODE -ne 0) { throw 'Installer artifact privacy scan failed' }

$installerFiles = @(
    Get-ChildItem -LiteralPath $InstallerRoot -File -Filter 'Amadeus-*-setup.exe' |
        Sort-Object Name
)
$manifestLines = foreach ($file in $installerFiles) {
    $digest = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$digest  $($file.Name)"
}
if ($manifestLines.Count -eq 0) { throw 'No installer exists for the SHA-256 manifest' }
[IO.File]::WriteAllText(
    $ManifestPath,
    (($manifestLines -join "`n") + "`n"),
    [Text.UTF8Encoding]::new($false)
)

$expectedHash = (Get-FileHash -LiteralPath $expectedInstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "Installer: $expectedInstallerPath"
Write-Output "SHA256: $expectedHash"
Write-Output "Manifest: $ManifestPath"
Write-Output "Commit: $commitSha"
