[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$BaselineInstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedBuildInfoPath,
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
$ChinesePathSegment = 'P7 ' + [char]0x4e2d + [char]0x6587 + ' ' + [char]0x7a7a + [char]0x683c
$CustomInstallRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs\$ChinesePathSegment\Amadeus"))
$Regions = @('config', 'data', 'pets', 'personas', 'models', 'backups', 'logs')
$script:Scenarios = [Collections.Generic.List[object]]::new()
$script:InstallerHashes = [ordered]@{}
$script:DefenderEvidence = [ordered]@{}
$script:RuntimeEvidence = [ordered]@{}
$script:ExecutionIdentity = [ordered]@{}
$script:BuildIdentity = [ordered]@{}
$script:InstalledScanEvidence = [ordered]@{}
$script:ShortcutEvidence = [ordered]@{}
$script:SqliteEvidence = [ordered]@{
    fixture_row_count = 0
    fts_match_count = 0
    verification_count = 0
    runtime_sha256 = ''
    dependency_sha256 = ''
}
$script:CurrentInstallRoot = $null
$script:ExternalExport = $null
$script:FakeCredentialSecret = $null
$script:ExpectedBuildInfo = $null
$script:SqliteProbeExecutable = $null
$script:SqliteProbeLibrary = $null
$script:SqliteProbeDependency = $null
$script:NonElevatedMutationCount = 0

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

public sealed class AmadeusP7TokenEvidence
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
    private static extern bool OpenProcessToken(IntPtr process, uint desiredAccess, out IntPtr token);

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

    public static AmadeusP7TokenEvidence Read()
    {
        IntPtr token;
        if (!OpenProcessToken(GetCurrentProcess(), TokenQuery, out token))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        try
        {
            TokenElevationValue elevation = ReadStruct<TokenElevationValue>(token, TokenElevation);
            int rid = ReadIntegrityRid(token);
            return new AmadeusP7TokenEvidence {
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
            byte subAuthorityCount = Marshal.ReadByte(label.LabelSid, 1);
            if (subAuthorityCount == 0)
                throw new InvalidOperationException("Token integrity SID has no sub-authority.");
            return Marshal.ReadInt32(label.LabelSid, 8 + ((subAuthorityCount - 1) * 4));
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

function Get-ExecutionIdentityEvidence {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $token = [AmadeusP7TokenEvidence]::Read()
    return [ordered]@{
        user_name = [string]$identity.Name
        sandbox_user_name = [string]$env:USERNAME
        is_administrator_role = $principal.IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator
        )
        is_elevated = [bool]$token.IsElevated
        integrity_level = [string]$token.IntegrityLevel
        integrity_rid = [int]$token.IntegrityRid
    }
}

function Assert-NonElevatedToken {
    $evidence = Get-ExecutionIdentityEvidence
    Assert-True -Condition (-not [bool]$evidence['is_elevated']) -Category 'sandbox_token_is_elevated'
    Assert-True -Condition (-not [bool]$evidence['is_administrator_role']) -Category 'sandbox_token_has_administrator_role'
    Assert-True -Condition ([string]$evidence['integrity_level'] -ceq 'medium') -Category 'sandbox_token_integrity_not_medium'
    if ($script:ExecutionIdentity.Count -eq 0) {
        $script:ExecutionIdentity = $evidence
    }
    else {
        Assert-True -Condition (
            [string]$script:ExecutionIdentity['user_name'] -ceq [string]$evidence['user_name'] -and
            [int]$script:ExecutionIdentity['integrity_rid'] -eq [int]$evidence['integrity_rid']
        ) -Category 'sandbox_token_identity_changed'
    }
}

function Read-ExpectedBuildInfo {
    param([Parameter(Mandatory = $true)][string]$Path)
    $bytes = [IO.File]::ReadAllBytes($Path)
    try {
        Assert-True -Condition ($bytes.Length -gt 0 -and $bytes.Length -le 4096) -Category 'expected_build_info_size_invalid'
        try {
            $payload = [Text.UTF8Encoding]::new($false, $true).GetString($bytes) | ConvertFrom-Json
        }
        catch {
            Throw-Category -Category 'expected_build_info_invalid'
        }
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
    $properties = @($payload.PSObject.Properties.Name | Sort-Object)
    Assert-True -Condition (
        @($properties).Count -eq 4 -and
        ($properties -join ',') -ceq 'build_date_utc,commit_sha,schema_version,version'
    ) -Category 'expected_build_info_fields_invalid'
    Assert-True -Condition ([int]$payload.schema_version -eq 1) -Category 'expected_build_info_schema_invalid'
    Assert-True -Condition ([string]$payload.version -ceq $ExpectedVersion) -Category 'expected_build_info_version_invalid'
    Assert-True -Condition ([string]$payload.commit_sha -cmatch '^[0-9a-f]{40}$') -Category 'expected_build_info_commit_invalid'
    $parsedDate = [DateTime]::MinValue
    Assert-True -Condition ([DateTime]::TryParseExact(
        [string]$payload.build_date_utc,
        'yyyy-MM-dd',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None,
        [ref]$parsedDate
    )) -Category 'expected_build_info_date_invalid'
    return $payload
}

function Assert-InstalledBuildInfo {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $resource = Resolve-ContainedFile `
        -Path (Join-Path $InstallRoot '_internal\amadeus_desktop\resources\build-info.json') `
        -Root $InstallRoot `
        -Category 'installed_build_info_outside_payload'
    $actual = Read-ExpectedBuildInfo -Path $resource
    Assert-True -Condition (
        [string]$actual.version -ceq [string]$script:ExpectedBuildInfo.version -and
        [string]$actual.commit_sha -ceq [string]$script:ExpectedBuildInfo.commit_sha -and
        [string]$actual.build_date_utc -ceq [string]$script:ExpectedBuildInfo.build_date_utc
    ) -Category 'installed_build_info_mismatch'
    $exeVersion = (Get-Item -LiteralPath (Join-Path $InstallRoot 'Amadeus.exe')).VersionInfo
    Assert-True -Condition (
        $exeVersion.FileVersion.Trim() -ceq $ExpectedVersion -and
        $exeVersion.ProductVersion.Trim() -ceq $ExpectedVersion
    ) -Category 'installed_executable_version_invalid'
    $script:BuildIdentity = [ordered]@{
        version = [string]$actual.version
        commit_sha = [string]$actual.commit_sha
        build_date_utc = [string]$actual.build_date_utc
    }
}

function Initialize-SqliteProbe {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $installedLibrary = Resolve-ContainedFile `
        -Path (Join-Path $InstallRoot '_internal\sqlite3.dll') `
        -Root $InstallRoot `
        -Category 'sqlite_runtime_outside_install'
    $installedDependency = Resolve-ContainedFile `
        -Path (Join-Path $InstallRoot '_internal\vcruntime140.dll') `
        -Root $InstallRoot `
        -Category 'sqlite_dependency_outside_install'
    $probeRoot = Join-Path $env:TEMP "amadeus-p7-sqlite-$RunId"
    [void](New-Item -ItemType Directory -Path $probeRoot -Force)
    $script:SqliteProbeLibrary = Join-Path $probeRoot 'sqlite3.dll'
    $script:SqliteProbeDependency = Join-Path $probeRoot 'vcruntime140.dll'
    $script:SqliteProbeExecutable = Join-Path $probeRoot 'AmadeusP7SqliteProbe.exe'
    Copy-Item -LiteralPath $installedLibrary -Destination $script:SqliteProbeLibrary
    Copy-Item -LiteralPath $installedDependency -Destination $script:SqliteProbeDependency
    $script:SqliteEvidence['runtime_sha256'] = (
        Get-FileHash -LiteralPath $script:SqliteProbeLibrary -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $script:SqliteEvidence['dependency_sha256'] = (
        Get-FileHash -LiteralPath $script:SqliteProbeDependency -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    Add-Type -TypeDefinition @'
using System;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

public static class AmadeusP7SqliteProbe
{
    private const int SqliteOk = 0;
    private const int SqliteOpenReadOnly = 0x00000001;
    private const int SqliteOpenReadWrite = 0x00000002;
    private const int SqliteOpenCreate = 0x00000004;
    private const int SqliteOpenFullMutex = 0x00010000;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr LoadLibraryW(string path);

    [DllImport("kernel32.dll")]
    private static extern uint SetErrorMode(uint mode);

    [DllImport("sqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_initialize();

    [DllImport("sqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_open_v2(IntPtr filename, out IntPtr database, int flags, IntPtr vfs);

    [DllImport("sqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_close_v2(IntPtr database);

    [DllImport("sqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_busy_timeout(IntPtr database, int milliseconds);

    [DllImport("sqlite3.dll", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    private static extern int sqlite3_exec(
        IntPtr database,
        string sql,
        SqliteCallback callback,
        IntPtr context,
        out IntPtr error
    );

    [DllImport("sqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern void sqlite3_free(IntPtr value);

    private delegate int SqliteCallback(IntPtr context, int columns, IntPtr values, IntPtr names);

    public static int Main(string[] arguments)
    {
        SetErrorMode(0x0001u | 0x0002u | 0x8000u);
        if (arguments.Length != 4 || (arguments[0] != "create" && arguments[0] != "verify"))
            return 64;
        if (!Regex.IsMatch(arguments[3], "^[0-9a-f]{32}$", RegexOptions.CultureInvariant))
            return 65;
        string library = Path.GetFullPath(arguments[1]);
        string databasePath = Path.GetFullPath(arguments[2]);
        if (!File.Exists(library) || LoadLibraryW(library) == IntPtr.Zero)
            return 66;
        if (sqlite3_initialize() != SqliteOk)
            return 72;
        IntPtr database;
        int flags = arguments[0] == "create"
            ? SqliteOpenReadWrite | SqliteOpenCreate | SqliteOpenFullMutex
            : SqliteOpenReadOnly | SqliteOpenFullMutex;
        int opened = OpenUtf8(databasePath, flags, out database);
        if (opened != SqliteOk)
            return 67;
        try
        {
            sqlite3_busy_timeout(database, 15000);
            string runId = arguments[3];
            string marker = "amadeusp7" + runId;
            if (arguments[0] == "create")
            {
                string sql =
                    "BEGIN IMMEDIATE;" +
                    "CREATE TABLE IF NOT EXISTS p7_acceptance_fixture(" +
                    "run_id TEXT PRIMARY KEY, marker TEXT NOT NULL);" +
                    "CREATE VIRTUAL TABLE IF NOT EXISTS p7_acceptance_fts " +
                    "USING fts5(run_id UNINDEXED, marker);" +
                    "INSERT OR REPLACE INTO p7_acceptance_fixture(run_id,marker) VALUES('" + runId + "','" + marker + "');" +
                    "DELETE FROM p7_acceptance_fts WHERE run_id='" + runId + "';" +
                    "INSERT INTO p7_acceptance_fts(run_id,marker) VALUES('" + runId + "','" + marker + "');" +
                    "COMMIT;";
                if (Execute(database, sql, null) != SqliteOk)
                    return 68;
            }
            int rows = -1;
            int matches = -1;
            SqliteCallback rowCallback = delegate(IntPtr context, int columns, IntPtr values, IntPtr names) {
                rows = ReadInteger(columns, values);
                return 0;
            };
            SqliteCallback matchCallback = delegate(IntPtr context, int columns, IntPtr values, IntPtr names) {
                matches = ReadInteger(columns, values);
                return 0;
            };
            if (Execute(database,
                "SELECT count(*) FROM p7_acceptance_fixture WHERE run_id='" + runId +
                "' AND marker='" + marker + "';", rowCallback) != SqliteOk)
                return 69;
            if (Execute(database,
                "SELECT count(*) FROM p7_acceptance_fts WHERE run_id='" + runId +
                "' AND p7_acceptance_fts MATCH 'marker:" + marker + "';", matchCallback) != SqliteOk)
                return 70;
            return rows == 1 && matches == 1 ? 0 : 71;
        }
        finally
        {
            sqlite3_close_v2(database);
        }
    }

    private static int OpenUtf8(string path, int flags, out IntPtr database)
    {
        byte[] value = Encoding.UTF8.GetBytes(path + "\0");
        GCHandle pinned = GCHandle.Alloc(value, GCHandleType.Pinned);
        try { return sqlite3_open_v2(pinned.AddrOfPinnedObject(), out database, flags, IntPtr.Zero); }
        finally
        {
            pinned.Free();
            Array.Clear(value, 0, value.Length);
        }
    }

    private static int Execute(IntPtr database, string sql, SqliteCallback callback)
    {
        IntPtr error;
        int result = sqlite3_exec(database, sql, callback, IntPtr.Zero, out error);
        if (error != IntPtr.Zero)
            sqlite3_free(error);
        return result;
    }

    private static int ReadInteger(int columns, IntPtr values)
    {
        if (columns != 1)
            return -1;
        IntPtr value = Marshal.ReadIntPtr(values);
        int parsed;
        return value != IntPtr.Zero && Int32.TryParse(
            Marshal.PtrToStringAnsi(value),
            NumberStyles.None,
            CultureInfo.InvariantCulture,
            out parsed
        ) ? parsed : -1;
    }
}
'@ -OutputAssembly $script:SqliteProbeExecutable -OutputType ConsoleApplication
    Assert-True -Condition (Test-Path -LiteralPath $script:SqliteProbeExecutable -PathType Leaf) -Category 'sqlite_probe_compile_failed'
}

function Assert-SqliteRuntimeMatchesInstall {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $installedLibrary = Resolve-ContainedFile `
        -Path (Join-Path $InstallRoot '_internal\sqlite3.dll') `
        -Root $InstallRoot `
        -Category 'sqlite_runtime_outside_install'
    $installedDependency = Resolve-ContainedFile `
        -Path (Join-Path $InstallRoot '_internal\vcruntime140.dll') `
        -Root $InstallRoot `
        -Category 'sqlite_dependency_outside_install'
    $actual = (Get-FileHash -LiteralPath $installedLibrary -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-True -Condition ($actual -ceq [string]$script:SqliteEvidence['runtime_sha256']) -Category 'sqlite_runtime_changed_between_installers'
    $actualDependency = (
        Get-FileHash -LiteralPath $installedDependency -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    Assert-True -Condition (
        $actualDependency -ceq [string]$script:SqliteEvidence['dependency_sha256']
    ) -Category 'sqlite_dependency_changed_between_installers'
}

function Invoke-SqliteFixtureProbe {
    param([Parameter(Mandatory = $true)][ValidateSet('create', 'verify')][string]$Mode)
    Assert-True -Condition (
        $script:SqliteProbeExecutable -and
        (Test-Path -LiteralPath $script:SqliteProbeExecutable -PathType Leaf) -and
        $script:SqliteProbeLibrary -and
        (Test-Path -LiteralPath $script:SqliteProbeLibrary -PathType Leaf) -and
        $script:SqliteProbeDependency -and
        (Test-Path -LiteralPath $script:SqliteProbeDependency -PathType Leaf)
    ) -Category 'sqlite_probe_unavailable'
    $databasePath = Join-Path $AppDataRoot 'data\amadeus.sqlite3'
    Assert-True -Condition (Test-Path -LiteralPath $databasePath -PathType Leaf) -Category 'sqlite_fixture_database_missing'
    [void](Invoke-CheckedProcess -FilePath $script:SqliteProbeExecutable -Arguments @(
        $Mode,
        $script:SqliteProbeLibrary,
        $databasePath,
        $RunId
    ) -TimeoutSeconds 60)
    $script:SqliteEvidence['fixture_row_count'] = 1
    $script:SqliteEvidence['fts_match_count'] = 1
    $script:SqliteEvidence['verification_count'] = [int]$script:SqliteEvidence['verification_count'] + 1
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

function Test-SecretPatternInFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    $patterns = @(
        '(?:sk|tp)-[A-Za-z0-9_-]{24,}',
        '(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._-]{20,}',
        '(?i)["'']?api[-_ ]?key["'']?\s*[=:]\s*["'']?[A-Za-z0-9._-]{24,}'
    )
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $buffer = [byte[]]::new(1MB)
        $tail = ''
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $text = $tail + [Text.Encoding]::ASCII.GetString($buffer, 0, $read)
            $text = [regex]::Replace(
                $text,
                '(?i)(?:sk|tp)-invalid-test-[A-Za-z0-9_-]+',
                'invalid'
            )
            foreach ($pattern in $patterns) {
                if ([regex]::IsMatch($text, $pattern, [Text.RegularExpressions.RegexOptions]::CultureInvariant)) {
                    return $true
                }
            }
            $tail = if ($text.Length -gt 512) { $text.Substring($text.Length - 512) } else { $text }
        }
        return $false
    }
    finally {
        [Array]::Clear($buffer, 0, $buffer.Length)
        $stream.Dispose()
    }
}

function Assert-InstalledPrivacyAndModelBoundary {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $modelHashes = [ordered]@{
        'model_optimized.onnx' = '1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38'
        'config.json' = '9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3'
        'tokenizer.json' = '48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26'
        'tokenizer_config.json' = 'e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a'
        'special_tokens_map.json' = 'b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3'
    }
    $modelRootRelative = '_internal/amadeus_desktop/resources/embedding_model/'
    $privateAssetFolderName = [char]0x514b + [char]0x91cc + [char]0x65af + [char]0x63d0 + [char]0x62c9
    $modelFilesSeen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $files = @(Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction Stop | Sort-Object FullName)
    foreach ($file in $files) {
        Assert-True -Condition (-not ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) -Category 'installed_scan_reparse_file'
        $relative = $file.FullName.Substring($root.Length).TrimStart('\').Replace('\', '/')
        $normalized = $relative.ToLowerInvariant()
        $normalizedWithRoot = '/' + $normalized
        $basename = $file.Name.ToLowerInvariant()
        $suffix = $file.Extension.ToLowerInvariant()
        $hasLocalPrivateFile = (
            $basename -eq '.env' -or
            $basename.StartsWith('.env.') -or
            $normalized.Contains('.amadeus-backup') -or
            $basename -in @('amadeus-chat-export.json', 'amadeus-memory-export.json')
        )
        Assert-True -Condition (-not $hasLocalPrivateFile) -Category 'installed_scan_local_private_file'
        $hasPrivateMaterial = (
            $suffix -in @('.sqlite', '.sqlite3', '.db', '.log', '.jsonl') -or
            $basename.EndsWith('-wal') -or
            $basename.EndsWith('-shm') -or
            $normalized.Contains('reference/amadeus') -or
            $normalized.Contains($privateAssetFolderName) -or
            $normalizedWithRoot -like '*/personas/*'
        )
        Assert-True -Condition (-not $hasPrivateMaterial) -Category 'installed_scan_private_material'
        if ($suffix -in @('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.wav', '.mp3', '.flac')) {
            $isPublicSheet = $normalized -eq '_internal/amadeus_desktop/resources/builtin_pet/spritesheet.png'
            $mediaHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            Assert-True -Condition (
                $isPublicSheet -and
                $mediaHash -ceq '2d9795265224b99619d34320e57b070a081ebc1c55df0152fd3041242dbd953e'
            ) -Category 'installed_scan_unauthorized_character_asset'
        }
        if ($normalized.StartsWith($modelRootRelative, [StringComparison]::Ordinal)) {
            Assert-True -Condition ($normalized.Substring($modelRootRelative.Length) -notmatch '/') -Category 'installed_scan_nested_model_file'
            Assert-True -Condition ($modelFilesSeen.Add($basename)) -Category 'installed_scan_duplicate_model_file'
            if ($basename -eq 'amadeus-model.json') {
                try {
                    $manifest = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                }
                catch {
                    Throw-Category -Category 'installed_scan_model_manifest_invalid'
                }
                $manifestFiles = @($manifest.files.PSObject.Properties.Name | Sort-Object)
                Assert-True -Condition (
                    [int]$manifest.schema_version -eq 1 -and
                    [string]$manifest.api_name -ceq 'BAAI/bge-small-zh-v1.5' -and
                    [string]$manifest.repository -ceq 'Qdrant/bge-small-zh-v1.5' -and
                    [string]$manifest.revision -ceq '46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59' -and
                    [string]$manifest.onnx_file -ceq 'model_optimized.onnx' -and
                    [string]$manifest.onnx_sha256 -ceq [string]$modelHashes['model_optimized.onnx'] -and
                    [int]$manifest.dimension -eq 512 -and
                    ($manifestFiles -join ',') -ceq (@($modelHashes.Keys | Sort-Object) -join ',')
                ) -Category 'installed_scan_model_manifest_invalid'
                foreach ($name in $modelHashes.Keys) {
                    $entry = $manifest.files.PSObject.Properties[$name].Value
                    Assert-True -Condition (
                        [string]$entry.sha256 -ceq [string]$modelHashes[$name] -and
                        [long]$entry.size -gt 0
                    ) -Category 'installed_scan_model_manifest_invalid'
                }
            }
            else {
                Assert-True -Condition ($modelHashes.Contains($basename)) -Category 'installed_scan_unverified_model_file'
                $modelHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                Assert-True -Condition ($modelHash -ceq [string]$modelHashes[$basename]) -Category 'installed_scan_unverified_model_file'
            }
        }
        elseif ($suffix -eq '.onnx') {
            Throw-Category -Category 'installed_scan_unexpected_model_file'
        }
        Assert-True -Condition (-not (Test-SecretPatternInFile -Path $file.FullName)) -Category 'installed_scan_credential_pattern'
    }
    $expectedModelFiles = @($modelHashes.Keys) + @('amadeus-model.json')
    Assert-True -Condition (
        $modelFilesSeen.Count -eq $expectedModelFiles.Count -and
        @($expectedModelFiles | Where-Object { -not $modelFilesSeen.Contains($_) }).Count -eq 0
    ) -Category 'installed_scan_model_bundle_incomplete'
    $script:InstalledScanEvidence = [ordered]@{
        status = 'passed'
        files_scanned = $files.Count
        credential_pattern_count = 0
        private_material_count = 0
        unauthorized_character_asset_count = 0
        model_file_count = $modelFilesSeen.Count
        payload_sha256 = Get-PayloadDigest -Root $root
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

function Invoke-ElevatedDefenderAcceptance {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedInstallerHash,
        [Parameter(Mandatory = $true)][string]$ExpectedInstalledDigest,
        [Parameter(Mandatory = $true)][string]$ExpectedUserName
    )
    Assert-NonElevatedToken
    $inputRoot = 'C:\AmadeusP7\Input'
    $outputRoot = 'C:\AmadeusP7\Output'
    $helperPath = Resolve-ContainedFile `
        -Path (Join-Path $inputRoot 'invoke-sandbox-defender.ps1') `
        -Root $inputRoot `
        -Category 'defender_helper_outside_input'
    $evidencePath = [IO.Path]::GetFullPath((Join-Path $outputRoot 'defender-evidence.json'))
    $outputPrefix = [IO.Path]::GetFullPath($outputRoot).TrimEnd(
        [IO.Path]::DirectorySeparatorChar
    ) + [IO.Path]::DirectorySeparatorChar
    Assert-True -Condition (
        $evidencePath.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)
    ) -Category 'defender_evidence_outside_output'
    Assert-True -Condition (-not (Test-Path -LiteralPath $evidencePath)) -Category 'defender_evidence_not_clean'

    $powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    try {
        # This is the sole elevated process in P7 acceptance. It can only scan
        # fixed paths; every installer and uninstaller remains in this medium
        # integrity parent process before and after this call.
        $process = Start-Process `
            -FilePath $powershellPath `
            -ArgumentList @(
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                $helperPath
            ) `
            -Verb RunAs `
            -PassThru `
            -WindowStyle Hidden
    }
    catch {
        Throw-Category -Category 'defender_elevation_rejected'
    }
    if (-not $process.WaitForExit(1500000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Throw-Category -Category 'defender_helper_timeout'
    }
    $process.Refresh()
    Assert-True -Condition (Test-Path -LiteralPath $evidencePath -PathType Leaf) -Category 'defender_evidence_missing'
    $evidenceItem = Get-Item -LiteralPath $evidencePath -ErrorAction Stop
    Assert-True -Condition (-not ($evidenceItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) -Category 'defender_evidence_reparse'
    Assert-True -Condition ($evidenceItem.Length -gt 0 -and $evidenceItem.Length -le 65536) -Category 'defender_evidence_size_invalid'
    $bytes = [IO.File]::ReadAllBytes($evidencePath)
    try {
        try {
            $evidence = [Text.UTF8Encoding]::new($false, $true).GetString($bytes) | ConvertFrom-Json
        }
        catch {
            Throw-Category -Category 'defender_evidence_invalid'
        }
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
    $properties = @($evidence.PSObject.Properties.Name | Sort-Object)
    Assert-True -Condition (
        $properties.Count -eq 7 -and
        ($properties -join ',') -ceq 'current_installer,execution_identity,failure_category,installed_directory,product_status,schema,status'
    ) -Category 'defender_evidence_fields_invalid'
    Assert-True -Condition ($process.ExitCode -eq 0) -Category 'defender_helper_failed'
    Assert-True -Condition (
        [string]$evidence.schema -ceq 'amadeus-p7-sandbox-defender/v1' -and
        [string]$evidence.status -ceq 'passed' -and
        [string]$evidence.failure_category -ceq ''
    ) -Category 'defender_evidence_status_invalid'
    Assert-True -Condition (
        [string]$evidence.execution_identity.user_name -ceq $ExpectedUserName -and
        [bool]$evidence.execution_identity.is_administrator_role -and
        [bool]$evidence.execution_identity.is_elevated -and
        [string]$evidence.execution_identity.integrity_level -ceq 'high' -and
        [int]$evidence.execution_identity.integrity_rid -ge 0x3000 -and
        [int]$evidence.execution_identity.integrity_rid -lt 0x4000
    ) -Category 'defender_execution_identity_invalid'
    Assert-True -Condition (
        [bool]$evidence.product_status.am_service_enabled -and
        [bool]$evidence.product_status.antivirus_enabled -and
        [string]$evidence.product_status.scanner_signature_status -ceq 'Valid' -and
        [string]$evidence.product_status.scanner_publisher -ceq 'Microsoft'
    ) -Category 'defender_product_status_invalid'
    $scanExpectations = @(
        [pscustomobject]@{
            Name = 'current_installer'
            Kind = 'file'
            Digest = $ExpectedInstallerHash
        },
        [pscustomobject]@{
            Name = 'installed_directory'
            Kind = 'directory_payload'
            Digest = $ExpectedInstalledDigest
        }
    )
    foreach ($expected in $scanExpectations) {
        $scan = $evidence.PSObject.Properties[$expected.Name].Value
        $started = [DateTimeOffset]::MinValue
        $finished = [DateTimeOffset]::MinValue
        Assert-True -Condition (
            [string]$scan.status -ceq 'passed' -and
            [string]$scan.target_kind -ceq $expected.Kind -and
            [string]$scan.target_sha256 -ceq $expected.Digest -and
            [bool]$scan.target_digest_verified_after_scan -and
            [bool]$scan.remediation_disabled -and
            [int]$scan.exit_code -eq 0 -and
            [int]$scan.new_detection_count -eq 0 -and
            [DateTimeOffset]::TryParse([string]$scan.started_utc, [ref]$started) -and
            [DateTimeOffset]::TryParse([string]$scan.finished_utc, [ref]$finished) -and
            $finished -ge $started -and
            -not [string]::IsNullOrWhiteSpace([string]$scan.antimalware_product_version) -and
            -not [string]::IsNullOrWhiteSpace([string]$scan.engine_version) -and
            -not [string]::IsNullOrWhiteSpace([string]$scan.antivirus_signature_version) -and
            -not [string]::IsNullOrWhiteSpace([string]$scan.scanner_file_version)
        ) -Category 'defender_scan_evidence_invalid'
    }
    $script:DefenderEvidence = $evidence
    Assert-NonElevatedToken
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
        Assert-NonElevatedToken
        $script:NonElevatedMutationCount++
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

function Assert-StartMenuShortcuts {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedExecutable,
        [Parameter(Mandatory = $true)][string]$ExpectedUninstaller
    )
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $links = @(Get-ChildItem -LiteralPath $programs -Recurse -File -Filter '*Amadeus*.lnk' -ErrorAction SilentlyContinue)
    $shell = New-Object -ComObject WScript.Shell
    $applicationCount = 0
    $uninstallCount = 0
    foreach ($link in $links) {
        $target = [string]$shell.CreateShortcut($link.FullName).TargetPath
        if ([string]::IsNullOrWhiteSpace($target)) { continue }
        $resolvedTarget = [IO.Path]::GetFullPath($target)
        if ($resolvedTarget -eq [IO.Path]::GetFullPath($ExpectedExecutable)) { $applicationCount++ }
        if ($resolvedTarget -eq [IO.Path]::GetFullPath($ExpectedUninstaller)) { $uninstallCount++ }
    }
    Assert-True -Condition ($applicationCount -ge 1) -Category 'start_menu_application_shortcut_missing'
    Assert-True -Condition ($uninstallCount -ge 1) -Category 'start_menu_uninstall_shortcut_missing'
    $script:ShortcutEvidence = [ordered]@{
        application_shortcut_count = $applicationCount
        uninstall_shortcut_count = $uninstallCount
    }
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
        Assert-NonElevatedToken
        $script:NonElevatedMutationCount++
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

function Set-SyntheticAutostartChoice {
    param(
        [Parameter(Mandatory = $true)][bool]$Enabled,
        [Parameter(Mandatory = $true)][string]$ExecutablePath
    )
    $settingsPath = Join-Path $AppDataRoot 'config\settings.json'
    $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $settings.general.launch_at_login = $Enabled
    [IO.File]::WriteAllText(
        $settingsPath,
        ($settings | ConvertTo-Json -Depth 20),
        [Text.UTF8Encoding]::new($false)
    )
    if ($Enabled) {
        Set-RunValue -ExecutablePath $ExecutablePath
    }
    else {
        Remove-RunValue
    }
    return [ordered]@{
        settings_sha256 = (Get-FileHash -LiteralPath $settingsPath -Algorithm SHA256).Hash
    }
}

function Write-SyntheticState {
    param(
        [Parameter(Mandatory = $true)][bool]$AutostartEnabled,
        [Parameter(Mandatory = $true)][string]$ExecutablePath
    )
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
    Invoke-SqliteFixtureProbe -Mode create
    return Set-SyntheticAutostartChoice -Enabled $AutostartEnabled -ExecutablePath $ExecutablePath
}

function Assert-SyntheticState {
    param([Parameter(Mandatory = $true)][Collections.IDictionary]$Hashes)
    foreach ($region in $Regions) {
        Assert-True -Condition (Test-Path -LiteralPath (Join-Path $AppDataRoot "$region\p7-$RunId.sentinel") -PathType Leaf) -Category 'synthetic_region_not_preserved'
    }
    $settingsHash = (Get-FileHash -LiteralPath (Join-Path $AppDataRoot 'config\settings.json') -Algorithm SHA256).Hash
    Assert-True -Condition ($settingsHash -eq $Hashes['settings_sha256']) -Category 'settings_not_preserved'
    Invoke-SqliteFixtureProbe -Mode verify
}

function Assert-CleanMachineRuntime {
    $uninstallRoots = @(
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    $runtimeProducts = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($root in $uninstallRoots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        foreach ($key in Get-ChildItem -LiteralPath $root -ErrorAction Stop) {
            $item = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop
            $displayName = $item.PSObject.Properties['DisplayName']
            if ($null -eq $displayName) { continue }
            $name = [string]$displayName.Value
            if ($name -match '(?i)(?:^|\s)(?:Python(?:\s|$)|Node\.js(?:\s|$)|Electron(?:\s|$))') {
                [void]$runtimeProducts.Add($key.PSPath)
            }
        }
    }
    $runtimeCommands = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $executionAliases = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($commandName in @('python.exe', 'python3.exe', 'py.exe', 'node.exe', 'npm.cmd', 'npx.cmd', 'electron.exe', 'electron.cmd')) {
        foreach ($command in @(Get-Command $commandName -All -CommandType Application -ErrorAction SilentlyContinue)) {
            $path = [IO.Path]::GetFullPath([string]$command.Source)
            $isWindowsAppsAlias = $path -match '(?i)\\Microsoft\\WindowsApps\\' -and (
                ((Get-Item -LiteralPath $path -ErrorAction SilentlyContinue).Length -eq 0) -or
                ((Get-Item -LiteralPath $path -ErrorAction SilentlyContinue).Attributes -band [IO.FileAttributes]::ReparsePoint)
            )
            if ($isWindowsAppsAlias) {
                [void]$executionAliases.Add($path)
            }
            else {
                [void]$runtimeCommands.Add($path)
            }
        }
    }
    $runtimePaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $literalCandidates = @(
        (Join-Path $env:ProgramFiles 'nodejs'),
        (Join-Path ${env:ProgramFiles(x86)} 'nodejs'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        (Join-Path $env:LOCALAPPDATA 'Programs\nodejs'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Electron'),
        (Join-Path $env:APPDATA 'npm\node.exe'),
        (Join-Path $env:APPDATA 'npm\electron.cmd')
    )
    foreach ($candidate in $literalCandidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            [void]$runtimePaths.Add([IO.Path]::GetFullPath($candidate))
        }
    }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if (-not $root -or -not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        foreach ($filter in @('Python*', 'Node*', 'Electron*')) {
            foreach ($directory in @(Get-ChildItem -LiteralPath $root -Directory -Filter $filter -ErrorAction SilentlyContinue)) {
                [void]$runtimePaths.Add($directory.FullName)
            }
        }
    }
    $total = $runtimeProducts.Count + $runtimeCommands.Count + $runtimePaths.Count
    $script:RuntimeEvidence = [ordered]@{
        uninstall_product_count = $runtimeProducts.Count
        command_runtime_count = $runtimeCommands.Count
        common_path_runtime_count = $runtimePaths.Count
        app_execution_alias_count = $executionAliases.Count
        forbidden_runtime_detection_count = $total
    }
    Assert-True -Condition ($total -eq 0) -Category 'clean_machine_has_forbidden_runtime'
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
    Assert-NonElevatedToken
    $script:NonElevatedMutationCount++
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
        schema = 'amadeus-p7-sandbox-acceptance/v2'
        status = $Status
        product_version = $ExpectedVersion
        scenario_count = $script:Scenarios.Count
        scenarios = $script:Scenarios
        current_installer_sha256 = if ($script:InstallerHashes.Contains('current')) { $script:InstallerHashes['current'] } else { '' }
        baseline_installer_sha256 = if ($script:InstallerHashes.Contains('baseline')) { $script:InstallerHashes['baseline'] } else { '' }
        execution_identity = $script:ExecutionIdentity
        non_elevated_mutation_count = $script:NonElevatedMutationCount
        clean_runtime = $script:RuntimeEvidence
        defender = $script:DefenderEvidence
        installed_payload_scan = $script:InstalledScanEvidence
        build_identity = $script:BuildIdentity
        sqlite_fts_fixture = $script:SqliteEvidence
        start_menu_shortcuts = $script:ShortcutEvidence
        forbidden_runtime_product_count = if ($script:RuntimeEvidence.Contains('forbidden_runtime_detection_count')) {
            [int]$script:RuntimeEvidence['forbidden_runtime_detection_count']
        } else { -1 }
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
    $ExpectedBuildInfoPath = Resolve-ContainedFile -Path $ExpectedBuildInfoPath -Root $inputRoot -Category 'expected_build_info_outside_input'
    $script:ExpectedBuildInfo = Read-ExpectedBuildInfo -Path $ExpectedBuildInfoPath
    $resolvedOutput = [IO.Path]::GetFullPath($RequestedOutputPath)
    $outputPrefix = [IO.Path]::GetFullPath($outputRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    Assert-True -Condition ($resolvedOutput.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)) -Category 'summary_outside_output'
    Assert-True -Condition ($resolvedOutput -eq [IO.Path]::GetFullPath($OutputPath)) -Category 'summary_name_invalid'
    Assert-True -Condition ($InstallerPath -ne $BaselineInstallerPath) -Category 'installers_must_differ'
    $script:InstallerHashes['current'] = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $script:InstallerHashes['baseline'] = (Get-FileHash -LiteralPath $BaselineInstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()

    Assert-NonElevatedToken
    Assert-CleanMachineRuntime

    # Prove a final current build installs cleanly with the first-install
    # autostart task left at its default, then run the only elevated operation:
    # the fixed-path Defender helper. The application lifecycle and all later
    # installer/uninstaller mutations remain in this medium-integrity process.
    Invoke-Installer -Path $InstallerPath
    $freshCurrent = Get-InstallRecord
    Assert-True -Condition ($freshCurrent.Version -eq $ExpectedVersion) -Category 'fresh_current_version_invalid'
    Assert-True -Condition ($freshCurrent.InstallLocation -eq $DefaultInstallRoot) -Category 'fresh_current_install_scope_invalid'
    $freshCurrentExe = Join-Path $freshCurrent.InstallLocation 'Amadeus.exe'
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'fresh_current_enabled_autostart_by_default'
    Assert-InstalledPayload -InstallRoot $freshCurrent.InstallLocation
    Assert-InstalledBuildInfo -InstallRoot $freshCurrent.InstallLocation
    $freshUninstaller = Get-UninstallerPath -InstallRoot $freshCurrent.InstallLocation
    Assert-StartMenuShortcuts -ExpectedExecutable $freshCurrentExe -ExpectedUninstaller $freshUninstaller
    Assert-InstalledPrivacyAndModelBoundary -InstallRoot $freshCurrent.InstallLocation
    Invoke-AppLifecycle -ExecutablePath $freshCurrentExe
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'fresh_current_lifecycle_enabled_autostart'
    Invoke-ElevatedDefenderAcceptance `
        -ExpectedInstallerHash ([string]$script:InstallerHashes['current']) `
        -ExpectedInstalledDigest ([string]$script:InstalledScanEvidence['payload_sha256']) `
        -ExpectedUserName ([string]$script:ExecutionIdentity['user_name'])

    Invoke-Uninstaller -InstallRoot $freshCurrent.InstallLocation -DeleteUserData
    Wait-UninstallCompletion -InstallRoot $freshCurrent.InstallLocation
    Assert-True -Condition (-not (Test-Path -LiteralPath $freshCurrent.InstallLocation)) -Category 'fresh_current_program_not_removed'
    Assert-NoStartMenuShortcut
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'fresh_current_autostart_not_removed'
    Assert-True -Condition (-not (Test-CredentialExists)) -Category 'fresh_current_credential_not_clean'
    foreach ($region in $Regions) {
        Assert-True -Condition (-not (
            Test-Path -LiteralPath (Join-Path $AppDataRoot $region)
        )) -Category 'fresh_current_data_region_not_removed'
    }
    Assert-NonElevatedToken
    Add-PassedScenario -Name 'clean_windows_runtime'

    # Upgrade case 1: the user kept launch-at-login enabled.
    Invoke-Installer -Path $BaselineInstallerPath -EnableAutostart
    $baseline = Get-InstallRecord
    $baselineComparable = ConvertTo-ComparableVersion -Value $baseline.Version
    $currentComparable = ConvertTo-ComparableVersion -Value $ExpectedVersion
    Assert-True -Condition ($baselineComparable -lt $currentComparable) -Category 'baseline_version_not_lower'
    Assert-True -Condition ($baseline.InstallLocation -eq $DefaultInstallRoot) -Category 'baseline_install_scope_invalid'
    $baselineExe = Join-Path $baseline.InstallLocation 'Amadeus.exe'
    Assert-True -Condition ($null -ne (Get-RunValue)) -Category 'baseline_autostart_task_failed'
    Invoke-AppLifecycle -ExecutablePath $baselineExe
    Initialize-SqliteProbe -InstallRoot $baseline.InstallLocation
    $syntheticHashes = Write-SyntheticState -AutostartEnabled $true -ExecutablePath $baselineExe
    Set-FakeCredential

    Invoke-Installer -Path $InstallerPath
    $current = Get-InstallRecord
    Assert-True -Condition ($current.Version -eq $ExpectedVersion) -Category 'upgrade_version_invalid'
    Assert-True -Condition ($current.InstallLocation -eq $DefaultInstallRoot) -Category 'upgrade_install_scope_changed'
    $script:CurrentInstallRoot = $current.InstallLocation
    $currentExe = Join-Path $current.InstallLocation 'Amadeus.exe'
    Assert-InstalledPayload -InstallRoot $current.InstallLocation
    Assert-InstalledBuildInfo -InstallRoot $current.InstallLocation
    Assert-SqliteRuntimeMatchesInstall -InstallRoot $current.InstallLocation
    $currentUninstaller = Get-UninstallerPath -InstallRoot $current.InstallLocation
    Assert-StartMenuShortcuts -ExpectedExecutable $currentExe -ExpectedUninstaller $currentUninstaller
    Assert-SyntheticState -Hashes $syntheticHashes
    Assert-FakeCredential -Category 'credential_not_preserved_on_upgrade'
    Assert-True -Condition ($null -ne (Get-RunValue)) -Category 'upgrade_disabled_enabled_autostart_choice'
    Invoke-AppLifecycle -ExecutablePath $currentExe
    Assert-SyntheticState -Hashes $syntheticHashes
    Assert-True -Condition ($null -ne (Get-RunValue)) -Category 'application_lost_enabled_autostart_choice'

    # Preserve the synthetic database and credential while returning to the
    # lower-version baseline for the independent disabled-choice upgrade case.
    Invoke-Uninstaller -InstallRoot $current.InstallLocation
    Wait-UninstallCompletion -InstallRoot $current.InstallLocation
    Assert-NoStartMenuShortcut
    Assert-SyntheticState -Hashes $syntheticHashes
    Assert-FakeCredential -Category 'credential_not_preserved_between_upgrade_cases'
    Invoke-Installer -Path $BaselineInstallerPath
    $baseline = Get-InstallRecord
    Assert-True -Condition ($baseline.Version -ne $ExpectedVersion) -Category 'second_baseline_version_invalid'
    Assert-True -Condition ($baseline.InstallLocation -eq $DefaultInstallRoot) -Category 'second_baseline_install_scope_invalid'
    $baselineExe = Join-Path $baseline.InstallLocation 'Amadeus.exe'
    Assert-SqliteRuntimeMatchesInstall -InstallRoot $baseline.InstallLocation
    Assert-SyntheticState -Hashes $syntheticHashes

    # Upgrade case 2: emulate the user's later in-app choice to disable startup.
    $syntheticHashes = Set-SyntheticAutostartChoice -Enabled $false -ExecutablePath $baselineExe
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'disabled_autostart_fixture_failed'
    Invoke-Installer -Path $InstallerPath
    $current = Get-InstallRecord
    Assert-True -Condition ($current.Version -eq $ExpectedVersion) -Category 'second_upgrade_version_invalid'
    Assert-True -Condition ($current.InstallLocation -eq $DefaultInstallRoot) -Category 'second_upgrade_install_scope_changed'
    $script:CurrentInstallRoot = $current.InstallLocation
    $currentExe = Join-Path $current.InstallLocation 'Amadeus.exe'
    Assert-InstalledPayload -InstallRoot $current.InstallLocation
    Assert-InstalledBuildInfo -InstallRoot $current.InstallLocation
    Assert-SqliteRuntimeMatchesInstall -InstallRoot $current.InstallLocation
    $currentUninstaller = Get-UninstallerPath -InstallRoot $current.InstallLocation
    Assert-StartMenuShortcuts -ExpectedExecutable $currentExe -ExpectedUninstaller $currentUninstaller
    Assert-SyntheticState -Hashes $syntheticHashes
    Assert-FakeCredential -Category 'credential_not_preserved_on_second_upgrade'
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'upgrade_overrode_disabled_autostart_choice'
    Invoke-AppLifecycle -ExecutablePath $currentExe
    Assert-SyntheticState -Hashes $syntheticHashes
    Assert-True -Condition ($null -eq (Get-RunValue)) -Category 'application_overrode_disabled_autostart_choice'
    Invoke-WinCredProbe -ExecutablePath $currentExe
    Remove-FakeCredential
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
    Assert-InstalledPayload -InstallRoot $current.InstallLocation
    Assert-InstalledBuildInfo -InstallRoot $current.InstallLocation
    Assert-SqliteRuntimeMatchesInstall -InstallRoot $current.InstallLocation
    $currentUninstaller = Get-UninstallerPath -InstallRoot $current.InstallLocation
    Assert-StartMenuShortcuts -ExpectedExecutable $currentExe -ExpectedUninstaller $currentUninstaller
    Assert-SyntheticState -Hashes $syntheticHashes
    Assert-FakeCredential -Category 'credential_not_restored_after_reinstall'
    Invoke-AppLifecycle -ExecutablePath $currentExe
    Assert-FakeCredential -Category 'credential_not_preserved_after_reinstall_lifecycle'
    Remove-FakeCredential

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

    Assert-NonElevatedToken
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
    if ($script:SqliteProbeExecutable) {
        $probeRoot = Split-Path -Parent $script:SqliteProbeExecutable
        if ($probeRoot -and (Test-Path -LiteralPath $probeRoot -PathType Container)) {
            Remove-Item -LiteralPath $script:SqliteProbeExecutable -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $script:SqliteProbeLibrary -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $script:SqliteProbeDependency -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $probeRoot -Force -ErrorAction SilentlyContinue
        }
    }
    & "$env:SystemRoot\System32\shutdown.exe" /s /t 0 *> $null
}
