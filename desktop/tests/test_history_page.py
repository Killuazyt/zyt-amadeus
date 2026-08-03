from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox

from amadeus_desktop.ui.history_page import HistoryPage


@dataclass(frozen=True)
class ConversationView:
    conversation_id: str
    title: str
    updated_at: str
    message_count: int


def make_page(qtbot) -> HistoryPage:
    page = HistoryPage()
    qtbot.addWidget(page)
    return page


def test_refresh_is_signal_safe_and_duck_typed_messages_prepend_idempotently(qtbot) -> None:
    page = make_page(qtbot)
    selected: list[str] = []
    page.conversation_selected.connect(selected.append)

    page.set_conversations(
        [
            ConversationView("c1", "咖啡", "2026-08-01 10:00", 2),
            {"id": "c2", "name": "工作", "last_activity_at": "2026-08-01 11:00"},
        ]
    )

    assert selected == []
    assert page.current_conversation_id == "c1"
    assert "咖啡" in page.conversation_list.item(0).text()

    page.set_messages(
        "c1",
        [{"id": "m2", "role": "assistant", "text": "第二条", "status": "completed"}],
        has_older=True,
    )
    page.set_messages(
        "c1",
        [
            {"message_id": "m0", "role": "user", "content": "更早第一条"},
            {"message_id": "m1", "role": "assistant", "content": "更早第二条"},
            {"message_id": "m2", "role": "assistant", "content": "重复"},
        ],
        prepend=True,
        has_older=False,
    )

    assert page.message_ids == ("m0", "m1", "m2")
    assert page.focus_message("m1")
    assert not page.focus_message("missing")
    assert page.message_view.currentItem().text(3) == "更早第二条"
    assert page.load_older_button.isHidden()


def test_user_selection_and_non_destructive_toolbar_actions_emit_stable_ids(qtbot) -> None:
    page = make_page(qtbot)
    page.set_conversations(
        [
            {"conversation_id": "c1", "title": "第一段"},
            {"conversation_id": "c2", "title": "第二段"},
        ]
    )
    selected: list[str] = []
    refreshed: list[bool] = []
    created: list[bool] = []
    older: list[str] = []
    exports: list[bool] = []
    page.conversation_selected.connect(selected.append)
    page.refresh_requested.connect(lambda: refreshed.append(True))
    page.new_conversation_requested.connect(lambda: created.append(True))
    page.load_older_messages_requested.connect(older.append)
    page.export_requested.connect(lambda: exports.append(True))

    page.conversation_list.setCurrentRow(1)
    page.refresh_button.click()
    page.new_button.click()
    page.set_messages("c2", [], has_older=True)
    page.load_older_button.click()
    page.export_button.click()

    assert selected == ["c2"]
    assert refreshed == [True]
    assert created == [True]
    assert older == ["c2"]
    assert exports == [True]


def test_shutdown_recovery_is_not_labeled_as_user_stop(qtbot) -> None:
    page = make_page(qtbot)
    page.set_conversations([{"id": "c1", "title": "恢复会话"}])
    page.set_messages(
        "c1",
        [
            {
                "id": "assistant-shutdown",
                "role": "assistant",
                "content": "应用退出前的部分回复",
                "status": "stopped",
                "terminal_reason": "shutdown",
            }
        ],
    )

    assert page.message_view.topLevelItem(0).text(2) == "应用退出时已停止"


def test_rename_trims_title_and_ignores_cancel_or_unchanged(monkeypatch, qtbot) -> None:
    page = make_page(qtbot)
    page.set_conversations([{"id": "c1", "title": "旧名称"}])
    renamed: list[tuple[str, str]] = []
    page.rename_conversation_requested.connect(
        lambda conversation_id, title: renamed.append((conversation_id, title))
    )

    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("  新名称  ", True))
    qtbot.mouseClick(page.rename_button, Qt.MouseButton.LeftButton)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("旧名称", True))
    page.rename_button.click()
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("取消", False))
    page.rename_button.click()

    assert renamed == [("c1", "新名称")]


def test_delete_and_clear_require_explicit_confirmation(monkeypatch, qtbot) -> None:
    page = make_page(qtbot)
    page.set_conversations([{"id": "c1", "title": "私人会话"}])
    deleted: list[str] = []
    cleared: list[bool] = []
    prompts: list[str] = []
    page.delete_conversation_requested.connect(deleted.append)
    page.clear_history_requested.connect(lambda: cleared.append(True))

    def decline(*args, **kwargs):
        prompts.append(str(args[2]))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    page.delete_button.click()
    page.clear_button.click()
    assert deleted == []
    assert cleared == []

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    page.delete_button.click()
    page.clear_button.click()

    assert deleted == ["c1"]
    assert cleared == [True]
    assert all("长期记忆" in prompt and "无法撤销" in prompt for prompt in prompts)
