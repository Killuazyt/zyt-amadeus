[CmdletBinding()]
param(
    [string]$OnedirPath = ''
)

$ErrorActionPreference = 'Stop'
$DesktopRoot = Split-Path -Parent $PSScriptRoot
$LicenseRoot = Join-Path $DesktopRoot 'src\amadeus_desktop\resources\licenses'
$ManifestPath = Join-Path $LicenseRoot 'runtime-license-manifest.json'
$NoticePath = Join-Path $LicenseRoot 'THIRD_PARTY_NOTICES.txt'
$RuntimeLockPath = Join-Path $DesktopRoot 'requirements.lock'
$DevLockPath = Join-Path $DesktopRoot 'requirements-dev.lock'
$RepositoryLicensePath = Join-Path (Split-Path -Parent $DesktopRoot) 'LICENSE'
$DesktopLicensePath = Join-Path $DesktopRoot 'LICENSE'

function Normalize-PackageName {
    param([Parameter(Mandatory = $true)][string]$Name)
    return ($Name.Trim().ToLowerInvariant() -replace '[-_.]+', '-')
}

function Read-LockedPackages {
    param([Parameter(Mandatory = $true)][string]$Path)
    $result = @{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match '^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)\s*$') {
            $name = Normalize-PackageName $Matches[1]
            if ($result.ContainsKey($name)) { throw "Duplicate locked distribution: $name" }
            $result[$name] = $Matches[2]
        }
    }
    if ($result.Count -eq 0) { throw 'Runtime dependency lock could not be parsed' }
    return $result
}

foreach ($required in @(
    $RepositoryLicensePath,
    $DesktopLicensePath,
    $ManifestPath,
    $NoticePath,
    (Join-Path $LicenseRoot 'QT_LGPL_COMPLIANCE.txt'),
    (Join-Path $LicenseRoot 'LGPL-3.0.txt'),
    (Join-Path $LicenseRoot 'GPL-3.0.txt'),
    (Join-Path $LicenseRoot 'CC0-1.0.txt'),
    (Join-Path $LicenseRoot 'KURISU-ASSET-NOTICE.txt'),
    (Join-Path $LicenseRoot 'KURISU-ICON-NOTICE.txt'),
    (Join-Path $LicenseRoot 'PYTHON-3.11-LICENSE.txt'),
    (Join-Path $LicenseRoot 'PYINSTALLER_COPYING.txt'),
    (Join-Path $LicenseRoot 'INNO_SETUP_LICENSE.txt')
)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw 'A required release license artifact is missing'
    }
}

if (
    (Get-FileHash -LiteralPath $RepositoryLicensePath -Algorithm SHA256).Hash -ne
    (Get-FileHash -LiteralPath $DesktopLicensePath -Algorithm SHA256).Hash
) {
    throw 'Repository and desktop MIT license files differ'
}

$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.schema_version -ne 1 -or $manifest.python.version -ne '3.11') {
    throw 'Runtime license manifest schema or Python series is invalid'
}
$lockedPackages = Read-LockedPackages $RuntimeLockPath
$manifestPackages = @{}
foreach ($package in @($manifest.packages)) {
    $name = Normalize-PackageName ([string]$package.name)
    if ($manifestPackages.ContainsKey($name)) { throw "Duplicate license manifest entry: $name" }
    if (
        [string]::IsNullOrWhiteSpace([string]$package.version) -or
        [string]::IsNullOrWhiteSpace([string]$package.license) -or
        -not ([uri]::IsWellFormedUriString([string]$package.source, [UriKind]::Absolute))
    ) {
        throw "Incomplete license manifest entry: $name"
    }
    $manifestPackages[$name] = [string]$package.version
}

$missingManifest = @($lockedPackages.Keys | Where-Object { -not $manifestPackages.ContainsKey($_) })
$unexpectedManifest = @($manifestPackages.Keys | Where-Object { -not $lockedPackages.ContainsKey($_) })
if ($missingManifest.Count -ne 0 -or $unexpectedManifest.Count -ne 0) {
    throw 'Runtime lock and license manifest package sets differ'
}
foreach ($name in $lockedPackages.Keys) {
    if ($manifestPackages[$name] -ne $lockedPackages[$name]) {
        throw "Runtime lock and license version differ for $name"
    }
}

$notice = Get-Content -LiteralPath $NoticePath -Raw -Encoding UTF8
foreach ($package in @($manifest.packages)) {
    $entryPattern = '(?im)^' + [regex]::Escape([string]$package.name) + ' ' +
        [regex]::Escape([string]$package.version) + ' \|'
    if ($notice -notmatch $entryPattern) {
        throw "Third-party notice is missing $($package.name) $($package.version)"
    }
}

$components = @{}
foreach ($component in @($manifest.bundled_components)) {
    $components[[string]$component.name] = [string]$component.version
}
if ($components['Inno Setup'] -ne '6.7.3') { throw 'Inno Setup license identity is not fixed' }
if ($components['PyInstaller bootloader'] -ne '6.21.0') {
    throw 'PyInstaller bootloader license identity is not fixed'
}
$kurisuComponent = @(
    $manifest.bundled_components |
        Where-Object { [string]$_.name -eq 'Amadeus built-in Kurisu 4x high-resolution spritesheet derivative' }
)
if (
    $kurisuComponent.Count -ne 1 -or
    [string]$kurisuComponent[0].version -ne 'sha256:cca259ac33ffc7c8170b401a315f9a177a865eb063ba44a4da87c3ab13fa90b7' -or
    [string]$kurisuComponent[0].license -ne 'NOASSERTION'
) {
    throw 'Built-in Kurisu asset identity or NOASSERTION status is invalid'
}
$kurisuNotice = Get-Content -LiteralPath (Join-Path $LicenseRoot 'KURISU-ASSET-NOTICE.txt') -Raw -Encoding UTF8
if (
    $kurisuNotice -notmatch 'NOASSERTION' -or
    $kurisuNotice -notmatch 'cca259ac33ffc7c8170b401a315f9a177a865eb063ba44a4da87c3ab13fa90b7' -or
    $kurisuNotice -notmatch 'not\s+independently\s+verified'
) {
    throw 'Built-in Kurisu asset risk notice is incomplete'
}
$kurisuIconComponent = @(
    $manifest.bundled_components |
        Where-Object { [string]$_.name -eq 'Amadeus Kurisu portrait application icon derivative' }
)
if (
    $kurisuIconComponent.Count -ne 1 -or
    [string]$kurisuIconComponent[0].version -ne 'sha256:ded30eeb568f26e3df64998131698e472603bf48d531885b27a7c04293d3b0b5' -or
    [string]$kurisuIconComponent[0].license -ne 'NOASSERTION'
) {
    throw 'Kurisu application icon identity or NOASSERTION status is invalid'
}
$kurisuIconNotice = Get-Content -LiteralPath (Join-Path $LicenseRoot 'KURISU-ICON-NOTICE.txt') -Raw -Encoding UTF8
if (
    $kurisuIconNotice -notmatch 'NOASSERTION' -or
    $kurisuIconNotice -notmatch 'ded30eeb568f26e3df64998131698e472603bf48d531885b27a7c04293d3b0b5' -or
    $kurisuIconNotice -notmatch 'not\s+independently\s+verified'
) {
    throw 'Kurisu application icon risk notice is incomplete'
}
$devPackages = Read-LockedPackages $DevLockPath
if ($devPackages['pyinstaller'] -ne $components['PyInstaller bootloader']) {
    throw 'PyInstaller bootloader notice differs from the development lock'
}
foreach ($qtPackage in @('pyside6', 'pyside6-addons', 'pyside6-essentials', 'shiboken6')) {
    $entry = @($manifest.packages | Where-Object { (Normalize-PackageName $_.name) -eq $qtPackage })
    if ($entry.Count -ne 1 -or $entry[0].version -ne '6.11.1' -or $entry[0].license -ne 'LGPL-3.0-only') {
        throw "Qt for Python LGPL selection is invalid for $qtPackage"
    }
}

if (-not [string]::IsNullOrWhiteSpace($OnedirPath)) {
    $resolvedOnedir = (Resolve-Path -LiteralPath $OnedirPath -ErrorAction Stop).Path
    $onedirItem = Get-Item -LiteralPath $resolvedOnedir -Force
    if (-not $onedirItem.PSIsContainer -or ($onedirItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'OnedirPath must be a real, non-reparse directory'
    }
    $internal = Join-Path $resolvedOnedir '_internal'
    $packagedLicenseRoot = Join-Path $internal 'amadeus_desktop\resources\licenses'
    foreach ($name in @(
        'runtime-license-manifest.json',
        'THIRD_PARTY_NOTICES.txt',
        'QT_LGPL_COMPLIANCE.txt',
        'LGPL-3.0.txt',
        'GPL-3.0.txt',
        'CC0-1.0.txt',
        'KURISU-ASSET-NOTICE.txt',
        'KURISU-ICON-NOTICE.txt',
        'PYTHON-3.11-LICENSE.txt',
        'PYINSTALLER_COPYING.txt',
        'INNO_SETUP_LICENSE.txt'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $packagedLicenseRoot $name) -PathType Leaf)) {
            throw "Packaged license artifact is missing: $name"
        }
    }

    $packagedPetRoot = Join-Path $internal 'amadeus_desktop\resources\builtin_pet'
    $packagedPetSheet = Join-Path $packagedPetRoot 'spritesheet.webp'
    $packagedPetManifestPath = Join-Path $packagedPetRoot 'pet.amadeus.json'
    $packagedPetNoticePath = Join-Path $packagedPetRoot 'LICENSE.txt'
    foreach ($requiredPetFile in @($packagedPetSheet, $packagedPetManifestPath, $packagedPetNoticePath)) {
        if (-not (Test-Path -LiteralPath $requiredPetFile -PathType Leaf)) {
            throw 'Packaged built-in pet resource is incomplete'
        }
    }
    if (
        (Get-FileHash -LiteralPath $packagedPetSheet -Algorithm SHA256).Hash.ToLowerInvariant() -cne
        'cca259ac33ffc7c8170b401a315f9a177a865eb063ba44a4da87c3ab13fa90b7'
    ) {
        throw 'Packaged built-in pet spritesheet differs from the approved lossless WebP'
    }
    $packagedPetManifest = Get-Content -LiteralPath $packagedPetManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        [string]$packagedPetManifest.id -ne 'builtin-amadeus' -or
        [string]$packagedPetManifest.license -ne 'NOASSERTION' -or
        [string]$packagedPetManifest.spritesheet.path -ne 'spritesheet.webp' -or
        [int]$packagedPetManifest.spritesheet.frameWidth -ne 768 -or
        [int]$packagedPetManifest.spritesheet.frameHeight -ne 832 -or
        [int]$packagedPetManifest.spritesheet.logicalFrameWidth -ne 192 -or
        [int]$packagedPetManifest.spritesheet.logicalFrameHeight -ne 208 -or
        [int]$packagedPetManifest.spritesheet.columns -ne 8 -or
        [int]$packagedPetManifest.spritesheet.rows -ne 9
    ) {
        throw 'Packaged built-in pet manifest identity is invalid'
    }
    $packagedPetNotice = Get-Content -LiteralPath $packagedPetNoticePath -Raw -Encoding UTF8
    if (
        $packagedPetNotice -notmatch 'NOASSERTION' -or
        $packagedPetNotice -notmatch 'not\s+independently\s+verified'
    ) {
        throw 'Packaged built-in pet notice is incomplete'
    }

    $packagedIconRoot = Join-Path $internal 'amadeus_desktop\resources\app_icon'
    $packagedKurisuIcon = Join-Path $packagedIconRoot 'amadeus-kurisu.png'
    $packagedIconNotice = Join-Path $packagedIconRoot 'LICENSE.txt'
    foreach ($requiredIconFile in @($packagedKurisuIcon, $packagedIconNotice)) {
        if (-not (Test-Path -LiteralPath $requiredIconFile -PathType Leaf)) {
            throw 'Packaged application icon resource is incomplete'
        }
    }
    if (
        (Get-FileHash -LiteralPath $packagedKurisuIcon -Algorithm SHA256).Hash.ToLowerInvariant() -cne
        'ded30eeb568f26e3df64998131698e472603bf48d531885b27a7c04293d3b0b5'
    ) {
        throw 'Packaged Kurisu application icon differs from the approved PNG'
    }
    $packagedIconNoticeText = Get-Content -LiteralPath $packagedIconNotice -Raw -Encoding UTF8
    if (
        $packagedIconNoticeText -notmatch 'NOASSERTION' -or
        $packagedIconNoticeText -notmatch 'not\s+independently\s+verified'
    ) {
        throw 'Packaged Kurisu application icon notice is incomplete'
    }

    $metadataPackages = @{}
    foreach ($metadataPath in Get-ChildItem -LiteralPath $internal -File -Filter 'METADATA' -Recurse) {
        if ($metadataPath.Directory.Name -notlike '*.dist-info') { continue }
        $metadata = Get-Content -LiteralPath $metadataPath.FullName -Encoding UTF8
        $nameLine = $metadata | Where-Object { $_ -match '^Name:\s*(.+)$' } | Select-Object -First 1
        $metadataName = Normalize-PackageName ([regex]::Match($nameLine, '^Name:\s*(.+)$').Groups[1].Value)
        $versionLine = $metadata | Where-Object { $_ -match '^Version:\s*(.+)$' } | Select-Object -First 1
        $metadataVersion = [regex]::Match($versionLine, '^Version:\s*(.+)$').Groups[1].Value.Trim()
        if ([string]::IsNullOrWhiteSpace($metadataName) -or [string]::IsNullOrWhiteSpace($metadataVersion)) {
            throw 'Packaged distribution metadata is incomplete'
        }
        if ($metadataPackages.ContainsKey($metadataName) -and $metadataPackages[$metadataName] -ne $metadataVersion) {
            throw "Conflicting packaged metadata versions for $metadataName"
        }
        $metadataPackages[$metadataName] = $metadataVersion
    }
    $missingMetadata = @($manifestPackages.Keys | Where-Object { -not $metadataPackages.ContainsKey($_) })
    $unexpectedMetadata = @($metadataPackages.Keys | Where-Object { -not $manifestPackages.ContainsKey($_) })
    if ($missingMetadata.Count -ne 0 -or $unexpectedMetadata.Count -ne 0) {
        throw 'Onedir distribution metadata and license manifest package sets differ'
    }
    foreach ($name in $manifestPackages.Keys) {
        if ($metadataPackages[$name] -ne $manifestPackages[$name]) {
            throw "Onedir distribution metadata version differs for $name"
        }
    }

    $forbiddenQtNames = @(
        'Qt6Charts.dll',
        'Qt6DataVisualization.dll',
        'Qt6Graphs.dll',
        'Qt6Lottie.dll',
        'Qt6Pdf.dll',
        'Qt6PdfWidgets.dll',
        'Qt6VirtualKeyboard.dll',
        'qpdf.dll',
        'qtvirtualkeyboardplugin.dll'
    )
    foreach ($name in $forbiddenQtNames) {
        if (Get-ChildItem -LiteralPath $internal -File -Filter $name -Recurse -ErrorAction SilentlyContinue) {
            throw "Unselected Qt module or plugin is present: $name"
        }
    }
    foreach ($name in @('Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Network.dll', 'Qt6Svg.dll', 'Qt6Widgets.dll')) {
        if (-not (Test-Path -LiteralPath (Join-Path $internal "PySide6\$name") -PathType Leaf)) {
            throw "Required dynamically replaceable Qt library is missing: $name"
        }
    }
    if (-not (Get-ChildItem -LiteralPath $internal -File -Filter 'win32cred.pyd' -Recurse)) {
        throw 'Packaged WinCred extension is missing'
    }

    $buildInfoPath = Join-Path $internal 'amadeus_desktop\resources\build-info.json'
    $buildInfo = Get-Content -LiteralPath $buildInfoPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $buildInfo.schema_version -ne 1 -or
        $buildInfo.version -ne '0.7.0.dev7' -or
        $buildInfo.commit_sha -notmatch '^[0-9a-f]{40}$' -or
        $buildInfo.build_date_utc -notmatch '^\d{4}-\d{2}-\d{2}$'
    ) {
        throw 'Packaged deterministic build information is invalid'
    }
}

Write-Output "Release license closure passed: $($manifestPackages.Count) locked runtime distributions."
