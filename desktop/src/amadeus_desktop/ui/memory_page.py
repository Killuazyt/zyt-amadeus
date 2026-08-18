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
    "reflection": "反思",
    "persona": "人格印象",
    "static_persona": "静态角色资料",
    "current_message": "当前消息",
    "retrieval": "召回对象",
    "summary": "滚动摘要",
    "user": "用户消息",
    "assistant": "助手消息",
    "conversation_followup": "待续话题",
    "memory_followup": "授权记忆",
}
_STATUS_LABELS = {
    "active": "有效",
    "archived": "已归档",
    "superseded": "已替代",
    "deleted": "已删除",
    "tentative": "待确认",
    "confirmed": "已确认",
    "promoted": "已提升",
    "merged": "已合并",
    "disputed": "有争议",
    "denied": "已否认",
    "readonly": "只读",
    "proposed": "待确认",
    "surfaced": "已展示",
    "resolved": "已解决",
    "rejected": "已拒绝",
    "expired": "已过期",
}
_LAYER_LABELS = {
    "working": "工作层",
    "recent": "近期层",
    "fact": "事实层",
    "reflection": "反思层",
    "persona": "人格层",
    "static_persona": "静态角色资料",
    "timeline": "事件时间线",
    "audit": "审计",
    "cue": "待续",
}
_FACT_STATUS_OPTIONS = (
    ("全部状态", ""),
    ("有效", "active"),
    ("已归档", "archived"),
)
_CUE_STATUS_OPTIONS = (
    ("全部线索", ""),
    ("待确认", "proposed"),
    ("可主动使用", "active"),
    ("已展示", "surfaced"),
    ("已解决", "resolved"),
    ("已拒绝", "rejected"),
    ("已过期", "expired"),
)
_SCOPE_LABELS = {
    "user": "用户",
    "companion": "助手",
    "relationship": "关系",
    "": "不适用",
}
_TASK_TYPE_LABELS = {
    "extract_memory": "记忆提炼",
    "memory_extraction": "记忆提炼",
    "summarize_conversation": "会话摘要",
    "conversation_summary": "会话摘要",
    "deep_memory_cycle": "证据与反思评估",
    "persona_promotion": "人格印象提升",
}
_METHOD_LABELS = {
    "automatic": "自动提炼",
    "manual": "手工编辑",
}
_MODEL_STATUS_LABELS = {
    "ready": "离线向量模型已就绪",
    "loading": "正在加载离线向量模型",
    "missing": "本地模型缺失，当前使用 FTS5",
    "corrupt": "本地模型校验失败，当前使用 FTS5",
    "version_mismatch": "本地模型版本不符，当前使用 FTS5",
    "unavailable": "离线向量不可用，当前使用 FTS5",
    "failed": "离线向量不可用，当前使用 FTS5",
    "unknown": "尚未验证本地向量模型",
}
_GENERATION_STATUS_LABELS = {
    "building": "构建中",
    "active": "有效",
    "retired": "已退役",
    "failed": "失败",
    "missing": "未建立",
    "": "未建立",
}
_SAFE_RETRIEVAL_ERRORS = {
    "model_missing": "模型文件缺失",
    "model_corrupt": "模型文件校验失败",
    "model_version_mismatch": "模型版本不符",
    "model_runtime_unavailable": "CPU 推理后端不可用",
    "model_inference_failed": "本地向量推理失败",
    "generation_model_mismatch": "索引模型版本不符",
    "provider_unavailable": "CPU 推理后端不可用",
    "inference_failed": "本地向量推理失败",
    "index_failed": "本地向量索引构建失败",
    "invalid_vector": "本地向量数据无效",
    "storage_error": "本地索引存储不可用",
}


class MemoryPage(QWidget):
    """View/edit memory DTOs while delegating all persistence to application services."""

    refresh_requested = Signal()
    enabled_changed = Signal(bool)
    deep_enabled_changed = Signal(bool)
    search_requested = Signal(str, str, str, str)
    edit_requested = Signal(str, str)
    pin_requested = Signal(str, bool)
    archive_requested = Signal(str)
    restore_requested = Signal(str)
    delete_requested = Signal(str)
    delete_impact_requested = Signal(str, str)
    source_requested = Signal(str, str)
    lineage_requested = Signal(str, str)
    retry_task_requested = Signal(str)
    memory_selected = Signal(str)
    layer_memory_selected = Signal(str, str)
    derived_edit_requested = Signal(str, str, str)
    derived_pin_requested = Signal(str, str, bool)
    derived_archive_requested = Signal(str, str)
    derived_restore_requested = Signal(str, str)
    derived_delete_requested = Signal(str, str)
    derived_confirm_requested = Signal(str, str)
    derived_deny_requested = Signal(str, str)
    rollback_requested = Signal(str, str, str)
    conflict_resolution_requested = Signal(str, str, str)
    verify_model_requested = Signal()
    rebuild_index_requested = Signal()
    export_requested = Signal()
    backup_requested = Signal()
    clear_all_requested = Signal()
    cue_confirm_requested = Signal(str, str, str, bool)
    cue_reject_requested = Signal(str)
    cue_resolve_requested = Signal(str)
    cue_delete_requested = Signal(str)
    cue_keep_requested = Signal(str, bool)
    cue_open_requested = Signal(str)
    memory_authorization_changed = Signal(str, str, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("memoryPage")
        self._memories: dict[str, dict[str, Any]] = {}
        self._memory_order: list[str] = []
        self._sources_by_memory: dict[str, list[dict[str, Any]]] = {}
        self._versions_by_memory: dict[str, list[dict[str, Any]]] = {}
        self._layer_rows: dict[str, list[object]] = {"fact": []}
        self._conflicts_by_target: dict[tuple[str, str], dict[str, Any]] = {}

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

        self.deep_enabled_check = QCheckBox("启用证据、反思与人格印象")
        self.deep_enabled_check.setObjectName("deepMemoryEnabled")
        self.deep_enabled_check.setChecked(True)
        self.deep_enabled_notice = QLabel(
            "关闭后暂停深层派生与召回；事实、近期和角色资料不受影响。"
        )
        self.deep_enabled_notice.setWordWrap(True)
        deep_enabled_row = QHBoxLayout()
        deep_enabled_row.addWidget(self.deep_enabled_check)
        deep_enabled_row.addWidget(self.deep_enabled_notice, 1)

        self.retrieval_status_label = QLabel("尚未验证本地向量模型")
        self.retrieval_status_label.setObjectName("memoryRetrievalStatus")
        self.retrieval_status_label.setWordWrap(True)
        self.retrieval_index_label = QLabel("用户记忆索引：未建立　角色资料索引：未建立")
        self.retrieval_index_label.setObjectName("memoryIndexStatus")
        self.retrieval_index_label.setWordWrap(True)
        self.verify_model_button = QPushButton("验证本地模型")
        self.verify_model_button.setObjectName("verifyMemoryModel")
        self.rebuild_index_button = QPushButton("重新构建索引")
        self.rebuild_index_button.setObjectName("rebuildMemoryIndex")
        self.rebuild_index_button.setEnabled(False)

        retrieval_actions = QHBoxLayout()
        retrieval_actions.addWidget(self.retrieval_status_label, 1)
        retrieval_actions.addWidget(self.verify_model_button)
        retrieval_actions.addWidget(self.rebuild_index_button)
        retrieval_group = QGroupBox("本地混合检索")
        retrieval_layout = QVBoxLayout(retrieval_group)
        retrieval_layout.addLayout(retrieval_actions)
        retrieval_layout.addWidget(self.retrieval_index_label)

        self.export_button = QPushButton("导出记忆 JSON…")
        self.export_button.setObjectName("exportMemories")
        self.backup_button = QPushButton("备份数据库…")
        self.backup_button.setObjectName("backupMemoryDatabase")
        self.clear_all_button = QPushButton("清空全部记忆…")
        self.clear_all_button.setObjectName("clearAllMemories")
        data_actions = QHBoxLayout()
        data_actions.addWidget(self.export_button)
        data_actions.addWidget(self.backup_button)
        data_actions.addStretch(1)
        data_actions.addWidget(self.clear_all_button)
        data_group = QGroupBox("记忆数据")
        data_group.setLayout(data_actions)

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
        for label, value in _FACT_STATUS_OPTIONS:
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
            ("最近召回", "recalled_desc"),
        ):
            self.sort_combo.addItem(label, value)
        self.search_button = QPushButton("搜索")
        self.refresh_button = QPushButton("刷新")

        self.layer_combo = QComboBox()
        self.layer_combo.setObjectName("memoryLayerView")
        for label, value in (
            ("工作层", "working"),
            ("近期层", "recent"),
            ("事实层", "fact"),
            ("反思层", "reflection"),
            ("人格层", "persona"),
            ("事件时间线", "timeline"),
            ("审计", "audit"),
            ("待续", "cue"),
        ):
            self.layer_combo.addItem(label, value)
        self.layer_combo.setCurrentIndex(2)

        filters = QHBoxLayout()
        filters.addWidget(self.layer_combo)
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.type_combo)
        filters.addWidget(self.status_combo)
        filters.addWidget(self.pinned_combo)
        filters.addWidget(self.sort_combo)
        filters.addWidget(self.search_button)
        filters.addWidget(self.refresh_button)

        self.memory_table = QTableWidget(0, 8)
        self.memory_table.setObjectName("memoryTable")
        self.memory_table.setAccessibleName("长期记忆列表")
        self.memory_table.setHorizontalHeaderLabels(
            ["置顶", "类型", "状态", "内容", "重要性", "置信度", "最近召回", "更新时间"]
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
        self.cue_topic_edit = QLineEdit()
        self.cue_topic_edit.setObjectName("companionCueTopicEditor")
        self.cue_topic_edit.setPlaceholderText("待续主题")
        self.cue_topic_edit.setVisible(False)

        self.save_edit_button = QPushButton("保存修改")
        self.pin_button = QPushButton("置顶")
        self.archive_button = QPushButton("归档")
        self.restore_button = QPushButton("恢复")
        self.delete_button = QPushButton("永久删除")
        self.confirm_button = QPushButton("确认")
        self.deny_button = QPushButton("否认")
        self.rollback_combo = QComboBox()
        self.rollback_combo.setObjectName("memoryVersionRollback")
        self.rollback_button = QPushButton("复制旧版本回滚")
        self.memory_authorization_check = QCheckBox("允许她主动提起")
        self.memory_authorization_check.setObjectName("memoryProactiveAuthorization")
        self.memory_authorization_check.setVisible(False)
        self.keep_cue_check = QCheckBox("保留至解决")
        self.keep_cue_check.setObjectName("keepCompanionCueUntilResolved")
        self.keep_cue_check.setVisible(False)
        self.resolve_cue_button = QPushButton("标记已解决")
        self.resolve_cue_button.setVisible(False)
        self.open_cue_button = QPushButton("在聊天中打开")
        self.open_cue_button.setVisible(False)

        memory_actions = QHBoxLayout()
        memory_actions.addWidget(self.save_edit_button)
        memory_actions.addWidget(self.pin_button)
        memory_actions.addWidget(self.archive_button)
        memory_actions.addWidget(self.restore_button)
        memory_actions.addWidget(self.confirm_button)
        memory_actions.addWidget(self.deny_button)
        memory_actions.addWidget(self.resolve_cue_button)
        memory_actions.addWidget(self.open_cue_button)
        memory_actions.addWidget(self.keep_cue_check)
        memory_actions.addWidget(self.memory_authorization_check)
        memory_actions.addStretch(1)
        memory_actions.addWidget(self.delete_button)

        version_actions = QHBoxLayout()
        version_actions.addWidget(QLabel("版本历史"))
        version_actions.addWidget(self.rollback_combo, 1)
        version_actions.addWidget(self.rollback_button)

        self.conflict_notice = QLabel()
        self.conflict_notice.setObjectName("memoryConflictNotice")
        self.conflict_notice.setWordWrap(True)
        self.keep_conflict_button = QPushButton("保留现状")
        self.accept_conflict_button = QPushButton("接受新版本")
        self.merge_conflict_button = QPushButton("手工合并")
        conflict_actions = QHBoxLayout()
        conflict_actions.addWidget(self.keep_conflict_button)
        conflict_actions.addWidget(self.accept_conflict_button)
        conflict_actions.addWidget(self.merge_conflict_button)
        conflict_actions.addStretch(1)

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
        self.open_lineage_button = QPushButton("跳转到上游记忆")

        source_actions = QHBoxLayout()
        source_actions.addWidget(self.open_source_button)
        source_actions.addWidget(self.open_lineage_button)
        source_actions.addStretch(1)

        self.sources_group = QGroupBox("来源")
        sources_layout = QVBoxLayout(self.sources_group)
        sources_layout.addWidget(self.source_list)
        sources_layout.addWidget(self.source_preview)
        sources_layout.addLayout(source_actions)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(8, 0, 0, 0)
        detail_layout.addWidget(self.detail_title_label)
        detail_layout.addWidget(self.detail_meta_label)
        detail_layout.addWidget(self.cue_topic_edit)
        detail_layout.addWidget(self.detail_edit)
        detail_layout.addLayout(memory_actions)
        detail_layout.addLayout(version_actions)
        detail_layout.addWidget(self.conflict_notice)
        detail_layout.addLayout(conflict_actions)
        detail_layout.addWidget(self.sources_group, 1)

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
        layout.addLayout(deep_enabled_row)
        layout.addWidget(retrieval_group)
        layout.addWidget(data_group)
        layout.addLayout(filters)
        layout.addWidget(splitter, 1)
        layout.addWidget(failed_group)
        layout.addWidget(self.status_label)

        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        self.deep_enabled_check.toggled.connect(self.deep_enabled_changed.emit)
        self.layer_combo.currentIndexChanged.connect(self._on_layer_changed)
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
        self.confirm_button.clicked.connect(self._request_confirm)
        self.deny_button.clicked.connect(self._request_deny)
        self.rollback_button.clicked.connect(self._request_rollback)
        self.rollback_combo.currentIndexChanged.connect(self._sync_rollback_action)
        self.keep_conflict_button.clicked.connect(lambda: self._request_conflict_resolution("keep"))
        self.accept_conflict_button.clicked.connect(
            lambda: self._request_conflict_resolution("accept")
        )
        self.merge_conflict_button.clicked.connect(
            lambda: self._request_conflict_resolution("merge")
        )
        self.source_list.currentItemChanged.connect(self._on_source_selection_changed)
        self.source_list.itemDoubleClicked.connect(self._open_source)
        self.open_source_button.clicked.connect(self._open_current_source)
        self.open_lineage_button.clicked.connect(self._open_current_lineage)
        self.lineage_requested.connect(self.select_layer_memory)
        self.failed_task_view.itemSelectionChanged.connect(self._sync_task_action)
        self.retry_task_button.clicked.connect(self._request_task_retry)
        self.verify_model_button.clicked.connect(self.verify_model_requested.emit)
        self.rebuild_index_button.clicked.connect(self.rebuild_index_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self.backup_button.clicked.connect(self.backup_requested.emit)
        self.clear_all_button.clicked.connect(self._request_clear_all)
        self.resolve_cue_button.clicked.connect(self._request_resolve_cue)
        self.open_cue_button.clicked.connect(self._request_open_cue)
        self.keep_cue_check.toggled.connect(self._request_keep_cue)
        self.memory_authorization_check.toggled.connect(self._request_memory_authorization)
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

    @property
    def current_layer(self) -> str:
        return str(self.layer_combo.currentData() or "fact")

    def set_memory_enabled(self, enabled: bool) -> None:
        with QSignalBlocker(self.enabled_check):
            self.enabled_check.setChecked(enabled)
        self._sync_enabled_notice()

    def set_deep_memory_enabled(self, enabled: bool) -> None:
        with QSignalBlocker(self.deep_enabled_check):
            self.deep_enabled_check.setChecked(enabled)

    def set_layer_data(
        self,
        *,
        working: Iterable[object] = (),
        recent: Iterable[object] = (),
        facts: Iterable[object] = (),
        reflections: Iterable[object] = (),
        personas: Iterable[object] = (),
        static_persona: Iterable[object] = (),
        timeline: Iterable[object] = (),
        audit: Iterable[object] = (),
        conflicts: Iterable[object] = (),
        cues: Iterable[object] = (),
        selected_id: str | None = None,
    ) -> None:
        self._layer_rows = {
            "working": list(working),
            "recent": list(recent),
            "fact": list(facts),
            "reflection": list(reflections),
            "persona": [*personas, *static_persona],
            "timeline": list(timeline),
            "audit": list(audit),
            "cue": list(cues),
        }
        self.set_conflicts(conflicts)
        if self.current_layer == "cue":
            self._render_cue_filter()
            if selected_id is not None:
                self.select_memory(selected_id)
        else:
            self._render_memories(self._layer_rows.get(self.current_layer, ()), selected_id)

    def set_memories(
        self,
        memories: Iterable[object],
        selected_id: str | None = None,
    ) -> None:
        values = list(memories)
        self._layer_rows["fact"] = values
        if self.current_layer == "fact":
            self._render_memories(values, selected_id)

    def _render_memories(
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
                source_key = f"{row['layer']}:{row['memory_id']}"
                self._sources_by_memory[source_key] = [
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
                    row["last_recalled_at"] or "从未",
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

    def set_conflicts(self, conflicts: Iterable[object]) -> None:
        rows = [_conflict_row(conflict) for conflict in conflicts]
        self._conflicts_by_target = {
            (row["target_layer"], row["target_group_id"]): row
            for row in rows
            if row["status"] == "open"
        }
        self._sync_memory_detail()

    def set_versions(self, layer: str, memory_id: str, versions: Iterable[object]) -> None:
        key = f"{layer}:{memory_id}"
        self._versions_by_memory[key] = [_version_row(version) for version in versions]
        if memory_id == self.current_memory_id and layer == self.current_layer:
            self._render_versions(layer, memory_id)

    def confirm_delete_impact(
        self,
        layer: str,
        memory_id: str,
        impact: object,
    ) -> None:
        """Show the exact durable lineage scope before a destructive delete."""

        memory = self._memories.get(memory_id)
        if memory is None or memory["layer"] != layer:
            return
        facts = _nonnegative_int(_member(impact, "facts", default=0))
        reflections = _nonnegative_int(_member(impact, "reflections", default=0))
        personas = _nonnegative_int(_member(impact, "personas", default=0))
        answer = QMessageBox.question(
            self,
            "永久删除记忆？",
            "本次事务将永久删除以下内容血缘范围：\n"
            f"事实 {facts} 条、反思 {reflections} 条、人格印象 {personas} 条。\n\n"
            "对应的全部版本、来源关联、索引、证据、冲突和无正文审计事件"
            "也会一并删除。原始聊天不会随之删除。此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if layer == "fact":
            self.delete_requested.emit(memory_id)
        elif layer in {"reflection", "persona"}:
            self.derived_delete_requested.emit(layer, memory_id)

    def select_memory(self, memory_id: str) -> bool:
        try:
            row = self._memory_order.index(memory_id)
        except ValueError:
            return False
        with QSignalBlocker(self.memory_table):
            self.memory_table.selectRow(row)
        self._sync_memory_detail()
        return True

    @Slot(str, str)
    def select_layer_memory(self, layer: str, memory_id: str) -> bool:
        view_layer = "persona" if layer == "static_persona" else layer
        index = self.layer_combo.findData(view_layer)
        if index < 0:
            return False
        self.layer_combo.setCurrentIndex(index)
        return self.select_memory(memory_id)

    def set_sources(self, memory_id: str, sources: Iterable[object]) -> None:
        self.set_layer_sources(self.current_layer, memory_id, sources)

    def set_layer_sources(
        self,
        layer: str,
        memory_id: str,
        sources: Iterable[object],
    ) -> None:
        key = f"{layer}:{memory_id}"
        self._sources_by_memory[key] = [_source_row(source) for source in sources]
        current = self._memories.get(self.current_memory_id or "")
        if memory_id == self.current_memory_id and current and current["layer"] == layer:
            self._render_sources(layer, memory_id)

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

    def set_retrieval_status(self, status: object) -> None:
        """Render only bounded status metadata; unknown error text is never displayed."""

        row = _retrieval_status_row(status)
        model_status = row["model_status"]
        summary = _MODEL_STATUS_LABELS.get(model_status, _MODEL_STATUS_LABELS["unavailable"])
        safe_error = _SAFE_RETRIEVAL_ERRORS.get(row["safe_error_category"])
        if safe_error:
            summary = f"{summary}（{safe_error}）"
        self.retrieval_status_label.setText(summary)
        self.retrieval_status_label.setProperty("degraded", model_status != "ready")
        self.retrieval_status_label.style().unpolish(self.retrieval_status_label)
        self.retrieval_status_label.style().polish(self.retrieval_status_label)

        user_generation = _generation_text(
            row["user_generation"],
            row["user_generation_status"],
            row["user_index_count"],
        )
        persona_generation = _generation_text(
            row["persona_generation"],
            row["persona_generation_status"],
            row["persona_index_count"],
        )
        reflection_generation = _generation_text(
            row["reflection_generation"],
            row["reflection_generation_status"],
            row["reflection_index_count"],
        )
        impression_generation = _generation_text(
            row["impression_generation"],
            row["impression_generation_status"],
            row["impression_index_count"],
        )
        rebuild_status = _GENERATION_STATUS_LABELS.get(
            row["last_rebuild_status"],
            "未知",
        )
        self.retrieval_index_label.setText(
            f"事实索引：{user_generation}　反思索引：{reflection_generation}\n"
            f"人格印象索引：{impression_generation}　角色资料索引：{persona_generation}\n"
            f"最近重建：{rebuild_status}"
        )
        self.rebuild_index_button.setEnabled(model_status == "ready")

    @Slot(bool)
    def _on_enabled_toggled(self, enabled: bool) -> None:
        self._sync_enabled_notice()
        self.enabled_changed.emit(enabled)

    @Slot()
    def _on_layer_changed(self, *_args: object) -> None:
        layer = self.current_layer
        self._sync_status_options(layer)
        self.type_combo.setEnabled(layer == "fact")
        self.status_combo.setEnabled(layer in {"fact", "cue"})
        self.pinned_combo.setEnabled(layer == "fact")
        self.sort_combo.setEnabled(layer == "fact")
        if layer == "cue":
            self._render_cue_filter()
        else:
            self._render_memories(self._layer_rows.get(layer, ()))

    @Slot()
    def _emit_search(self, *_args: object) -> None:
        if self.current_layer == "cue":
            self._render_cue_filter()
            return
        self.search_requested.emit(
            self.search_edit.text().strip(),
            str(self.type_combo.currentData() or ""),
            str(self.status_combo.currentData() or ""),
            str(self.sort_combo.currentData() or "updated_desc"),
        )

    def _sync_status_options(self, layer: str) -> None:
        options = _CUE_STATUS_OPTIONS if layer == "cue" else _FACT_STATUS_OPTIONS
        current = str(self.status_combo.currentData() or "")
        valid = {value for _label, value in options}
        with QSignalBlocker(self.status_combo):
            self.status_combo.clear()
            for label, value in options:
                self.status_combo.addItem(label, value)
            index = self.status_combo.findData(current if current in valid else "")
            self.status_combo.setCurrentIndex(max(0, index))

    def _render_cue_filter(self) -> None:
        status = str(self.status_combo.currentData() or "")
        query = self.search_edit.text().strip().casefold()
        values = [
            value
            for value in self._layer_rows.get("cue", ())
            if (not status or _enum_text(_member(value, "status", default="")) == status)
            and (
                not query
                or query in str(_member(value, "topic", "topic_key", default="")).casefold()
                or query in str(_member(value, "frozen_text", "content", default="")).casefold()
            )
        ]
        self._render_memories(values)

    @Slot()
    def _on_memory_selection_changed(self) -> None:
        self._sync_memory_detail()
        memory_id = self.current_memory_id
        if memory_id is not None:
            layer = str(self._memories[memory_id]["layer"])
            self.layer_memory_selected.emit(layer, memory_id)
            if layer == "fact":
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
        layer = str(self._memories[memory_id]["layer"])
        if layer == "fact":
            self.edit_requested.emit(memory_id, content)
        elif layer in {"reflection", "persona"}:
            self.derived_edit_requested.emit(layer, memory_id, content)

    @Slot()
    def _request_pin(self) -> None:
        memory_id = self.current_memory_id
        if memory_id is None:
            return
        pinned = not bool(self._memories[memory_id]["pinned"])
        layer = str(self._memories[memory_id]["layer"])
        if layer == "fact":
            self.pin_requested.emit(memory_id, pinned)
        elif layer in {"reflection", "persona"}:
            self.derived_pin_requested.emit(layer, memory_id, pinned)

    @Slot()
    def _request_archive(self) -> None:
        if self.current_memory_id is not None:
            layer = str(self._memories[self.current_memory_id]["layer"])
            if layer == "fact":
                self.archive_requested.emit(self.current_memory_id)
            elif layer in {"reflection", "persona"}:
                self.derived_archive_requested.emit(
                    layer,
                    self.current_memory_id,
                )

    @Slot()
    def _request_restore(self) -> None:
        if self.current_memory_id is not None:
            layer = str(self._memories[self.current_memory_id]["layer"])
            if layer == "fact":
                self.restore_requested.emit(self.current_memory_id)
            elif layer in {"reflection", "persona"}:
                self.derived_restore_requested.emit(
                    layer,
                    self.current_memory_id,
                )

    @Slot()
    def _request_delete(self) -> None:
        memory_id = self.current_memory_id
        if memory_id is None:
            return
        layer = str(self._memories[memory_id]["layer"])
        if layer in {"fact", "reflection", "persona"}:
            self.set_status("正在计算永久删除的血缘影响范围。")
            self.delete_impact_requested.emit(layer, memory_id)
        elif layer == "cue":
            answer = QMessageBox.question(
                self,
                "删除这条陪伴线索？",
                "将永久清除线索文本和全部来源关系，只保留不含正文的审计事件。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.cue_delete_requested.emit(memory_id)

    @Slot()
    def _request_confirm(self) -> None:
        memory_id = self.current_memory_id
        if memory_id is not None:
            layer = str(self._memories[memory_id]["layer"])
            if layer in {"reflection", "persona"}:
                self.derived_confirm_requested.emit(layer, memory_id)
            elif layer == "cue":
                topic = self.cue_topic_edit.text().strip()
                frozen_text = self.detail_edit.toPlainText().strip()
                if not topic or not frozen_text:
                    self.set_status("主题和确认文本都不能为空。", error=True)
                    return
                self.cue_confirm_requested.emit(
                    memory_id,
                    topic,
                    frozen_text,
                    self.keep_cue_check.isChecked(),
                )

    @Slot()
    def _request_deny(self) -> None:
        memory_id = self.current_memory_id
        if memory_id is not None:
            layer = str(self._memories[memory_id]["layer"])
            if layer in {"reflection", "persona"}:
                self.derived_deny_requested.emit(layer, memory_id)
            elif layer == "cue":
                self.cue_reject_requested.emit(memory_id)

    @Slot()
    def _request_resolve_cue(self) -> None:
        memory_id = self.current_memory_id
        if memory_id is not None and self.current_layer == "cue":
            self.cue_resolve_requested.emit(memory_id)

    @Slot()
    def _request_open_cue(self) -> None:
        memory_id = self.current_memory_id
        if memory_id is not None and self.current_layer == "cue":
            self.cue_open_requested.emit(memory_id)

    @Slot(bool)
    def _request_keep_cue(self, enabled: bool) -> None:
        memory_id = self.current_memory_id
        memory = self._memories.get(memory_id or "")
        if (
            memory is not None
            and memory["layer"] == "cue"
            and memory["status"] in {"active", "surfaced"}
            and bool(memory.get("keep_until_resolved")) != bool(enabled)
        ):
            self.cue_keep_requested.emit(memory_id or "", bool(enabled))

    @Slot(bool)
    def _request_memory_authorization(self, enabled: bool) -> None:
        memory_id = self.current_memory_id
        memory = self._memories.get(memory_id or "")
        if memory is None or memory["layer"] not in {"fact", "reflection", "persona"}:
            return
        currently_enabled = bool(memory.get("companion_cue_id"))
        if currently_enabled == bool(enabled):
            return
        self.memory_authorization_changed.emit(
            str(memory["layer"]),
            str(memory["version_id"]),
            bool(enabled),
        )

    @Slot()
    def _request_rollback(self) -> None:
        memory_id = self.current_memory_id
        version_id = self.rollback_combo.currentData()
        if memory_id is not None and version_id:
            layer = str(self._memories[memory_id]["layer"])
            if layer in {"fact", "reflection", "persona"}:
                self.rollback_requested.emit(layer, memory_id, str(version_id))

    def _request_conflict_resolution(self, resolution: str) -> None:
        memory_id = self.current_memory_id
        if memory_id is None:
            return
        layer = str(self._memories[memory_id]["layer"])
        conflict = self._conflicts_by_target.get((layer, memory_id))
        if conflict is None:
            return
        merged = ""
        if resolution == "merge":
            merged = self.detail_edit.toPlainText().strip()
            if not merged:
                self.set_status("手工合并内容不能为空。", error=True)
                return
        self.conflict_resolution_requested.emit(
            conflict["conflict_id"],
            resolution,
            merged,
        )

    @Slot()
    def _request_clear_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "清空全部长期记忆？",
            "将永久删除全部长期记忆、不可变版本和来源关系。\n\n"
            "原始聊天不会随之删除。此操作无法撤销，建议先导出记忆或创建备份。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.clear_all_requested.emit()

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
            self.open_lineage_button.setEnabled(False)
            return
        self.source_preview.setPlainText(source["content"])
        self.open_source_button.setEnabled(bool(source["available"] and source["message_id"]))
        self.open_lineage_button.setEnabled(
            bool(source["parent_layer"] and source["parent_group_id"])
        )

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
    def _open_current_lineage(self) -> None:
        source = _source_item_data(self.source_list.currentItem())
        if source is not None and source["parent_layer"] and source["parent_group_id"]:
            self.lineage_requested.emit(source["parent_layer"], source["parent_group_id"])

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
            self.enabled_notice.setText(
                "已停用：不会提炼或召回用户记忆；角色资料与会话摘要保持可用。"
            )

    def _sync_memory_detail(self) -> None:
        memory_id = self.current_memory_id
        memory = self._memories.get(memory_id or "")
        enabled = memory is not None
        if memory is None:
            self.detail_title_label.setText("请选择一条记忆")
            self.detail_meta_label.clear()
            self.detail_edit.clear()
            self.cue_topic_edit.clear()
            self.cue_topic_edit.setVisible(False)
            self.memory_authorization_check.setVisible(False)
            self.keep_cue_check.setVisible(False)
            self.resolve_cue_button.setVisible(False)
            self.open_cue_button.setVisible(False)
            self.archive_button.setVisible(True)
            self.restore_button.setVisible(False)
            self.source_list.clear()
            self.source_preview.clear()
            self.open_source_button.setEnabled(False)
            self.open_lineage_button.setEnabled(False)
            self.rollback_combo.clear()
            self.rollback_button.setEnabled(False)
            self.conflict_notice.clear()
            for widget in (
                self.detail_edit,
                self.save_edit_button,
                self.pin_button,
                self.archive_button,
                self.restore_button,
                self.delete_button,
                self.confirm_button,
                self.deny_button,
                self.keep_conflict_button,
                self.accept_conflict_button,
                self.merge_conflict_button,
                self.resolve_cue_button,
                self.open_cue_button,
                self.keep_cue_check,
                self.memory_authorization_check,
            ):
                widget.setEnabled(False)
            return
        layer = str(memory["layer"])
        cue_layer = layer == "cue"
        cue_proposed = cue_layer and memory["status"] == "proposed"
        cue_confirmed = cue_layer and memory["status"] in {"active", "surfaced"}
        editable = layer in {"fact", "reflection", "persona"} or cue_proposed
        derived = layer in {"reflection", "persona"}
        self.detail_edit.setReadOnly(not editable or (cue_layer and not cue_proposed))
        self.detail_edit.setEnabled(enabled)
        self.save_edit_button.setVisible(not cue_layer)
        self.save_edit_button.setEnabled(editable and not cue_layer)
        self.pin_button.setVisible(not cue_layer)
        self.pin_button.setEnabled(editable and not cue_layer)
        self.delete_button.setEnabled(editable or cue_layer)
        self.confirm_button.setEnabled(derived or cue_proposed)
        self.deny_button.setEnabled(
            derived or memory["status"] in {"proposed", "active", "surfaced"}
        )
        self.detail_title_label.setText(
            f"{memory['type_label']} · {memory['status_label']}"
            if cue_layer
            else f"{memory['type_label']} · {memory['status_label']} · 版本 {memory['version']}"
        )
        if cue_layer:
            expiry_text = memory["expires_at"] or (
                "保留至解决" if memory["keep_until_resolved"] else "未设置"
            )
            self.detail_meta_label.setText(
                f"来源类别：{memory['source_label']}　原因：{memory['reason'] or '用户授权'}　"
                f"置信度：{_score_text(memory['confidence'])}\n"
                f"确认：{memory['confirmed_at'] or '尚未确认'}　"
                f"过期：{expiry_text}　"
                f"展示：{memory['surfaced_at'] or '尚未展示'}"
            )
        else:
            self.detail_meta_label.setText(
                f"主题：{memory['topic_key'] or '未设置'}　"
                f"重要性：{_score_text(memory['importance'])}　"
                f"置信度：{_score_text(memory['confidence'])}\n"
                f"层级：{memory['layer_label']}　范围：{memory['subject_scope_label']}　"
                f"证据分：{memory['evidence_score_text']}\n"
                f"创建：{memory['created_at'] or '未知'}　"
                f"更新：{memory['updated_at'] or '未知'}　"
                f"最近召回：{memory['last_recalled_at'] or '从未'}"
            )
        self.detail_edit.setPlainText(memory["content"])
        self.cue_topic_edit.setVisible(cue_layer)
        self.cue_topic_edit.setReadOnly(not cue_proposed)
        self.cue_topic_edit.setText(memory["topic_key"])
        self.pin_button.setText("取消置顶" if memory["pinned"] else "置顶")
        archived = memory["status"] == "archived"
        self.archive_button.setVisible(not cue_layer and editable and not archived)
        self.archive_button.setEnabled(not cue_layer and editable and not archived)
        self.restore_button.setVisible(not cue_layer and editable and archived)
        self.restore_button.setEnabled(not cue_layer and editable and archived)
        self.confirm_button.setVisible(derived or cue_proposed)
        self.confirm_button.setText("确认并启用" if cue_layer else "确认")
        self.deny_button.setVisible(
            derived or (cue_layer and memory["status"] in {"proposed", "active", "surfaced"})
        )
        self.deny_button.setText("拒绝 / 撤销" if cue_layer else "否认")
        self.resolve_cue_button.setVisible(cue_confirmed)
        self.resolve_cue_button.setEnabled(cue_confirmed)
        self.open_cue_button.setVisible(cue_confirmed)
        self.open_cue_button.setEnabled(cue_confirmed)
        self.keep_cue_check.setVisible(cue_proposed or cue_confirmed)
        self.keep_cue_check.setEnabled(cue_proposed or cue_confirmed)
        with QSignalBlocker(self.keep_cue_check):
            self.keep_cue_check.setChecked(bool(memory.get("keep_until_resolved")))
        authorization_eligible = (
            (layer == "fact" and memory["status"] == "active" and not memory["conflicted"])
            or (
                layer == "reflection"
                and memory["status"] in {"confirmed", "promoted"}
                and not memory["conflicted"]
            )
            or (layer == "persona" and memory["status"] == "active" and not memory["conflicted"])
        )
        has_authorization = bool(memory.get("companion_cue_id"))
        self.memory_authorization_check.setVisible(
            layer in {"fact", "reflection", "persona"}
            and (authorization_eligible or has_authorization)
        )
        self.memory_authorization_check.setEnabled(authorization_eligible or has_authorization)
        with QSignalBlocker(self.memory_authorization_check):
            self.memory_authorization_check.setChecked(has_authorization)
        self.memory_authorization_check.setToolTip(
            "开启后先生成本地可编辑草稿，仍需在“待续”中确认；不会调用远程模型。"
        )
        self._render_sources(layer, memory["memory_id"])
        self._render_versions(layer, memory["memory_id"])
        conflict = self._conflicts_by_target.get((layer, memory["memory_id"]))
        self.conflict_notice.setText(
            "检测到未解决冲突：该项及派生项已停止召回。" if conflict else ""
        )
        conflict_enabled = conflict is not None and layer == "fact"
        for widget in (
            self.keep_conflict_button,
            self.accept_conflict_button,
            self.merge_conflict_button,
        ):
            widget.setVisible(conflict_enabled)
            widget.setEnabled(conflict_enabled)

    def _render_sources(self, layer: str, memory_id: str) -> None:
        sources = self._sources_by_memory.get(f"{layer}:{memory_id}", [])
        with QSignalBlocker(self.source_list):
            self.source_list.clear()
            for source in sources:
                if source["method"] == "companion_cue_source":
                    prefix = source["content"]
                elif source["method"] == "manual":
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
                parent = (
                    f"上游 {source['parent_layer']}:{source['parent_version_id']}"
                    if source["parent_version_id"]
                    else ""
                )
                if parent:
                    details = " · ".join(value for value in (details, parent) if value)
                text = prefix if not details else f"{prefix} · {details}"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, source)
                self.source_list.addItem(item)
            if self.source_list.count():
                self.source_list.setCurrentRow(0)
        self._on_source_selection_changed(self.source_list.currentItem(), None)

    def _render_versions(self, layer: str, memory_id: str) -> None:
        versions = self._versions_by_memory.get(f"{layer}:{memory_id}", [])
        current_version = self._memories.get(memory_id, {}).get("version_id", "")
        with QSignalBlocker(self.rollback_combo):
            self.rollback_combo.clear()
            for version in reversed(versions):
                label = f"v{version['version']} · {version['created_at']} · {version['operation']}"
                self.rollback_combo.addItem(label, version["version_id"])
                if version["version_id"] == current_version:
                    index = self.rollback_combo.count() - 1
                    self.rollback_combo.setItemData(
                        index,
                        "当前版本",
                        Qt.ItemDataRole.ToolTipRole,
                    )
        self.rollback_button.setEnabled(
            layer in {"fact", "reflection", "persona"} and self.rollback_combo.count() > 1
        )
        self._sync_rollback_action()

    @Slot()
    def _sync_rollback_action(self, *_args: object) -> None:
        memory_id = self.current_memory_id
        memory = self._memories.get(memory_id or "")
        selected_version = str(self.rollback_combo.currentData() or "")
        self.rollback_button.setEnabled(
            memory is not None
            and memory["layer"] in {"fact", "reflection", "persona"}
            and self.rollback_combo.count() > 1
            and bool(selected_version)
            and selected_version != memory["version_id"]
        )

    def _sync_task_action(self) -> None:
        self.retry_task_button.setEnabled(self.failed_task_view.currentItem() is not None)


def _memory_row(value: object) -> dict[str, Any]:
    record = _member(value, "memory", default=value)
    version = _member(record, "current_version", default=None)
    memory_type = _enum_text(_member(record, "memory_type", "kind", "type", "category")).lower()
    status = _enum_text(_member(record, "status", default="active")).lower()
    memory_id = str(_member(record, "memory_id", "group_id", "id"))
    layer = _enum_text(_member(record, "layer", default="fact")).lower()
    subject_scope = _enum_text(_member(record, "subject_scope", default="")).lower()
    content = str(
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
        or ""
    )
    if not content:
        reason = str(_member(record, "reason", default="") or "")
        if layer == "audit":
            event_type = memory_type or "审计事件"
            delta = _number(_member(record, "reinforcement_delta", default=0.0)) - _number(
                _member(record, "disputation_delta", default=0.0)
            )
            content = f"{event_type} · {reason or '无正文审计元数据'} · 分值变化 {delta:+.2f}"
        else:
            content = reason
    version_id = str(
        _member(
            record,
            "version_id",
            default=_member(version, "version_id", default=""),
        )
        or ""
    )
    return {
        "memory_id": memory_id,
        "version_id": version_id,
        "layer": layer,
        "layer_label": _LAYER_LABELS.get(layer, layer),
        "subject_scope": subject_scope,
        "subject_scope_label": _SCOPE_LABELS.get(subject_scope, subject_scope or "不适用"),
        "type": memory_type,
        "type_label": _TYPE_LABELS.get(memory_type, memory_type),
        "status": status,
        "status_label": _STATUS_LABELS.get(status, status),
        "content": content,
        "topic_key": str(_member(record, "topic_key", "subject_key", default="")),
        "importance": _number(
            _member(record, "importance", default=_member(version, "importance", default=0.0))
        ),
        "confidence": _number(
            _member(record, "confidence", default=_member(version, "confidence", default=0.0))
        ),
        "evidence_score": _number(_member(record, "evidence_score", default=0.0)),
        "evidence_score_text": _score_text(_member(record, "evidence_score", default=0.0)),
        "conflicted": bool(_member(record, "conflicted", default=False)),
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
        "last_recalled_at": _display_value(
            _member(
                record,
                "last_recalled_at",
                "last_successful_recall_at",
                "recalled_at",
                default="",
            )
        ),
        "companion_cue_id": str(_member(record, "companion_cue_id", default="") or ""),
        "companion_cue_status": str(_member(record, "companion_cue_status", default="") or ""),
        "keep_until_resolved": bool(_member(record, "keep_until_resolved", default=False)),
        "source_label": str(_member(record, "source_label", default="") or ""),
        "reason": _enum_text(_member(record, "reason", default="")),
        "confirmed_at": _display_value(_member(record, "confirmed_at", default="")),
        "expires_at": _display_value(_member(record, "expires_at", default="")),
        "surfaced_at": _display_value(_member(record, "surfaced_at", default="")),
        "resolved_at": _display_value(_member(record, "resolved_at", default="")),
    }


def _conflict_row(value: object) -> dict[str, Any]:
    resolution = _member(value, "resolution", default=None)
    return {
        "conflict_id": str(_member(value, "conflict_id", "id")),
        "target_layer": _enum_text(_member(value, "target_layer", default="fact")).lower(),
        "target_group_id": str(_member(value, "target_group_id", "group_id")),
        "incumbent_version_id": str(_member(value, "incumbent_version_id", default="") or ""),
        "challenger_version_id": str(_member(value, "challenger_version_id", default="") or ""),
        "status": str(_member(value, "status", default="open")),
        "resolution": "" if resolution is None else _enum_text(resolution),
        "created_at": _display_value(_member(value, "created_at", default="")),
    }


def _version_row(value: object) -> dict[str, Any]:
    operation = _enum_text(_member(value, "operation", "origin", default="version"))
    return {
        "version_id": str(_member(value, "version_id", "id")),
        "version": int(_member(value, "version_number", "version", default=1)),
        "content": str(_member(value, "content", default="")),
        "operation": operation,
        "created_at": _display_value(_member(value, "created_at", default="")),
    }


def _retrieval_status_row(value: object) -> dict[str, Any]:
    return {
        "model_status": _enum_text(
            _member(value, "model_status", "status", default="unknown")
        ).lower(),
        "safe_error_category": _enum_text(
            _member(value, "safe_error_category", "error_category", default="")
        ).lower(),
        "user_generation": str(
            _member(value, "user_generation", "user_generation_id", default="") or ""
        ),
        "user_generation_status": _enum_text(
            _member(value, "user_generation_status", default="")
        ).lower(),
        "user_index_count": _nonnegative_int(
            _member(value, "user_index_count", "user_vector_count", default=0)
        ),
        "persona_generation": str(
            _member(value, "persona_generation", "persona_generation_id", default="") or ""
        ),
        "persona_generation_status": _enum_text(
            _member(value, "persona_generation_status", default="")
        ).lower(),
        "persona_index_count": _nonnegative_int(
            _member(value, "persona_index_count", "persona_vector_count", default=0)
        ),
        "reflection_generation": str(_member(value, "reflection_generation_id", default="") or ""),
        "reflection_generation_status": (
            "active" if _member(value, "reflection_generation_id", default=None) else ""
        ),
        "reflection_index_count": _nonnegative_int(_member(value, "reflection_count", default=0)),
        "impression_generation": str(
            _member(value, "persona_impression_generation_id", default="") or ""
        ),
        "impression_generation_status": (
            "active" if _member(value, "persona_impression_generation_id", default=None) else ""
        ),
        "impression_index_count": _nonnegative_int(
            _member(value, "persona_impression_count", default=0)
        ),
        "last_rebuild_status": _enum_text(
            _member(value, "last_rebuild_status", "rebuild_status", default="")
        ).lower(),
    }


def _generation_text(generation: str, status: str, count: int) -> str:
    if not generation:
        return "未建立"
    safe_generation = generation if len(generation) <= 12 else f"{generation[:12]}…"
    status_label = _GENERATION_STATUS_LABELS.get(status, "未知")
    return f"{safe_generation} · {status_label} · {count} 条"


def _source_row(value: object) -> dict[str, Any]:
    cue_source_kind = str(_member(value, "source_kind", default="") or "")
    cue_source_target = str(_member(value, "source_target_id", default="") or "")
    if cue_source_kind:
        label = {
            "user_message": "用户消息",
            "fact_version": "事实版本",
            "reflection_version": "反思版本",
            "persona_version": "人格印象版本",
        }.get(cue_source_kind, cue_source_kind)
        return {
            "conversation_id": "",
            "message_id": cue_source_target if cue_source_kind == "user_message" else "",
            "content": f"{label} · {cue_source_target}",
            "created_at": "",
            "method": "companion_cue_source",
            "available": False,
            "version_number": 0,
            "parent_version_id": (cue_source_target if cue_source_kind != "user_message" else ""),
            "parent_group_id": "",
            "parent_layer": "",
        }
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
        "parent_version_id": str(_member(value, "parent_version_id", default="") or ""),
        "parent_group_id": str(_member(value, "parent_group_id", default="") or ""),
        "parent_layer": str(_member(value, "parent_layer", default="") or ""),
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


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


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
