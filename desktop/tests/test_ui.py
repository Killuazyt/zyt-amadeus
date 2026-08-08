from __future__ import annotations

import asyncio
import logging
import threading
import time
from copy import deepcopy
from dataclasses import replace

import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from amadeus_desktop.chat_models import ChatRequest, PromptMessage, PromptRole
from amadeus_desktop.chat_provider import (
    CancellationRequested,
    CancellationToken,
    ScriptedChatProvider,
    ScriptedScenario,
)
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.credential_store import CredentialStoreError, InMemoryCredentialStore
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.provider_config import ProviderConfig, ProviderPreset
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsError, SettingsRepository
from amadeus_desktop.ui.control_window import ControlWindow
from amadeus_desktop.ui.tray import TrayController, create_app_icon


class FakeInstanceGuard(QObject):
    activation_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSystemTrayIcon(QObject):
    """Headless tray double; Qt's offscreen plugin cannot own a native tray safely."""

    activated = Signal(object)

    def __init__(self, icon: QIcon, parent: QObject) -> None:
        super().__init__(parent)
        del icon
        self._visible = False
        self._menu: QMenu | None = None

    def setToolTip(self, _tooltip: str) -> None:
        pass

    def setContextMenu(self, menu: QMenu | None) -> None:
        self._menu = menu

    def show(self) -> None:
        self._visible = True

    def hide(self) -> None:
        self._visible = False

    def isVisible(self) -> bool:
        return self._visible


class RecordHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class CountingCredentialStore(InMemoryCredentialStore):
    def __init__(self, initial_secret: str | None = None) -> None:
        super().__init__(initial_secret)
        self.has_calls = 0
        self.read_calls = 0

    def has_secret(self) -> bool:
        self.has_calls += 1
        return super().has_secret()

    def read_secret(self) -> str | None:
        self.read_calls += 1
        return super().read_secret()


def make_logger() -> logging.Logger:
    logger = logging.getLogger("amadeus.test.ui")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def make_controller(
    qapp,
    tmp_path,
    *,
    tray_available: bool,
    mock_chat: bool = False,
    allow_saved_provider: bool = True,
    credential_store: InMemoryCredentialStore | None = None,
    chat_provider: object | None = None,
    connection_tester: object | None = None,
) -> ApplicationController:
    paths = AppPaths.for_current_user(tmp_path)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = deepcopy(DEFAULT_SETTINGS)
    repository.save(settings)
    return ApplicationController(
        qapp,
        FakeInstanceGuard(),  # type: ignore[arg-type]
        make_logger(),
        paths=paths,
        settings_repository=repository,
        settings=settings,
        tray_available=tray_available,
        mock_chat=mock_chat,
        allow_saved_provider=allow_saved_provider,
        credential_store=credential_store or InMemoryCredentialStore(),
        chat_provider=chat_provider,  # type: ignore[arg-type]
        connection_tester=connection_tester,  # type: ignore[arg-type]
    )


class BlockingConnectionTester:
    async def test(self, config, secret, cancellation: CancellationToken):
        del config, secret
        cancellation.bind_current_task()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as exc:
            raise CancellationRequested from exc
        finally:
            cancellation.unbind_current_task()


class SlowCancellationProvider:
    def __init__(self, delay_seconds: float = 0.25) -> None:
        self.delay_seconds = delay_seconds
        self.started = threading.Event()

    async def stream(self, _request, _cancellation):
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Model a provider that needs bounded cleanup after cancellation.
            await asyncio.sleep(self.delay_seconds)
        yield "late synthetic output"


def test_pinned_application_icon_loads(qapp) -> None:
    assert create_app_icon().isNull() is False


def test_control_window_close_hides_when_tray_exists(qtbot) -> None:
    window = ControlWindow(tray_available=True)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible)

    window.close()

    qtbot.waitUntil(lambda: not window.isVisible())


def test_control_window_close_requests_exit_without_tray(qtbot) -> None:
    window = ControlWindow(tray_available=False)
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.exit_requested, timeout=1000):
        window.close()


def test_tray_double_click_requests_open_chat_and_legacy_show(qtbot) -> None:
    tray = TrayController(system_tray_factory=FakeSystemTrayIcon)  # type: ignore[arg-type]
    try:
        opened: list[bool] = []
        shown: list[bool] = []
        tray.open_chat_requested.connect(lambda: opened.append(True))
        tray.show_requested.connect(lambda: shown.append(True))
        with qtbot.waitSignal(tray.open_chat_requested, timeout=1000):
            tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
        assert opened == [True]
        assert shown == [True]
    finally:
        tray.close()


def test_tray_has_exact_p6_action_order_and_state_sync_is_signal_safe(qtbot) -> None:
    tray = TrayController(system_tray_factory=FakeSystemTrayIcon)  # type: ignore[arg-type]
    try:
        assert [action.text() for action in tray.actions] == [
            "隐藏宠物",
            "打开对话",
            "记忆管理…",
            "设置…",
            "始终置顶",
            "今天暂停主动互动",
            "开机启动",
            "退出",
        ]
        assert all(not action.isSeparator() for action in tray.actions)
        assert [action.isCheckable() for action in tray.actions] == [
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
        ]

        topmost: list[bool] = []
        paused: list[bool] = []
        autostart: list[bool] = []
        tray.always_on_top_changed.connect(topmost.append)
        tray.pause_proactive_today_changed.connect(paused.append)
        tray.launch_at_login_changed.connect(autostart.append)
        tray.apply_state(
            pet_visible=False,
            always_on_top=True,
            proactive_paused_today=True,
            launch_at_login=True,
        )
        assert tray.toggle_action.text() == "显示宠物"
        assert tray.always_on_top_action.isChecked()
        assert tray.pause_proactive_today_action.isChecked()
        assert tray.launch_at_login_action.isChecked()
        assert topmost == [] and paused == [] and autostart == []

        tray.always_on_top_action.trigger()
        tray.pause_proactive_today_action.trigger()
        tray.launch_at_login_action.trigger()
        assert topmost == [False]
        assert paused == [False]
        assert autostart == [False]
    finally:
        tray.close()


def test_model_settings_entry_points_emit(qtbot) -> None:
    window = ControlWindow(tray_available=False)
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.model_settings_requested, timeout=1000):
        window.model_settings_button.click()

    tray = TrayController(system_tray_factory=FakeSystemTrayIcon)  # type: ignore[arg-type]
    try:
        with qtbot.waitSignal(tray.model_settings_requested, timeout=1000):
            tray.model_settings_action.trigger()
    finally:
        tray.close()


def test_production_without_credential_is_unconfigured_and_never_invokes_mock(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=False)
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    try:
        assert controller.chat_panel.provider_mode == "unconfigured"
        assert not controller.chat_panel.input.isEnabled()

        controller._send_chat_message("生产模式不得模拟回答")

        assert controller.conversation.turns == ()
        assert "请先配置" in controller.chat_panel.status_label.text()

        first_window = controller.model_settings_window
        controller.show_model_settings()
        controller.show_model_settings()
        assert controller.model_settings_window is first_window
    finally:
        controller.request_exit()


def test_mock_chat_requires_explicit_runtime_flag(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        mock_chat=True,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    try:
        assert controller.chat_panel.provider_mode == "mock"
        controller._send_chat_message("显式模拟")
        qtbot.waitUntil(lambda: len(controller.conversation.turns) == 1)
    finally:
        controller.request_exit()


def test_untrusted_settings_never_auto_enable_an_existing_credential(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    store = InMemoryCredentialStore("invalid-existing-key")
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        allow_saved_provider=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    try:
        assert controller.chat_panel.provider_mode == "unconfigured"
        controller._send_chat_message("不得自动发送")
        assert controller.conversation.turns == ()
    finally:
        controller.request_exit()


def test_disabled_marker_blocks_existing_credential_on_restart(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    store = CountingCredentialStore("invalid-existing-key")
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    try:
        assert controller.settings["provider_enabled"] is False
        assert controller.chat_panel.provider_mode == "unconfigured"
        controller._send_chat_message("禁用状态不得发送")
        assert controller.conversation.turns == ()
        controller.model_settings_window.test_button.click()
        assert not controller.model_settings_window.test_running
        assert "输入对应的新 API 密钥" in controller.model_settings_window.status_label.text()
        assert store.has_calls == 0
        assert store.read_calls == 0
    finally:
        controller.request_exit()


def test_provider_save_failure_restores_previous_wincred_and_json(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=False)
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    previous_settings = deepcopy(controller.settings)
    controller.credential_store.write_secret("old-invalid-test-key")
    original_save = controller.settings_repository.save
    save_calls = 0

    def fail_once(settings) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            raise SettingsError("simulated atomic save failure")
        original_save(settings)

    monkeypatch.setattr(controller.settings_repository, "save", fail_once)
    try:
        controller._save_provider_configuration(
            controller.provider_config,
            "new-invalid-test-key",
        )

        assert controller.credential_store.read_secret() == "old-invalid-test-key"
        assert controller.settings_repository.load() == previous_settings
        assert controller.settings == previous_settings
        assert "原配置已恢复" in controller.model_settings_window.status_label.text()
    finally:
        controller.request_exit()


def test_successful_provider_transaction_enables_real_provider(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    store = InMemoryCredentialStore()
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    try:
        controller._save_provider_configuration(
            controller.provider_config,
            "new-invalid-test-key",
        )

        persisted = controller.settings_repository.load()
        assert persisted["provider_enabled"] is True
        assert store.read_secret() == "new-invalid-test-key"
        assert controller.chat_panel.provider_mode == "provider"
        assert not controller.memory_jobs.is_paused
        assert not controller.background_generation.is_paused
    finally:
        controller.request_exit()


def test_provider_switch_waits_for_slow_background_cancel_without_blocking_qt(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    store = InMemoryCredentialStore()
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        credential_store=store,
    )
    slow_provider = SlowCancellationProvider()
    heartbeats = 0
    timer = QTimer()
    timer.setInterval(10)

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    timer.timeout.connect(heartbeat)
    request = ChatRequest(
        request_id="background-switch-test",
        turn_id="background-switch-test",
        attempt=1,
        messages=(PromptMessage(PromptRole.USER, "synthetic"),),
    )
    try:
        assert controller.background_generation.set_provider(slow_provider)
        assert controller.background_generation.start(
            request,
            on_success=lambda _content: None,
            on_failure=lambda _category: None,
        )
        qtbot.waitUntil(slow_provider.started.is_set, timeout=1_000)
        timer.start()

        started = time.perf_counter()
        controller._save_provider_configuration(
            controller.provider_config,
            "new-invalid-test-key",
        )
        elapsed = time.perf_counter() - started

        assert elapsed < 0.1
        assert controller._provider_switch_pending
        qtbot.waitUntil(lambda: not controller._provider_switch_pending, timeout=2_000)
        assert heartbeats >= 5
        assert controller.settings["provider_enabled"] is True
        assert store.read_secret() == "new-invalid-test-key"
        assert not controller.background_generation.is_paused
        assert not controller.memory_jobs.is_paused
    finally:
        timer.stop()
        controller.request_exit()


def test_controller_refuses_saved_secret_reuse_across_provider_scope(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    store = CountingCredentialStore("old-invalid-test-key")
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    controller.settings["provider_enabled"] = True
    controller.settings_repository.save(controller.settings)
    previous_settings = deepcopy(controller.settings)
    try:
        controller._save_provider_configuration(
            ProviderConfig.for_preset(ProviderPreset.MIMO_PAYG),
            None,
        )

        assert store.read_secret() == "old-invalid-test-key"
        assert controller.settings_repository.load() == previous_settings
        assert "输入对应的新 API 密钥" in controller.model_settings_window.status_label.text()
    finally:
        controller.request_exit()


def test_crash_after_credential_replace_restarts_from_disabled_marker(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["provider_enabled"] = True
    repository.save(settings)
    store = InMemoryCredentialStore("old-invalid-test-key")
    controller = ApplicationController(
        qapp,
        FakeInstanceGuard(),  # type: ignore[arg-type]
        make_logger(),
        paths=paths,
        settings_repository=repository,
        settings=settings,
        tray_available=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    original_write = store.write_secret
    assert controller.chat_panel.provider_mode == "provider"

    def replace_then_crash(secret: str) -> None:
        original_write(secret)
        raise SystemExit("simulated process crash")

    monkeypatch.setattr(store, "write_secret", replace_then_crash)
    try:
        with pytest.raises(SystemExit, match="simulated process crash"):
            controller._save_provider_configuration(
                ProviderConfig.for_preset(ProviderPreset.MIMO_PAYG),
                "new-invalid-test-key",
            )

        persisted = controller.settings_repository.load()
        assert persisted["provider_enabled"] is False
        assert persisted["provider"]["preset"] == ProviderPreset.DEEPSEEK_PAYG.value
        assert store.read_secret() == "new-invalid-test-key"
    finally:
        controller.request_exit()


def test_crash_between_credential_and_settings_rollback_stays_disabled(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["provider_enabled"] = True
    repository.save(settings)
    store = InMemoryCredentialStore("old-invalid-test-key")
    controller = ApplicationController(
        qapp,
        FakeInstanceGuard(),  # type: ignore[arg-type]
        make_logger(),
        paths=paths,
        settings_repository=repository,
        settings=settings,
        tray_available=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    original_set_provider = controller.conversation.set_provider
    switch_calls = 0

    def fail_candidate_switch(provider) -> bool:
        nonlocal switch_calls
        switch_calls += 1
        if switch_calls == 2:
            return False
        return original_set_provider(provider)

    def crash_before_settings_restore(_snapshot) -> None:
        assert store.read_secret() == "old-invalid-test-key"
        raise SystemExit("simulated rollback crash")

    monkeypatch.setattr(controller.conversation, "set_provider", fail_candidate_switch)
    monkeypatch.setattr(repository, "restore_snapshot", crash_before_settings_restore)
    try:
        with pytest.raises(SystemExit, match="simulated rollback crash"):
            controller._save_provider_configuration(
                ProviderConfig.for_preset(ProviderPreset.MIMO_PAYG),
                "new-invalid-test-key",
            )

        persisted = repository.load()
        assert persisted["provider_enabled"] is False
        assert persisted["provider"]["preset"] == ProviderPreset.DEEPSEEK_PAYG.value
        assert store.read_secret() == "old-invalid-test-key"
    finally:
        controller.request_exit()


def test_candidate_save_failure_preserves_untrusted_settings_bytes(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    original_bytes = b'{"schema_version":99,"private_future_field":true}'
    repository.path.write_bytes(original_bytes)
    store = InMemoryCredentialStore("old-invalid-test-key")
    controller = ApplicationController(
        qapp,
        FakeInstanceGuard(),  # type: ignore[arg-type]
        make_logger(),
        paths=paths,
        settings_repository=repository,
        settings=deepcopy(DEFAULT_SETTINGS),
        tray_available=False,
        allow_saved_provider=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    monkeypatch.setattr(
        repository,
        "save",
        lambda _settings: (_ for _ in ()).throw(SettingsError("invalid-test-save")),
    )
    try:
        controller._save_provider_configuration(
            controller.provider_config,
            "new-invalid-test-key",
        )

        assert repository.path.read_bytes() == original_bytes
        assert store.read_secret() == "old-invalid-test-key"
        assert controller.chat_panel.provider_mode == "unconfigured"
    finally:
        controller.request_exit()


def test_credential_write_failure_keeps_previous_configuration(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    store = InMemoryCredentialStore("old-invalid-test-key")
    original_write = store.write_secret

    def fail_new(secret: str) -> None:
        if secret == "new-invalid-test-key":
            raise CredentialStoreError("invalid-test-write")
        original_write(secret)

    monkeypatch.setattr(store, "write_secret", fail_new)
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    previous_settings = deepcopy(controller.settings)
    try:
        controller._save_provider_configuration(
            controller.provider_config,
            "new-invalid-test-key",
        )

        assert store.read_secret() == "old-invalid-test-key"
        assert controller.settings_repository.load() == previous_settings
        assert "原配置已恢复" in controller.model_settings_window.status_label.text()
    finally:
        controller.request_exit()


def test_rollback_failure_persists_disabled_marker_and_blocks_restart(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    store = InMemoryCredentialStore("old-invalid-test-key")
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    original_set_provider = controller.conversation.set_provider
    switch_calls = 0

    def fail_candidate_once(provider) -> bool:
        nonlocal switch_calls
        switch_calls += 1
        if switch_calls == 2:
            return False
        return original_set_provider(provider)

    monkeypatch.setattr(controller.conversation, "set_provider", fail_candidate_once)
    monkeypatch.setattr(
        controller.settings_repository,
        "restore_snapshot",
        lambda _snapshot: (_ for _ in ()).throw(SettingsError("invalid-test-rollback")),
    )
    candidate = replace(controller.provider_config, model="deepseek-v4-pro")
    try:
        controller._save_provider_configuration(candidate, "new-invalid-test-key")

        persisted = controller.settings_repository.load()
        assert persisted["provider"]["model"] == controller.provider_config.model
        assert persisted["provider_enabled"] is False
        assert store.read_secret() == "old-invalid-test-key"
        assert controller.chat_panel.provider_mode == "unconfigured"
        controller._send_chat_message("禁用标记后不得发送")
        assert controller.conversation.turns == ()
    finally:
        controller.request_exit()


def test_credential_restore_failure_persists_disabled_marker(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    store = CountingCredentialStore("old-invalid-test-key")
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    original_write = store.write_secret
    candidate_written = False

    def fail_restore(secret: str) -> None:
        nonlocal candidate_written
        if secret == "new-invalid-test-key":
            candidate_written = True
            original_write(secret)
            return
        if secret == "old-invalid-test-key" and candidate_written:
            raise CredentialStoreError("invalid-test-restore")
        original_write(secret)

    original_set_provider = controller.conversation.set_provider
    switch_calls = 0

    def fail_candidate_once(provider) -> bool:
        nonlocal switch_calls
        switch_calls += 1
        if switch_calls == 2:
            return False
        return original_set_provider(provider)

    monkeypatch.setattr(store, "write_secret", fail_restore)
    monkeypatch.setattr(controller.conversation, "set_provider", fail_candidate_once)
    try:
        controller._save_provider_configuration(
            replace(controller.provider_config, model="deepseek-v4-pro"),
            "new-invalid-test-key",
        )

        persisted = controller.settings_repository.load()
        assert persisted["provider"]["model"] == controller.provider_config.model
        assert persisted["provider_enabled"] is False
        assert store.read_secret() == "new-invalid-test-key"
        assert controller.chat_panel.provider_mode == "unconfigured"
        reads_before = store.read_calls
        controller.model_settings_window.test_button.click()
        assert not controller.model_settings_window.test_running
        assert "输入对应的新 API 密钥" in controller.model_settings_window.status_label.text()
        assert store.read_calls == reads_before
    finally:
        controller.request_exit()


def test_mock_mode_incomplete_rollback_never_reuses_orphan_credential(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    store = CountingCredentialStore()
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        mock_chat=True,
        credential_store=store,
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    controller._save_provider_configuration(
        controller.provider_config,
        "old-invalid-test-key",
    )
    assert controller.settings["provider_enabled"] is True
    original_save = controller.settings_repository.save
    original_write = store.write_secret
    save_calls = 0
    candidate_written = False

    def fail_candidate_save(settings) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise SettingsError("invalid-test-candidate-save")
        original_save(settings)

    def fail_old_credential_restore(secret: str) -> None:
        nonlocal candidate_written
        if secret == "new-invalid-test-key":
            candidate_written = True
            original_write(secret)
            return
        if secret == "old-invalid-test-key" and candidate_written:
            raise CredentialStoreError("invalid-test-restore")
        original_write(secret)

    monkeypatch.setattr(controller.settings_repository, "save", fail_candidate_save)
    monkeypatch.setattr(store, "write_secret", fail_old_credential_restore)
    try:
        controller._save_provider_configuration(
            ProviderConfig.for_preset(ProviderPreset.MIMO_PAYG),
            "new-invalid-test-key",
        )

        assert controller.chat_panel.provider_mode == "mock"
        assert controller.settings["provider_enabled"] is False
        assert controller.settings_repository.load()["provider_enabled"] is False
        assert store.read_secret() == "new-invalid-test-key"
        reads_before = store.read_calls
        controller.model_settings_window.test_button.click()
        assert not controller.model_settings_window.test_running
        assert store.read_calls == reads_before
        controller._save_provider_configuration(controller.provider_config, None)
        assert controller.settings["provider_enabled"] is False
    finally:
        controller.request_exit()


def test_shared_exit_deadline_cleans_chat_and_connection_test(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        chat_provider=ScriptedChatProvider(ScriptedScenario.NEVER),
        connection_tester=BlockingConnectionTester(),
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    controller.model_settings_window.secret_edit.setText("invalid-test-key")
    controller.model_settings_window.test_button.click()
    controller._send_chat_message("阻塞对话")
    qtbot.waitUntil(lambda: controller.model_settings_window.test_running, timeout=1_000)
    qtbot.waitUntil(lambda: controller.conversation.has_running_worker, timeout=1_000)

    started = time.perf_counter()
    controller.request_exit()
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0
    assert not controller.model_settings_window.test_running
    assert not controller.conversation.has_running_worker
    assert not controller.conversation.has_active_timers


def test_conversation_evidence_log_contains_metadata_but_no_message_text(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    controller = make_controller(
        qapp,
        tmp_path,
        tray_available=False,
        chat_provider=ScriptedChatProvider(first_delay_ms=0, chunk_delay_ms=0),
    )
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)
    qtbot.addWidget(controller.model_settings_window)
    handler = RecordHandler()
    logger = logging.getLogger("amadeus.test.private-evidence")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False
    controller.logger = logger
    private_marker = "private-user-message-must-not-be-logged"
    try:
        controller._send_chat_message(private_marker)
        qtbot.waitUntil(
            lambda: any("Conversation evidence" in message for message in handler.messages),
            timeout=2_000,
        )

        evidence = "\n".join(handler.messages)
        assert private_marker not in evidence
        assert "provider=explicit_mock" in evidence
        assert "status=completed" in evidence
        assert "latency_ms=" in evidence
    finally:
        controller.request_exit()


def test_controller_tray_path_toggles_pet(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=True)
    guard = controller.instance_guard
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    try:
        assert controller.tray is not None
        assert not controller.window.isVisible()
        assert controller.pet_window.isVisible()

        controller.tray.toggle_action.trigger()
        qtbot.waitUntil(lambda: not controller.pet_window.isVisible())
        assert controller.tray.toggle_action.text() == "显示宠物"

        controller.tray.toggle_action.trigger()
        qtbot.waitUntil(controller.pet_window.isVisible)
        assert controller.tray.toggle_action.text() == "隐藏宠物"
    finally:
        controller.request_exit()
        assert guard.closed is True


def test_controller_fallback_path_shows_exit_window(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=False)
    guard = controller.instance_guard
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    try:
        qtbot.waitUntil(controller.window.isVisible)
        assert controller.tray is None
        assert not controller.window.hide_button.isVisible()
        assert controller.pet_window.isVisible()
    finally:
        controller.request_exit()
        assert guard.closed is True


def test_instance_activation_shows_existing_pet(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=False)
    guard = controller.instance_guard
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    try:
        controller.pet_window.hide()
        assert not controller.pet_window.isVisible()
        guard.activation_requested.emit()
        qtbot.waitUntil(controller.pet_window.isVisible)
    finally:
        controller.request_exit()


def test_drag_finish_persists_relative_position(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=False)
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    try:
        controller.pet_window.move(100, 100)
        controller.pet_window.drag_finished.emit(controller.pet_window.pos())

        saved = controller.settings_repository.load()
        assert saved["pet"]["position"] is not None
        assert 0 <= saved["pet"]["position"]["x_ratio"] <= 1
        assert 0 <= saved["pet"]["position"]["y_ratio"] <= 1
    finally:
        controller.request_exit()
