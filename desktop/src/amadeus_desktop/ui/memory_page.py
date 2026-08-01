"""Auditable long-term memory management page for P5A."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

_MISSING = object()
_TYPE_LABELS = {
    "fact": "事实",
    "preference": "偏好",
    "event": "事件",
    "relationship": "关系状态",
    "relationship_state": "关系状态",
}
_STATUS_LABELS = {
    "active": "有效",
    "archived": "已归档",
    "superseded": "已替代",
    "deleted": "已删除",
}
_TASK_TYPE_LABELS = {
    "extract_memory": "记忆提炼",
    "memory_extraction": "记忆提炼",
    "summarize_conversation": "会话摘要",
    "conversation_summary": "会话摘要",
}
_METHOD_LABELS = {
    "automatic": "自动提炼",
    "manual": "手工编辑",
}


class MemoryPage(QWidget):
    """View/edit memory DTOs while delegating all persistence to application services."""

    refresh_requested = Signal()
    enabled_changed = Signal(bool)
    search_requested = Signal(str, str, str, str)
    edit_requested = Signal(str, str)
    pin_requested = Signal(str, bool)
    archive_requested = Signal(str)
    restore_requested = Signal(str)
    delete_requested = Signal(str)
    source_requested = Signal(str, str)
    retry_task_requested = Signal(str)
    memory_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("memoryPage")
        self._memories: dict[str, dict[str, Any]] = {}
        self._memory_order: list[str] = []
        self._sources_by_memory: dict[str, list[dict[str, Any]]] = {}

        heading = QLabel("长期记忆")
        heading.setObjectName("settingsPageHeading")
        explanation = QLabel(
            "这里只显示从用户消息提炼的本地记忆。编辑会创建用户优先的新版本；"
            "删除记忆不会删除原始聊天。"
        )
        explanation.setWordWrap(True)

        self.enabled_check = QCheckBox("启用长期记忆提炼与召回")
        self.enabled_check.setChecked(True)
        self.enabled_notice = QLabel()
        self.enabled_notice.setObjectName("memoryEnabledNotice")
        self.enabled_notice.setWordWrap(True)

        enabled_row = QHBoxLayout()
        enabled_row.addWidget(self.enabled_check)
        enabled_row.addWidget(self.enabled_notice, 1)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("memorySearch")
        self.search_edit.setAccessibleName("搜索长期记忆")
        self.search_edit.setPlaceholderText("搜索记忆内容或主题")
        self.type_combo = QComboBox()
        self.type_combo.setObjectName("memoryTypeFilter")
        for label, value in (
            ("全部类型", ""),
            ("事实", "fact"),
            ("偏好", "preference"),
            ("事件", "event"),
            ("关系状态", "relationship"),
        ):
            self.type_combo.addItem(label, value)
        self.status_combo = QComboBox()
        self.status_combo.setObjectName("memoryStatusFilter")
        for label, value in (
            ("全部状态", ""),
            ("有效", "active"),
            ("已归档", "archived"),
        ):
            self.status_combo.addItem(label, value)
        self.pinned_combo = QComboBox()
        self.pinned_combo.setObjectName("memoryPinnedFilter")
        self.pinned_combo.addItem("全部置顶状态", None)
        self.pinned_combo.addItem("仅置顶", True)
        self.pinned_combo.addItem("仅未置顶", False)
        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("memorySort")
        for label, value in (
            ("最近更新", "updated_desc"),
            ("最近创建", "created_desc"),
            ("置顶优先", "pinned_first"),
            ("重要性最高", "importance_desc"),
            ("置信度最高", "confidence_desc"),
        ):
            self.sort_combo.addItem(label, value)
        self.search_button = QPushButton("搜索")
        self.refresh_button = QPushButton("刷新")

        filters = QHBoxLayout()
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.type_combo)
        filters.addWidget(self.status_combo)
        filters.addWidget(self.pinned_combo)
        filters.addWidget(self.sort_combo)
        filters.addWidget(self.search_button)
        filters.addWidget(self.refresh_button)

        self.memory_table = QTableWidget(0, 7)
        self.memory_table.setObjectName("memoryTable")
        self.memory_table.setAccessibleName("长期记忆列表")
        self.memory_table.setHorizontalHeaderLabels(
            ["置顶", "类型", "状态", "内容", "重要性", "置信度", "更新时间"]
        )
        self.memory_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.memory_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.memory_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.memory_table.setAlternatingRowColors(True)
        self.memory_table.setWordWrap(True)
        self.memory_table.verticalHeader().setVisible(False)
        self.memory_table.horizontalHeader().setStretchLastSection(False)
        self.memory_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        self.detail_title_label = QLabel("请选择一条记忆")
        self.detail_title_label.setObjectName("memoryDetailTitle")
        self.detail_meta_label = QLabel()
        self.detail_meta_label.setObjectName("memoryDetailMeta")
        self.detail_meta_label.setWordWrap(True)
        self.detail_edit = QPlainTextEdit()
        self.detail_edit.setObjectName("memoryContentEditor")
        self.detail_edit.setAccessibleName("记忆内容编辑器")
        self.detail_edit.setPlaceholderText("选择记忆后可编辑内容")
        self.detail_edit.setMinimumHeight(100)

        self.save_edit_button = QPushButton("保存修改")
        self.pin_button = QPushButton("置顶")
        self.archive_button = QPushButton("归档")
        self.restore_button = QPushButton("恢复")
        self.delete_button = QPushButton("永久删除")

        memory_actions = QHBoxLayout()
        memory_actions.addWidget(self.save_edit_button)
        memory_actions.addWidget(self.pin_button)
        memory_actions.addWidget(self.archive_button)
        memory_actions.addWidget(self.restore_button)
        memory_actions.addStretch(1)
        memory_actions.addWidget(self.delete_button)

        self.source_list = QListWidget()
        self.source_list.setObjectName("memorySourceList")
        self.source_list.setAccessibleName("记忆来源列表")
        self.source_list.setMinimumHeight(90)
        self.source_preview = QPlainTextEdit()
        self.source_preview.setObjectName("memorySourcePreview")
        self.source_preview.setReadOnly(True)
        self.source_preview.setPlaceholderText("选择来源可查看本地正文；已删除来源不保留正文。")
        self.source_preview.setMaximumHeight(100)
        self.open_source_button = QPushButton("跳转到来源消息")

        sources_group = QGroupBox("来源")
        sources_layout = QVBoxLayout(sources_group)
        sources_layout.addWidget(self.source_list)
        sources_layout.addWidget(self.source_preview)
        sources_layout.addWidget(self.open_source_button)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(8, 0, 0, 0)
        detail_layout.addWidget(self.detail_title_label)
        detail_layout.addWidget(self.detail_meta_label)
        detail_layout.addWidget(self.detail_edit)
        detail_layout.addLayout(memory_actions)
        detail_layout.addWidget(sources_group, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("memorySplitter")
        splitter.addWidget(self.memory_table)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.failed_task_view = QTreeWidget()
        self.failed_task_view.setObjectName("failedMemoryTasks")
        self.failed_task_view.setHeaderLabels(["任务", "尝试次数", "最近错误"])
        self.failed_task_view.setRootIsDecorated(False)
        self.failed_task_view.setAlternatingRowColors(True)
        self.failed_task_view.header().setStretchLastSection(True)
        self.failed_task_view.setMinimumHeight(90)
        self.retry_task_button = QPushButton("重试选中的失败任务")

        failed_group = QGroupBox("需要处理的后台任务")
        failed_layout = QVBoxLayout(failed_group)
        failed_layout.addWidget(self.failed_task_view)
        failed_layout.addWidget(self.retry_task_button)

        self.status_label = QLabel()
        self.status_label.setObjectName("memoryStatus")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(explanation)
        layout.addLayout(enabled_row)
        layout.addLayout(filters)
        layout.addWidget(splitter, 1)
        layout.addWidget(failed_group)
        layout.addWidget(self.status_label)

        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        self.search_button.clicked.connect(self._emit_search)
        self.search_edit.returnPressed.connect(self._emit_search)
        self.type_combo.currentIndexChanged.connect(self._emit_search)
        self.status_combo.currentIndexChanged.connect(self._emit_search)
        self.pinned_combo.currentIndexChanged.connect(self._emit_search)
        self.sort_combo.currentIndexChanged.connect(self._emit_search)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.memory_table.itemSelectionChanged.connect(self._on_memory_selection_changed)
        self.save_edit_button.clicked.connect(self._request_edit)
        self.pin_button.clicked.connect(self._request_pin)
        self.archive_button.clicked.connect(self._request_archive)
        self.restore_button.clicked.connect(self._request_restore)
        self.delete_button.clicked.connect(self._request_delete)
        self.source_list.currentItemChanged.connect(self._on_source_selection_changed)
        self.source_list.itemDoubleClicked.connect(self._open_source)
        self.open_source_button.clicked.connect(self._open_current_source)
        self.failed_task_view.itemSelectionChanged.connect(self._sync_task_action)
        self.retry_task_button.clicked.connect(self._request_task_retry)
        self._sync_enabled_notice()
        self._sync_memory_detail()
        self._sync_task_action()

    @property
    def current_memory_id(self) -> str | None:
        selected = self.memory_table.selectionModel().selectedRows()
        if not selected:
            return None
        item = self.memory_table.item(selected[0].row(), 0)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return None if value is None else str(value)

    @property
    def pinned_filter(self) -> bool | None:
        value = self.pinned_combo.currentData()
        return value if isinstance(value, bool) else None

    def set_memory_enabled(self, enabled: bool) -> None:
        with QSignalBlocker(self.enabled_check):
            self.enabled_check.setChecked(enabled)
        self._sync_enabled_notice()

    def set_memories(
        self,
        memories: Iterable[object],
        selected_id: str | None = None,
    ) -> None:
        """Replace result rows without emitting user search or mutation signals."""

        previous_id = selected_id or self.current_memory_id
        values = list(memories)
        rows = [_memory_row(memory) for memory in values]
        self._memories = {row["memory_id"]: row for row in rows}
        self._memory_order = [row["memory_id"] for row in rows]
        for memory, row in zip(values, rows, strict=True):
            raw_sources = _member(memory, "sources", default=None)
            if raw_sources is not None:
                self._sources_by_memory[row["memory_id"]] = [
                    _source_row(source) for source in raw_sources
                ]

        with QSignalBlocker(self.memory_table):
            self.memory_table.setRowCount(len(rows))
            selected_row = -1
            for row_index, row in enumerate(rows):
                cells = (
                    "是" if row["pinned"] else "",
                    row["type_label"],
                    row["status_label"],
                    row["content"],
                    _score_text(row["importance"]),
                    _score_text(row["confidence"]),
                    row["updated_at"],
                )
                for column, value in enumerate(cells):
                    item = QTableWidgetItem(value)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, row["memory_id"])
                    item.setToolTip(row["content"] if column == 3 else value)
                    self.memory_table.setItem(row_index, column, item)
                if row["memory_id"] == previous_id:
                    selected_row = row_index
            if selected_row >= 0:
                self.memory_table.selectRow(selected_row)
            elif rows:
                self.memory_table.selectRow(0)
        self.memory_table.resizeColumnsToContents()
        self.memory_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._sync_memory_detail()

    def select_memory(self, memory_id: str) -> bool:
        try:
            row = self._memory_order.index(memory_id)
        except ValueError:
            return False
        with QSignalBlocker(self.memory_table):
            self.memory_table.selectRow(row)
        self._sync_memory_detail()
        return True

    def set_sources(self, memory_id: str, sources: Iterable[object]) -> None:
        self._sources_by_memory[memory_id] = [_source_row(source) for source in sources]
        if memory_id == self.current_memory_id:
            self._render_sources(memory_id)

    def set_failed_tasks(self, tasks: Iterable[object]) -> None:
        with QSignalBlocker(self.failed_task_view):
            self.failed_task_view.clear()
            for task in tasks:
                row = _task_row(task)
                item = QTreeWidgetItem(
                    [row["task_type_label"], str(row["attempts"]), row["error_summary"]]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, row["task_id"])
                item.setToolTip(2, row["error_summary"])
                self.failed_task_view.addTopLevelItem(item)
        self._sync_task_action()

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("error", error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    @Slot(bool)
    def _on_enabled_toggled(self, enabled: bool) -> None:
        self._sync_enabled_notice()
        self.enabled_changed.emit(enabled)

    @Slot()
    def _emit_search(self, *_args: object) -> None:
        self.search_requested.emit(
            self.search_edit.text().strip(),
            str(self.type_combo.currentData() or ""),
            str(self.status_combo.currentData() or ""),
            str(self.sort_combo.currentData() or "updated_desc"),
        )

    @Slot()
    def _on_memory_selection_changed(self) -> None:
        self._sync_memory_detail()
        memory_id = self.current_memory_id
        if memory_id is not None:
            self.memory_selected.emit(memory_id)

    @Slot()
    def _request_edit(self) -> None:
        memory_id = self.current_memory_id
        if memory_id is None:
            return
        content = self.detail_edit.toPlainText().strip()
        if not content:
            self.set_status("记忆内容不能为空。", error=True)
            return
        if content == self._memories[memory_id]["content"]:
            self.set_status("记忆内容没有变化。")
            return
        self.edit_requested.emit(memory_id, content)

    @Slot()
    def _request_pin(self) -> None:
        memory_id = self.current_memory_id
        if memory_id is None:
            return
        self.pin_requested.emit(memory_id, not bool(self._memories[memory_id]["pinned"]))

    @Slot()
    def _request_archive(self) -> None:
        if self.current_memory_id is not None:
            self.archive_requested.emit(self.current_memory_id)

    @Slot()
    def _request_restore(self) -> None:
        if self.current_memory_id is not None:
            self.restore_requested.emit(self.current_memory_id)

    @Slot()
    def _request_delete(self) -> None:
        memory_id = self.current_memory_id
        if memory_id is None:
            return
        answer = QMessageBox.question(
            self,
            "永久删除记忆？",
            "将永久删除这条记忆的全部版本、来源关联和检索索引。\n\n"
            "原始聊天不会随之删除。此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(memory_id)

    @Slot(QListWidgetItem, QListWidgetItem)
    def _on_source_selection_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        del previous
        source = _source_item_data(current)
        if source is None:
            self.source_preview.clear()
            self.open_source_button.setEnabled(False)
            return
        self.source_preview.setPlainText(source["content"])
        self.open_source_button.setEnabled(bool(source["available"] and source["message_id"]))

    @Slot(QListWidgetItem)
    def _open_source(self, item: QListWidgetItem) -> None:
        source = _source_item_data(item)
        if source is not None:
            self._emit_source(source)

    @Slot()
    def _open_current_source(self) -> None:
        source = _source_item_data(self.source_list.currentItem())
        if source is not None:
            self._emit_source(source)

    @Slot()
    def _request_task_retry(self) -> None:
        item = self.failed_task_view.currentItem()
        if item is None:
            return
        task_id = item.data(0, Qt.ItemDataRole.UserRole)
        if task_id is not None:
            self.retry_task_requested.emit(str(task_id))

    def _emit_source(self, source: Mapping[str, Any]) -> None:
        if source["available"] and source["conversation_id"] and source["message_id"]:
            self.source_requested.emit(source["conversation_id"], source["message_id"])

    def _sync_enabled_notice(self) -> None:
        if self.enabled_check.isChecked():
            self.enabled_notice.setText("已启用：新对话可提炼并召回长期记忆。")
        else:
            self.enabled_notice.setText("已停用：不会提炼或召回；现有数据保持不变。")

    def _sync_memory_detail(self) -> None:
        memory_id = self.current_memory_id
        memory = self._memories.get(memory_id or "")
        enabled = memory is not None
        for widget in (
            self.detail_edit,
            self.save_edit_button,
            self.pin_button,
            self.archive_button,
            self.restore_button,
            self.delete_button,
        ):
            widget.setEnabled(enabled)
        if memory is None:
            self.detail_title_label.setText("请选择一条记忆")
            self.detail_meta_label.clear()
            self.detail_edit.clear()
            self.archive_button.setVisible(True)
            self.restore_button.setVisible(False)
            self.source_list.clear()
            self.source_preview.clear()
            self.open_source_button.setEnabled(False)
            return
        self.detail_title_label.setText(
            f"{memory['type_label']} · {memory['status_label']} · 版本 {memory['version']}"
        )
        self.detail_meta_label.setText(
            f"主题：{memory['topic_key'] or '未设置'}　"
            f"重要性：{_score_text(memory['importance'])}　"
            f"置信度：{_score_text(memory['confidence'])}\n"
            f"创建：{memory['created_at'] or '未知'}　"
            f"更新：{memory['updated_at'] or '未知'}"
        )
        self.detail_edit.setPlainText(memory["content"])
        self.pin_button.setText("取消置顶" if memory["pinned"] else "置顶")
        archived = memory["status"] == "archived"
        self.archive_button.setVisible(not archived)
        self.restore_button.setVisible(archived)
        self._render_sources(memory["memory_id"])

    def _render_sources(self, memory_id: str) -> None:
        sources = self._sources_by_memory.get(memory_id, [])
        with QSignalBlocker(self.source_list):
            self.source_list.clear()
            for source in sources:
                if source["method"] == "manual":
                    prefix = "手工编辑"
                elif not source["available"]:
                    prefix = "来源已删除"
                else:
                    prefix = "用户消息"
                details = " · ".join(
                    value
                    for value in (
                        f"版本 {source['version_number']}" if source["version_number"] else "",
                        source["created_at"],
                        _METHOD_LABELS.get(source["method"], source["method"]),
                    )
                    if value
                )
                text = prefix if not details else f"{prefix} · {details}"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, source)
                self.source_list.addItem(item)
            if self.source_list.count():
                self.source_list.setCurrentRow(0)
        self._on_source_selection_changed(self.source_list.currentItem(), None)

    def _sync_task_action(self) -> None:
        self.retry_task_button.setEnabled(self.failed_task_view.currentItem() is not None)


def _memory_row(value: object) -> dict[str, Any]:
    record = _member(value, "memory", default=value)
    version = _member(record, "current_version", default=None)
    memory_type = _enum_text(_member(record, "memory_type", "kind", "type", "category")).lower()
    status = _enum_text(_member(record, "status", default="active")).lower()
    memory_id = str(_member(record, "memory_id", "group_id", "id"))
    return {
        "memory_id": memory_id,
        "type": memory_type,
        "type_label": _TYPE_LABELS.get(memory_type, memory_type),
        "status": status,
        "status_label": _STATUS_LABELS.get(status, status),
        "content": str(
            _member(
                record,
                "content",
                "text",
                "normalized_content",
                default=_member(
                    version,
                    "content",
                    "normalized_content",
                    default="",
                ),
            )
        ),
        "topic_key": str(_member(record, "topic_key", "subject_key", default="")),
        "importance": _number(
            _member(record, "importance", default=_member(version, "importance", default=0.0))
        ),
        "confidence": _number(
            _member(record, "confidence", default=_member(version, "confidence", default=0.0))
        ),
        "pinned": bool(_member(record, "pinned", "is_pinned", default=False)),
        "version": str(
            _member(
                record,
                "version",
                "version_number",
                default=_member(version, "version_number", default=1),
            )
        ),
        "created_at": _display_value(_member(record, "created_at", default="")),
        "updated_at": _display_value(_member(record, "updated_at", default="")),
    }


def _source_row(value: object) -> dict[str, Any]:
    method = str(_member(value, "method", "extraction_method", default=""))
    deleted = bool(_member(value, "deleted", "is_deleted", "source_deleted", default=False))
    available = bool(_member(value, "available", default=not deleted)) and not deleted
    content = str(_member(value, "content", "message_content", "excerpt", default="") or "")
    if method == "manual":
        content = "此版本由用户在记忆页手工编辑。"
        available = False
    elif not available:
        content = "来源消息已删除，正文已永久清除。"
    conversation_id = _member(
        value,
        "live_conversation_id",
        "conversation_id",
        "source_conversation_id",
        default="",
    )
    message_id = _member(
        value,
        "live_message_id",
        "message_id",
        "source_message_id",
        default="",
    )
    return {
        "conversation_id": "" if conversation_id is None else str(conversation_id),
        "message_id": "" if message_id is None else str(message_id),
        "content": content,
        "created_at": _display_value(
            _member(value, "message_created_at", "created_at", default="")
        ),
        "method": method,
        "available": available,
        "version_number": int(_member(value, "version_number", default=0) or 0),
    }


def _task_row(value: object) -> dict[str, Any]:
    task_type = _enum_text(_member(value, "task_type", "kind", "type", default="memory_extraction"))
    return {
        "task_id": str(_member(value, "task_id", "job_id", "id")),
        "task_type_label": _TASK_TYPE_LABELS.get(task_type, task_type),
        "attempts": int(_member(value, "attempts", "attempt_count", default=0)),
        "error_summary": str(
            _member(
                value,
                "error_summary",
                "last_error",
                "safe_error",
                "last_error_code",
                default="",
            )
        ),
    }


def _source_item_data(item: QListWidgetItem | None) -> dict[str, Any] | None:
    if item is None:
        return None
    value = item.data(Qt.ItemDataRole.UserRole)
    return value if isinstance(value, dict) else None


def _score_text(value: object) -> str:
    return f"{_number(value):.2f}"


def _number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
    raise ValueError(f"Memory view data is missing one of these fields: {joined}")
