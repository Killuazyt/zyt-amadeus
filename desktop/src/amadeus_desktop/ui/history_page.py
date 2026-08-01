"""Conversation history page backed by application-layer view data and signals."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

_MISSING = object()
_ROLE_LABELS = {"user": "你", "assistant": "Amadeus", "system": "系统"}
_STATUS_LABELS = {
    "pending": "等待中",
    "streaming": "生成中",
    "completed": "已完成",
    "user_stopped": "用户已停止",
    "stopped": "用户已停止",
    "shutdown": "应用退出时已停止",
    "failed": "失败",
}


class HistoryPage(QWidget):
    """Inspect and manage conversations without depending on a storage implementation."""

    refresh_requested = Signal()
    conversation_selected = Signal(str)
    new_conversation_requested = Signal()
    rename_conversation_requested = Signal(str, str)
    delete_conversation_requested = Signal(str)
    clear_history_requested = Signal()
    load_older_messages_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("historyPage")
        self._conversation_titles: dict[str, str] = {}
        self._conversation_id: str | None = None
        self._message_rows: list[dict[str, str]] = []

        heading = QLabel("聊天历史")
        heading.setObjectName("settingsPageHeading")
        explanation = QLabel(
            "聊天正文保存在本机。删除聊天不会删除长期记忆；记忆来源会显示为已删除。"
        )
        explanation.setWordWrap(True)

        self.refresh_button = QPushButton("刷新")
        self.new_button = QPushButton("新建会话")
        self.rename_button = QPushButton("重命名")
        self.delete_button = QPushButton("删除会话")
        self.clear_button = QPushButton("清空全部聊天")

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.new_button)
        toolbar.addWidget(self.rename_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.clear_button)

        self.conversation_list = QListWidget()
        self.conversation_list.setObjectName("conversationList")
        self.conversation_list.setAccessibleName("会话列表")
        self.conversation_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.conversation_list.setMinimumWidth(230)

        conversation_column = QWidget()
        conversation_layout = QVBoxLayout(conversation_column)
        conversation_layout.setContentsMargins(0, 0, 0, 0)
        conversation_layout.addWidget(QLabel("会话"))
        conversation_layout.addWidget(self.conversation_list, 1)

        self.current_title_label = QLabel("请选择会话")
        self.current_title_label.setObjectName("historyConversationTitle")
        self.load_older_button = QPushButton("加载更早的 40 条")
        self.load_older_button.setVisible(False)

        message_header = QHBoxLayout()
        message_header.addWidget(self.current_title_label, 1)
        message_header.addWidget(self.load_older_button)

        self.message_view = QTreeWidget()
        self.message_view.setObjectName("historyMessageView")
        self.message_view.setAccessibleName("会话消息")
        self.message_view.setHeaderLabels(["角色", "时间", "状态", "内容"])
        self.message_view.setRootIsDecorated(False)
        self.message_view.setAlternatingRowColors(True)
        self.message_view.setWordWrap(True)
        self.message_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.message_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.message_view.setUniformRowHeights(False)
        self.message_view.header().setStretchLastSection(True)

        self.message_empty_label = QLabel("选择会话后可查看消息。")
        self.message_empty_label.setObjectName("historyEmptyState")
        self.message_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_empty_label.setWordWrap(True)

        message_column = QWidget()
        message_layout = QVBoxLayout(message_column)
        message_layout.setContentsMargins(0, 0, 0, 0)
        message_layout.addLayout(message_header)
        message_layout.addWidget(self.message_view, 1)
        message_layout.addWidget(self.message_empty_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("historySplitter")
        splitter.addWidget(conversation_column)
        splitter.addWidget(message_column)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self.status_label = QLabel()
        self.status_label.setObjectName("historyStatus")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(explanation)
        layout.addLayout(toolbar)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status_label)

        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.new_button.clicked.connect(self.new_conversation_requested.emit)
        self.rename_button.clicked.connect(self._request_rename)
        self.delete_button.clicked.connect(self._request_delete)
        self.clear_button.clicked.connect(self._request_clear)
        self.load_older_button.clicked.connect(self._request_older_messages)
        self.conversation_list.currentItemChanged.connect(self._on_conversation_changed)
        self._sync_actions()
        self._render_messages()

    @property
    def current_conversation_id(self) -> str | None:
        return self._conversation_id

    @property
    def message_ids(self) -> tuple[str, ...]:
        return tuple(row["message_id"] for row in self._message_rows)

    def set_conversations(
        self,
        conversations: Iterable[object],
        selected_id: str | None = None,
    ) -> None:
        """Replace the conversation list without treating the refresh as user input."""

        previous_id = selected_id or self.current_conversation_id
        rows = [_conversation_row(conversation) for conversation in conversations]
        self._conversation_titles = {row["conversation_id"]: row["title"] for row in rows}
        with QSignalBlocker(self.conversation_list):
            self.conversation_list.clear()
            selected_item: QListWidgetItem | None = None
            for row in rows:
                item = QListWidgetItem(_conversation_item_text(row))
                item.setData(Qt.ItemDataRole.UserRole, row["conversation_id"])
                item.setToolTip(row["title"])
                self.conversation_list.addItem(item)
                if row["conversation_id"] == previous_id:
                    selected_item = item
            if selected_item is not None:
                self.conversation_list.setCurrentItem(selected_item)
            elif self.conversation_list.count():
                self.conversation_list.setCurrentRow(0)

        selected = self.conversation_list.currentItem()
        self._conversation_id = _item_id(selected)
        self.current_title_label.setText(
            self._conversation_titles.get(self._conversation_id or "", "请选择会话")
        )
        if not rows:
            self._message_rows = []
            self._render_messages()
        self._sync_actions()

    def select_conversation(self, conversation_id: str, *, emit: bool = False) -> bool:
        for index in range(self.conversation_list.count()):
            item = self.conversation_list.item(index)
            if _item_id(item) != conversation_id:
                continue
            if emit:
                self.conversation_list.setCurrentItem(item)
            else:
                with QSignalBlocker(self.conversation_list):
                    self.conversation_list.setCurrentItem(item)
                self._conversation_id = conversation_id
                self.current_title_label.setText(
                    self._conversation_titles.get(conversation_id, "请选择会话")
                )
                self._sync_actions()
            return True
        return False

    def set_messages(
        self,
        conversation_id: str,
        messages: Iterable[object],
        *,
        prepend: bool = False,
        has_older: bool = False,
    ) -> None:
        """Render one page of messages; prepending is idempotent by stable message ID."""

        rows = [_message_row(message) for message in messages]
        if prepend and conversation_id == self._conversation_id:
            existing_ids = {row["message_id"] for row in self._message_rows}
            rows = [row for row in rows if row["message_id"] not in existing_ids]
            self._message_rows = rows + self._message_rows
        else:
            self._conversation_id = conversation_id
            self._message_rows = rows
            self.select_conversation(conversation_id)
        self.load_older_button.setVisible(has_older)
        self._render_messages()
        self._sync_actions()

    def focus_message(self, message_id: str) -> bool:
        for index in range(self.message_view.topLevelItemCount()):
            item = self.message_view.topLevelItem(index)
            if str(item.data(0, Qt.ItemDataRole.UserRole)) != message_id:
                continue
            self.message_view.setCurrentItem(item)
            self.message_view.scrollToItem(item)
            return True
        return False

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("error", error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    @Slot(QListWidgetItem, QListWidgetItem)
    def _on_conversation_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        del previous
        conversation_id = _item_id(current)
        self._conversation_id = conversation_id
        self._message_rows = []
        self.load_older_button.setVisible(False)
        self.current_title_label.setText(
            self._conversation_titles.get(conversation_id or "", "请选择会话")
        )
        self._render_messages()
        self._sync_actions()
        if conversation_id is not None:
            self.conversation_selected.emit(conversation_id)

    @Slot()
    def _request_rename(self) -> None:
        conversation_id = self.current_conversation_id
        if conversation_id is None:
            return
        current_title = self._conversation_titles.get(conversation_id, "")
        title, accepted = QInputDialog.getText(
            self,
            "重命名会话",
            "会话名称：",
            text=current_title,
        )
        title = title.strip()
        if accepted and title and title != current_title:
            self.rename_conversation_requested.emit(conversation_id, title)

    @Slot()
    def _request_delete(self) -> None:
        conversation_id = self.current_conversation_id
        if conversation_id is None:
            return
        title = self._conversation_titles.get(conversation_id, "当前会话")
        answer = QMessageBox.question(
            self,
            "永久删除会话？",
            f"将永久删除“{title}”的全部聊天正文。\n\n"
            "长期记忆不会随之删除，但相关来源将显示为已删除。此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_conversation_requested.emit(conversation_id)

    @Slot()
    def _request_clear(self) -> None:
        answer = QMessageBox.question(
            self,
            "清空全部聊天？",
            "将永久删除所有会话及聊天正文。\n\n"
            "长期记忆不会随之删除，但相关来源将显示为已删除。此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.clear_history_requested.emit()

    @Slot()
    def _request_older_messages(self) -> None:
        if self.current_conversation_id is not None:
            self.load_older_messages_requested.emit(self.current_conversation_id)

    def _render_messages(self) -> None:
        with QSignalBlocker(self.message_view):
            self.message_view.clear()
            for row in self._message_rows:
                item = QTreeWidgetItem(
                    [row["role"], row["created_at"], row["status"], row["content"]]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, row["message_id"])
                item.setToolTip(3, row["content"])
                self.message_view.addTopLevelItem(item)
        has_messages = bool(self._message_rows)
        self.message_view.setVisible(has_messages)
        if has_messages:
            self.message_empty_label.hide()
        else:
            self.message_empty_label.setText(
                "此会话还没有消息。" if self._conversation_id else "选择会话后可查看消息。"
            )
            self.message_empty_label.show()
        for column in range(3):
            self.message_view.resizeColumnToContents(column)

    def _sync_actions(self) -> None:
        selected = self.current_conversation_id is not None
        self.rename_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)
        self.load_older_button.setEnabled(selected)
        self.clear_button.setEnabled(self.conversation_list.count() > 0)


def _conversation_row(value: object) -> dict[str, str]:
    conversation_id = str(_member(value, "conversation_id", "id"))
    title = str(_member(value, "title", "name", default="未命名会话")).strip()
    updated_at = _display_value(
        _member(value, "last_activity_at", "updated_at", "created_at", default="")
    )
    count = _member(value, "message_count", "count", default="")
    return {
        "conversation_id": conversation_id,
        "title": title or "未命名会话",
        "updated_at": updated_at,
        "message_count": "" if count in (None, "") else str(count),
    }


def _conversation_item_text(row: Mapping[str, str]) -> str:
    details = [value for value in (row["updated_at"], row["message_count"]) if value]
    if row["message_count"]:
        details[-1] = f"{row['message_count']} 条消息"
    return row["title"] if not details else f"{row['title']}\n{' · '.join(details)}"


def _message_row(value: object) -> dict[str, str]:
    role = _enum_text(_member(value, "role", default="assistant")).lower()
    status = _enum_text(_member(value, "status", default="")).lower()
    terminal_reason = _enum_text(_member(value, "terminal_reason", default="")).lower()
    status_key = "shutdown" if status == "stopped" and terminal_reason == "shutdown" else status
    return {
        "message_id": str(_member(value, "message_id", "id")),
        "role": _ROLE_LABELS.get(role, role or "未知"),
        "created_at": _display_value(_member(value, "created_at", "timestamp", default="")),
        "status": _STATUS_LABELS.get(status_key, status_key),
        "content": str(_member(value, "content", "text", default="")),
    }


def _item_id(item: QListWidgetItem | None) -> str | None:
    if item is None:
        return None
    value = item.data(Qt.ItemDataRole.UserRole)
    return None if value is None else str(value)


def _display_value(value: object) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat(sep=" ", timespec="seconds"))
        except TypeError:
            return str(isoformat())
    return str(value)


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


def _member(value: object, *names: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    else:
        for name in names:
            if hasattr(value, name):
                return getattr(value, name)
    if default is not _MISSING:
        return default
    joined = ", ".join(names)
    raise ValueError(f"History view data is missing one of these fields: {joined}")
