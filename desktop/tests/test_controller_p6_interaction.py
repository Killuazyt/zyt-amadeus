from __future__ import annotations

import asyncio
import logging
import threading
from copy import deepcopy
from datetime import date, datetime

from PySide6.QtCore import QObject, QRect, Signal

from amadeus_desktop.chat_models import GenerationPurpose
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.conversation_store import ProactiveInteractionStore
from amadeus_desktop.credential_store import InMemoryCredentialStore
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.presence import PresenceSnapshot
from amadeus_desktop.proactive import ProactiveTrigger
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsError, SettingsRepository
from amadeus_desktop.storage_models import ProactiveDisposition


class _InstanceGuard(QObject):
    activation_requested = Signal()

    def close(self) -> None:
        return


class _Autostart:
    def is_enabled(self) -> bool:
        return False

    def set_enabled(self, enabled: bool) -> bool:
        return bool(enabled)


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _Presence:
    def snapshot(self) -> PresenceSnapshot:
        return PresenceSnapshot(0.0, False, False)


def _controller(
    qapp,
    tmp_path,
    *,
    clock=None,
    paused_local_date: str | None = None,
    chat_provider=None,
    background_jobs_enabled: bool = False,
) -> ApplicationController:
    paths = AppPaths.for_current_user(tmp_path)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["proactive"]["mode"] = "restrained"
    settings["proactive"]["ai_greetings_enabled"] = True
    settings["proactive"]["paused_local_date"] = paused_local_date
    settings["provider_enabled"] = chat_provider is not None
    repository.save(settings)
    logger = logging.getLogger(f"amadeus.test.p6.interaction.{tmp_path.name}")
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
        mock_chat=chat_provider is None,
        credential_store=InMemoryCredentialStore(),
        chat_provider=chat_provider,
        background_jobs_enabled=background_jobs_enabled,
        autostart_manager=_Autostart(),  # type: ignore[arg-type]
        presence_probe=_Presence(),  # type: ignore[arg-type]
        clock=clock or (lambda: datetime(2026, 8, 3, 12)),
        proactive_startup_delay_ms=60_000,
    )


class _SlowCancellationGreetingProvider:
    """Keep cancellation cleanup alive long enough to expose provider overlap."""

    def __init__(self) -> None:
        self.proactive_started = threading.Event()
        self.conversation_started = threading.Event()
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    async def stream(self, request, _cancellation):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if request.options.purpose is GenerationPurpose.PROACTIVE_GREETING:
                self.proactive_started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    deadline = asyncio.get_running_loop().time() + 0.25
                    while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                        try:
                            await asyncio.sleep(remaining)
                        except asyncio.CancelledError:
                            continue
                return
            self.conversation_started.set()
            yield "前台回复"
        finally:
            with self._lock:
                self._active -= 1


def _show_greeting(controller: ApplicationController) -> None:
    controller.greeting_bubble.show_message(
        "有空的话，来聊两句？",
        controller.pet_window.geometry(),
        [QRect(0, 0, 1920, 1080)],
        timeout_ms=60_000,
    )


def _arm_inflight_ai(controller: ApplicationController, monkeypatch):
    interactions = controller.proactive_interactions
    cancel_waits: list[int] = []
    releases: list[bool] = []

    interactions._pending_trigger = ProactiveTrigger.STARTUP
    interactions._pending_date = date(2026, 8, 3)
    interactions._pending_displayed_count = 0
    interactions._ai_lane_owned = True
    monkeypatch.setattr(
        interactions,
        "_cancel_ai_lane",
        lambda wait_ms: cancel_waits.append(wait_ms) or True,
    )
    monkeypatch.setattr(
        interactions,
        "_release_ai_lane",
        lambda: releases.append(True),
    )
    return interactions, cancel_waits, releases


def _assert_durable_dismissal_without_message(database_path, event_id: str) -> None:
    reopened = SQLiteDatabase(database_path).open()
    try:
        event = ProactiveInteractionStore(reopened).get(event_id)
        assert event.disposition is ProactiveDisposition.DISMISSED
        assert event.message_id is None
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE origin = 'proactive'"
            ).fetchone()[0]
            == 0
        )
    finally:
        reopened.close()


def test_show_chat_immediately_dismisses_visible_proactive_greeting(qapp, qtbot, tmp_path) -> None:
    controller = _controller(qapp, tmp_path)
    try:
        _show_greeting(controller)
        qtbot.waitUntil(controller.greeting_bubble.isVisible)

        controller.show_chat()

        assert controller.greeting_bubble.isVisible() is False
        assert controller.chat_panel.isVisible()
    finally:
        controller._cleanup()


def test_switching_proactive_mode_off_cancels_ai_and_dismisses_bubble(
    qapp, qtbot, monkeypatch, tmp_path
) -> None:
    controller = _controller(qapp, tmp_path)
    try:
        _show_greeting(controller)
        qtbot.waitUntil(controller.greeting_bubble.isVisible)
        interactions, cancel_waits, releases = _arm_inflight_ai(controller, monkeypatch)

        off_index = controller.proactive_page.mode_combo.findData("off")
        assert off_index >= 0
        controller.proactive_page.mode_combo.setCurrentIndex(off_index)

        assert cancel_waits == [0]
        assert releases == [True]
        assert interactions._ai_lane_owned is False
        assert interactions._pending_trigger is None
        assert controller.greeting_bubble.isVisible() is False
        assert controller.settings["proactive"]["mode"] == "off"
        assert controller.proactive_page.mode_combo.currentData() == "off"
        assert controller.settings_repository.load()["proactive"]["mode"] == "off"
        assert controller.tray is not None
        assert controller.tray.pause_proactive_today_action.isChecked() is False
    finally:
        controller._cleanup()


def test_pausing_today_from_tray_cancels_ai_dismisses_bubble_and_syncs_settings(
    qapp, qtbot, monkeypatch, tmp_path
) -> None:
    controller = _controller(qapp, tmp_path)
    try:
        assert controller.tray is not None
        _show_greeting(controller)
        qtbot.waitUntil(controller.greeting_bubble.isVisible)
        interactions, cancel_waits, releases = _arm_inflight_ai(controller, monkeypatch)

        controller.tray.pause_proactive_today_action.setChecked(True)

        assert cancel_waits == [0]
        assert releases == [True]
        assert interactions._ai_lane_owned is False
        assert interactions._pending_trigger is None
        assert controller.greeting_bubble.isVisible() is False
        assert controller.settings["proactive"]["paused_local_date"] == "2026-08-03"
        assert controller.proactive_page.pause_today.isChecked()
        assert controller.tray.pause_proactive_today_action.isChecked()
        assert (
            controller.settings_repository.load()["proactive"]["paused_local_date"] == "2026-08-03"
        )
    finally:
        controller._cleanup()


def test_running_across_midnight_clears_pause_and_syncs_settings_page_and_tray(
    qapp, qtbot, tmp_path
) -> None:
    clock = _MutableClock(datetime(2026, 8, 3, 23, 59))
    controller = _controller(
        qapp,
        tmp_path,
        clock=clock,
        paused_local_date="2026-08-03",
    )
    try:
        assert controller.tray is not None
        qtbot.waitUntil(lambda: controller._data_initialized)
        qtbot.waitUntil(lambda: controller.proactive_interactions._running)
        assert controller.proactive_page.pause_today.isChecked()
        assert controller.tray.pause_proactive_today_action.isChecked()

        clock.value = datetime(2026, 8, 4, 0, 1)
        controller.proactive_interactions.poll()

        assert controller.settings["proactive"]["paused_local_date"] is None
        assert controller.settings_repository.load()["proactive"]["paused_local_date"] is None
        assert controller.proactive_page.pause_today.isChecked() is False
        assert controller.tray.pause_proactive_today_action.isChecked() is False
    finally:
        controller._cleanup()


def test_midnight_pause_expiry_stays_unpaused_when_save_fails_then_retries(
    qapp, qtbot, monkeypatch, tmp_path
) -> None:
    clock = _MutableClock(datetime(2026, 8, 3, 23, 59))
    controller = _controller(
        qapp,
        tmp_path,
        clock=clock,
        paused_local_date="2026-08-03",
    )
    try:
        assert controller.tray is not None
        qtbot.waitUntil(lambda: controller._data_initialized)
        qtbot.waitUntil(lambda: controller.proactive_interactions._running)
        original_save = controller.settings_repository.save

        def fail_save(_document) -> None:
            raise SettingsError("synthetic")

        monkeypatch.setattr(
            controller.settings_repository,
            "save",
            fail_save,
        )

        clock.value = datetime(2026, 8, 4, 0, 1)
        controller.proactive_interactions.poll()

        assert controller.settings["proactive"]["paused_local_date"] is None
        assert (
            controller.settings_repository.load()["proactive"]["paused_local_date"] == "2026-08-03"
        )
        assert controller.proactive_page.pause_today.isChecked() is False
        assert controller.tray.pause_proactive_today_action.isChecked() is False
        assert controller._last_safe_error_category == "storage_error"
        assert controller._proactive_pause_persist_pending
        assert controller._proactive_pause_retry_timer.isActive()

        monkeypatch.setattr(controller.settings_repository, "save", original_save)
        controller._retry_expired_proactive_pause_save()

        assert controller.settings_repository.load()["proactive"]["paused_local_date"] is None
        assert controller._proactive_pause_persist_pending is False
        assert controller._proactive_pause_retry_timer.isActive() is False
    finally:
        controller._cleanup()


def test_greeting_timer_timeout_durably_dismisses_without_creating_message(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    controller = _controller(qapp, tmp_path)
    database_path = controller.paths.database_file
    displayed: list[object] = []
    dismissed: list[object] = []
    controller.data_service.proactive_event_displayed.connect(displayed.append)
    controller.data_service.proactive_event_dismissed.connect(dismissed.append)
    event_id = ""
    try:
        qtbot.waitUntil(lambda: controller._data_initialized)
        qtbot.waitUntil(lambda: controller.proactive_interactions._running)

        controller.proactive_interactions._request_opportunity(ProactiveTrigger.STARTUP)
        qtbot.waitUntil(lambda: controller.greeting_bubble.isVisible() and len(displayed) == 1)
        event_id = displayed[0].event_id

        controller.greeting_bubble._timer.start(10)
        qtbot.waitUntil(lambda: len(dismissed) == 1)

        assert controller.greeting_bubble.isVisible() is False
        assert controller.greeting_bubble._timer.isActive() is False
    finally:
        controller._cleanup()

    _assert_durable_dismissal_without_message(database_path, event_id)


def test_request_exit_with_visible_greeting_durably_dismisses_without_message(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    controller = _controller(qapp, tmp_path)
    database_path = controller.paths.database_file
    displayed: list[object] = []
    controller.data_service.proactive_event_displayed.connect(displayed.append)
    event_id = ""
    try:
        qtbot.waitUntil(lambda: controller._data_initialized)
        qtbot.waitUntil(lambda: controller.proactive_interactions._running)

        controller.proactive_interactions._request_opportunity(ProactiveTrigger.STARTUP)
        qtbot.waitUntil(lambda: controller.greeting_bubble.isVisible() and len(displayed) == 1)
        event_id = displayed[0].event_id

        controller.request_exit()

        assert controller.shutdown_clean is True
        assert controller.greeting_bubble.isVisible() is False
    finally:
        if not controller._exiting:
            controller._cleanup()

    _assert_durable_dismissal_without_message(database_path, event_id)


def test_user_send_waits_for_slow_proactive_cancel_without_provider_overlap(
    qapp, qtbot, tmp_path
) -> None:
    provider = _SlowCancellationGreetingProvider()
    clock = _MutableClock(datetime(2026, 8, 3, 12))
    controller = _controller(
        qapp,
        tmp_path,
        clock=clock,
        chat_provider=provider,
        background_jobs_enabled=True,
    )
    try:
        qtbot.waitUntil(lambda: controller._data_initialized)
        qtbot.waitUntil(
            lambda: (
                not controller.memory_jobs.has_active_job
                and not controller.background_generation.is_running
            )
        )
        interactions = controller.proactive_interactions
        interactions._pending_trigger = ProactiveTrigger.STARTUP
        interactions._pending_date = clock.value.date()
        interactions._pending_displayed_count = 0
        assert interactions._start_ai_generation(ProactiveTrigger.STARTUP, clock.value)
        qtbot.waitUntil(provider.proactive_started.is_set)

        controller.chat_panel.input.setPlainText("前台消息必须等待主动问候退出")
        assert controller.chat_panel.action_button.isEnabled()
        controller.chat_panel.action_button.click()

        assert controller._pending_foreground_action == (
            "send",
            "前台消息必须等待主动问候退出",
        )
        assert controller.chat_panel.action_button.isEnabled() is False
        qtbot.waitUntil(provider.conversation_started.is_set, timeout=2_000)
        qtbot.waitUntil(lambda: bool(controller.conversation.turns), timeout=2_000)

        assert provider.max_active == 1
        assert controller._pending_foreground_action is None
        assert controller.conversation.turns[-1].user_message.content == (
            "前台消息必须等待主动问候退出"
        )
        assert controller.chat_panel.input.toPlainText() == ""
    finally:
        controller._cleanup()
