[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$InputRoot = 'C:\AmadeusP7\Input'
$OutputRoot = 'C:\AmadeusP7\Output'
$InstallerPath = 'C:\AmadeusP7\Input\current-setup.exe'
$InstallRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs\Amadeus'))
$EvidencePath = 'C:\AmadeusP7\Output\defender-evidence.json'
$script:ExecutionIdentity = [ordered]@{}
$script:DefenderStatus = [ordered]@{}
$script:Scans = [ordered]@{}

Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public sealed class AmadeusP7ElevatedTokenEvidence
{
    public bool IsElevated { get; private set; }
    public int IntegrityRid { get; private set; }
    public string IntegrityLevel { get; private set; }

    private const uint TokenQuery = 0x0008;
    private const int TokenElevation = 20;
    private const int TokenIntegrityLevel = 25;

    [StructLayout(LayoutKind.Sequential)]
    private struct TokenElevationValue
    {
        public int TokenIsElevated;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct TokenMandatoryLabel
    {
        public IntPtr LabelSid;
        public int Attributes;
    }

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(
        IntPtr token,
        int informationClass,
        IntPtr information,
        int informationLength,
        out int returnLength
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    public static AmadeusP7ElevatedTokenEvidence Read()
    {
        IntPtr token;
        if (!OpenProcessToken(GetCurrentProcess(), TokenQuery, out token))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        try
        {
            TokenElevationValue elevation = ReadStruct<TokenElevationValue>(token, TokenElevation);
            int rid = ReadIntegrityRid(token);
            return new AmadeusP7ElevatedTokenEvidence {
                IsElevated = elevation.TokenIsElevated != 0,
                IntegrityRid = rid,
                IntegrityLevel = IntegrityName(rid)
            };
        }
        finally
        {
            CloseHandle(token);
        }
    }

    private static T ReadStruct<T>(IntPtr token, int informationClass) where T : struct
    {
        int required;
        GetTokenInformation(token, informationClass, IntPtr.Zero, 0, out required);
        if (required <= 0)
            throw new Win32Exception(Marshal.GetLastWin32Error());
        IntPtr buffer = Marshal.AllocHGlobal(required);
        try
        {
            if (!GetTokenInformation(token, informationClass, buffer, required, out required))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return (T)Marshal.PtrToStructure(buffer, typeof(T));
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    private static int ReadIntegrityRid(IntPtr token)
    {
        int required;
        GetTokenInformation(token, TokenIntegrityLevel, IntPtr.Zero, 0, out required);
        if (required <= 0)
            throw new Win32Exception(Marshal.GetLastWin32Error());
        IntPtr buffer = Marshal.AllocHGlobal(required);
        try
        {
            if (!GetTokenInformation(token, TokenIntegrityLevel, buffer, required, out required))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            TokenMandatoryLabel label = (TokenMandatoryLabel)Marshal.PtrToStructure(
                buffer,
                typeof(TokenMandatoryLabel)
            );
            byte count = Marshal.ReadByte(label.LabelSid, 1);
            if (count == 0)
                throw new InvalidOperationException("Token integrity SID has no sub-authority.");
            return Marshal.ReadInt32(label.LabelSid, 8 + ((count - 1) * 4));
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    private static string IntegrityName(int rid)
    {
        if (rid < 0x1000) return "untrusted";
        if (rid < 0x2000) return "low";
        if (rid < 0x3000) return "medium";
        if (rid < 0x4000) return "high";
        if (rid < 0x5000) return "system";
        return "protected";
    }
}
'@

function Throw-DefenderCategory {
    param([Parameter(Mandatory = $true)][string]$Category)
    throw "p7_defender_$Category"
}

function Assert-DefenderCondition {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Category
    )
    if (-not $Condition) {
        Throw-DefenderCategory -Category $Category
    }
}

function Resolve-FixedContainedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Category
    )
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $expected = [IO.Path]::GetFullPath($ExpectedPath)
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $prefix = $rootPath + [IO.Path]::DirectorySeparatorChar
    Assert-DefenderCondition -Condition ($resolved -ceq $expected) -Category $Category
    Assert-DefenderCondition -Condition (
        $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    ) -Category $Category
    Assert-DefenderCondition -Condition (-not (
        (Get-Item -LiteralPath $resolved -ErrorAction Stop).Attributes -band
        [IO.FileAttributes]::ReparsePoint
    )) -Category $Category
    return $resolved
}

function Assert-NoReparseTree {
    param([Parameter(Mandatory = $true)][string]$Root)
    $resolvedRoot = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path
    Assert-DefenderCondition -Condition (-not (
        (Get-Item -LiteralPath $resolvedRoot -ErrorAction Stop).Attributes -band
        [IO.FileAttributes]::ReparsePoint
    )) -Category 'install_root_reparse'
    foreach ($entry in Get-ChildItem -LiteralPath $resolvedRoot -Recurse -Force -ErrorAction Stop) {
        Assert-DefenderCondition -Condition (-not (
            $entry.Attributes -band [IO.FileAttributes]::ReparsePoint
        )) -Category 'installed_tree_reparse'
    }
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

function Get-DetectionKeys {
    $keys = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($item in @(Get-MpThreatDetection -ErrorAction Stop)) {
        $detectionId = $item.PSObject.Properties['DetectionID']
        $threatId = $item.PSObject.Properties['ThreatID']
        $initialTime = $item.PSObject.Properties['InitialDetectionTime']
        $key = if ($null -ne $detectionId -and $detectionId.Value) {
            [string]$detectionId.Value
        }
        else {
            ([string]$threatId.Value) + '|' + ([string]$initialTime.Value)
        }
        [void]$keys.Add($key)
    }
    return ,$keys
}

function Resolve-MicrosoftDefenderScanner {
    $platformRoot = Join-Path $env:ProgramData 'Microsoft\Windows Defender\Platform'
    $candidates = @()
    if (Test-Path -LiteralPath $platformRoot -PathType Container) {
        $candidates += @(
            Get-ChildItem -LiteralPath $platformRoot -Directory -ErrorAction Stop |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName 'MpCmdRun.exe' }
        )
    }
    $candidates += Join-Path $env:ProgramFiles 'Windows Defender\MpCmdRun.exe'
    $scanner = @($candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }) |
        Select-Object -First 1
    Assert-DefenderCondition -Condition (-not [string]::IsNullOrWhiteSpace($scanner)) -Category 'scanner_missing'
    $scanner = (Resolve-Path -LiteralPath $scanner -ErrorAction Stop).Path
    Assert-DefenderCondition -Condition (-not (
        (Get-Item -LiteralPath $scanner).Attributes -band [IO.FileAttributes]::ReparsePoint
    )) -Category 'scanner_reparse'
    $signature = Get-AuthenticodeSignature -LiteralPath $scanner
    Assert-DefenderCondition -Condition (
        $signature.Status -eq [Management.Automation.SignatureStatus]::Valid -and
        $null -ne $signature.SignerCertificate -and
        $signature.SignerCertificate.Subject -match 'Microsoft'
    ) -Category 'scanner_signature_invalid'
    return [pscustomobject]@{
        Path = $scanner
        FileVersion = [string](Get-Item -LiteralPath $scanner).VersionInfo.FileVersion
        SignatureStatus = [string]$signature.Status
    }
}

function Invoke-DefenderCustomScan {
    param(
        [Parameter(Mandatory = $true)][string]$ScannerPath,
        [Parameter(Mandatory = $true)][string]$ScannerVersion,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][ValidateSet('file', 'directory_payload')][string]$TargetKind,
        [Parameter(Mandatory = $true)][Collections.Generic.HashSet[string]]$DetectionKeysBefore,
        [Parameter(Mandatory = $true)][object]$Status
    )
    $targetDigest = if ($TargetKind -eq 'file') {
        (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    else {
        Get-PayloadDigest -Root $TargetPath
    }
    $started = [DateTimeOffset]::UtcNow
    $process = Start-Process `
        -FilePath $ScannerPath `
        -ArgumentList @('-Scan', '-ScanType', '3', '-File', $TargetPath, '-DisableRemediation') `
        -PassThru `
        -WindowStyle Hidden
    if (-not $process.WaitForExit(600000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Throw-DefenderCategory -Category 'scan_timeout'
    }
    $process.Refresh()
    $finished = [DateTimeOffset]::UtcNow
    $targetDigestAfter = if ($TargetKind -eq 'file') {
        (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    else {
        Get-PayloadDigest -Root $TargetPath
    }
    $targetDigestStable = $targetDigestAfter -ceq $targetDigest
    $afterKeys = Get-DetectionKeys
    $newDetectionCount = @($afterKeys | Where-Object { -not $DetectionKeysBefore.Contains($_) }).Count
    $evidence = [ordered]@{
        status = if (
            $process.ExitCode -eq 0 -and
            $targetDigestStable -and
            $newDetectionCount -eq 0
        ) { 'passed' } else { 'failed' }
        target_kind = $TargetKind
        target_sha256 = $targetDigest
        target_digest_verified_after_scan = $targetDigestStable
        remediation_disabled = $true
        started_utc = $started.ToString('o')
        finished_utc = $finished.ToString('o')
        exit_code = [int]$process.ExitCode
        new_detection_count = $newDetectionCount
        antimalware_product_version = [string]$Status.AMProductVersion
        engine_version = [string]$Status.AMEngineVersion
        antivirus_signature_version = [string]$Status.AntivirusSignatureVersion
        scanner_file_version = $ScannerVersion
    }
    Assert-DefenderCondition -Condition ($process.ExitCode -eq 0) -Category 'scan_failed'
    Assert-DefenderCondition -Condition $targetDigestStable -Category 'target_changed_during_scan'
    Assert-DefenderCondition -Condition ($newDetectionCount -eq 0) -Category 'threat_detected'
    return $evidence
}

function Write-DefenderEvidence {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('passed', 'failed')][string]$Status,
        [string]$FailureCategory = ''
    )
    $document = [ordered]@{
        schema = 'amadeus-p7-sandbox-defender/v1'
        status = $Status
        execution_identity = $script:ExecutionIdentity
        product_status = $script:DefenderStatus
        current_installer = if ($script:Scans.Contains('current_installer')) {
            $script:Scans['current_installer']
        } else { [ordered]@{} }
        installed_directory = if ($script:Scans.Contains('installed_directory')) {
            $script:Scans['installed_directory']
        } else { [ordered]@{} }
        failure_category = $FailureCategory
    }
    [IO.File]::WriteAllText(
        $EvidencePath,
        ($document | ConvertTo-Json -Depth 6),
        [Text.UTF8Encoding]::new($false)
    )
}

try {
    Assert-DefenderCondition -Condition ($env:USERNAME -eq 'WDAGUtilityAccount') -Category 'not_sandbox_user'
    $selfPath = Resolve-FixedContainedFile `
        -Path $MyInvocation.MyCommand.Path `
        -ExpectedPath (Join-Path $InputRoot 'invoke-sandbox-defender.ps1') `
        -Root $InputRoot `
        -Category 'helper_identity_invalid'
    [void]$selfPath
    $InstallerPath = Resolve-FixedContainedFile `
        -Path $InstallerPath `
        -ExpectedPath $InstallerPath `
        -Root $InputRoot `
        -Category 'installer_path_invalid'
    $resolvedInstallRoot = (Resolve-Path -LiteralPath $InstallRoot -ErrorAction Stop).Path
    Assert-DefenderCondition -Condition ($resolvedInstallRoot -ceq $InstallRoot) -Category 'install_root_invalid'
    Assert-NoReparseTree -Root $resolvedInstallRoot
    Assert-DefenderCondition -Condition (
        [IO.Path]::GetFullPath((Split-Path -Parent $EvidencePath)) -ceq [IO.Path]::GetFullPath($OutputRoot)
    ) -Category 'output_path_invalid'
    Assert-DefenderCondition -Condition (-not (Test-Path -LiteralPath $EvidencePath)) -Category 'evidence_already_exists'

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $token = [AmadeusP7ElevatedTokenEvidence]::Read()
    $script:ExecutionIdentity = [ordered]@{
        user_name = [string]$identity.Name
        is_administrator_role = $principal.IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator
        )
        is_elevated = [bool]$token.IsElevated
        integrity_level = [string]$token.IntegrityLevel
        integrity_rid = [int]$token.IntegrityRid
    }
    Assert-DefenderCondition -Condition ([bool]$script:ExecutionIdentity['is_administrator_role']) -Category 'administrator_role_missing'
    Assert-DefenderCondition -Condition ([bool]$script:ExecutionIdentity['is_elevated']) -Category 'token_not_elevated'
    Assert-DefenderCondition -Condition (
        [string]$script:ExecutionIdentity['integrity_level'] -ceq 'high'
    ) -Category 'integrity_not_high'

    $scanner = Resolve-MicrosoftDefenderScanner
    $status = Get-MpComputerStatus -ErrorAction Stop
    $script:DefenderStatus = [ordered]@{
        am_service_enabled = [bool]$status.AMServiceEnabled
        antivirus_enabled = [bool]$status.AntivirusEnabled
        antimalware_product_version = [string]$status.AMProductVersion
        engine_version = [string]$status.AMEngineVersion
        antivirus_signature_version = [string]$status.AntivirusSignatureVersion
        scanner_file_version = [string]$scanner.FileVersion
        scanner_signature_status = [string]$scanner.SignatureStatus
        scanner_publisher = 'Microsoft'
    }
    Assert-DefenderCondition -Condition ([bool]$status.AMServiceEnabled) -Category 'service_disabled'
    Assert-DefenderCondition -Condition ([bool]$status.AntivirusEnabled) -Category 'antivirus_disabled'
    $detectionKeys = Get-DetectionKeys
    $script:Scans['current_installer'] = Invoke-DefenderCustomScan `
        -ScannerPath $scanner.Path `
        -ScannerVersion $scanner.FileVersion `
        -TargetPath $InstallerPath `
        -TargetKind 'file' `
        -DetectionKeysBefore $detectionKeys `
        -Status $status
    $detectionKeys = Get-DetectionKeys
    $script:Scans['installed_directory'] = Invoke-DefenderCustomScan `
        -ScannerPath $scanner.Path `
        -ScannerVersion $scanner.FileVersion `
        -TargetPath $resolvedInstallRoot `
        -TargetKind 'directory_payload' `
        -DetectionKeysBefore $detectionKeys `
        -Status $status
    Write-DefenderEvidence -Status 'passed'
    exit 0
}
catch {
    $category = [string]$_.Exception.Message
    if ($category -notmatch '^p7_defender_[a-z0-9_]+$') {
        $category = 'p7_defender_unexpected_failure'
    }
    try {
        if (
            [IO.Path]::GetFullPath((Split-Path -Parent $EvidencePath)) -ceq
            [IO.Path]::GetFullPath($OutputRoot)
        ) {
            Write-DefenderEvidence -Status 'failed' -FailureCategory $category
        }
    }
    catch {
        # The fixed mapped output itself is the final fail-closed boundary.
    }
    exit 1
}
