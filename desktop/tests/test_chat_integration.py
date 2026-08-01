from __future__ import annotations

import logging
import time
from copy import deepcopy

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal

from amadeus_desktop.chat_models import (
    ConversationState,
    MessageStatus,
)
from amadeus_desktop.chat_provider import ScriptedChatProvider, ScriptedScenario
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsRepository


class FakeInstanceGuard(QObject):
    activation_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def make_logger() -> logging.Logger:
    logger = logging.getLogger("amadeus.test.chat-integration")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def make_controller(
    qapp,
    tmp_path,
    provider: ScriptedChatProvider,
    *,
    first_chunk_timeout_ms: int = 1_000,
    stream_idle_timeout_ms: int = 1_000,
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
        tray_available=False,
        chat_provider=provider,
        first_chunk_timeout_ms=first_chunk_timeout_ms,
        stream_idle_timeout_ms=stream_idle_timeout_ms,
    )


def add_controller_widgets(qtbot, controller: ApplicationController) -> None:
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    qtbot.addWidget(controller.chat_panel)


def send_from_panel(qtbot, controller: ApplicationController, text: str) -> None:
    controller.show_chat()
    controller.chat_panel.input.setPlainText(text)
    qtbot.keyClick(controller.chat_panel.input, Qt.Key.Key_Return)


def test_pet_click_toggles_attached_panel(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, ScriptedChatProvider())
    add_controller_widgets(qtbot, controller)
    try:
        assert not controller.chat_panel.isVisible()

        controller.pet_window.clicked.emit()
        qtbot.waitUntil(controller.chat_panel.isVisible)

        controller.pet_window.clicked.emit()
        qtbot.waitUntil(lambda: not controller.chat_panel.isVisible())
    finally:
        controller.request_exit()


def test_normal_stream_updates_panel_and_pet_states(qapp, qtbot, tmp_path) -> None:
    provider = ScriptedChatProvider(first_delay_ms=10, chunk_delay_ms=5)
    controller = make_controller(qapp, tmp_path, provider)
    add_controller_widgets(qtbot, controller)
    conversation_states: list[ConversationState] = []
    animation_states: list[str] = []
    controller.conversation.state_changed.connect(conversation_states.append)
    controller.pet_window.animation.state_changed.connect(animation_states.append)
    try:
        send_from_panel(qtbot, controller, "第一行\n第二行")
        qtbot.waitUntil(
            lambda: (
                controller.conversation.state is ConversationState.IDLE
                and bool(controller.conversation.turns)
                and controller.conversation.turns[0].assistant_message.status
                is MessageStatus.COMPLETED
            ),
            timeout=3_000,
        )

        turn = controller.conversation.turns[0]
        assert turn.user_message.content == "第一行\n第二行"
        assert turn.assistant_message.content == "".join(provider.chunks)
        assert controller.chat_panel.message_widget(turn.user_message.message_id) is not None
        assert controller.chat_panel.message_widget(turn.assistant_message.message_id) is not None
        assert ConversationState.SENDING in conversation_states
        assert ConversationState.WAITING_FIRST_CHUNK in conversation_states
        assert ConversationState.STREAMING in conversation_states
        assert ConversationState.COMPLETED in conversation_states
        assert "waiting" in animation_states
        assert "responding" in animation_states
        assert "jump" in animation_states
    finally:
        controller.request_exit()


def test_hidden_panel_keeps_request_and_stop_restores_stable_state(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    provider = ScriptedChatProvider(ScriptedScenario.NEVER)
    controller = make_controller(qapp, tmp_path, provider)
    add_controller_widgets(qtbot, controller)
    try:
        send_from_panel(qtbot, controller, "请等待")
        qtbot.waitUntil(
            lambda: (
                controller.conversation.state is ConversationState.WAITING_FIRST_CHUNK
                and controller.conversation.has_running_worker
            )
        )

        controller.hide_chat()
        qtbot.wait(30)
        assert not controller.chat_panel.isVisible()
        assert controller.conversation.is_active

        controller.show_chat()
        qtbot.mouseClick(controller.chat_panel.action_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(
            lambda: (
                controller.conversation.state is ConversationState.IDLE
                and not controller.conversation.has_running_worker
            ),
            timeout=3_000,
        )

        turn = controller.conversation.turns[0]
        assert turn.assistant_message.status is MessageStatus.STOPPED
        assert turn.assistant_message.content == ""
        assert controller.pet_window.animation.state == "idle"
    finally:
        controller.request_exit()


def test_failed_retry_reuses_messages_without_duplicate_user(qapp, qtbot, tmp_path) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.PARTIAL_ERROR,
        first_delay_ms=5,
        chunk_delay_ms=5,
    )
    controller = make_controller(qapp, tmp_path, provider)
    add_controller_widgets(qtbot, controller)
    try:
        send_from_panel(qtbot, controller, "重试这一轮")
        qtbot.waitUntil(
            lambda: (
                controller.conversation.state is ConversationState.IDLE
                and controller.conversation.turns[0].assistant_message.status
                is MessageStatus.FAILED
            ),
            timeout=3_000,
        )
        failed = controller.conversation.turns[0]
        user_id = failed.user_message.message_id
        assistant_id = failed.assistant_message.message_id
        provider.scenario = ScriptedScenario.NORMAL
        retry = controller.chat_panel.message_widget(assistant_id)
        assert retry is not None and retry.retry_button.isVisible()

        qtbot.mouseClick(retry.retry_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(
            lambda: (
                provider.call_count == 2
                and controller.conversation.state is ConversationState.IDLE
                and controller.conversation.turns[0].assistant_message.status
                is MessageStatus.COMPLETED
            ),
            timeout=3_000,
        )

        completed = controller.conversation.turns[0]
        assert len(controller.conversation.turns) == 1
        assert completed.attempt == 2
        assert completed.user_message.message_id == user_id
        assert completed.assistant_message.message_id == assistant_id
        assert controller.chat_panel.message_ids.count(user_id) == 1
        assert controller.chat_panel.message_ids.count(assistant_id) == 1
    finally:
        controller.request_exit()


def test_panel_follows_pet_and_remains_inside_work_area(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, ScriptedChatProvider())
    add_controller_widgets(qtbot, controller)
    try:
        controller.show_chat()
        work_area = qapp.primaryScreen().availableGeometry()

        controller.pet_window.move(
            work_area.right() - controller.pet_window.width() + 1,
            work_area.bottom() - controller.pet_window.height() + 1,
        )
        qtbot.waitUntil(
            lambda: (
                controller.chat_panel.geometry().right() < controller.pet_window.geometry().left()
            )
        )
        assert work_area.contains(controller.chat_panel.geometry())

        controller.pet_window.move(QPoint(work_area.left(), work_area.top()))
        qtbot.waitUntil(
            lambda: (
                controller.chat_panel.geometry().left() > controller.pet_window.geometry().right()
            )
        )
        assert work_area.contains(controller.chat_panel.geometry())
    finally:
        controller.request_exit()


def test_hiding_pet_hides_panel_without_cancelling(qapp, qtbot, tmp_path) -> None:
    provider = ScriptedChatProvider(ScriptedScenario.NEVER)
    controller = make_controller(qapp, tmp_path, provider)
    add_controller_widgets(qtbot, controller)
    try:
        send_from_panel(qtbot, controller, "隐藏也继续")
        qtbot.waitUntil(lambda: controller.conversation.is_active)

        controller.toggle_pet()

        assert not controller.pet_window.isVisible()
        assert not controller.chat_panel.isVisible()
        assert controller.conversation.is_active
        controller.show_pet()
        assert not controller.chat_panel.isVisible()
    finally:
        controller.request_exit()
        assert not controller.conversation.has_running_worker


def test_failure_feedback_survives_terminal_to_idle_transition(qapp, qtbot, tmp_path) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.PARTIAL_ERROR,
        first_delay_ms=5,
        chunk_delay_ms=5,
    )
    controller = make_controller(qapp, tmp_path, provider)
    add_controller_widgets(qtbot, controller)
    animation_states: list[str] = []
    controller.pet_window.animation.state_changed.connect(animation_states.append)
    try:
        send_from_panel(qtbot, controller, "制造错误反馈")
        qtbot.waitUntil(
            lambda: (
                controller.conversation.state is ConversationState.IDLE
                and not controller.conversation.has_running_worker
            ),
            timeout=3_000,
        )

        assert "error" in animation_states
        assert controller.pet_window.animation.state == "error"
    finally:
        controller.request_exit()


def test_drag_activity_temporarily_overrides_conversation_animation(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.STALL,
        chunks=("已开始回复",),
        first_delay_ms=5,
    )
    controller = make_controller(qapp, tmp_path, provider, stream_idle_timeout_ms=1_000)
    add_controller_widgets(qtbot, controller)
    try:
        send_from_panel(qtbot, controller, "拖动优先级")
        qtbot.waitUntil(
            lambda: (
                controller.conversation.state is ConversationState.STREAMING
                and controller.pet_window.animation.state == "responding"
            )
        )

        controller.pet_window.animation.set_activity("move_left", True)
        assert controller.pet_window.animation.state == "move_left"
        controller.pet_window.animation.set_activity("move_left", False)
        assert controller.pet_window.animation.state == "responding"
    finally:
        controller.request_exit()


def test_hidden_panel_continues_streaming_and_reopens_with_completed_text(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    provider = ScriptedChatProvider(first_delay_ms=30, chunk_delay_ms=15)
    controller = make_controller(qapp, tmp_path, provider)
    add_controller_widgets(qtbot, controller)
    try:
        send_from_panel(qtbot, controller, "收起后继续")
        controller.hide_chat()
        qtbot.waitUntil(
            lambda: (
                controller.conversation.state is ConversationState.IDLE
                and controller.conversation.turns[0].assistant_message.status
                is MessageStatus.COMPLETED
            ),
            timeout=3_000,
        )
        assert not controller.chat_panel.isVisible()

        controller.show_chat()
        completed = controller.conversation.turns[0].assistant_message
        bubble = controller.chat_panel.message_widget(completed.message_id)
        assert bubble is not None
        assert bubble.text == completed.content
    finally:
        controller.request_exit()


def test_streaming_keeps_ui_event_loop_responsive(qapp, qtbot, tmp_path) -> None:
    provider = ScriptedChatProvider(
        chunks=tuple(f"片段{index}" for index in range(20)),
        first_delay_ms=10,
        chunk_delay_ms=5,
    )
    controller = make_controller(qapp, tmp_path, provider)
    add_controller_widgets(qtbot, controller)
    heartbeats = 0
    timer = QTimer()
    timer.setInterval(2)

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    timer.timeout.connect(heartbeat)
    timer.start()
    try:
        send_from_panel(qtbot, controller, "保持界面响应")
        qtbot.waitUntil(
            lambda: (
                controller.conversation.state is ConversationState.IDLE
                and not controller.conversation.has_running_worker
            ),
            timeout=3_000,
        )

        assert heartbeats >= 10
    finally:
        timer.stop()
        controller.request_exit()


def test_pet_click_opens_interactive_panel_under_two_hundred_ms(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    controller = make_controller(qapp, tmp_path, ScriptedChatProvider())
    add_controller_widgets(qtbot, controller)
    try:
        started = time.perf_counter()
        controller.pet_window.clicked.emit()
        qtbot.waitUntil(controller.chat_panel.isVisible)
        elapsed_ms = (time.perf_counter() - started) * 1_000

        assert controller.chat_panel.input.isEnabled()
        assert elapsed_ms < 200
    finally:
        controller.request_exit()
