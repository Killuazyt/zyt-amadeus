from __future__ import annotations

import json
import re
import subprocess
import sys
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image

DESKTOP_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_ROOT = DESKTOP_ROOT / "packaging"
LICENSE_ROOT = DESKTOP_ROOT / "src" / "amadeus_desktop" / "resources" / "licenses"
WORKFLOW_PATH = DESKTOP_ROOT.parent / ".github" / "workflows" / "desktop-ci.yml"


def _locked_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    pattern = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)\s*$")
    for line in (DESKTOP_ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if match := pattern.match(line):
            packages[re.sub(r"[-_.]+", "-", match.group(1).lower())] = match.group(2)
    return packages


def test_ci_degraded_onedir_smoke_allows_bounded_cold_start() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "timeout-minutes: 15" in source
    assert "faulthandler_timeout=300" in source
    assert "python -m pytest -vv --durations=30" in source
    assert "--auto-exit-ms=500" in source
    assert "$process.WaitForExit(60000)" in source
    assert "Stop-Process -Id $process.Id -Force" in source
    assert "$process.WaitForExit(15000)" not in source


def test_ci_test_only_dispatch_skips_every_build_and_installer_step() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    test_only_guard = (
        "if: ${{ github.event_name != 'workflow_dispatch' || inputs.test_only != true }}"
    )

    assert "test_only:" in source
    assert "type: boolean" in source
    assert "default: false" in source
    for step_name in (
        "Install project package",
        "Build Python package",
        "Verify Python package assets",
        "Verify isolated wheel install",
        "Build FTS-degraded onedir",
        "Smoke FTS-degraded onedir",
    ):
        step_start = source.index(f"- name: {step_name}")
        run_start = source.index("\n        run:", step_start)
        assert test_only_guard in source[step_start:run_start]
    installer_start = source.index("  package-installer:")
    installer_needs = source.index("\n    needs: test", installer_start)
    installer_guard = source[installer_start:installer_needs]
    assert "github.event_name != 'workflow_dispatch'" in installer_guard
    assert "inputs.test_only != true" in installer_guard


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


def test_builtin_kurisu_packaging_pins_webp_notice_and_license_manifest() -> None:
    expected_hash = "cca259ac33ffc7c8170b401a315f9a177a865eb063ba44a4da87c3ab13fa90b7"
    pyproject = (DESKTOP_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    notice = (LICENSE_ROOT / "KURISU-ASSET-NOTICE.txt").read_text(encoding="utf-8")
    manifest = json.loads(
        (LICENSE_ROOT / "runtime-license-manifest.json").read_text(encoding="utf-8")
    )
    components = {entry["name"]: entry for entry in manifest["bundled_components"]}

    assert '"resources/builtin_pet/*.webp"' in pyproject
    assert '"resources/licenses/*.txt"' in pyproject
    assert '"resources/provider_catalog/*.json"' in pyproject
    assert expected_hash in notice
    assert "NOASSERTION" in notice
    component_name = "Amadeus built-in Kurisu 4x high-resolution spritesheet derivative"
    assert components[component_name] == {
        "name": component_name,
        "version": f"sha256:{expected_hash}",
        "license": "NOASSERTION",
        "source": "resources/builtin_pet/LICENSE.txt",
    }


def test_python_package_asset_checker_verifies_both_archive_formats(tmp_path: Path) -> None:
    package_root = DESKTOP_ROOT / "src" / "amadeus_desktop"
    relative_files = (
        "resources/app_icon/LICENSE.txt",
        "resources/app_icon/amadeus-kurisu.png",
        "resources/app_icon/spritesheet.png",
        "resources/builtin_pet/LICENSE.txt",
        "resources/builtin_pet/pet.amadeus.json",
        "resources/builtin_pet/spritesheet.webp",
        "resources/licenses/CC0-1.0.txt",
        "resources/licenses/KURISU-ASSET-NOTICE.txt",
        "resources/licenses/KURISU-ICON-NOTICE.txt",
        "resources/licenses/runtime-license-manifest.json",
        "resources/provider_catalog/providers.json",
    )
    wheel = tmp_path / "amadeus_desktop-0.7.0.dev7-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for relative in relative_files:
            archive.write(package_root / relative, f"amadeus_desktop/{relative}")

    sdist = tmp_path / "amadeus_desktop-0.7.0.dev7.tar.gz"
    with tarfile.open(sdist, "w:gz", compresslevel=1) as archive:
        for relative in relative_files:
            payload = (package_root / relative).read_bytes()
            member = tarfile.TarInfo(f"amadeus_desktop-0.7.0.dev7/src/amadeus_desktop/{relative}")
            member.size = len(payload)
            archive.addfile(member, BytesIO(payload))

    command = (
        sys.executable,
        str(DESKTOP_ROOT / "scripts" / "check_python_package_assets.py"),
        "--dist",
        str(tmp_path),
    )
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout)["status"] == "passed"
    assert "python scripts/check_python_package_assets.py --dist dist" in WORKFLOW_PATH.read_text(
        encoding="utf-8"
    )

    with zipfile.ZipFile(wheel, "w") as archive:
        for relative in relative_files:
            payload = (package_root / relative).read_bytes()
            if relative == "resources/builtin_pet/spritesheet.webp":
                payload += b"tampered"
            archive.writestr(f"amadeus_desktop/{relative}", payload)
    failed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)

    assert failed.returncode == 1
    assert json.loads(failed.stdout)["status"] == "failed"
    assert "built-in pet hash is invalid" in failed.stdout


def test_pyinstaller_spec_has_p7_resources_and_excludes_unselected_qt_plugins() -> None:
    source = (PACKAGING_ROOT / "amadeus-desktop.spec").read_text(encoding="utf-8")

    assert 'hiddenimports = ["pywintypes", "win32api", "win32cred", "win32timezone"]' in source
    assert "AMADEUS_PYINSTALLER_BUILD_INFO" in source
    assert "AMADEUS_PYINSTALLER_ICON" in source
    assert 'version=str(desktop_root / "packaging" / "amadeus-version-info.txt")' in source
    assert '"amadeus_desktop/resources/provider_catalog"' in source
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


def test_sandbox_defender_is_the_only_elevated_fixed_path_helper() -> None:
    main = (DESKTOP_ROOT / "scripts" / "accept-installer-sandbox.ps1").read_text(encoding="utf-8")
    helper = (DESKTOP_ROOT / "scripts" / "invoke-sandbox-defender.ps1").read_text(encoding="utf-8")

    assert main.count("-Verb RunAs") == 1
    assert "Invoke-ElevatedDefenderAcceptance" in main
    assert "defender_execution_identity_invalid" in main
    assert "Assert-NonElevatedToken" in main
    assert "MpCmdRun.exe" not in main
    assert "Get-MpComputerStatus" not in main
    assert "WaitForExit(1500000)" in main
    for marker in (
        "param()",
        "$InstallerPath = 'C:\\AmadeusP7\\Input\\current-setup.exe'",
        "$EvidencePath = 'C:\\AmadeusP7\\Output\\defender-evidence.json'",
        "Programs\\Amadeus",
        "AmadeusP7ElevatedTokenEvidence",
        "integrity_not_high",
        "Get-AuthenticodeSignature -LiteralPath $scanner",
        "Get-MpComputerStatus -ErrorAction Stop",
        "@('-Scan', '-ScanType', '3', '-File', $TargetPath, '-DisableRemediation')",
        "remediation_disabled = $true",
        "target_digest_verified_after_scan = $targetDigestStable",
        "target_changed_during_scan",
        "return ,$keys",
        "Get-PayloadDigest -Root $TargetPath",
        "amadeus-p7-sandbox-defender/v1",
    ):
        assert marker in helper
    assert "Invoke-Installer" not in helper
    assert "Invoke-Uninstaller" not in helper
    assert "Remove-Item" not in helper
    assert main.index("fresh_current_enabled_autostart_by_default") < main.index("# Upgrade case 1")
    assert main.index(
        "Assert-InstalledPrivacyAndModelBoundary -InstallRoot $freshCurrent.InstallLocation"
    ) < main.index("Invoke-ElevatedDefenderAcceptance `")


def test_sandbox_acceptance_hardens_identity_runtime_upgrade_and_sqlite_fts() -> None:
    source = (DESKTOP_ROOT / "scripts" / "accept-installer-sandbox.ps1").read_text(encoding="utf-8")

    for marker in (
        "AmadeusP7TokenEvidence",
        "ReadIntegrityRid",
        "is_administrator_role",
        "sandbox_token_is_elevated",
        "sandbox_token_integrity_not_medium",
        "forbidden_runtime_detection_count",
        "Get-Command $commandName -All -CommandType Application",
        "$item.PSObject.Properties['DisplayName']",
        "Programs\\Python",
        "Programs\\nodejs",
        "app_execution_alias_count",
        "upgrade_disabled_enabled_autostart_choice",
        "upgrade_overrode_disabled_autostart_choice",
        "credential_not_restored_after_reinstall",
        "credential_not_preserved_after_reinstall_lifecycle",
        "CREATE VIRTUAL TABLE IF NOT EXISTS p7_acceptance_fts",
        "p7_acceptance_fts MATCH 'marker:",
        "sqlite3_initialize()",
        "SetErrorMode(0x0001u | 0x0002u | 0x8000u)",
        "_internal\\vcruntime140.dll",
        "dependency_sha256",
        "Invoke-SqliteFixtureProbe -Mode verify",
        "$script:SqliteEvidence['fixture_row_count'] = 1",
        "$script:SqliteEvidence['fts_match_count'] = 1",
        "application_shortcut_count",
        "uninstall_shortcut_count",
        "installed_scan_credential_pattern",
        "installed_scan_unauthorized_character_asset",
        "installed_scan_model_bundle_incomplete",
        "Assert-InstalledBuildInfo",
        "fresh_current_enabled_autostart_by_default",
        "fresh_current_data_region_not_removed",
        "amadeus-p7-sandbox-acceptance/v2",
    ):
        assert marker in source
    assert "'(?:sk|tp)-[A-Za-z0-9_-]{24,}'" in source
    assert "'(?i)(?:sk|tp)-[A-Za-z0-9_-]{24,}'" not in source
    assert "Get-ItemPropertyValue -LiteralPath $key.PSPath -Name DisplayName" not in source
    installer_source = source[
        source.index("function Invoke-Installer") : source.index("function Get-InstallRecords")
    ]
    uninstaller_source = source[
        source.index("function Invoke-Uninstaller") : source.index(
            "function Wait-UninstallCompletion"
        )
    ]
    assert "RunAs" not in installer_source
    assert "RunAs" not in uninstaller_source
    assert "database_sha256" not in source
    assert "forbidden_runtime_product_count = 0" not in source


def test_sandbox_host_v2_fail_closed_contract_and_build_identity_handoff() -> None:
    source = (DESKTOP_ROOT / "scripts" / "start-sandbox-acceptance.ps1").read_text(encoding="utf-8")
    template = (PACKAGING_ROOT / "windows-sandbox" / "p7-acceptance.wsb.template").read_text(
        encoding="utf-8"
    )

    for marker in (
        "expected-build-info.json",
        "invoke-sandbox-defender.ps1",
        "sandbox_acceptance_git_worktree_dirty",
        "sandbox_acceptance_build_info_not_current_head",
        "amadeus-p7-sandbox-acceptance/v2",
        "sandbox_acceptance_execution_identity_invalid",
        "sandbox_acceptance_runtime_evidence_invalid",
        "sandbox_acceptance_build_identity_mismatch",
        "summary.defender.current_installer",
        "summary.defender.installed_directory",
        "summary.defender.execution_identity.integrity_level",
        "summary.defender.product_status.scanner_publisher",
        "defender-evidence.json",
        "sandbox_acceptance_defender_evidence_copy_mismatch",
        "sandbox_acceptance_defender_evidence_invalid",
        "sandbox_acceptance_installed_payload_scan_invalid",
        "sandbox_acceptance_sqlite_fixture_invalid",
        "sandbox_acceptance_shortcut_evidence_invalid",
        "WaitForExit(3600000)",
        "sandbox-host-summary.json.tmp",
        "[IO.File]::Move($hostSummaryTemporaryPath, $hostSummaryPath)",
        "amadeus-p7-sandbox-host/v2",
    ):
        assert marker in source
    assert "RunAs" not in source
    assert "-ExpectedBuildInfoPath C:\\AmadeusP7\\Input\\expected-build-info.json" in template


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


def test_kurisu_icon_generator_produces_multisize_windows_icon(tmp_path: Path) -> None:
    source = (
        DESKTOP_ROOT / "src" / "amadeus_desktop" / "resources" / "app_icon" / "amadeus-kurisu.png"
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
