from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import amadeus_desktop.data_management as data_management
from amadeus_desktop import __version__, app
from amadeus_desktop.conversation_store import ConversationStore
from amadeus_desktop.data_management import (
    RestoreError,
    apply_validated_restore,
    create_backup_archive,
    discard_staged_restore,
    recover_interrupted_restore,
    stage_backup_for_restore,
)
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.paths import AppDirectory, AppPaths
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsRepository


class _SimulatedPowerLoss(BaseException):
    pass


def _settings(scale_percent: int) -> dict[str, object]:
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["pet"]["scale_percent"] = scale_percent
    return settings


def _create_database(path: Path, conversation_id: str) -> None:
    database = SQLiteDatabase(path, backup_dir=path.parent / "migration-backups").open()
    try:
        ConversationStore(database).create_conversation(
            conversation_id,
            conversation_id=conversation_id,
        )
    finally:
        database.close()


def _prepare_restore(tmp_path: Path):
    source_path = tmp_path / "source" / "amadeus.sqlite3"
    _create_database(source_path, "new-state")
    source = SQLiteDatabase(
        source_path,
        backup_dir=source_path.parent / "migration-backups",
    ).open()
    archive = tmp_path / "restore.amadeus-backup"
    try:
        create_backup_archive(
            archive,
            database_backup=source.create_backup,
            settings_snapshot=_settings(125),
            app_version=__version__,
        )
    finally:
        source.close()
    payload = stage_backup_for_restore(archive, tmp_path / "staging")

    paths = AppPaths.for_current_user(tmp_path / "target-user")
    _create_database(paths.database_file, "old-state")
    SettingsRepository(paths.settings_file).save(_settings(90))
    wal = Path(f"{paths.database_file}-wal")
    shm = Path(f"{paths.database_file}-shm")
    wal.write_bytes(b"old-wal-exact")
    shm.write_bytes(b"old-shm-exact")
    originals = {
        paths.database_file: paths.database_file.read_bytes(),
        paths.settings_file: paths.settings_file.read_bytes(),
        wal: wal.read_bytes(),
        shm: shm.read_bytes(),
    }
    return payload, paths, originals


def _assert_old_state(paths: AppPaths, originals: dict[Path, bytes]) -> None:
    assert {path: path.read_bytes() for path in originals} == originals
    settings = json.loads(paths.settings_file.read_text(encoding="utf-8"))
    assert settings["pet"]["scale_percent"] == 90


def _assert_new_state(paths: AppPaths) -> None:
    settings = json.loads(paths.settings_file.read_text(encoding="utf-8"))
    assert settings["pet"]["scale_percent"] == 125
    with SQLiteDatabase(
        paths.database_file,
        backup_dir=paths.migration_backup_directory,
    ).open() as database:
        identifiers = {
            str(row[0])
            for row in database.connection.execute("SELECT id FROM conversations").fetchall()
        }
    assert identifiers == {"new-state"}
    assert not Path(f"{paths.database_file}-wal").exists()
    assert not Path(f"{paths.database_file}-shm").exists()


def _assert_transaction_clean(paths: AppPaths) -> None:
    marker = paths.directory(AppDirectory.BACKUPS) / ".restore-transaction.json"
    assert not marker.exists()
    assert not list(paths.root.rglob("*.rollback"))
    assert not list(paths.root.rglob("*.new"))
    assert not list(paths.root.rglob("*.recover"))


@pytest.mark.parametrize(
    ("checkpoint_name", "committed"),
    (
        ("marker_persisted", False),
        ("database_replaced", False),
        ("commit_persisted", True),
    ),
)
def test_startup_recovery_handles_each_durable_restore_interruption(
    checkpoint_name: str,
    committed: bool,
    tmp_path: Path,
) -> None:
    payload, paths, originals = _prepare_restore(tmp_path)

    def interrupt(name: str) -> None:
        if name == checkpoint_name:
            raise _SimulatedPowerLoss

    try:
        with pytest.raises(_SimulatedPowerLoss):
            apply_validated_restore(payload, paths, checkpoint=interrupt)

        marker = paths.directory(AppDirectory.BACKUPS) / ".restore-transaction.json"
        assert marker.is_file()
        assert recover_interrupted_restore(paths)
        assert not recover_interrupted_restore(paths)
        if committed:
            _assert_new_state(paths)
        else:
            _assert_old_state(paths, originals)
        _assert_transaction_clean(paths)
    finally:
        discard_staged_restore(payload)


def test_normal_restore_commits_without_transaction_artifacts(tmp_path: Path) -> None:
    payload, paths, _originals = _prepare_restore(tmp_path)
    try:
        apply_validated_restore(payload, paths)
        _assert_new_state(paths)
        _assert_transaction_clean(paths)
        assert not recover_interrupted_restore(paths)
    finally:
        discard_staged_restore(payload)


def test_empty_wal_and_shm_round_trip_through_interrupted_restore(tmp_path: Path) -> None:
    payload, paths, originals = _prepare_restore(tmp_path)
    wal = Path(f"{paths.database_file}-wal")
    shm = Path(f"{paths.database_file}-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"")
    originals[wal] = b""
    originals[shm] = b""

    def interrupt(name: str) -> None:
        if name == "database_replaced":
            raise _SimulatedPowerLoss

    try:
        with pytest.raises(_SimulatedPowerLoss):
            apply_validated_restore(payload, paths, checkpoint=interrupt)

        assert recover_interrupted_restore(paths)
        _assert_old_state(paths, originals)
        _assert_transaction_clean(paths)
    finally:
        discard_staged_restore(payload)


def test_zero_metadata_remains_forbidden_for_database_and_settings(
    tmp_path: Path,
) -> None:
    payload, paths, originals = _prepare_restore(tmp_path)

    def interrupt(name: str) -> None:
        if name == "database_replaced":
            raise _SimulatedPowerLoss

    try:
        with pytest.raises(_SimulatedPowerLoss):
            apply_validated_restore(payload, paths, checkpoint=interrupt)
        marker_path = paths.directory(AppDirectory.BACKUPS) / ".restore-transaction.json"
        original_marker = json.loads(marker_path.read_text(encoding="utf-8"))

        for section, key in (
            ("old", "database"),
            ("old", "settings"),
            ("new", "database"),
            ("new", "settings"),
        ):
            invalid_marker = json.loads(json.dumps(original_marker))
            invalid_marker[section][key]["size"] = 0
            marker_path.write_text(json.dumps(invalid_marker), encoding="utf-8")
            with pytest.raises(RestoreError, match="file size is invalid"):
                recover_interrupted_restore(paths)

        marker_path.write_text(json.dumps(original_marker), encoding="utf-8")
        assert recover_interrupted_restore(paths)
        _assert_old_state(paths, originals)
        _assert_transaction_clean(paths)
    finally:
        discard_staged_restore(payload)


def test_restore_preparation_wraps_raw_directory_error_and_keeps_live_data(
    monkeypatch,
    tmp_path: Path,
) -> None:
    payload, paths, originals = _prepare_restore(tmp_path)
    original_mkdir = Path.mkdir

    def fail_settings_parent(path: Path, *args, **kwargs) -> None:
        if path == paths.settings_file.parent:
            raise OSError("synthetic target directory failure")
        original_mkdir(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "mkdir", fail_settings_parent)
            with pytest.raises(RestoreError, match="could not be prepared safely"):
                apply_validated_restore(payload, paths)

        _assert_old_state(paths, originals)
        _assert_transaction_clean(paths)
    finally:
        discard_staged_restore(payload)


def test_startup_recovery_wraps_raw_filesystem_error(monkeypatch, tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(tmp_path)

    def fail_marker_path(_paths: AppPaths, *, create_parent: bool) -> Path:
        del create_parent
        raise OSError("synthetic recovery filesystem failure")

    monkeypatch.setattr(data_management, "_restore_marker_path", fail_marker_path)

    with pytest.raises(RestoreError, match="filesystem is unavailable"):
        recover_interrupted_restore(paths)


def test_startup_recovery_fails_closed_when_marker_cannot_be_probed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    marker = paths.directory(AppDirectory.BACKUPS) / ".restore-transaction.json"
    original_lstat = data_management.os.lstat

    def deny_marker(path: Path | str, *args, **kwargs):
        if Path(path) == marker:
            raise PermissionError("synthetic marker metadata denial")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(data_management.os, "lstat", deny_marker)

    with pytest.raises(RestoreError, match="filesystem is unavailable"):
        recover_interrupted_restore(paths)


def test_restore_snapshot_probe_failure_never_replaces_live_data(
    monkeypatch,
    tmp_path: Path,
) -> None:
    payload, paths, originals = _prepare_restore(tmp_path)
    original_lexists = data_management._lexists

    def deny_database_snapshot(path: Path) -> bool:
        if path == paths.database_file:
            raise PermissionError("synthetic live database metadata denial")
        return original_lexists(path)

    try:
        monkeypatch.setattr(data_management, "_lexists", deny_database_snapshot)
        with pytest.raises(RestoreError, match="previous data was restored"):
            apply_validated_restore(payload, paths)

        _assert_old_state(paths, originals)
        _assert_transaction_clean(paths)
    finally:
        discard_staged_restore(payload)


def test_missing_prepared_rollback_copy_fails_closed_and_keeps_marker(
    tmp_path: Path,
) -> None:
    payload, paths, _originals = _prepare_restore(tmp_path)

    def interrupt(name: str) -> None:
        if name == "database_replaced":
            raise _SimulatedPowerLoss

    try:
        with pytest.raises(_SimulatedPowerLoss):
            apply_validated_restore(payload, paths, checkpoint=interrupt)
        rollback = next(paths.database_file.parent.glob("*.rollback"))
        rollback.unlink()

        with pytest.raises(RestoreError, match="rollback copy"):
            recover_interrupted_restore(paths)

        marker = paths.directory(AppDirectory.BACKUPS) / ".restore-transaction.json"
        assert marker.is_file()
    finally:
        discard_staged_restore(payload)


def test_application_fails_closed_before_settings_when_restore_recovery_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    guard = SimpleNamespace(acquire=lambda: True, close_calls=0)

    def close_guard() -> None:
        guard.close_calls += 1

    guard.close = close_guard

    class FakeApplication:
        def __init__(self, _arguments) -> None:
            pass

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    logger = SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        critical=lambda *_args, **_kwargs: None,
    )
    settings_reads: list[bool] = []

    class ForbiddenSettingsRepository:
        def __init__(self, _path) -> None:
            settings_reads.append(True)

    def fail_recovery(_paths: AppPaths) -> bool:
        raise RestoreError("synthetic interrupted restore damage")

    monkeypatch.setattr(app, "QApplication", FakeApplication)
    monkeypatch.setattr(app, "SingleInstance", lambda _name: guard)
    monkeypatch.setattr(app.AppPaths, "for_current_user", lambda: paths)
    monkeypatch.setattr(app, "configure_logging", lambda _path: logger)
    monkeypatch.setattr(app, "close_logger", lambda _logger: None)
    monkeypatch.setattr(app, "recover_interrupted_restore", fail_recovery)
    monkeypatch.setattr(app, "SettingsRepository", ForbiddenSettingsRepository)
    monkeypatch.setattr(
        app,
        "ApplicationController",
        lambda *_args, **_kwargs: pytest.fail("controller must not be created"),
    )

    assert app.main(("Amadeus",)) == 4
    assert settings_reads == []
    assert guard.close_calls == 1
