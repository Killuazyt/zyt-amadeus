"""Fixed-character settings page with local-only persona knowledge controls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_INDEX_STATUS_LABELS = {
    "active": "索引已就绪",
    "ready": "索引已就绪",
    "building": "正在构建索引",
    "missing": "尚未建立索引",
    "failed": "索引不可用，当前使用关键词检索",
    "degraded": "向量索引不可用，当前使用关键词检索",
    "unavailable": "向量索引不可用，当前使用关键词检索",
    "unknown": "索引状态尚未确认",
}


class PersonaPage(QWidget):
    """Show the single MVP persona and delegate imports/rebuilds to the controller."""

    import_requested = Signal(str)
    rebuild_index_requested = Signal()
    follow_user_language_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("personaPage")

        heading = QLabel("角色")
        heading.setObjectName("settingsPageHeading")
        heading.setStyleSheet("font-size: 18px; font-weight: 650;")
        explanation = QLabel(
            "MVP 固定使用一个现实陪伴角色。角色资料与用户记忆分开保存，"
            "导入资料不会被记作用户说过的话。"
        )
        explanation.setWordWrap(True)

        self.name_value = QLabel("牧濑红莉栖")
        self.name_value.setObjectName("personaNameValue")
        self.source_value = QLabel("公开安全的内置核心设定")
        self.source_value.setObjectName("personaSourceValue")
        self.source_value.setWordWrap(True)
        self.knowledge_count_value = QLabel("0 条")
        self.knowledge_count_value.setObjectName("personaKnowledgeCountValue")

        identity_form = QFormLayout()
        identity_form.addRow("当前角色", self.name_value)
        identity_form.addRow("设定来源", self.source_value)
        identity_form.addRow("本地知识条目", self.knowledge_count_value)
        identity_group = QGroupBox("固定角色")
        identity_group.setLayout(identity_form)

        self.follow_user_language = QCheckBox("回答语言跟随用户当前使用的语言")
        self.follow_user_language.setObjectName("followUserLanguage")
        language_note = QLabel("关闭后仍使用简体中文界面；此开关只影响角色回答语言。")
        language_note.setWordWrap(True)
        language_layout = QVBoxLayout()
        language_layout.addWidget(self.follow_user_language)
        language_layout.addWidget(language_note)
        language_group = QGroupBox("回答语言")
        language_group.setLayout(language_layout)

        self.index_status_value = QLabel("索引状态尚未确认")
        self.index_status_value.setObjectName("personaIndexStatus")
        self.index_status_value.setWordWrap(True)
        self.import_button = QPushButton("导入本地 JSONL…")
        self.import_button.setObjectName("importPersonaKnowledge")
        self.rebuild_index_button = QPushButton("重新构建角色索引")
        self.rebuild_index_button.setObjectName("rebuildPersonaIndex")
        actions = QHBoxLayout()
        actions.addWidget(self.import_button)
        actions.addWidget(self.rebuild_index_button)
        actions.addStretch(1)
        local_note = QLabel(
            "仅接受严格校验的 UTF-8 JSONL。原始角色资料保留在本机，不会进入公开构建。"
        )
        local_note.setWordWrap(True)
        knowledge_layout = QVBoxLayout()
        knowledge_layout.addWidget(self.index_status_value)
        knowledge_layout.addLayout(actions)
        knowledge_layout.addWidget(local_note)
        knowledge_group = QGroupBox("本地角色知识")
        knowledge_group.setLayout(knowledge_layout)

        self.status_label = QLabel()
        self.status_label.setObjectName("personaStatus")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(explanation)
        layout.addWidget(identity_group)
        layout.addWidget(language_group)
        layout.addWidget(knowledge_group)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.follow_user_language.toggled.connect(self.follow_user_language_changed.emit)
        self.import_button.clicked.connect(self._choose_knowledge_file)
        self.rebuild_index_button.clicked.connect(self.rebuild_index_requested.emit)

    def apply_settings(self, *, follow_user_language: bool) -> None:
        with QSignalBlocker(self.follow_user_language):
            self.follow_user_language.setChecked(bool(follow_user_language))

    def set_summary(
        self,
        *,
        name: str,
        source: str,
        knowledge_count: int,
    ) -> None:
        self.name_value.setText(name.strip() or "固定角色")
        self.source_value.setText(source.strip() or "公开安全的内置核心设定")
        self.knowledge_count_value.setText(f"{max(0, int(knowledge_count))} 条")

    def set_index_status(
        self,
        status: str,
        *,
        generation: str | None = None,
        count: int | None = None,
    ) -> None:
        """Render only bounded metadata, never an arbitrary backend error string."""

        normalized = _enum_text(status).lower()
        text = _INDEX_STATUS_LABELS.get(normalized, _INDEX_STATUS_LABELS["unknown"])
        details: list[str] = []
        if generation:
            safe_generation = str(generation)
            if len(safe_generation) > 12:
                safe_generation = f"{safe_generation[:12]}…"
            details.append(f"generation {safe_generation}")
        if count is not None:
            details.append(f"{max(0, int(count))} 条")
        if details:
            text = f"{text} · {' · '.join(details)}"
        self.index_status_value.setText(text)
        self.rebuild_index_button.setEnabled(normalized not in {"building"})

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("error", error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def apply_view(self, value: Mapping[str, Any] | object) -> None:
        """Convenience adapter for controller DTOs without a UI-to-storage dependency."""

        self.set_summary(
            name=str(_member(value, "name", "display_name", default="固定角色")),
            source=str(_member(value, "source", "source_label", default="内置核心设定")),
            knowledge_count=_safe_count(_member(value, "knowledge_count", "count", default=0)),
        )
        self.set_index_status(
            _enum_text(_member(value, "index_status", "status", default="unknown")),
            generation=_optional_text(_member(value, "generation", "generation_id", default=None)),
            count=_safe_count(_member(value, "index_count", default=0)),
        )

    def _choose_knowledge_file(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择本地角色知识 JSONL",
            "",
            "JSON Lines (*.jsonl);;所有文件 (*)",
        )
        if path:
            self.import_requested.emit(path)


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


def _safe_count(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_text(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _member(value: Mapping[str, Any] | object, *names: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    else:
        for name in names:
            if hasattr(value, name):
                return getattr(value, name)
    return default
