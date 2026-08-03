"""Bounded, redacted runtime diagnostics for the local desktop application."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_DATABASE_STATE_LABELS = {
    "ready": "可读写",
    "read_write": "可读写",
    "read-write": "可读写",
    "writable": "可读写",
    "read_only": "只读",
    "unavailable": "不可用",
    "failed": "不可用",
    "unknown": "尚未确认",
}
_MODEL_STATE_LABELS = {
    "ready": "离线向量模型已就绪",
    "loading": "正在加载离线向量模型",
    "missing": "模型缺失，使用 FTS5",
    "corrupt": "模型校验失败，使用 FTS5",
    "version_mismatch": "模型版本不符，使用 FTS5",
    "failed": "模型不可用，使用 FTS5",
    "unavailable": "模型不可用，使用 FTS5",
    "unknown": "尚未确认",
}
_INDEX_STATE_LABELS = {
    "active": "有效",
    "ready": "有效",
    "building": "构建中",
    "retired": "已退役",
    "missing": "未建立",
    "failed": "不可用",
    "unavailable": "不可用",
    "unknown": "尚未确认",
    "": "未建立",
}
_SAFE_ERROR_LABELS = {
    "authentication": "对话供应商鉴权失败",
    "content_filter": "对话内容被供应商拒绝",
    "credential": "Windows 凭据不可用",
    "database_unavailable": "数据库不可用",
    "database_read_only": "数据库只读",
    "model_missing": "离线模型缺失",
    "model_corrupt": "离线模型校验失败",
    "model_version_mismatch": "离线模型版本不符",
    "model_runtime_unavailable": "离线推理后端不可用",
    "model_inference_failed": "离线向量推理失败",
    "generation_model_mismatch": "索引模型版本不符",
    "index_failed": "本地索引构建失败",
    "provider_unconfigured": "对话供应商未配置",
    "provider_authentication": "对话供应商鉴权失败",
    "provider_rate_limited": "对话供应商请求受限",
    "provider_timeout": "对话供应商请求超时",
    "provider_network": "对话供应商网络不可用",
    "insufficient_balance": "对话供应商余额不足",
    "model_or_parameter": "对话模型或参数无效",
    "network": "网络不可用",
    "not_configured": "对话供应商未配置",
    "protocol": "对话供应商响应格式无效",
    "rate_limit": "对话供应商请求受限",
    "server": "对话供应商服务异常",
    "storage_error": "本地存储不可用",
    "timeout": "对话供应商请求超时",
    "unknown": "未知错误类别",
}


class DiagnosticsPage(QWidget):
    """Render a strict allowlist of state fields and stable error categories."""

    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("diagnosticsPage")

        heading = QLabel("诊断")
        heading.setObjectName("settingsPageHeading")
        heading.setStyleSheet("font-size: 18px; font-weight: 650;")
        explanation = QLabel(
            "这里只显示运行状态和有界错误类别，不显示 API 密钥、聊天、记忆、"
            "请求内容或私人角色资料。"
        )
        explanation.setWordWrap(True)

        self.version_value = _value_label("diagnosticsVersion")
        self.settings_schema_value = _value_label("diagnosticsSettingsSchema")
        self.database_schema_value = _value_label("diagnosticsDatabaseSchema")
        self.data_path_value = _value_label("diagnosticsDataPath")
        self.database_state_value = _value_label("diagnosticsDatabaseState")
        self.model_state_value = _value_label("diagnosticsModelState")
        self.user_index_value = _value_label("diagnosticsUserIndex")
        self.persona_index_value = _value_label("diagnosticsPersonaIndex")
        self.provider_value = _value_label("diagnosticsProvider")
        self.errors_value = _value_label("diagnosticsSafeErrors")

        build_form = QFormLayout()
        build_form.addRow("应用版本", self.version_value)
        build_form.addRow("设置 schema", self.settings_schema_value)
        build_form.addRow("SQLite schema", self.database_schema_value)
        build_group = QGroupBox("版本")
        build_group.setLayout(build_form)

        local_form = QFormLayout()
        local_form.addRow("数据位置", self.data_path_value)
        local_form.addRow("数据库", self.database_state_value)
        local_form.addRow("离线模型", self.model_state_value)
        local_form.addRow("用户记忆索引", self.user_index_value)
        local_form.addRow("角色资料索引", self.persona_index_value)
        local_group = QGroupBox("本地运行状态")
        local_group.setLayout(local_form)

        provider_form = QFormLayout()
        provider_form.addRow("对话供应商", self.provider_value)
        provider_form.addRow("最近错误类别", self.errors_value)
        provider_group = QGroupBox("连接与错误")
        provider_group.setLayout(provider_form)

        self.refresh_button = QPushButton("刷新诊断状态")
        self.refresh_button.setObjectName("refreshDiagnostics")
        self.status_label = QLabel()
        self.status_label.setObjectName("diagnosticsStatus")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(explanation)
        layout.addWidget(build_group)
        layout.addWidget(local_group)
        layout.addWidget(provider_group)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.set_diagnostics({})

    def set_diagnostics(self, value: Mapping[str, Any] | object) -> None:
        version = _bounded_plain_text(_member(value, "version", "app_version", default="未知"), 64)
        settings_schema = _bounded_integer_text(
            _member(value, "settings_schema", "settings_schema_version", default=None)
        )
        database_schema = _bounded_integer_text(
            _member(
                value,
                "database_schema",
                "database_schema_version",
                "sqlite_schema",
                default=None,
            )
        )
        data_path = _bounded_plain_text(
            _member(value, "data_path", "data_root", default="尚未确认"), 512
        )
        database_state = _enum_text(
            _member(value, "database_state", "database_status", default="unknown")
        ).lower()
        model_state = _enum_text(
            _member(value, "model_state", "model_status", default="unknown")
        ).lower()

        self.version_value.setText(version)
        self.settings_schema_value.setText(settings_schema)
        self.database_schema_value.setText(database_schema)
        self.data_path_value.setText(data_path)
        self.database_state_value.setText(
            _DATABASE_STATE_LABELS.get(database_state, _DATABASE_STATE_LABELS["unknown"])
        )
        self.model_state_value.setText(
            _MODEL_STATE_LABELS.get(model_state, _MODEL_STATE_LABELS["unknown"])
        )
        self.user_index_value.setText(_index_text(value, "user"))
        self.persona_index_value.setText(_index_text(value, "persona"))
        provider_configured = _strict_bool(_member(value, "provider_configured", default=False))
        self.provider_value.setText("已配置" if provider_configured else "未配置")

        categories = _member(
            value,
            "safe_error_categories",
            "error_categories",
            "last_error_category",
            default=(),
        )
        self.errors_value.setText(_safe_errors_text(categories))

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("error", error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)


def _value_label(object_name: str) -> QLabel:
    label = QLabel()
    label.setObjectName(object_name)
    label.setWordWrap(True)
    label.setTextInteractionFlags(label.textInteractionFlags())
    return label


def _index_text(value: Mapping[str, Any] | object, prefix: str) -> str:
    state = _enum_text(
        _member(value, f"{prefix}_index_state", f"{prefix}_index_status", default="unknown")
    ).lower()
    raw_count = _member(
        value,
        f"{prefix}_index_count",
        f"{prefix}_vector_count",
        default=None,
    )
    generation = _member(
        value,
        f"{prefix}_generation",
        f"{prefix}_generation_id",
        default=None,
    )
    label = _INDEX_STATE_LABELS.get(state, _INDEX_STATE_LABELS["unknown"])
    details: list[str] = []
    if raw_count is not None:
        details.append(f"{_safe_nonnegative_int(raw_count)} 条")
    if generation not in (None, ""):
        bounded = _bounded_plain_text(generation, 12)
        details.insert(0, f"generation {bounded}")
    return label if not details else f"{label} · {' · '.join(details)}"


def _safe_errors_text(value: object) -> str:
    values = (value,) if isinstance(value, str) or not isinstance(value, Iterable) else value
    labels: list[str] = []
    for raw in values:
        label = _SAFE_ERROR_LABELS.get(_enum_text(raw).lower())
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= 8:
            break
    return "无" if not labels else "；".join(labels)


def _strict_bool(value: object) -> bool:
    return value is True


def _bounded_integer_text(value: object) -> str:
    if isinstance(value, bool):
        return "未知"
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return "未知"
    return str(parsed) if parsed >= 0 else "未知"


def _safe_nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _bounded_plain_text(value: object, limit: int) -> str:
    text = str(value).strip()
    if not text:
        return "未知"
    text = " ".join(text.splitlines())
    return text if len(text) <= limit else f"{text[:limit]}…"


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


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
