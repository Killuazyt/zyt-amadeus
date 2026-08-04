from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from amadeus_desktop import maintenance as maintenance_module
from amadeus_desktop import paths as paths_module
from amadeus_desktop.autostart import AutostartError
from amadeus_desktop.credential_store import CredentialStoreError
from amadeus_desktop.maintenance import MaintenanceError, delete_all_local_data_for_uninstall
from amadeus_desktop.paths import AppDirectory, AppPaths


@dataclass
class FakeAutostart:
    value: str | None = '"C:\\Program Files\\Amadeus.exe"'
    fail: bool = False
    changes: int = 0

    def set_enabled(self, enabled: bool) -> bool:
        assert enabled is False
        self.changes += 1
        if self.fail:
            raise AutostartError("synthetic")
        self.value = None
        return False

    def registered_command(self) -> str | None:
        return self.value


@dataclass
class FakeCredentials:
    secret: str | None = "invalid-fake-uninstall-secret"
    fail: bool = False
    deletes: int = 0

    def has_secret(self) -> bool:
        return self.secret is not None

    def read_secret(self) -> str | None:
        return self.secret

    def write_secret(self, secret: str) -> None:
        self.secret = secret

    def delete_secret(self) -> None:
        self.deletes += 1
        if self.fail:
            raise CredentialStoreError("synthetic")
        self.secret = None


class CreatingAutostart(FakeAutostart):
    def __init__(self, create_path: Path) -> None:
        super().__init__()
        self.create_path = create_path

    def set_enabled(self, enabled: bool) -> bool:
        result = super().set_enabled(enabled)
        self.create_path.mkdir(parents=True)
        (self.create_path / "concurrent.txt").write_text("preserved", encoding="utf-8")
        return result


def _seed_regions(tmp_path: Path) -> tuple[AppPaths, tuple[Path, ...]]:
    paths = AppPaths.for_current_user(tmp_path)
    targets = tuple(paths.directory(region) for region in AppDirectory)
    for target in targets:
        target.mkdir(parents=True, exist_ok=True)
        (target / "synthetic.txt").write_text("synthetic", encoding="utf-8")
    return paths, targets


def test_delete_data_uninstall_removes_only_seven_regions_and_fixed_states(
    tmp_path: Path,
) -> None:
    paths, targets = _seed_regions(tmp_path)
    export = tmp_path / "用户导出.json"
    export.write_text("preserved", encoding="utf-8")
    autostart = FakeAutostart()
    credentials = FakeCredentials()
    mutex_checks: list[bool] = []

    delete_all_local_data_for_uninstall(
        paths=paths,
        autostart_manager=autostart,  # type: ignore[arg-type]
        credential_store=credentials,
        mutex_present=lambda: mutex_checks.append(False) or False,
    )

    assert len(targets) == 7
    assert all(not target.exists() for target in targets)
    assert paths.root.is_dir()
    assert export.read_text(encoding="utf-8") == "preserved"
    assert autostart.value is None
    assert credentials.secret is None
    assert mutex_checks == [False, False]


def test_delete_data_uninstall_accepts_absent_allowlisted_regions(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    autostart = FakeAutostart()
    credentials = FakeCredentials()

    delete_all_local_data_for_uninstall(
        paths=paths,
        autostart_manager=autostart,  # type: ignore[arg-type]
        credential_store=credentials,
        mutex_present=lambda: False,
    )

    assert autostart.value is None
    assert credentials.secret is None
    assert paths.root.exists() is False


def test_absent_root_created_during_cleanup_fails_without_deleting_it(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    concurrent_data = paths.directory(AppDirectory.DATA)
    credentials = FakeCredentials()

    with pytest.raises(MaintenanceError, match="appeared"):
        delete_all_local_data_for_uninstall(
            paths=paths,
            autostart_manager=CreatingAutostart(concurrent_data),  # type: ignore[arg-type]
            credential_store=credentials,
            remove_tree=lambda _path: pytest.fail("new data root must never be deleted"),
            mutex_present=lambda: False,
        )

    assert (concurrent_data / "concurrent.txt").read_text(encoding="utf-8") == "preserved"


def test_running_application_blocks_cleanup_before_any_state_change(tmp_path: Path) -> None:
    paths, targets = _seed_regions(tmp_path)
    autostart = FakeAutostart()
    credentials = FakeCredentials()

    with pytest.raises(MaintenanceError, match="still running"):
        delete_all_local_data_for_uninstall(
            paths=paths,
            autostart_manager=autostart,  # type: ignore[arg-type]
            credential_store=credentials,
            mutex_present=lambda: True,
        )

    assert all(target.is_dir() for target in targets)
    assert autostart.changes == 0
    assert credentials.deletes == 0


def test_unsafe_region_type_fails_before_registry_or_credential_deletion(tmp_path: Path) -> None:
    paths, targets = _seed_regions(tmp_path)
    target = targets[0]
    for child in target.iterdir():
        child.unlink()
    target.rmdir()
    target.write_text("not a directory", encoding="utf-8")
    autostart = FakeAutostart()
    credentials = FakeCredentials()

    with pytest.raises(MaintenanceError, match="safe directory"):
        delete_all_local_data_for_uninstall(
            paths=paths,
            autostart_manager=autostart,  # type: ignore[arg-type]
            credential_store=credentials,
            mutex_present=lambda: False,
        )

    assert autostart.changes == 0
    assert credentials.deletes == 0


def test_autostart_failure_preserves_credentials_and_all_data(tmp_path: Path) -> None:
    paths, targets = _seed_regions(tmp_path)
    credentials = FakeCredentials()

    with pytest.raises(MaintenanceError, match="Launch-at-login"):
        delete_all_local_data_for_uninstall(
            paths=paths,
            autostart_manager=FakeAutostart(fail=True),  # type: ignore[arg-type]
            credential_store=credentials,
            mutex_present=lambda: False,
        )

    assert credentials.deletes == 0
    assert all(target.is_dir() for target in targets)


def test_credential_failure_preserves_all_filesystem_data(tmp_path: Path) -> None:
    paths, targets = _seed_regions(tmp_path)

    with pytest.raises(MaintenanceError, match="Credential"):
        delete_all_local_data_for_uninstall(
            paths=paths,
            autostart_manager=FakeAutostart(),  # type: ignore[arg-type]
            credential_store=FakeCredentials(fail=True),
            mutex_present=lambda: False,
        )

    assert all(target.is_dir() for target in targets)


def test_default_uninstall_cleanup_ignores_tampered_localappdata_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "trusted-known-folder"
    attacker_root = tmp_path / "attacker-environment"
    paths, targets = _seed_regions(trusted_root)
    attacker_marker = attacker_root / "Amadeus" / "data" / "must-survive.txt"
    attacker_marker.parent.mkdir(parents=True)
    attacker_marker.write_text("preserved", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(attacker_root))
    monkeypatch.setattr(paths_module, "_known_local_app_data", lambda: trusted_root)

    delete_all_local_data_for_uninstall(
        autostart_manager=FakeAutostart(),  # type: ignore[arg-type]
        credential_store=FakeCredentials(),
        mutex_present=lambda: False,
    )

    assert paths.root == trusted_root / "Amadeus"
    assert all(not target.exists() for target in targets)
    assert attacker_marker.read_text(encoding="utf-8") == "preserved"


def test_nested_reparse_fails_before_any_registry_credential_or_data_deletion(
    tmp_path: Path,
) -> None:
    paths, targets = _seed_regions(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_marker = outside / "must-survive.txt"
    outside_marker.write_text("preserved", encoding="utf-8")
    nested_link = paths.directory(AppDirectory.DATA) / "nested-reparse"
    try:
        nested_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink privilege unavailable: {type(exc).__name__}")
    autostart = FakeAutostart()
    credentials = FakeCredentials()

    with pytest.raises(MaintenanceError, match="reparse"):
        delete_all_local_data_for_uninstall(
            paths=paths,
            autostart_manager=autostart,  # type: ignore[arg-type]
            credential_store=credentials,
            mutex_present=lambda: False,
        )

    assert autostart.changes == 0
    assert credentials.deletes == 0
    assert all(target.is_dir() for target in targets)
    assert outside_marker.read_text(encoding="utf-8") == "preserved"


def test_recursive_preflight_rejects_nested_reparse_metadata_without_os_privilege(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths, targets = _seed_regions(tmp_path)
    nested = paths.directory(AppDirectory.DATA) / "nested" / "deeper"
    nested.mkdir(parents=True)
    if os.name == "nt":
        original_entry_check = maintenance_module._windows_entry_is_reparse

        def identify_nested_entry(entry) -> bool:
            return entry.name == nested.name or original_entry_check(entry)

        monkeypatch.setattr(
            maintenance_module,
            "_windows_entry_is_reparse",
            identify_nested_entry,
        )
    else:
        nested_identity = (os.lstat(nested).st_dev, os.lstat(nested).st_ino)
        original_status_check = maintenance_module._status_is_reparse

        def identify_nested_status(status: os.stat_result) -> bool:
            return (status.st_dev, status.st_ino) == nested_identity or original_status_check(
                status
            )

        monkeypatch.setattr(
            maintenance_module,
            "_status_is_reparse",
            identify_nested_status,
        )
    autostart = FakeAutostart()
    credentials = FakeCredentials()

    with pytest.raises(MaintenanceError, match="reparse"):
        delete_all_local_data_for_uninstall(
            paths=paths,
            autostart_manager=autostart,  # type: ignore[arg-type]
            credential_store=credentials,
            mutex_present=lambda: False,
        )

    assert autostart.changes == 0
    assert credentials.deletes == 0
    assert all(target.is_dir() for target in targets)


@pytest.mark.skipif(os.name != "nt", reason="Win32 handle identity gate")
def test_handle_delete_blocks_concurrent_directory_replacement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    victim = target / "victim"
    replacement = tmp_path / "replacement"
    moved_original = tmp_path / "moved-original"
    victim.mkdir(parents=True)
    replacement.mkdir()
    (victim / "original.txt").write_text("original", encoding="utf-8")
    (replacement / "external.txt").write_text("external", encoding="utf-8")
    original_enumerate = maintenance_module._enumerate_windows_directory
    replacement_blocked = False

    def enumerate_and_swap(handle: int):
        nonlocal replacement_blocked
        entries = original_enumerate(handle)
        if not replacement_blocked and any(entry.name == victim.name for entry in entries):
            victim.rename(moved_original)
            try:
                replacement.rename(victim)
            except OSError:
                replacement_blocked = True
                raise
        return entries

    monkeypatch.setattr(
        maintenance_module,
        "_enumerate_windows_directory",
        enumerate_and_swap,
    )

    with pytest.raises((MaintenanceError, OSError)):
        maintenance_module._remove_tree_without_reparse(target)

    assert replacement_blocked is True
    assert (moved_original / "original.txt").read_text(encoding="utf-8") == "original"
    assert (replacement / "external.txt").read_text(encoding="utf-8") == "external"


@pytest.mark.skipif(os.name != "nt", reason="Win32 handle identity gate")
def test_handle_delete_rejects_root_replacement_after_preflight(tmp_path: Path) -> None:
    target = tmp_path / "target"
    replacement = tmp_path / "replacement"
    moved_original = tmp_path / "moved-original"
    target.mkdir()
    replacement.mkdir()
    (target / "original.txt").write_text("original", encoding="utf-8")
    (replacement / "external.txt").write_text("external", encoding="utf-8")
    snapshot = maintenance_module._snapshot_windows_tree(target)
    target.rename(moved_original)
    replacement.rename(target)

    with pytest.raises(MaintenanceError, match="root changed"):
        maintenance_module._remove_windows_tree(target, expected_snapshot=snapshot)

    assert (moved_original / "original.txt").read_text(encoding="utf-8") == "original"
    assert (target / "external.txt").read_text(encoding="utf-8") == "external"


@pytest.mark.skipif(os.name != "nt", reason="Win32 path anchor gate")
def test_path_anchors_block_app_root_and_ancestor_replacement(tmp_path: Path) -> None:
    trusted_root = tmp_path / "trusted" / "Amadeus"
    trusted_root.mkdir(parents=True)
    handles, complete = maintenance_module._open_windows_path_anchors(trusted_root)
    assert complete is True
    try:
        with pytest.raises(OSError):
            trusted_root.rename(tmp_path / "moved-amadeus")
        with pytest.raises(OSError):
            trusted_root.parent.rename(tmp_path / "moved-trusted")
    finally:
        for handle in reversed(handles):
            maintenance_module._close_windows_handle(handle)

    trusted_root.rename(tmp_path / "moved-amadeus")
