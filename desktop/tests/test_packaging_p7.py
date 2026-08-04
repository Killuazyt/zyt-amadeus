from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image

DESKTOP_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_ROOT = DESKTOP_ROOT / "packaging"
LICENSE_ROOT = DESKTOP_ROOT / "src" / "amadeus_desktop" / "resources" / "licenses"


def _locked_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    pattern = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)\s*$")
    for line in (DESKTOP_ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if match := pattern.match(line):
            packages[re.sub(r"[-_.]+", "-", match.group(1).lower())] = match.group(2)
    return packages


def test_release_license_manifest_exactly_covers_runtime_lock() -> None:
    manifest = json.loads(
        (LICENSE_ROOT / "runtime-license-manifest.json").read_text(encoding="utf-8")
    )
    packages = {
        re.sub(r"[-_.]+", "-", entry["name"].lower()): entry["version"]
        for entry in manifest["packages"]
    }

    assert manifest["schema_version"] == 1
    assert packages == _locked_packages()
    assert all(entry["license"] and entry["source"] for entry in manifest["packages"])
    assert {entry["name"]: entry["version"] for entry in manifest["bundled_components"]}[
        "Inno Setup"
    ] == "6.7.3"


def test_release_notices_include_qt_compliance_and_canonical_license_texts() -> None:
    notices = (LICENSE_ROOT / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    qt_notice = (LICENSE_ROOT / "QT_LGPL_COMPLIANCE.txt").read_text(encoding="utf-8")

    for name, version in _locked_packages().items():
        assert re.search(rf"(?im)^{re.escape(name)} {re.escape(version)} \|", notices)
    assert "6ffd9835bb0dd2c56f061d62f1616bb1707cfc0202b80e3165d6be087f3965e2" in qt_notice
    assert "252acef8c5ae68074d91cadba2ee4a83465051bbb970dd26e8f0daa0f3904e03" in qt_notice
    assert "ABI-compatible" in qt_notice
    assert "reverse engineer" in qt_notice.lower()
    assert "For three years" in qt_notice
    assert (LICENSE_ROOT / "LGPL-3.0.txt").stat().st_size > 20_000
    assert (LICENSE_ROOT / "GPL-3.0.txt").stat().st_size > 30_000
    assert "Bootloader Exception" in (LICENSE_ROOT / "PYINSTALLER_COPYING.txt").read_text(
        encoding="utf-8"
    )
    assert "Inno Setup" in (LICENSE_ROOT / "INNO_SETUP_LICENSE.txt").read_text(encoding="utf-8")


def test_pyinstaller_spec_has_p7_resources_and_excludes_unselected_qt_plugins() -> None:
    source = (PACKAGING_ROOT / "amadeus-desktop.spec").read_text(encoding="utf-8")

    assert 'hiddenimports = ["pywintypes", "win32api", "win32cred", "win32timezone"]' in source
    assert "AMADEUS_PYINSTALLER_BUILD_INFO" in source
    assert "AMADEUS_PYINSTALLER_ICON" in source
    assert 'version=str(desktop_root / "packaging" / "amadeus-version-info.txt")' in source
    assert '"pyside6/plugins/imageformats/qpdf.dll"' in source
    assert '"pyside6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll"' in source


def test_inno_script_locks_p7_identity_and_safe_uninstall_contract() -> None:
    source = (PACKAGING_ROOT / "amadeus.iss").read_text(encoding="utf-8")

    required = (
        '#define ProductAppId "8739af85-a7c4-54f5-a318-5d15264178e9"',
        '#define Publisher "Killuazyt"',
        "DefaultDirName={localappdata}\\Programs\\Amadeus",
        "PrivilegesRequired=lowest",
        "ArchitecturesAllowed=x64compatible",
        "AppMutex={#ProductMutex}",
        "CloseApplications=no",
        "UsePreviousTasks=no",
        'Name: "autostart"',
        "Flags: unchecked; Check: IsFreshInstall",
        "ewWaitUntilTerminated",
        "if ResultCode <> 0 then",
        "Abort;",
        "{param:DELETEUSERDATA|0}",
        "RegDeleteValue(HKCU, 'Software\\Microsoft\\Windows\\CurrentVersion\\Run', 'Amadeus')",
    )
    for marker in required:
        assert marker in source
    assert "VersionInfoVersion={#NumericVersion}" in source
    assert "VersionInfoVersion={#AppVersion}" not in source
    assert "[UninstallRun]" not in source


def test_installer_build_script_has_formal_and_acceptance_outputs() -> None:
    source = (DESKTOP_ROOT / "scripts" / "build-installer.ps1").read_text(encoding="utf-8")

    assert "[Parameter(Mandatory = $true)]" in source
    assert "[string]$ModelPath" in source
    assert "$ExpectedInnoVersion = '6.7.3'" in source
    assert "Installer build requires a clean Git working tree" in source
    assert "[switch]$AllowDirty" in source
    assert "[switch]$BuildAcceptanceBaseline" in source
    assert "Amadeus-0.7.0.dev7-win64-setup" in source
    assert "Amadeus-0.6.0.dev6-win64-acceptance-baseline-setup" in source
    assert "SHA256SUMS.txt" in source
    assert "Get-AuthenticodeSignature" in source
    assert "SignatureStatus]::Valid" in source
    assert "SignatureStatus]::NotSigned" in source
    assert "Compiler engine version: Inno Setup 6\\.7\\.3" in source


def test_sandbox_acceptance_requires_microsoft_defender_installer_scan() -> None:
    source = (DESKTOP_ROOT / "scripts" / "accept-installer-sandbox.ps1").read_text(encoding="utf-8")

    assert "Get-AuthenticodeSignature -LiteralPath $scanner" in source
    assert "Get-MpComputerStatus" in source
    assert "@('-Scan', '-ScanType', '3', '-File', $TargetPath)" in source
    assert "defender_installer_scan_failed" in source
    assert "antivirus_signature_version" in source
    assert "defender = $script:DefenderEvidence" in source


def test_onedir_build_reads_back_exact_git_and_windows_version_identity() -> None:
    source = (DESKTOP_ROOT / "scripts" / "build-onedir.ps1").read_text(encoding="utf-8")

    assert "$packagedBuildInfo.commit_sha -ne $expectedCommitSha" in source
    assert "$packagedBuildInfo.build_date_utc -ne $expectedBuildDate" in source
    assert "$executableVersionInfo.CompanyName.Trim() -ne 'Killuazyt'" in source
    assert "$executableVersionInfo.FileVersion.Trim() -ne $ExpectedVersion" in source
    assert "$executableVersionInfo.ProductVersion.Trim() -ne $ExpectedVersion" in source
    assert "payload_manifest.py" in source
    assert "Get-VerifiedChildProcesses" in source
    assert "CreationDate disambiguates" in source
    assert "-ObservationEndTimeUtc $processExitTimeUtc" in source
    assert "$childCreationTimeUtc -le $processExitTimeUtc" in source
    assert "candidate whose identity cannot be disambiguated must fail closed" in source
    assert "created a child process: $childEvidence" in source


def test_onedir_child_probe_disambiguates_reused_process_ids_by_time_window() -> None:
    source = (DESKTOP_ROOT / "scripts" / "build-onedir.ps1").read_text(encoding="utf-8")
    helper_source = source[
        source.index("function Convert-CimProcessCreationDateToUtc") : source.index(
            "function Invoke-PackagedProbe"
        )
    ]
    probe = f"""
{helper_source}
function Get-CimInstance {{
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)][string]$ClassName,
        [string]$Filter
    )
    return $script:Candidates
}}
$start = [DateTimeOffset]::Parse('2026-08-04T00:00:00Z').UtcDateTime
$end = $start.AddSeconds(10)
$script:Candidates = @(
    [pscustomobject]@{{ Name = 'before'; ProcessId = 1; CreationDate = $start.AddTicks(-1) }},
    [pscustomobject]@{{ Name = 'at-start'; ProcessId = 2; CreationDate = $start }},
    [pscustomobject]@{{ Name = 'inside'; ProcessId = 3; CreationDate = $start.AddSeconds(5) }},
    [pscustomobject]@{{ Name = 'at-end'; ProcessId = 4; CreationDate = $end }},
    [pscustomobject]@{{ Name = 'after'; ProcessId = 5; CreationDate = $end.AddTicks(1) }},
    [pscustomobject]@{{ Name = 'invalid'; ProcessId = 6; CreationDate = 'invalid' }}
)
$result = @(
    Get-VerifiedChildProcesses `
        -ParentProcessId 100 `
        -ParentCreationTimeUtc $start `
        -ObservationEndTimeUtc $end
)
@($result | ForEach-Object {{ $_.Name }}) | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ("powershell.exe", "-NoProfile", "-Command", probe),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == [
        "at-start",
        "inside",
        "at-end",
        "invalid",
    ]


def test_payload_manifest_is_generated_and_detects_changes(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    (payload / "nested").mkdir(parents=True)
    (payload / "Amadeus.exe").write_bytes(b"synthetic-executable")
    (payload / "nested" / "中文 file.txt").write_text("safe", encoding="utf-8")
    script = DESKTOP_ROOT / "scripts" / "payload_manifest.py"

    created = subprocess.run(
        (sys.executable, str(script), "create", "--root", str(payload)),
        check=False,
        capture_output=True,
        text=True,
    )
    verified = subprocess.run(
        (sys.executable, str(script), "verify", "--root", str(payload)),
        check=False,
        capture_output=True,
        text=True,
    )

    assert created.returncode == 0, created.stderr
    assert verified.returncode == 0, verified.stderr
    manifest = (payload / "PAYLOAD-SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "  Amadeus.exe\n" in manifest
    assert "  nested/中文 file.txt\n" in manifest

    (payload / "nested" / "中文 file.txt").write_text("changed", encoding="utf-8")
    rejected = subprocess.run(
        (sys.executable, str(script), "verify", "--root", str(payload)),
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "payload hash differs" in rejected.stderr


def test_cc0_icon_generator_produces_multisize_windows_icon(tmp_path: Path) -> None:
    source = (
        DESKTOP_ROOT / "src" / "amadeus_desktop" / "resources" / "builtin_pet" / "spritesheet.png"
    )
    output = tmp_path / "amadeus.ico"
    completed = subprocess.run(
        (
            sys.executable,
            str(DESKTOP_ROOT / "scripts" / "generate_app_icon.py"),
            "--source",
            str(source),
            "--output",
            str(output),
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    with Image.open(output) as icon:
        assert icon.format == "ICO"
        assert (256, 256) in icon.info["sizes"]
        assert (16, 16) in icon.info["sizes"]
