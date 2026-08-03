from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Qt, QTimer, Signal

import amadeus_desktop.controller as controller_module
from amadeus_desktop.chat_models import (
    ChatMessage,
    ChatRequest,
    ConversationState,
    ConversationTurn,
    MessageRole,
    MessageStatus,
    TurnTerminalReason,
)
from amadeus_desktop.chat_provider import (
    CancellationToken,
    ScriptedChatProvider,
    ScriptedScenario,
)
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.credential_store import InMemoryCredentialStore
from amadeus_desktop.data_runtime import SerialDataThread
from amadeus_desktop.database import SCHEMA_VERSION
from amadeus_desktop.local_data_service import (
    ConversationSnapshot,
    LocalDataService,
    OlderMessagesSnapshot,
    create_local_data_stores,
)
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.prompt_context import DefaultPromptContextService
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsRepository


class FakeInstanceGuard(QObject):
    activation_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class PersistedFirstProvider:
    """Inspect SQLite from the provider thread before producing any output."""

    def __init__(self, database_path: Path, expected_user_text: str) -> None:
        self._database_path = database_path
        self._expected_user_text = expected_user_text
        self.observations: list[tuple[int, str, int]] = []

    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        del request
        cancellation.raise_if_cancelled()
        uri = f"{self._database_path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            user_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE role = 'user' AND content = ?",
                    (self._expected_user_text,),
                ).fetchone()[0]
            )
            assistant_status = str(
                connection.execute(
                    """
                    SELECT status FROM messages
                    WHERE role = 'assistant'
                    ORDER BY sequence DESC LIMIT 1
                    """
                ).fetchone()[0]
            )
        self.observations.append((user_count, assistant_status, threading.get_ident()))
        yield "合成回复"


class FailingPromptContextService:
    def build(self, _context):
        raise RuntimeError("synthetic prompt failure with private detail")


def _logger() -> logging.Logger:
    logger = logging.getLogger("amadeus.test.p5a-production-integration")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def _make_controller(
    qapp,
    local_app_data: Path,
    provider: object,
    *,
    memory_enabled: bool = False,
    background_jobs_enabled: bool | None = None,
    follow_user_language: bool = True,
) -> ApplicationController:
    paths = AppPaths.for_current_user(local_app_data)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["memory"]["enabled"] = memory_enabled
    settings["persona"]["follow_user_language"] = follow_user_language
    repository.save(settings)
    return ApplicationController(
        qapp,
        FakeInstanceGuard(),  # type: ignore[arg-type]
        _logger(),
        paths=paths,
        settings_repository=repository,
        settings=settings,
        tray_available=False,
        chat_provider=provider,  # type: ignore[arg-type]
        credential_store=InMemoryCredentialStore(),
        first_chunk_timeout_ms=1_000,
        stream_idle_timeout_ms=1_000,
        background_jobs_enabled=background_jobs_enabled,
    )


def _wait_ready(qtbot, controller: ApplicationController) -> None:
    qtbot.waitUntil(
        lambda: controller._data_initialized and controller._data_writable,
        timeout=3_000,
    )


def _wait_idle(qtbot, controller: ApplicationController) -> None:
    qtbot.waitUntil(
        lambda: (
            controller.conversation.state is ConversationState.IDLE
            and not controller.conversation.has_running_worker
        ),
        timeout=3_000,
    )


def _shutdown_controller(controller: ApplicationController) -> None:
    assert controller._shutdown_background_tasks(timeout_ms=3_000)
    controller._exiting = True
    if controller.tray is not None:
        controller.tray.close()
    controller.settings_window.close()
    controller.chat_panel.close()
    controller.pet_window.close()
    controller.window.close()
    controller.instance_guard.close()


def _scalar(database_path: Path, sql: str, params: tuple[object, ...] = ()) -> object:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def _wait_for_scalar(
    qtbot,
    database_path: Path,
    sql: str,
    expected: object,
    params: tuple[object, ...] = (),
) -> None:
    def matches() -> bool:
        try:
            return _scalar(database_path, sql, params) == expected
        except sqlite3.Error:
            return False

    qtbot.waitUntil(matches, timeout=3_000)


def test_provider_is_called_only_after_user_turn_is_committed(qapp, qtbot, tmp_path) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    user_text = "合成先存后发消息"
    provider = PersistedFirstProvider(paths.database_file, user_text)
    controller = _make_controller(qapp, tmp_path, provider)
    try:
        _wait_ready(qtbot, controller)
        ui_thread_id = threading.get_ident()

        controller._send_chat_message(user_text)
        _wait_idle(qtbot, controller)
        _wait_for_scalar(
            qtbot,
            paths.database_file,
            "SELECT status FROM messages WHERE role = 'assistant'",
            "completed",
        )

        assert len(provider.observations) == 1
        user_count, assistant_status, provider_thread_id = provider.observations[0]
        assert user_count == 1
        assert assistant_status == "pending"
        assert provider_thread_id != ui_thread_id
        assert len(controller.conversation.turns) == 1
        assert controller.conversation.turns[0].assistant_message.status is MessageStatus.COMPLETED
        assert not controller.background_generation.is_paused
    finally:
        _shutdown_controller(controller)


@pytest.mark.parametrize(
    ("follow_user_language", "expected_instruction", "excluded_instruction"),
    (
        (
            True,
            "默认使用用户当前消息的主要语言回答；用户切换语言时跟随切换。",
            "默认使用简体中文回答，除非用户明确要求切换语言。",
        ),
        (
            False,
            "默认使用简体中文回答，除非用户明确要求切换语言。",
            "默认使用用户当前消息的主要语言回答；用户切换语言时跟随切换。",
        ),
    ),
)
def test_follow_user_language_reaches_final_provider_system_prompt(
    qapp,
    qtbot,
    tmp_path,
    follow_user_language: bool,
    expected_instruction: str,
    excluded_instruction: str,
) -> None:
    provider = ScriptedChatProvider(chunks=("合成回复",), first_delay_ms=0)
    controller = _make_controller(
        qapp,
        tmp_path,
        provider,
        follow_user_language=follow_user_language,
    )
    try:
        _wait_ready(qtbot, controller)
        controller._send_chat_message("Please answer this synthetic request.")
        _wait_idle(qtbot, controller)

        assert provider.call_count == 1
        system_prompt = "".join(
            message.content
            for message in provider.requests[0].messages
            if message.role.value == "system"
        )
        assert expected_instruction in system_prompt
        assert excluded_instruction not in system_prompt
    finally:
        _shutdown_controller(controller)


def test_stop_persists_one_stable_turn_without_duplicate_user(qapp, qtbot, tmp_path) -> None:
    provider = ScriptedChatProvider(ScriptedScenario.NEVER)
    controller = _make_controller(qapp, tmp_path, provider)
    database_path = AppPaths.for_current_user(tmp_path).database_file
    try:
        _wait_ready(qtbot, controller)
        controller._send_chat_message("合成停止消息")
        qtbot.waitUntil(lambda: provider.call_count == 1 and controller.conversation.is_active)
        original = controller.conversation.turns[0]

        assert controller.conversation.stop()
        _wait_idle(qtbot, controller)
        _wait_for_scalar(
            qtbot,
            database_path,
            "SELECT status FROM messages WHERE id = ?",
            "stopped",
            (original.assistant_message.message_id,),
        )

        restored = controller.conversation.turns[0]
        assert restored.turn_id == original.turn_id
        assert restored.user_message.message_id == original.user_message.message_id
        assert restored.assistant_message.message_id == original.assistant_message.message_id
        assert restored.terminal_reason is TurnTerminalReason.USER_STOPPED
        assert (
            _scalar(
                database_path,
                "SELECT COUNT(*) FROM messages WHERE turn_id = ? AND role = 'user'",
                (original.turn_id,),
            )
            == 1
        )
    finally:
        _shutdown_controller(controller)


def test_failed_retry_reuses_stable_ids_and_only_increments_attempt(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.PARTIAL_ERROR,
        chunks=("部分",),
        first_delay_ms=0,
        chunk_delay_ms=0,
    )
    controller = _make_controller(qapp, tmp_path, provider)
    database_path = AppPaths.for_current_user(tmp_path).database_file
    try:
        _wait_ready(qtbot, controller)
        controller._send_chat_message("合成重试消息")
        _wait_idle(qtbot, controller)
        failed = controller.conversation.turns[0]
        assert failed.assistant_message.status is MessageStatus.FAILED
        _wait_for_scalar(
            qtbot,
            database_path,
            "SELECT status FROM messages WHERE id = ?",
            "failed",
            (failed.assistant_message.message_id,),
        )

        provider.scenario = ScriptedScenario.NORMAL
        controller._retry_chat_turn(failed.turn_id)
        qtbot.waitUntil(lambda: provider.call_count == 2, timeout=3_000)
        _wait_idle(qtbot, controller)
        completed = controller.conversation.turns[0]
        _wait_for_scalar(
            qtbot,
            database_path,
            "SELECT attempt FROM messages WHERE id = ?",
            2,
            (completed.assistant_message.message_id,),
        )

        assert completed.user_message.message_id == failed.user_message.message_id
        assert completed.assistant_message.message_id == failed.assistant_message.message_id
        assert completed.attempt == 2
        assert completed.assistant_message.status is MessageStatus.COMPLETED
        assert (
            _scalar(
                database_path,
                "SELECT COUNT(*) FROM messages WHERE turn_id = ? AND role = 'user'",
                (completed.turn_id,),
            )
            == 1
        )
        assert (
            _scalar(
                database_path,
                "SELECT COUNT(*) FROM messages WHERE turn_id = ?",
                (completed.turn_id,),
            )
            == 2
        )
    finally:
        _shutdown_controller(controller)


def test_background_startup_restores_most_recent_conversation_and_pages_by_40(
    qtbot,
    tmp_path,
) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    stores = create_local_data_stores(
        paths.database_file,
        paths.migration_backup_directory,
    )
    try:
        stores.conversations.ensure_default_profile()
        older = stores.conversations.create_conversation("较早会话", conversation_id="older")
        stores.conversations.save_turn(
            older.conversation_id,
            "older-turn",
            "older-user",
            "较早合成消息",
            "older-assistant",
        )
        stores.conversations.finalize_assistant(
            "older-assistant",
            "较早合成回复",
            status="completed",
            terminal_reason="completed",
            attempt=1,
        )
        recent = stores.conversations.create_conversation("最近会话", conversation_id="recent")
        for index in range(25):
            stores.conversations.save_turn(
                recent.conversation_id,
                f"turn-{index:02d}",
                f"user-{index:02d}",
                f"合成消息 {index:02d}",
                f"assistant-{index:02d}",
            )
            stores.conversations.finalize_assistant(
                f"assistant-{index:02d}",
                f"合成回复 {index:02d}",
                status="completed",
                terminal_reason="completed",
                attempt=1,
            )
    finally:
        stores.close()

    runtime = SerialDataThread(
        lambda: create_local_data_stores(
            paths.database_file,
            paths.migration_backup_directory,
        ),
        resource_close=lambda value: value.close(),
    )
    service = LocalDataService(runtime, memory_enabled=False)
    startup: list[ConversationSnapshot] = []
    older_pages: list[OlderMessagesSnapshot] = []
    service.startup_loaded.connect(startup.append)
    service.older_messages_loaded.connect(older_pages.append)
    try:
        service.start()
        qtbot.waitUntil(lambda: bool(startup), timeout=3_000)

        snapshot = startup[0]
        assert snapshot.conversation is not None
        assert snapshot.conversation.conversation_id == "recent"
        assert len(snapshot.messages) == 40
        assert len(snapshot.turns) == 20
        assert snapshot.next_before_sequence is not None

        service.load_older_messages()
        qtbot.waitUntil(lambda: bool(older_pages), timeout=3_000)
        page = older_pages[0]
        assert page.conversation_id == "recent"
        assert len(page.messages) == 10
        assert len(page.turns) == 5
        assert page.next_before_sequence is None
    finally:
        assert service.shutdown(3_000)


def test_memory_disabled_still_persists_chat_without_recall_or_extraction(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    marker = "禁用时不得召回的合成记忆标记"
    provider = ScriptedChatProvider(chunks=("合成回复",), first_delay_ms=0)
    controller = _make_controller(qapp, tmp_path, provider, memory_enabled=False)
    database_path = AppPaths.for_current_user(tmp_path).database_file
    seeded: list[object] = []
    try:
        _wait_ready(qtbot, controller)
        controller.data_runtime.submit(
            lambda stores: stores.memories.create_memory(
                "preference",
                "synthetic-marker",
                marker,
                origin="manual",
            ),
            on_success=seeded.append,
        )
        qtbot.waitUntil(lambda: bool(seeded), timeout=3_000)

        controller._send_chat_message("合成禁用记忆消息")
        _wait_idle(qtbot, controller)
        _wait_for_scalar(
            qtbot,
            database_path,
            "SELECT status FROM messages WHERE role = 'assistant'",
            "completed",
        )

        assert provider.call_count == 1
        assert all(marker not in message.content for message in provider.requests[0].messages)
        assert (
            _scalar(
                database_path,
                "SELECT participates_in_memory FROM messages WHERE role = 'user'",
            )
            == 0
        )
        assert (
            _scalar(
                database_path,
                "SELECT COUNT(*) FROM background_jobs WHERE kind = 'memory_extraction'",
            )
            == 0
        )
    finally:
        _shutdown_controller(controller)


def test_controller_rejects_history_mutations_during_active_request(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    provider = ScriptedChatProvider(ScriptedScenario.NEVER)
    controller = _make_controller(qapp, tmp_path, provider)
    mutations: list[str] = []
    try:
        _wait_ready(qtbot, controller)
        controller._send_chat_message("合成活动请求")
        qtbot.waitUntil(lambda: controller.conversation.is_active)

        monkeypatch.setattr(
            controller.data_service,
            "switch_conversation",
            lambda _conversation_id: mutations.append("switch"),
        )
        monkeypatch.setattr(
            controller.data_service,
            "create_conversation",
            lambda: mutations.append("create"),
        )
        monkeypatch.setattr(
            controller.data_service,
            "delete_conversation",
            lambda _conversation_id: mutations.append("delete"),
        )
        monkeypatch.setattr(
            controller.data_service,
            "clear_conversations",
            lambda: mutations.append("clear"),
        )

        controller._request_conversation_switch("synthetic-other")
        controller._request_new_conversation()
        controller._request_delete_conversation("synthetic-other")
        controller._request_clear_history()

        assert mutations == []
        assert "当前回复" in controller.history_page.status_label.text()
        assert controller.conversation.stop()
        _wait_idle(qtbot, controller)
    finally:
        _shutdown_controller(controller)


def test_stale_older_page_is_not_applied_to_the_current_conversation(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    controller = _make_controller(qapp, tmp_path, ScriptedChatProvider())
    stale_turn = ConversationTurn(
        turn_id="stale-turn",
        user_message=ChatMessage(
            "stale-user",
            MessageRole.USER,
            "合成过期消息",
            MessageStatus.COMPLETED,
        ),
        assistant_message=ChatMessage(
            "stale-assistant",
            MessageRole.ASSISTANT,
            "合成过期回复",
            MessageStatus.COMPLETED,
        ),
        terminal_reason=TurnTerminalReason.COMPLETED,
    )
    try:
        _wait_ready(qtbot, controller)
        assert controller.data_service.current_conversation_id is not None
        assert controller.data_service.current_conversation_id != "stale-conversation"

        controller._on_older_messages_loaded(
            OlderMessagesSnapshot(
                "stale-conversation",
                (),
                (stale_turn,),
                None,
            )
        )
        qtbot.wait(20)

        assert "stale-user" not in controller.chat_panel.message_ids
        assert "stale-assistant" not in controller.chat_panel.message_ids
    finally:
        _shutdown_controller(controller)


def test_slow_database_operation_keeps_qt_heartbeat_responsive(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    controller = _make_controller(qapp, tmp_path, ScriptedChatProvider())
    completed: list[bool] = []
    heartbeats = 0
    heartbeats_during_operation = 0
    operation_started = threading.Event()
    operation_finished = threading.Event()
    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(2)

    def heartbeat() -> None:
        nonlocal heartbeats, heartbeats_during_operation
        heartbeats += 1
        if operation_started.is_set() and not operation_finished.is_set():
            heartbeats_during_operation += 1

    def slow_operation(_stores) -> bool:
        operation_started.set()
        try:
            time.sleep(0.18)
            return True
        finally:
            operation_finished.set()

    timer.timeout.connect(heartbeat)
    try:
        _wait_ready(qtbot, controller)
        timer.start()
        controller.data_runtime.submit(slow_operation, on_success=completed.append)
        qtbot.waitUntil(lambda: completed == [True], timeout=3_000)

        assert heartbeats >= 3
        assert heartbeats_during_operation >= 3
    finally:
        timer.stop()
        _shutdown_controller(controller)


def test_data_initialization_failure_is_visible_and_never_calls_provider(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    provider = ScriptedChatProvider()

    def fail_initialization(*_args, **_kwargs):
        raise RuntimeError("synthetic private initialization detail")

    monkeypatch.setattr(
        controller_module,
        "create_local_data_stores",
        fail_initialization,
    )
    controller = _make_controller(qapp, tmp_path, provider)
    try:
        qtbot.waitUntil(lambda: controller._data_initialized, timeout=3_000)
        assert not controller._data_writable

        controller._send_chat_message("不得发送的合成消息")

        assert provider.call_count == 0
        assert controller.conversation.turns == ()
        assert "无法安全写入" in controller.chat_panel.status_label.text()
    finally:
        _shutdown_controller(controller)


def test_stopped_data_thread_rejects_turn_visibly_before_network(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    provider = ScriptedChatProvider()
    controller = _make_controller(qapp, tmp_path, provider)
    try:
        _wait_ready(qtbot, controller)
        assert controller.data_runtime.shutdown(1_000)
        # Model the small window in which a caller still sees the last writable
        # snapshot although the serialized queue has already stopped accepting.
        controller._data_writable = True
        controller.data_service._writable = True

        controller._send_chat_message("终态队列拒绝的合成消息")
        _wait_idle(qtbot, controller)

        assert provider.call_count == 0
        assert len(controller.conversation.turns) == 1
        failed = controller.conversation.turns[0]
        assert failed.terminal_reason is TurnTerminalReason.LOCAL_PERSISTENCE_ERROR
        bubble = controller.chat_panel.message_widget(failed.assistant_message.message_id)
        assert bubble is not None
        assert "本地数据" in bubble.status_label.text()
    finally:
        _shutdown_controller(controller)


def test_prompt_failure_persists_failed_placeholder_and_stable_retry_recovers(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    provider = ScriptedChatProvider(chunks=("合成恢复回复",), first_delay_ms=0)
    controller = _make_controller(qapp, tmp_path, provider)
    database_path = AppPaths.for_current_user(tmp_path).database_file
    try:
        _wait_ready(qtbot, controller)
        controller.data_service._prompt_service = FailingPromptContextService()  # type: ignore[assignment]
        controller._send_chat_message("合成提示组装失败消息")
        _wait_idle(qtbot, controller)
        failed = controller.conversation.turns[0]
        _wait_for_scalar(
            qtbot,
            database_path,
            "SELECT status FROM messages WHERE id = ?",
            "failed",
            (failed.assistant_message.message_id,),
        )

        assert provider.call_count == 0
        assert failed.terminal_reason is TurnTerminalReason.LOCAL_PERSISTENCE_ERROR
        assert (
            _scalar(
                database_path,
                "SELECT COUNT(*) FROM messages WHERE turn_id = ?",
                (failed.turn_id,),
            )
            == 2
        )

        controller.data_service._prompt_service = DefaultPromptContextService()
        controller._retry_chat_turn(failed.turn_id)
        _wait_idle(qtbot, controller)
        completed = controller.conversation.turns[0]
        _wait_for_scalar(
            qtbot,
            database_path,
            "SELECT attempt FROM messages WHERE id = ?",
            2,
            (completed.assistant_message.message_id,),
        )
        assert provider.call_count == 1
        assert completed.user_message.message_id == failed.user_message.message_id
        assert completed.assistant_message.message_id == failed.assistant_message.message_id
        assert completed.assistant_message.status is MessageStatus.COMPLETED
    finally:
        _shutdown_controller(controller)


def test_terminal_persistence_failure_never_reports_durable_completion_and_recovers(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    provider = ScriptedChatProvider(chunks=("已生成但终态写入失败",), first_delay_ms=0)
    controller = _make_controller(
        qapp,
        tmp_path,
        provider,
        background_jobs_enabled=True,
    )
    database_path = AppPaths.for_current_user(tmp_path).database_file
    installed: list[bool] = []
    finished_states: list[ConversationState] = []
    controller.conversation.request_finished.connect(
        lambda _request_id, _turn, state: finished_states.append(state)
    )
    try:
        _wait_ready(qtbot, controller)
        assert controller.memory_jobs.is_accepting

        def install_failure(stores) -> bool:
            def fail_finalize(*_args, **_kwargs):
                raise sqlite3.OperationalError("synthetic terminal write failure")

            stores.conversations.finalize_assistant = fail_finalize
            return True

        controller.data_runtime.submit(install_failure, on_success=installed.append)
        qtbot.waitUntil(lambda: installed == [True], timeout=3_000)

        controller._send_chat_message("合成终态失败消息")
        _wait_idle(qtbot, controller)
        turn = controller.conversation.turns[0]

        assert finished_states == [ConversationState.FAILED]
        assert turn.terminal_reason is TurnTerminalReason.LOCAL_PERSISTENCE_ERROR
        assert turn.assistant_message.status is MessageStatus.FAILED
        assert turn.assistant_message.content == "已生成但终态写入失败"
        assert not controller._data_writable
        assert not controller.memory_jobs.is_accepting
        assert _scalar(
            database_path,
            "SELECT status FROM messages WHERE id = ?",
            (turn.assistant_message.message_id,),
        ) in {"pending", "streaming"}
    finally:
        _shutdown_controller(controller)

    restarted = _make_controller(qapp, tmp_path, ScriptedChatProvider())
    try:
        _wait_ready(qtbot, restarted)
        restored = restarted.conversation.turns[0]
        assert restored.assistant_message.message_id == turn.assistant_message.message_id
        assert restored.assistant_message.status is MessageStatus.STOPPED
        assert restored.terminal_reason is TurnTerminalReason.SHUTDOWN
    finally:
        _shutdown_controller(restarted)


def test_newer_database_opens_application_read_only_without_calling_provider(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    stores = create_local_data_stores(paths.database_file, paths.migration_backup_directory)
    try:
        stores.conversations.get_or_create_active_conversation()
        stores.database.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    finally:
        stores.close()

    provider = ScriptedChatProvider()
    controller = _make_controller(qapp, tmp_path, provider)
    try:
        qtbot.waitUntil(lambda: controller._data_initialized, timeout=3_000)
        assert not controller._data_writable
        assert not controller.memory_jobs.is_started
        assert not controller.memory_jobs.is_accepting
        assert controller.pet_window is not None
        controller.show_model_settings()
        assert controller.settings_window.isVisible()

        controller._send_chat_message("只读模式不得发送的合成消息")
        assert provider.call_count == 0
        assert controller.conversation.turns == ()
        assert "无法安全写入" in controller.chat_panel.status_label.text()
    finally:
        _shutdown_controller(controller)
