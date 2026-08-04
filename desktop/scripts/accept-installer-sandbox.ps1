[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$BaselineInstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RequestedOutputPath = $OutputPath
$OutputPath = 'C:\AmadeusP7\Output\acceptance-summary.json'
$ExpectedVersion = '0.7.0.dev7'
$ProductName = 'Amadeus Desktop Pet'
$CredentialTarget = 'Amadeus/DesktopPet/ChatProviderApiKey'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunValueName = 'Amadeus'
$RunId = [Guid]::NewGuid().ToString('N')
$AppDataRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Amadeus'))
$DefaultInstallRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs\Amadeus'))
$CustomInstallRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs\P7 中文 空格\Amadeus'))
$Regions = @('config', 'data', 'pets', 'personas', 'models', 'backups', 'logs')
$script:Scenarios = [Collections.Generic.List[object]]::new()
$script:InstallerHashes = [ordered]@{}
$script:DefenderEvidence = [ordered]@{}
$script:CurrentInstallRoot = $null
$script:ExternalExport = $null
$script:FakeCredentialSecret = $null

Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class AmadeusP7CredentialProbe
{
    private const uint CredTypeGeneric = 1;
    private const uint CredPersistLocalMachine = 2;
    private const int ErrorNotFound = 1168;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct Credential
    {
        public uint Flags;
        public uint Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredWrite(ref Credential credential, uint flags);

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredRead(string target, uint type, uint flags, out IntPtr credential);

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredDelete(string target, uint type, uint flags);

    [DllImport("advapi32.dll")]
    private static extern void CredFree(IntPtr buffer);

    public static void Write(string target, string userName, string secret)
    {
        byte[] blob = Encoding.Unicode.GetBytes(secret);
        IntPtr blobPointer = Marshal.AllocHGlobal(blob.Length);
        try
        {
            Marshal.Copy(blob, 0, blobPointer, blob.Length);
            Credential credential = new Credential {
                Type = CredTypeGeneric,
                TargetName = target,
                Comment = "Amadeus P7 synthetic acceptance credential",
                CredentialBlobSize = (uint)blob.Length,
                CredentialBlob = blobPointer,
                Persist = CredPersistLocalMachine,
                UserName = userName
            };
            if (!CredWrite(ref credential, 0))
                throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        finally
        {
            for (int index = 0; index < blob.Length; index++)
                Marshal.WriteByte(blobPointer, index, 0);
            Array.Clear(blob, 0, blob.Length);
            Marshal.FreeHGlobal(blobPointer);
        }
    }

    public static string Read(string target)
    {
        IntPtr pointer;
        if (!CredRead(target, CredTypeGeneric, 0, out pointer))
        {
            int error = Marshal.GetLastWin32Error();
            if (error == ErrorNotFound)
                return null;
            throw new Win32Exception(error);
        }
        try
        {
            Credential credential = (Credential)Marshal.PtrToStructure(pointer, typeof(Credential));
            if (credential.CredentialBlobSize == 0)
                return String.Empty;
            byte[] blob = new byte[credential.CredentialBlobSize];
            Marshal.Copy(credential.CredentialBlob, blob, 0, blob.Length);
            try { return Encoding.Unicode.GetString(blob); }
            finally { Array.Clear(blob, 0, blob.Length); }
        }
        finally { CredFree(pointer); }
    }

    public static void Delete(string target)
    {
        if (!CredDelete(target, CredTypeGeneric, 0))
        {
            int error = Marshal.GetLastWin32Error();
            if (error != ErrorNotFound)
                throw new Win32Exception(error);
        }
    }
}
'@

function Throw-Category {
    param([Parameter(Mandatory = $true)][string]$Category)
    throw "p7_$Category"
}

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Category
    )
    if (-not $Condition) {
        Throw-Category -Category $Category
    }
}

function Resolve-ContainedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Category
    )
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $prefix = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
    Assert-True -Condition ($resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) -Category $Category
    Assert-True -Condition (-not ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint)) -Category $Category
    return $resolved
}

function Get-PayloadDigest {
    param([Parameter(Mandatory = $true)][string]$Root)
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $entries = @(
        Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File -ErrorAction Stop |
            Sort-Object FullName |
            ForEach-Object {
                $relative = $_.FullName.Substring($resolvedRoot.Length).TrimStart('\')
                $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                "$relative`t$($_.Length)`t$hash"
            }
    )
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($entries -join "`n"))
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
        $algorithm.Dispose()
    }
}

function Assert-PayloadManifest {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $rootPrefix = $root + [IO.Path]::DirectorySeparatorChar
    $manifestPath = Join-Path $root 'PAYLOAD-SHA256SUMS.txt'
    Assert-True -Condition (Test-Path -LiteralPath $manifestPath -PathType Leaf) -Category 'payload_manifest_missing'
    $expected = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $lines = @(Get-Content -LiteralPath $manifestPath -Encoding UTF8)
    Assert-True -Condition ($lines.Count -gt 0) -Category 'payload_manifest_empty'
    foreach ($line in $lines) {
        $match = [regex]::Match([string]$line, '^([0-9a-f]{64})  (.+)$')
        Assert-True -Condition $match.Success -Category 'payload_manifest_invalid'
        $relative = $match.Groups[2].Value
        Assert-True -Condition (
            $relative -notmatch '[\\\r\n]' -and
            $relative -ne 'PAYLOAD-SHA256SUMS.txt' -and
            -not [IO.Path]::IsPathRooted($relative) -and
            @($relative.Split('/') | Where-Object { $_ -in @('', '.', '..') }).Count -eq 0
        ) -Category 'payload_manifest_path_invalid'
        Assert-True -Condition ($expected.Add($relative.Replace('/', '\'))) -Category 'payload_manifest_duplicate'
        $candidate = Join-Path $root ($relative.Replace('/', '\'))
        $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
        Assert-True -Condition ($resolved.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) -Category 'payload_manifest_path_escaped'
        Assert-True -Condition (-not ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint)) -Category 'payload_manifest_reparse_point'
        $actualHash = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant()
        Assert-True -Condition ($actualHash -eq $match.Groups[1].Value) -Category 'payload_manifest_hash_mismatch'
    }
    foreach ($file in Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction Stop) {
        $relative = $file.FullName.Substring($root.Length).TrimStart('\')
        if ($expected.Contains($relative)) { continue }
        $installerOwned = (
            $relative -eq 'PAYLOAD-SHA256SUMS.txt' -or
            $relative -eq 'LICENSE.txt' -or
            $relative -match '^licenses\\[^\\]+$' -or
            $relative -match '^unins\d+\.(?:dat|exe|msg)$'
        )
        Assert-True -Condition $installerOwned -Category 'installed_payload_has_unverified_extra'
    }
}

function Invoke-CheckedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 300,
        [switch]$Visible,
        [switch]$AllowNonZero
    )
    $options = @{
        FilePath = $FilePath
        ArgumentList = $Arguments
        PassThru = $true
    }
    if (-not $Visible) {
        $options.WindowStyle = 'Hidden'
    }
    $process = Start-Process @options
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Throw-Category -Category 'process_timeout'
    }
    if (-not $AllowNonZero -and $process.ExitCode -ne 0) {
        Throw-Category -Category 'process_failed'
    }
    return $process.ExitCode
}

function Invoke-DefenderInstallerScan {
    param([Parameter(Mandatory = $true)][string]$TargetPath)

    $scannerCandidates = @(
        Join-Path $env:ProgramFiles 'Windows Defender\MpCmdRun.exe'
    )
    $platformRoot = Join-Path $env:ProgramData 'Microsoft\Windows Defender\Platform'
    if (Test-Path -LiteralPath $platformRoot) {
        $scannerCandidates += @(
            Get-ChildItem -LiteralPath $platformRoot -Directory -ErrorAction Stop |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName 'MpCmdRun.exe' }
        )
    }
    $scanner = @($scannerCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }) |
        Select-Object -First 1
    Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($scanner)) -Category 'defender_scanner_missing'
    $scanner = (Resolve-Path -LiteralPath $scanner -ErrorAction Stop).Path
    $scannerSignature = Get-AuthenticodeSignature -LiteralPath $scanner
    Assert-True -Condition ($scannerSignature.Status -eq [Management.Automation.SignatureStatus]::Valid) -Category 'defender_scanner_signature_invalid'
    Assert-True -Condition ($scannerSignature.SignerCertificate.Subject -match 'Microsoft') -Category 'defender_scanner_publisher_invalid'

    $status = Get-MpComputerStatus
    $started = [DateTimeOffset]::UtcNow
    $exitCode = Invoke-CheckedProcess `
        -FilePath $scanner `
        -Arguments @('-Scan', '-ScanType', '3', '-File', $TargetPath) `
        -TimeoutSeconds 600 `
        -AllowNonZero
    $finished = [DateTimeOffset]::UtcNow
    $script:DefenderEvidence = [ordered]@{
        status = if ($exitCode -eq 0) { 'passed' } else { 'failed' }
        target_sha256 = (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToLowerInvariant()
        started_utc = $started.ToString('o')
        finished_utc = $finished.ToString('o')
        exit_code = $exitCode
        antimalware_product_version = [string]$status.AMProductVersion
        engine_version = [string]$status.AMEngineVersion
        antivirus_signature_version = [string]$status.AntivirusSignatureVersion
        scanner_file_version = [string](Get-Item -LiteralPath $scanner).VersionInfo.FileVersion
    }
    Assert-True -Condition ([bool]$status.AMServiceEnabled) -Category 'defender_service_disabled'
    Assert-True -Condition ([bool]$status.AntivirusEnabled) -Category 'defender_antivirus_disabled'
    Assert-True -Condition ($exitCode -eq 0) -Category 'defender_installer_scan_failed'
}

function Invoke-Installer {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$InstallDirectory = '',
        [switch]$EnableAutostart
    )
    $logPath = Join-Path $env:TEMP ("amadeus-p7-setup-$RunId.log")
    $arguments = @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        '/SP-',
        '/NOCLOSEAPPLICATIONS',
        '/NORESTARTAPPLICATIONS',
        "/LOG=`"$logPath`""
    )
    if ($InstallDirectory) {
        $arguments += "/DIR=`"$InstallDirectory`""
    }
    if ($EnableAutostart) {
        $arguments += '/TASKS=autostart'
    }
    try {
        [void](Invoke-CheckedProcess -FilePath $Path -Arguments $arguments)
    }
    finally {
        Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-InstallRecords {
    $roots = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    $matches = @()
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) {
            continue
        }
        foreach ($key in Get-ChildItem -LiteralPath $root -ErrorAction Stop) {
            $item = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop
            $displayName = $item.PSObject.Properties['DisplayName']
            if ($null -ne $displayName -and $displayName.Value -eq $ProductName) {
                $matches += $item
            }
        }
    }
    return @($matches)
}

function Get-InstallRecord {
    $matches = @(Get-InstallRecords)
    Assert-True -Condition ($matches.Count -eq 1) -Category 'uninstall_entry_invalid'
    $locationProperty = $matches[0].PSObject.Properties['InstallLocation']
    $versionProperty = $matches[0].PSObject.Properties['DisplayVersion']
    Assert-True -Condition ($null -ne $locationProperty -and $null -ne $versionProperty) -Category 'uninstall_entry_incomplete'
    $location = [IO.Path]::GetFullPath([string]$locationProperty.Value)
    return [pscustomobject]@{
        Version = [string]$versionProperty.Value
        InstallLocation = $location
    }
}

function Get-RunValue {
    if (-not (Test-Path -LiteralPath $RunKey)) {
        return $null
    }
    $item = Get-ItemProperty -LiteralPath $RunKey -ErrorAction Stop
    $property = $item.PSObject.Properties[$RunValueName]
    if ($null -eq $property) {
        return $null
    }
    return [string]$property.Value
}

function Remove-RunValue {
    if (Test-Path -LiteralPath $RunKey) {
        Remove-ItemProperty -LiteralPath $RunKey -Name $RunValueName -ErrorAction SilentlyContinue
    }
}

function Set-RunValue {
    param([Parameter(Mandatory = $true)][string]$ExecutablePath)
    [void](New-Item -Path $RunKey -Force)
    Set-ItemProperty -LiteralPath $RunKey -Name $RunValueName -Value ('"' + $ExecutablePath + '"') -Type String
}

function Test-CredentialExists {
    param([string]$Target = $CredentialTarget)
    return $null -ne [AmadeusP7CredentialProbe]::Read($Target)
}

function Set-FakeCredential {
    $script:FakeCredentialSecret = "sk-invalid-test-$RunId"
    [AmadeusP7CredentialProbe]::Write(
        $CredentialTarget,
        'Amadeus Desktop Pet',
        $script:FakeCredentialSecret
    )
    Assert-FakeCredential -Category 'fake_credential_seed_failed'
}

function Assert-FakeCredential {
    param([Parameter(Mandatory = $true)][string]$Category)
    Assert-True -Condition ($null -ne $script:FakeCredentialSecret) -Category $Category
    $actual = [AmadeusP7CredentialProbe]::Read($CredentialTarget)
    Assert-True -Condition ($actual -ceq $script:FakeCredentialSecret) -Category $Category
    $actual = $null
}

function Remove-FakeCredential {
    [AmadeusP7CredentialProbe]::Delete($CredentialTarget)
    $script:FakeCredentialSecret = $null
}

function Assert-InstalledPayload {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    Assert-PayloadManifest -InstallRoot $InstallRoot
    $exe = Join-Path $InstallRoot 'Amadeus.exe'
    Assert-True -Condition (Test-Path -LiteralPath $exe -PathType Leaf) -Category 'installed_executable_missing'
    $requiredLeaves = @(
        'win32cred.pyd',
        'qwindows.dll',
        'qjpeg.dll',
        'qico.dll',
        'model_optimized.onnx',
        'spritesheet.png',
        'pet.amadeus.json'
    )
    foreach ($leaf in $requiredLeaves) {
        $matches = @(Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Filter $leaf -ErrorAction Stop)
        Assert-True -Condition ($matches.Count -ge 1) -Category 'installed_payload_incomplete'
    }
    $forbidden = @(
        Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -ErrorAction Stop |
            Where-Object { $_.Name -match '^(?:pythonw?|node|electron)(?:\.exe)?$' }
    )
    Assert-True -Condition ($forbidden.Count -eq 0) -Category 'forbidden_runtime_in_installed_payload'
}

function Assert-StartMenuShortcut {
    param([Parameter(Mandatory = $true)][string]$ExpectedExecutable)
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $links = @(Get-ChildItem -LiteralPath $programs -Recurse -File -Filter '*Amadeus*.lnk' -ErrorAction SilentlyContinue)
    $shell = New-Object -ComObject WScript.Shell
    $matching = @(
        $links | Where-Object {
            $target = $shell.CreateShortcut($_.FullName).TargetPath
            [IO.Path]::GetFullPath($target) -eq [IO.Path]::GetFullPath($ExpectedExecutable)
        }
    )
    Assert-True -Condition ($matching.Count -ge 1) -Category 'start_menu_shortcut_missing'
}

function Assert-NoStartMenuShortcut {
    Assert-True -Condition (Test-NoStartMenuShortcut) -Category 'start_menu_shortcut_not_removed'
}

function Test-NoStartMenuShortcut {
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $links = @(Get-ChildItem -LiteralPath $programs -Recurse -File -Filter '*Amadeus*.lnk' -ErrorAction SilentlyContinue)
    return $links.Count -eq 0
}

function Invoke-AppLifecycle {
    param([Parameter(Mandatory = $true)][string]$ExecutablePath)
    $instance = 'amadeus-acceptance-' + [Guid]::NewGuid().ToString('N')
    [void](Invoke-CheckedProcess -FilePath $ExecutablePath -Arguments @(
        '--embedding-lifecycle-probe=ready',
        "--acceptance-instance-name=$instance"
    ) -TimeoutSeconds 90 -Visible)
}

function Invoke-WinCredProbe {
    param([Parameter(Mandatory = $true)][string]$ExecutablePath)
    $probeTarget = "Amadeus/DesktopPet/WinCredAcceptance/$RunId"
    Assert-True -Condition (-not (Test-CredentialExists -Target $probeTarget)) -Category 'wincred_probe_target_not_clean'
    [void](Invoke-CheckedProcess -FilePath $ExecutablePath -Arguments @("--wincred-acceptance-probe=$RunId") -TimeoutSeconds 30)
    Assert-True -Condition (-not (Test-CredentialExists -Target $probeTarget)) -Category 'wincred_probe_cleanup_failed'
}

function Get-UninstallerPath {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $matches = @(Get-ChildItem -LiteralPath $InstallRoot -File -Filter 'unins*.exe' -ErrorAction Stop)
    Assert-True -Condition ($matches.Count -eq 1) -Category 'uninstaller_missing'
    return $matches[0].FullName
}

function Invoke-Uninstaller {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [switch]$DeleteUserData,
        [switch]$ExpectFailure
    )
    $uninstaller = Get-UninstallerPath -InstallRoot $InstallRoot
    $logPath = Join-Path $env:TEMP ("amadeus-p7-uninstall-$RunId.log")
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$logPath`"")
    if ($DeleteUserData) {
        $arguments += '/DELETEUSERDATA=1'
    }
    try {
        $exitCode = Invoke-CheckedProcess -FilePath $uninstaller -Arguments $arguments -AllowNonZero:$ExpectFailure
        if ($ExpectFailure) {
            Assert-True -Condition ($exitCode -ne 0) -Category 'unsafe_cleanup_uninstall_succeeded'
        }
    }
    finally {
        Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
    }
}

function Wait-UninstallCompletion {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        $records = @(Get-InstallRecords)
        if (
            -not (Test-Path -LiteralPath $InstallRoot) -and
            $records.Count -eq 0 -and
            (Test-NoStartMenuShortcut)
        ) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    Throw-Category -Category 'uninstall_completion_timeout'
}

function Write-SyntheticState {
    Assert-True -Condition (Test-Path -LiteralPath (Join-Path $AppDataRoot 'config\settings.json')) -Category 'settings_not_created'
    Assert-True -Condition (Test-Path -LiteralPath (Join-Path $AppDataRoot 'data\amadeus.sqlite3')) -Category 'database_not_created'
    foreach ($region in $Regions) {
        $directory = Join-Path $AppDataRoot $region
        [void](New-Item -ItemType Directory -Path $directory -Force)
        Assert-True -Condition (-not ((Get-Item -LiteralPath $directory).Attributes -band [IO.FileAttributes]::ReparsePoint)) -Category 'synthetic_region_is_reparse_point'
        [IO.File]::WriteAllText(
            (Join-Path $directory "p7-$RunId.sentinel"),
            'synthetic-p7-acceptance',
            [Text.UTF8Encoding]::new($false)
        )
    }
    $settingsPath = Join-Path $AppDataRoot 'config\settings.json'
    $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $settings.general.launch_at_login = $false
    [IO.File]::WriteAllText(
        $settingsPath,
        ($settings | ConvertTo-Json -Depth 20),
        [Text.UTF8Encoding]::new($false)
    )
    Remove-RunValue
    return [ordered]@{
        settings_sha256 = (Get-FileHash -LiteralPath $settingsPath -Algorithm SHA256).Hash
        database_sha256 = (Get-FileHash -LiteralPath (Join-Path $AppDataRoot 'data\amadeus.sqlite3') -Algorithm SHA256).Hash
    }
}

function Assert-SyntheticState {
    param([Parameter(Mandatory = $true)][Collections.IDictionary]$Hashes)
    foreach ($region in $Regions) {
        Assert-True -Condition (Test-Path -LiteralPath (Join-Path $AppDataRoot "$region\p7-$RunId.sentinel") -PathType Leaf) -Category 'synthetic_region_not_preserved'
    }
    $settingsHash = (Get-FileHash -LiteralPath (Join-Path $AppDataRoot 'config\settings.json') -Algorithm SHA256).Hash
    $databaseHash = (Get-FileHash -LiteralPath (Join-Path $AppDataRoot 'data\amadeus.sqlite3') -Algorithm SHA256).Hash
    Assert-True -Condition ($settingsHash -eq $Hashes['settings_sha256']) -Category 'settings_not_preserved'
    Assert-True -Condition ($databaseHash -eq $Hashes['database_sha256']) -Category 'database_not_preserved'
}

function Assert-CleanMachineRuntime {
    $uninstallRoots = @(
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    $runtimeProducts = 0
    foreach ($root in $uninstallRoots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        foreach ($key in Get-ChildItem -LiteralPath $root -ErrorAction Stop) {
            $name = [string](Get-ItemPropertyValue -LiteralPath $key.PSPath -Name DisplayName -ErrorAction SilentlyContinue)
            if ($name -match '^(?:Python(?:\s|$)|Node\.js(?:\s|$)|Electron(?:\s|$))') {
                $runtimeProducts++
            }
        }
    }
    Assert-True -Condition ($runtimeProducts -eq 0) -Category 'clean_machine_has_forbidden_runtime'
}

function ConvertTo-ComparableVersion {
    param([Parameter(Mandatory = $true)][string]$Value)
    $match = [regex]::Match($Value, '^(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?$')
    Assert-True -Condition $match.Success -Category 'installer_version_format_invalid'
    $development = if ($match.Groups[4].Success) { [int]$match.Groups[4].Value } else { 65535 }
    return [version]::new(
        [int]$match.Groups[1].Value,
        [int]$match.Groups[2].Value,
        [int]$match.Groups[3].Value,
        $development
    )
}

function Assert-LifecycleMutex {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$MutatingExecutable,
        [Parameter(Mandatory = $true)][string[]]$MutatingArguments
    )
    $beforeRecord = Get-InstallRecord
    $beforeHash = Get-PayloadDigest -Root $beforeRecord.InstallLocation
    $instance = 'amadeus-acceptance-' + [Guid]::NewGuid().ToString('N')
    $app = Start-Process -FilePath $ExecutablePath -ArgumentList @(
        '--auto-exit-ms=12000',
        "--acceptance-instance-name=$instance"
    ) -PassThru
    Start-Sleep -Seconds 2
    Assert-True -Condition (-not $app.HasExited) -Category 'mutex_probe_app_not_running'
    $mutation = Start-Process -FilePath $MutatingExecutable -ArgumentList $MutatingArguments -PassThru -WindowStyle Hidden
    $mutationFinished = $mutation.WaitForExit(15000)
    $app.Refresh()
    Assert-True -Condition (-not $app.HasExited) -Category 'installer_forcibly_closed_application'
    if (-not $mutationFinished) {
        Stop-Process -Id $mutation.Id -Force -ErrorAction SilentlyContinue
        Throw-Category -Category 'mutation_did_not_reject_running_application'
    }
    Assert-True -Condition ($mutation.ExitCode -ne 0) -Category 'mutation_succeeded_while_application_running'
    $afterRecord = Get-InstallRecord
    Assert-True -Condition ($afterRecord.Version -eq $beforeRecord.Version) -Category 'mutation_changed_version_while_application_running'
    Assert-True -Condition ($afterRecord.InstallLocation -eq $beforeRecord.InstallLocation) -Category 'mutation_changed_location_while_application_running'
    Assert-True -Condition (Test-Path -LiteralPath $ExecutablePath -PathType Leaf) -Category 'mutation_removed_application_while_running'
    $afterHash = Get-PayloadDigest -Root $afterRecord.InstallLocation
    Assert-True -Condition ($afterHash -eq $beforeHash) -Category 'mutation_replaced_application_while_running'
    Assert-True -Condition ($app.WaitForExit(20000)) -Category 'mutex_probe_app_did_not_exit_safely'
    Assert-True -Condition ($app.ExitCode -eq 0) -Category 'mutex_probe_app_exit_failed'
}

function Add-PassedScenario {
    param([Parameter(Mandatory = $true)][string]$Name)
    $script:Scenarios.Add([ordered]@{ name = $Name; status = 'passed' })
}

function Write-Summary {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('passed', 'failed')][string]$Status,
        [string]$FailureCategory = ''
    )
    $outputRoot = Split-Path -Parent $OutputPath
    if ($outputRoot) {
        [void](New-Item -ItemType Directory -Path $outputRoot -Force)
    }
    $result = [ordered]@{
        schema = 'amadeus-p7-sandbox-acceptance/v1'
        status = $Status
        product_version = $ExpectedVersion
        scenario_count = $script:Scenarios.Count
        scenarios = $script:Scenarios
        current_installer_sha256 = if ($script:InstallerHashes.Contains('current')) { $script:InstallerHashes['current'] } else { '' }
        baseline_installer_sha256 = if ($script:InstallerHashes.Contains('baseline')) { $script:InstallerHashes['baseline'] } else { '' }
        defender = $script:DefenderEvidence
        forbidden_runtime_product_count = 0
        failure_category = $FailureCategory
    }
    [IO.File]::WriteAllText(
        $OutputPath,
        ($result | ConvertTo-Json -Depth 6),
        [Text.UTF8Encoding]::new($false)
    )
}

try {
    Assert-True -Condition ($env:USERNAME -eq 'WDAGUtilityAccount') -Category 'not_running_in_windows_sandbox'
    $inputRoot = 'C:\AmadeusP7\Input'
    $outputRoot = 'C:\AmadeusP7\Output'
    $InstallerPath = Resolve-ContainedFile -Path $InstallerPath -Root $inputRoot -Category 'current_installer_outside_input'
    $BaselineInstallerPath = Resolve-ContainedFile -Path $BaselineInstallerPath -Root $inputRoot -Category 'baseline_installer_outside_input'
    $resolvedOutput = [IO.Path]::GetFullPath($RequestedOutputPath)
    $outputPrefix = [IO.Path]::GetFullPath($outputRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    Assert-True -Condition ($resolvedOutput.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)) -Category 'summary_outside_output'
    Assert-True -Condition ($resolvedOutput -eq [IO.Path]::GetFullPath($OutputPath)) -Category 'summary_name_invalid'
    Assert-True -Condition ($InstallerPath -ne $BaselineInstallerPath) -Category 'installers_must_differ'
    $script:InstallerHashes['current'] = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $script:InstallerHashes['baseline'] = (Get-FileHash -LiteralPath $BaselineInstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()

    Assert-CleanMachineRuntime
    Add-PassedScenario -Name 'clean_windows_runtime'
    Invoke-DefenderInstallerScan -TargetPath $InstallerPath

    Invoke-Installer -Path $BaselineInstallerPath -EnableAutostart
    $baseline = Get-InstallRecord
    $baselineComparable = ConvertTo-ComparableVersion -Value $baseline.Version
    $currentComparable = ConvertTo-ComparableVersion -Value $ExpectedVersion
    Assert-True -Condition ($baselineComparable -lt $currentComparable) -Category 'baseline_version_not_lower'
    Assert-True -Condition ($baseline.InstallLocation -eq $DefaultInstallRoot) -Category 'baseline_install_scope_invalid'
    $baselineExe = Join-Path $baseline.InstallLocation 'Amadeus.exe'
    Assert-True -Condition ($null -ne (Get-RunValue)) -Category 'baseline_autostart_task_failed'
    Invoke-AppLifecycle -ExecutablePath $baselineExe
    $syntheticHashes = Write-SyntheticState
    Set-FakeCredential

    Invoke-Installer -Path $InstallerPath
    $current = Get-InstallRecord
    Assert-True -Condition ($current.Version -eq $ExpectedVersion) -Category 'upgrade_version_invalid'
    Assert-True -Condition ($current.InstallLocation -eq $DefaultInstallRoot) -Category 'upgrade_install_scope_changed'
    $script:CurrentInstallRoot = $current.InstallLocation
    $currentExe = Join-Path $current.InstallLocation 'Amadeus.exe'
    Assert-InstalledPayload -InstallRoot $current.InstallLocation
    Assert-StartMenuShortcut -ExpectedExecutable $currentExe
    Assert-SyntheticState -Hashes $syntheticHashes
    Assert-FakeCredential -Category 'credential_not_preserved_on_upgrade'
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'upgrade_overrode_autostart_choice'
    Invoke-AppLifecycle -ExecutablePath $currentExe
    Remove-FakeCredential
    Invoke-WinCredProbe -ExecutablePath $currentExe
    Add-PassedScenario -Name 'install_and_upgrade_preserve_state'

    Assert-LifecycleMutex -ExecutablePath $currentExe -MutatingExecutable $InstallerPath -MutatingArguments @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCLOSEAPPLICATIONS'
    )
    $uninstallerForMutex = Get-UninstallerPath -InstallRoot $current.InstallLocation
    Assert-LifecycleMutex -ExecutablePath $currentExe -MutatingExecutable $uninstallerForMutex -MutatingArguments @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
    )
    Add-PassedScenario -Name 'running_application_mutex'

    Set-FakeCredential
    Set-RunValue -ExecutablePath $currentExe
    Invoke-Uninstaller -InstallRoot $current.InstallLocation
    Wait-UninstallCompletion -InstallRoot $current.InstallLocation
    Assert-True -Condition (-not (Test-Path -LiteralPath $current.InstallLocation)) -Category 'program_not_removed_by_default_uninstall'
    Assert-NoStartMenuShortcut
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'autostart_not_removed_by_default_uninstall'
    Assert-SyntheticState -Hashes $syntheticHashes
    Assert-FakeCredential -Category 'credential_not_preserved_by_default_uninstall'
    Add-PassedScenario -Name 'default_uninstall_preserves_user_data'

    Invoke-Installer -Path $InstallerPath
    $current = Get-InstallRecord
    $currentExe = Join-Path $current.InstallLocation 'Amadeus.exe'
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'reinstall_enabled_autostart_by_default'
    Assert-SyntheticState -Hashes $syntheticHashes
    Remove-FakeCredential
    Invoke-AppLifecycle -ExecutablePath $currentExe

    $reparseRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) "amadeus-p7-reparse-$RunId"
    [void](New-Item -ItemType Directory -Path $reparseRoot -Force)
    $reparseSentinel = Join-Path $reparseRoot 'external-sentinel.txt'
    [IO.File]::WriteAllText(
        $reparseSentinel,
        'synthetic-p7-reparse-boundary',
        [Text.UTF8Encoding]::new($false)
    )
    $junction = Join-Path $AppDataRoot 'logs\external-junction'
    [void](New-Item -ItemType Junction -Path $junction -Target $reparseRoot -Force)
    Assert-True -Condition ((Get-Item -LiteralPath $junction).Attributes -band [IO.FileAttributes]::ReparsePoint) -Category 'reparse_fixture_not_created'
    Set-FakeCredential
    Set-RunValue -ExecutablePath $currentExe
    $payloadBeforeUnsafeUninstall = Get-PayloadDigest -Root $current.InstallLocation
    Invoke-Uninstaller -InstallRoot $current.InstallLocation -DeleteUserData -ExpectFailure
    $afterRejectedCleanup = Get-InstallRecord
    Assert-True -Condition ($afterRejectedCleanup.Version -eq $ExpectedVersion) -Category 'unsafe_cleanup_removed_uninstall_entry'
    Assert-True -Condition ((Get-PayloadDigest -Root $current.InstallLocation) -eq $payloadBeforeUnsafeUninstall) -Category 'unsafe_cleanup_changed_program'
    Assert-SyntheticState -Hashes $syntheticHashes
    Assert-FakeCredential -Category 'unsafe_cleanup_changed_credential'
    Assert-True -Condition ($null -ne (Get-RunValue)) -Category 'unsafe_cleanup_changed_autostart'
    Assert-True -Condition (Test-Path -LiteralPath $reparseSentinel -PathType Leaf) -Category 'unsafe_cleanup_followed_reparse_point'
    [IO.Directory]::Delete($junction)
    Assert-True -Condition (-not (Test-Path -LiteralPath $junction)) -Category 'reparse_fixture_cleanup_failed'
    Assert-True -Condition (Test-Path -LiteralPath $reparseSentinel -PathType Leaf) -Category 'reparse_fixture_cleanup_followed_target'
    Add-PassedScenario -Name 'reparse_cleanup_fails_closed'

    foreach ($region in $Regions) {
        $directory = Join-Path $AppDataRoot $region
        [void](New-Item -ItemType Directory -Path $directory -Force)
        [IO.File]::WriteAllText(
            (Join-Path $directory "delete-$RunId.sentinel"),
            'synthetic-p7-delete-acceptance',
            [Text.UTF8Encoding]::new($false)
        )
    }
    $rootSentinel = Join-Path $AppDataRoot "nonallowlisted-$RunId.sentinel"
    [IO.File]::WriteAllText(
        $rootSentinel,
        'synthetic-p7-root-boundary',
        [Text.UTF8Encoding]::new($false)
    )
    $script:ExternalExport = Join-Path ([Environment]::GetFolderPath('MyDocuments')) "amadeus-p7-external-$RunId.json"
    [IO.File]::WriteAllText(
        $script:ExternalExport,
        '{"format":"amadeus-chat-export/v1","synthetic":true}',
        [Text.UTF8Encoding]::new($false)
    )
    Set-FakeCredential
    Set-RunValue -ExecutablePath $currentExe
    Invoke-Uninstaller -InstallRoot $current.InstallLocation -DeleteUserData
    Wait-UninstallCompletion -InstallRoot $current.InstallLocation
    Assert-True -Condition (-not (Test-Path -LiteralPath $current.InstallLocation)) -Category 'program_not_removed_by_delete_data_uninstall'
    foreach ($region in $Regions) {
        Assert-True -Condition (-not (Test-Path -LiteralPath (Join-Path $AppDataRoot $region))) -Category 'local_data_region_not_deleted'
    }
    Assert-True -Condition (Test-Path -LiteralPath $rootSentinel -PathType Leaf) -Category 'nonallowlisted_root_file_was_deleted'
    Assert-True -Condition (-not (Test-CredentialExists)) -Category 'credential_not_deleted'
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'autostart_not_deleted'
    Assert-True -Condition (Test-Path -LiteralPath $script:ExternalExport -PathType Leaf) -Category 'external_export_was_deleted'
    Add-PassedScenario -Name 'explicit_delete_data_uninstall'

    Invoke-Installer -Path $InstallerPath -InstallDirectory $CustomInstallRoot
    $custom = Get-InstallRecord
    Assert-True -Condition ($custom.Version -eq $ExpectedVersion) -Category 'custom_path_version_invalid'
    Assert-True -Condition ($custom.InstallLocation -eq $CustomInstallRoot) -Category 'custom_unicode_path_not_honored'
    $customExe = Join-Path $custom.InstallLocation 'Amadeus.exe'
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'custom_install_enabled_autostart_by_default'
    Assert-InstalledPayload -InstallRoot $custom.InstallLocation
    Invoke-AppLifecycle -ExecutablePath $customExe
    Invoke-Uninstaller -InstallRoot $custom.InstallLocation
    Wait-UninstallCompletion -InstallRoot $custom.InstallLocation
    Assert-True -Condition (-not (Test-Path -LiteralPath $custom.InstallLocation)) -Category 'custom_install_not_removed'
    Add-PassedScenario -Name 'unicode_space_custom_install_path'

    Write-Summary -Status 'passed'
}
catch {
    $category = [string]$_.Exception.Message
    if ($category -notmatch '^p7_[a-z0-9_]+$') {
        $category = 'p7_unexpected_failure'
    }
    try {
        Write-Summary -Status 'failed' -FailureCategory $category
    }
    catch {
        # The mapped output itself is the final fail-closed boundary.
    }
}
finally {
    Remove-FakeCredential
    if ($script:ExternalExport) {
        Remove-Item -LiteralPath $script:ExternalExport -Force -ErrorAction SilentlyContinue
    }
    & "$env:SystemRoot\System32\shutdown.exe" /s /t 0 *> $null
}
