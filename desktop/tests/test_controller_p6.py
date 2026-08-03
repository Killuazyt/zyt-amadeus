from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox, QSystemTrayIcon

import amadeus_desktop.controller as controller_module
from amadeus_desktop.autostart import AutostartError
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.credential_store import InMemoryCredentialStore
from amadeus_desktop.data_management import (
    BackupMetadata,
    RestoreCleanupError,
    RestoreError,
    RestoreRollbackError,
    ValidatedRestorePayload,
)
from amadeus_desktop.paths import AppDirectory, AppPaths
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsError, SettingsRepository


class _InstanceGuard(QObject):
    activation_requested = Signal()

    def close(self) -> None:
        return


class _Autostart:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.fail_next = False
        self.calls: list[bool] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def set_enabled(self, enabled: bool) -> bool:
        self.calls.append(enabled)
        if self.fail_next:
            self.fail_next = False
            raise AutostartError("synthetic")
        self.enabled = enabled
        return enabled


def _controller(qapp, tmp_path, *, autostart: _Autostart) -> ApplicationController:
    paths = AppPaths.for_current_user(tmp_path)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["proactive"]["mode"] = "off"
    repository.save(settings)
    logger = logging.getLogger(f"amadeus.test.p6.{tmp_path.name}")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return ApplicationController(
        qapp,
        _InstanceGuard(),  # type: ignore[arg-type]
        logger,
        paths=paths,
        settings_repository=repository,
        settings=settings,
        tray_available=True,
        mock_chat=True,
        credential_store=InMemoryCredentialStore(),
        background_jobs_enabled=False,
        autostart_manager=autostart,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 8, 3, 12),
        proactive_startup_delay_ms=60_000,
    )


def _validated_restore_payload(tmp_path) -> ValidatedRestorePayload:
    staging_root = tmp_path / ".amadeus-restore-test"
    return ValidatedRestorePayload(
        archive_path=tmp_path / "backup.amadeus-backup",
        staging_root=staging_root,
        database_path=staging_root / "data" / "amadeus.sqlite3",
        settings_path=staging_root / "config" / "settings.json",
        metadata=BackupMetadata(
            created_at="2026-08-03T04:05:06Z",
            app_version="0.6.0.dev6",
            database_schema=3,
            settings_schema=5,
            database_size=1,
            database_sha256="0" * 64,
            settings_size=1,
            settings_sha256="0" * 64,
        ),
        staged_database_sha256="0" * 64,
        staged_settings_sha256="0" * 64,
    )


def test_hot_settings_persist_and_sync_three_topmost_windows_and_tray(
    qapp, qtbot, tmp_path
) -> None:
    controller = _controller(qapp, tmp_path, autostart=_Autostart())
    try:
        controller.general_page.always_on_top.setChecked(False)

        assert controller.pet_window.always_on_top is False
        assert controller.chat_panel.always_on_top is False
        assert controller.greeting_bubble.always_on_top is False
        assert controller.tray is not None
        assert controller.tray.always_on_top_action.isChecked() is False
        assert not (controller.settings_window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        persisted = controller.settings_repository.load()
        assert persisted["general"]["always_on_top"] is False

        controller.pet_page.scale.setValue(125)
        controller.pet_page.speed.setValue(150)
        assert controller.pet_window.scale_percent == 125
        assert controller.pet_window.animation.speed_percent == 150
        persisted = controller.settings_repository.load()
        assert persisted["pet"]["scale_percent"] == 125
        assert persisted["pet"]["animation_speed_percent"] == 150
    finally:
        controller._cleanup()


def test_pet_position_save_failure_restores_window_and_never_pollutes_later_save(
    qapp,
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    controller = _controller(qapp, tmp_path, autostart=_Autostart())
    qtbot.waitUntil(lambda: controller._data_initialized, timeout=5_000)
    original_document = deepcopy(controller.settings["pet"]["position"])
    original_window_position = controller.pet_window.pos()
    available = qapp.primaryScreen().availableGeometry()
    dragged_position = available.topLeft() + QPoint(12, 12)
    assert dragged_position != original_window_position

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                controller.settings_repository,
                "save",
                lambda _document: (_ for _ in ()).throw(SettingsError("synthetic write failure")),
            )
            controller.pet_window.move(dragged_position)

            controller._on_pet_drag_finished()

        assert controller.settings["pet"]["position"] == original_document
        assert controller.pet_window.pos() == original_window_position
        assert controller.settings_repository.load()["pet"]["position"] == original_document

        controller._set_animation_speed(175)

        persisted = controller.settings_repository.load()
        assert persisted["pet"]["position"] == original_document
        assert persisted["pet"]["animation_speed_percent"] == 175
    finally:
        controller._cleanup()


def test_startup_autostart_reconcile_save_failure_uses_hkcu_for_memory_page_and_tray(
    qapp,
    monkeypatch,
    tmp_path,
) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["proactive"]["mode"] = "off"
    repository.save(settings)
    autostart = _Autostart(enabled=True)
    logger = logging.getLogger(f"amadeus.test.p6.autostart-reconcile.{tmp_path.name}")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    controller: ApplicationController | None = None

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                repository,
                "save",
                lambda _document: (_ for _ in ()).throw(
                    SettingsError("synthetic raw storage detail")
                ),
            )
            controller = ApplicationController(
                qapp,
                _InstanceGuard(),  # type: ignore[arg-type]
                logger,
                paths=paths,
                settings_repository=repository,
                settings=settings,
                tray_available=True,
                mock_chat=True,
                credential_store=InMemoryCredentialStore(),
                background_jobs_enabled=False,
                autostart_manager=autostart,  # type: ignore[arg-type]
                clock=lambda: datetime(2026, 8, 3, 12),
                proactive_startup_delay_ms=60_000,
            )

            assert controller.settings["general"]["launch_at_login"] is True
            assert controller.general_page.launch_at_login.isChecked()
            assert controller.tray is not None
            assert controller.tray.launch_at_login_action.isChecked()
            tooltip = controller.general_page.launch_at_login.toolTip()
            assert tooltip == "Windows 开机启动状态已同步，但设置文件未能保存。"
            assert "synthetic" not in tooltip
            assert controller._last_safe_error_category == "storage_error"
            assert (
                SettingsRepository(paths.settings_file).load()["general"]["launch_at_login"]
                is False
            )

        controller._set_animation_speed(175)
        persisted = SettingsRepository(paths.settings_file).load()
        assert persisted["general"]["launch_at_login"] is True
        assert controller.general_page.launch_at_login.toolTip() == ""
    finally:
        if controller is not None:
            controller._cleanup()


def test_autostart_failure_rolls_back_settings_page_and_tray(qapp, tmp_path) -> None:
    autostart = _Autostart()
    controller = _controller(qapp, tmp_path, autostart=autostart)
    try:
        autostart.fail_next = True
        controller.general_page.launch_at_login.setChecked(True)

        assert controller.settings["general"]["launch_at_login"] is False
        assert controller.general_page.launch_at_login.isChecked() is False
        assert controller.tray is not None
        assert controller.tray.launch_at_login_action.isChecked() is False
        assert controller.settings_repository.load()["general"]["launch_at_login"] is False
    finally:
        controller._cleanup()


def test_autostart_double_failure_tracks_actual_state_until_next_settings_save(
    qapp,
    monkeypatch,
    tmp_path,
) -> None:
    class RollbackFailingAutostart(_Autostart):
        def set_enabled(self, enabled: bool) -> bool:
            self.calls.append(enabled)
            if enabled:
                self.enabled = True
                return True
            raise AutostartError("synthetic rollback failure")

    autostart = RollbackFailingAutostart()
    controller = _controller(qapp, tmp_path, autostart=autostart)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                controller.settings_repository,
                "save",
                lambda _document: (_ for _ in ()).throw(
                    SettingsError("synthetic settings failure")
                ),
            )
            controller._set_launch_at_login(True)

        assert autostart.calls == [True, False]
        assert autostart.enabled is True
        assert controller.settings["general"]["launch_at_login"] is True
        assert controller.general_page.launch_at_login.isChecked()
        assert controller.tray is not None
        assert controller.tray.launch_at_login_action.isChecked()
        assert (
            controller.general_page.launch_at_login.toolTip()
            == "Windows 开机启动状态已同步，但设置文件未能保存。"
        )
        assert (
            SettingsRepository(controller.paths.settings_file).load()["general"]["launch_at_login"]
            is False
        )

        controller._set_animation_speed(175)

        persisted = SettingsRepository(controller.paths.settings_file).load()
        assert persisted["general"]["launch_at_login"] is True
        assert persisted["pet"]["animation_speed_percent"] == 175
        assert controller.general_page.launch_at_login.toolTip() == ""
        assert controller._autostart_reconcile_error is None
    finally:
        controller._cleanup()


def test_tray_pause_and_double_click_open_chat_are_bidirectionally_synced(
    qapp, qtbot, tmp_path
) -> None:
    controller = _controller(qapp, tmp_path, autostart=_Autostart())
    try:
        assert controller.tray is not None
        controller.tray.pause_proactive_today_action.setChecked(True)
        assert controller.settings["proactive"]["paused_local_date"] == "2026-08-03"
        assert controller.proactive_page.pause_today.isChecked()

        controller.chat_panel.hide()
        controller.tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
        qtbot.waitUntil(controller.chat_panel.isVisible)
        assert controller.pet_window.isVisible()

        controller.proactive_page.pause_today.setChecked(False)
        assert controller.settings["proactive"]["paused_local_date"] is None
        assert controller.tray.pause_proactive_today_action.isChecked() is False
    finally:
        controller._cleanup()


@pytest.mark.parametrize("active_state", ("restore_validation", "restore_prebackup"))
def test_restore_states_exclude_second_restore_factory_reset_and_backup(
    active_state,
    qapp,
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    controller = _controller(qapp, tmp_path, autostart=_Autostart())
    qtbot.waitUntil(lambda: controller._data_initialized, timeout=5_000)
    controller._start_data_operation(active_state)
    forbidden_calls: list[str] = []
    busy_messages: list[str] = []

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                QFileDialog,
                "getOpenFileName",
                lambda *_args, **_kwargs: forbidden_calls.append("restore-dialog") or ("", ""),
            )
            patch.setattr(
                QFileDialog,
                "getSaveFileName",
                lambda *_args, **_kwargs: forbidden_calls.append("backup-dialog") or ("", ""),
            )
            patch.setattr(
                QMessageBox,
                "warning",
                lambda *_args, **_kwargs: (
                    forbidden_calls.append("factory-reset-confirmation")
                    or QMessageBox.StandardButton.No
                ),
            )
            patch.setattr(
                QMessageBox,
                "information",
                lambda _parent, _title, message, *_args, **_kwargs: (
                    busy_messages.append(message) or QMessageBox.StandardButton.Ok
                ),
            )
            patch.setattr(
                controller.data_runtime,
                "submit",
                lambda *_args, **_kwargs: forbidden_calls.append("data-submit"),
            )

            controller._request_restore()
            controller._request_factory_reset()
            controller._request_backup()

        assert forbidden_calls == []
        assert len(busy_messages) == 2
        assert controller._data_change_state == active_state
        assert not controller.general_page.backup_button.isEnabled()
        assert not controller.general_page.restore_button.isEnabled()
        assert not controller.general_page.factory_reset_button.isEnabled()
    finally:
        controller._finish_data_operation()
        controller._cleanup()


@pytest.mark.parametrize("failing_boundary", ("proactive", "memory"))
def test_restore_pause_failure_never_starts_prebackup_or_replacement_and_resumes_services(
    failing_boundary,
    qapp,
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    controller = _controller(qapp, tmp_path, autostart=_Autostart())
    qtbot.waitUntil(lambda: controller._data_initialized, timeout=5_000)
    assert controller._data_writable
    payload = _validated_restore_payload(tmp_path)
    proactive_starts: list[bool] = []
    memory_resumes: list[bool] = []
    submissions: list[object] = []
    discarded: list[ValidatedRestorePayload] = []

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                QMessageBox,
                "question",
                lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
            )
            patch.setattr(
                QMessageBox,
                "warning",
                lambda *_args, **_kwargs: QMessageBox.StandardButton.Ok,
            )
            patch.setattr(
                controller.proactive_interactions,
                "stop",
                lambda **_kwargs: failing_boundary != "proactive",
            )
            patch.setattr(
                controller.memory_jobs,
                "pause",
                lambda **_kwargs: failing_boundary != "memory",
            )
            patch.setattr(
                controller.proactive_interactions,
                "start",
                lambda: proactive_starts.append(True),
            )
            patch.setattr(
                controller.memory_jobs,
                "resume",
                lambda: memory_resumes.append(True),
            )
            patch.setattr(
                controller.data_runtime,
                "submit",
                lambda *args, **kwargs: submissions.append((args, kwargs)),
            )
            patch.setattr(
                controller_module,
                "apply_validated_restore",
                lambda *_args, **_kwargs: pytest.fail("restore replacement must not start"),
            )
            patch.setattr(
                controller_module,
                "discard_staged_restore",
                lambda candidate: discarded.append(candidate),
            )
            controller._start_data_operation("restore_validation")

            controller._on_restore_validated(payload)

        assert submissions == []
        assert discarded == [payload]
        assert proactive_starts
        assert memory_resumes
        assert controller._staged_restore is None
        assert controller._data_change_state == "idle"
        assert controller._conversation_switch_pending is False
        assert controller.general_page.backup_button.isEnabled()
        assert controller.general_page.restore_button.isEnabled()
        assert controller.general_page.factory_reset_button.isEnabled()
    finally:
        controller._cleanup()


@pytest.mark.parametrize(
    ("outcome", "expected_title", "expected_message_fragment"),
    (
        ("success", "恢复完成", "本地数据已恢复"),
        ("cleanup", "恢复完成（有清理警告）", "临时文件未能清理"),
        ("rolled-back", "恢复失败", "原数据库与设置已回滚"),
        ("rollback-uncertain", "恢复与回滚未完成", "恢复前备份已保留"),
    ),
)
def test_restore_apply_reports_distinct_terminal_outcomes(
    outcome,
    expected_title,
    expected_message_fragment,
    qapp,
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    controller = _controller(qapp, tmp_path, autostart=_Autostart())
    qtbot.waitUntil(lambda: controller._data_initialized, timeout=5_000)
    payload = _validated_restore_payload(tmp_path)
    messages: list[tuple[str, str]] = []
    exits: list[bool] = []

    def apply_outcome(*_args, **_kwargs) -> None:
        if outcome == "cleanup":
            raise RestoreCleanupError("synthetic cleanup failure")
        if outcome == "rolled-back":
            raise RestoreError("synthetic replacement failure with exact rollback")
        if outcome == "rollback-uncertain":
            raise RestoreRollbackError("synthetic rollback failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(controller, "_shutdown_background_tasks", lambda **_kwargs: True)
            patch.setattr(controller, "_exit_after_data_change", lambda: exits.append(True))
            patch.setattr(controller_module, "apply_validated_restore", apply_outcome)
            patch.setattr(controller_module, "discard_staged_restore", lambda _payload: None)
            patch.setattr(
                QMessageBox,
                "information",
                lambda _parent, title, message, *_args, **_kwargs: (
                    messages.append((title, message)) or QMessageBox.StandardButton.Ok
                ),
            )
            controller._staged_restore = payload
            controller._data_change_state = "restore_prebackup"

            controller._apply_staged_restore()

        assert exits == [True]
        assert len(messages) == 1
        assert messages[0][0] == expected_title
        assert expected_message_fragment in messages[0][1]
        assert controller._staged_restore is None
    finally:
        controller._cleanup()


def test_factory_reset_shutdown_failure_performs_no_destructive_action(
    qapp,
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    autostart = _Autostart(enabled=True)
    controller = _controller(qapp, tmp_path, autostart=autostart)
    qtbot.waitUntil(lambda: controller._data_initialized, timeout=5_000)
    controller.credential_store.write_secret("invalid-test-credential")
    markers = {}
    for region in AppDirectory:
        marker = controller.paths.directory(region) / f"{region.value}-must-survive.bin"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(region.value.encode("ascii"))
        markers[marker] = marker.read_bytes()
    external_export = tmp_path / "external-chat-export.json"
    external_export.write_bytes(b"external-export-must-survive")
    autostart_calls_before = tuple(autostart.calls)
    exits: list[bool] = []
    deletions: list[object] = []
    logging_shutdowns: list[bool] = []

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                QMessageBox,
                "warning",
                lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
            )
            patch.setattr(controller, "_shutdown_background_tasks", lambda **_kwargs: False)
            patch.setattr(controller, "_exit_after_data_change", lambda: exits.append(True))
            patch.setattr(
                controller_module.shutil,
                "rmtree",
                lambda target: deletions.append(target),
            )
            patch.setattr(
                controller_module.logging,
                "shutdown",
                lambda: logging_shutdowns.append(True),
            )

            controller._request_factory_reset()

        assert exits == [True]
        assert deletions == []
        assert logging_shutdowns == []
        assert tuple(autostart.calls) == autostart_calls_before
        assert autostart.enabled is True
        assert controller.credential_store.has_secret()
        assert {path: path.read_bytes() for path in markers} == markers
        assert external_export.read_bytes() == b"external-export-must-survive"
    finally:
        controller._cleanup()


def test_factory_reset_removes_only_isolated_amadeus_data_and_external_export_survives(
    qapp,
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    autostart = _Autostart(enabled=True)
    controller = _controller(qapp, tmp_path, autostart=autostart)
    qtbot.waitUntil(lambda: controller._data_initialized, timeout=5_000)
    controller.credential_store.write_secret("invalid-test-credential")
    reset_targets = tuple(controller.paths.directory(region) for region in AppDirectory)
    for region, target in zip(AppDirectory, reset_targets, strict=True):
        marker = target / f"{region.value}-private.bin"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(region.value.encode("ascii"))
    external_export = tmp_path / "external-memory-export.json"
    external_export.write_bytes(b"external-export-must-survive")
    exits: list[bool] = []
    information_messages: list[str] = []

    def finish_without_quitting_qapp() -> None:
        exits.append(True)
        controller._exiting = True
        controller.window.hide()
        controller.chat_panel.hide()
        controller.settings_window.hide()
        controller.greeting_bubble.hide()
        controller.pet_window.hide()
        if controller.tray is not None:
            controller.tray.close()
        controller.instance_guard.close()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                QMessageBox,
                "warning",
                lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
            )
            patch.setattr(
                QMessageBox,
                "information",
                lambda _parent, _title, message, *_args, **_kwargs: (
                    information_messages.append(message) or QMessageBox.StandardButton.Ok
                ),
            )
            patch.setattr(controller, "_exit_after_data_change", finish_without_quitting_qapp)
            patch.setattr(controller_module.logging, "shutdown", lambda: None)

            controller._request_factory_reset()

        assert exits == [True]
        assert information_messages == ["本地数据已清除，Amadeus 将退出。"]
        assert autostart.calls[-1] is False
        assert autostart.enabled is False
        assert not controller.credential_store.has_secret()
        assert controller.paths.root.is_dir()
        assert all(not target.exists() for target in reset_targets)
        assert external_export.read_bytes() == b"external-export-must-survive"
    finally:
        if not controller._exiting:
            controller._cleanup()
