from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import QWidget

from amadeus_desktop.ui.chat_panel import ChatPanel


class State(StrEnum):
    IDLE = "idle"
    SENDING = "sending"
    WAITING_FIRST_CHUNK = "waiting_first_chunk"
    STREAMING = "streaming"
    FAILED = "failed"


@dataclass(frozen=True)
class Message:
    message_id: str
    role: str
    content: str
    status: str


@dataclass(frozen=True)
class Turn:
    turn_id: str
    user_message: Message
    assistant_message: Message
    terminal_reason: str | None = None
    status_text: str | None = None
    error: str | None = None


@pytest.fixture
def panel(qtbot) -> ChatPanel:
    widget = ChatPanel()
    qtbot.addWidget(widget)
    return widget


def test_panel_is_focusable_tool_window_with_independent_mock_banner(panel) -> None:
    flags = panel.windowFlags()

    assert flags & Qt.WindowType.Tool
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.WindowType_Mask == Qt.WindowType.Tool
    assert not flags & Qt.WindowType.WindowDoesNotAcceptFocus
    assert panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert "本地模拟模式" in panel.mock_banner.text()

    panel.set_conversation_state(State.FAILED, "模拟中断")

    assert panel.status_label.text() == "模拟中断"
    assert "本地模拟模式" in panel.mock_banner.text()


def test_panel_paints_an_opaque_styled_background(panel, qtbot) -> None:
    panel.show()
    qtbot.waitUntil(panel.isVisible)

    image = panel.grab().toImage()

    assert image.pixelColor(5, panel.height() // 2).alpha() > 0


def test_show_reconciles_bubble_width_after_native_viewport_settles(panel, qtbot) -> None:
    bubble = panel.append_message("pre-show", "assistant", "高 DPI 宽度回归")
    bubble.set_bubble_width(600)

    panel.show()

    qtbot.waitUntil(lambda: bubble.width() <= panel.scroll_area.viewport().width())


def test_show_focuses_input_and_escape_hides_without_destroying(panel, qtbot) -> None:
    hidden: list[bool] = []
    panel.hide_requested.connect(lambda: hidden.append(True))
    panel.show_and_focus()
    qtbot.waitUntil(panel.isVisible)
    qtbot.waitUntil(panel.input.hasFocus)

    qtbot.keyClick(panel.input, Qt.Key.Key_Escape)

    qtbot.waitUntil(lambda: not panel.isVisible())
    assert hidden == [True]
    assert panel.input is not None


def test_outside_focus_change_does_not_hide_panel(panel, qtbot) -> None:
    other = QWidget()
    qtbot.addWidget(other)
    panel.show_and_focus()
    other.show()
    qtbot.waitUntil(panel.isVisible)

    other.activateWindow()
    other.setFocus()
    qtbot.wait(20)

    assert panel.isVisible()


def test_enter_sends_original_text_and_shift_enter_inserts_newline(panel, qtbot) -> None:
    sent: list[str] = []

    def accept_send(text: str) -> None:
        sent.append(text)
        panel.set_conversation_state(State.SENDING)

    panel.send_requested.connect(accept_send)
    panel.show_and_focus()
    panel.input.setPlainText("  保留两侧空格  ")

    qtbot.keyClick(panel.input, Qt.Key.Key_Return)

    assert sent == ["  保留两侧空格  "]
    assert panel.input.toPlainText() == ""
    assert panel.conversation_active

    panel.set_conversation_state(State.IDLE)
    panel.input.setPlainText("第一行")
    panel.input.moveCursor(panel.input.textCursor().MoveOperation.End)
    qtbot.keyClick(panel.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    qtbot.keyClicks(panel.input, "second")

    assert panel.input.toPlainText() == "第一行\nsecond"
    assert len(sent) == 1


def test_whitespace_and_rejected_send_are_safe(panel, qtbot) -> None:
    sent: list[str] = []
    panel.send_requested.connect(sent.append)
    panel.show_and_focus()

    panel.input.setPlainText(" \n\t ")
    qtbot.keyClick(panel.input, Qt.Key.Key_Return)
    assert sent == []

    panel.input.setPlainText("保留这条未接受的输入")
    qtbot.keyClick(panel.input, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: panel.action_button.isEnabled())

    assert sent == ["保留这条未接受的输入"]
    assert panel.input.toPlainText() == "保留这条未接受的输入"
    assert not panel.conversation_active


def test_active_enter_does_not_send_or_stop_and_button_stops_once(panel, qtbot) -> None:
    sent: list[str] = []
    stopped: list[bool] = []
    panel.send_requested.connect(sent.append)
    panel.stop_requested.connect(lambda: stopped.append(True))
    panel.show_and_focus()
    panel.input.setPlainText("下一条草稿")
    panel.set_conversation_state(State.STREAMING)

    qtbot.keyClick(panel.input, Qt.Key.Key_Return)
    assert sent == []
    assert stopped == []

    qtbot.mouseClick(panel.action_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(panel.action_button, Qt.MouseButton.LeftButton)

    assert stopped == [True]
    assert panel.action_button.text() == "正在停止…"
    assert not panel.action_button.isEnabled()


def test_message_apis_update_prepend_and_reject_duplicate_ids(panel, qtbot) -> None:
    panel.append_message("m2", "assistant", "第二条")
    panel.update_message("m2", text="第二条增量", status="streaming")
    panel.prepend_messages(
        [
            {"message_id": "m0", "role": "user", "content": "更早的第一条"},
            {"message_id": "m1", "role": "assistant", "content": "更早的第二条"},
        ]
    )
    qtbot.wait(10)

    assert panel.message_ids == ("m0", "m1", "m2")
    assert panel.message_widget("m2").text == "第二条增量"
    assert panel.message_widget("m2").status_label.text() == "正在回复"
    assert not panel.empty_state.isVisible()
    with pytest.raises(ValueError):
        panel.append_message("m2", "assistant", "重复")

    panel.clear_messages()
    assert panel.message_ids == ()
    assert not panel.empty_state.isHidden()


def test_render_turn_is_idempotent_and_retry_emits_turn_id(panel, qtbot) -> None:
    panel.show()
    qtbot.waitUntil(panel.isVisible)
    initial = Turn(
        "turn-7",
        Message("user-7", "user", "问题", "completed"),
        Message("assistant-7", "assistant", "部分", "streaming"),
    )
    failed = Turn(
        "turn-7",
        initial.user_message,
        Message("assistant-7", "assistant", "部分回答", "failed"),
        terminal_reason="stream_timeout",
        status_text="模拟流中停滞超时",
        error="internal detail is not rendered",
    )

    panel.render_turn(initial)
    panel.render_turn(failed)

    assert panel.message_ids == ("user-7", "assistant-7")
    assistant = panel.message_widget("assistant-7")
    assert assistant.text == "部分回答"
    assert assistant.status_label.text() == "模拟流中停滞超时"
    qtbot.waitUntil(assistant.retry_button.isVisible)

    retried: list[str] = []
    panel.retry_requested.connect(retried.append)
    qtbot.mouseClick(assistant.retry_button, Qt.MouseButton.LeftButton)
    assert retried == ["turn-7"]

    panel.set_conversation_state(State.STREAMING)
    assert not assistant.retry_button.isEnabled()
    qtbot.mouseClick(assistant.retry_button, Qt.MouseButton.LeftButton)
    assert retried == ["turn-7"]

    panel.set_conversation_state(State.IDLE)
    assert assistant.retry_button.isEnabled()


@pytest.mark.parametrize(
    "text",
    [
        "很长的中文消息" * 100,
        "This is a long English sentence. " * 80,
        "中文 mixed English 12345 " * 80,
        "X" * 1200,
        "第一行\n\n\n第五行\n" + "继续" * 200,
    ],
)
def test_long_selectable_text_wraps_anywhere_without_horizontal_overflow(
    panel,
    qtbot,
    text: str,
) -> None:
    panel.resize_for_work_area(QRect(0, 0, 1920, 1040))
    panel.show()
    bubble = panel.append_message("message", "assistant", text)
    qtbot.wait(10)

    assert bubble.width() <= panel.scroll_area.viewport().width()
    assert bubble.text_view.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert bubble.text_view.wordWrapMode() == QTextOption.WrapMode.WrapAnywhere
    assert bubble.text_view.horizontalScrollBar().maximum() == 0
    assert bubble.text_view.toPlainText() == text


def test_incremental_updates_follow_tail_only_when_user_is_near_bottom(panel, qtbot) -> None:
    panel.resize_for_work_area(QRect(0, 0, 1000, 520))
    panel.show()
    for index in range(24):
        panel.append_message(f"m{index}", "assistant", f"消息 {index}\n" * 3)
    qtbot.wait(30)
    scroll_bar = panel.scroll_area.verticalScrollBar()
    assert scroll_bar.maximum() > 0

    scroll_bar.setValue(0)
    panel.update_message("m23", text="末尾增量" * 100)
    qtbot.wait(20)
    assert scroll_bar.value() == 0

    scroll_bar.setValue(scroll_bar.maximum())
    panel.update_message("m23", text="继续增量" * 200)
    qtbot.wait(20)
    assert scroll_bar.value() == scroll_bar.maximum()


def test_prepend_preserves_existing_viewport(panel, qtbot) -> None:
    panel.resize_for_work_area(QRect(0, 0, 1000, 520))
    panel.show()
    for index in range(12):
        panel.append_message(f"m{index}", "assistant", f"现有消息 {index}\n" * 3)
    qtbot.wait(30)
    scroll_bar = panel.scroll_area.verticalScrollBar()
    scroll_bar.setValue(scroll_bar.maximum() // 2)
    old_value = scroll_bar.value()

    panel.prepend_messages(
        [
            {"message_id": f"old{index}", "role": "user", "content": "更早消息\n" * 3}
            for index in range(5)
        ]
    )
    qtbot.wait(30)

    assert scroll_bar.value() > old_value
    assert panel.message_ids[:5] == tuple(f"old{index}" for index in range(5))
