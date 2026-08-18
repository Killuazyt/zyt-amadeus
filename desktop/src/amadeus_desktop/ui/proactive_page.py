"""User controls for restrained, local-first proactive greetings."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QSignalBlocker, QTime, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

_MODE_LABELS = (
    ("克制模式", "restrained"),
    ("仅启动问候", "startup_only"),
    ("关闭", "off"),
)


class ProactivePage(QWidget):
    """Expose policy settings while leaving scheduling and persistence outside the UI."""

    mode_changed = Signal(str)
    quiet_hours_changed = Signal(int, int)
    daily_limit_changed = Signal(int)
    pause_today_changed = Signal(bool)
    ai_greetings_enabled_changed = Signal(bool)
    contextual_followups_enabled_changed = Signal(bool)
    greeting_file_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("proactivePage")

        heading = QLabel("主动互动")
        heading.setObjectName("settingsPageHeading")
        heading.setStyleSheet("font-size: 18px; font-weight: 650;")
        explanation = QLabel("问候只在条件允许时显示为宠物旁短气泡，不抢焦点，也不会自动展开聊天。")
        explanation.setWordWrap(True)

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("proactiveMode")
        for label, value in _MODE_LABELS:
            self.mode_combo.addItem(label, value)
        self.quiet_start = QTimeEdit()
        self.quiet_start.setObjectName("proactiveQuietStart")
        self.quiet_start.setDisplayFormat("HH:mm")
        self.quiet_end = QTimeEdit()
        self.quiet_end.setObjectName("proactiveQuietEnd")
        self.quiet_end.setDisplayFormat("HH:mm")
        self.daily_limit = QSpinBox()
        self.daily_limit.setObjectName("proactiveDailyLimit")
        self.daily_limit.setRange(1, 2)
        self.daily_limit.setSuffix(" 次")
        self.pause_today = QCheckBox("今天暂停主动互动")
        self.pause_today.setObjectName("pauseProactiveToday")

        policy_form = QFormLayout()
        policy_form.addRow("模式", self.mode_combo)
        policy_form.addRow("静默开始", self.quiet_start)
        policy_form.addRow("静默结束", self.quiet_end)
        policy_form.addRow("每日上限", self.daily_limit)
        policy_form.addRow("", self.pause_today)
        policy_group = QGroupBox("触发策略")
        policy_group.setLayout(policy_form)

        self.greeting_source_value = QLabel("公开安全的内置短句")
        self.greeting_source_value.setObjectName("greetingSourceValue")
        self.greeting_source_value.setWordWrap(True)
        self.import_greetings_button = QPushButton("加载本地问候 JSON…")
        self.import_greetings_button.setObjectName("importLocalGreetings")
        source_row = QHBoxLayout()
        source_row.addWidget(self.greeting_source_value, 1)
        source_row.addWidget(self.import_greetings_button)

        self.ai_greetings_enabled = QCheckBox("允许使用当前付费模型生成问候")
        self.ai_greetings_enabled.setObjectName("aiGreetingsEnabled")
        paid_note = QLabel(
            "默认关闭。开启后自动请求可能产生费用；请求不携带聊天历史或用户记忆，"
            "失败时回退到本地短句。"
        )
        paid_note.setWordWrap(True)
        source_layout = QVBoxLayout()
        source_layout.addLayout(source_row)
        source_layout.addWidget(self.ai_greetings_enabled)
        source_layout.addWidget(paid_note)
        source_group = QGroupBox("问候来源")
        source_group.setLayout(source_layout)

        self.contextual_followups_enabled = QCheckBox("允许使用逐条确认的陪伴线索")
        self.contextual_followups_enabled.setObjectName("contextualFollowupsEnabled")
        contextual_note = QLabel(
            "默认关闭。线索必须逐条审阅并确认；桌面气泡只显示泛化提示，"
            "每条只主动展示一次，默认确认后 30 天过期。点击后才会在聊天中展开原文。"
        )
        contextual_note.setWordWrap(True)
        contextual_layout = QVBoxLayout()
        contextual_layout.addWidget(self.contextual_followups_enabled)
        contextual_layout.addWidget(contextual_note)
        contextual_group = QGroupBox("上下文待续")
        contextual_group.setLayout(contextual_layout)

        self.status_label = QLabel()
        self.status_label.setObjectName("proactiveStatus")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(explanation)
        layout.addWidget(policy_group)
        layout.addWidget(source_group)
        layout.addWidget(contextual_group)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.mode_combo.currentIndexChanged.connect(self._emit_mode)
        self.quiet_start.timeChanged.connect(self._emit_quiet_hours)
        self.quiet_end.timeChanged.connect(self._emit_quiet_hours)
        self.daily_limit.valueChanged.connect(self.daily_limit_changed.emit)
        self.pause_today.toggled.connect(self.pause_today_changed.emit)
        self.ai_greetings_enabled.toggled.connect(self.ai_greetings_enabled_changed.emit)
        self.contextual_followups_enabled.toggled.connect(
            self.contextual_followups_enabled_changed.emit
        )
        self.import_greetings_button.clicked.connect(self._choose_greeting_file)

    def apply_settings(
        self,
        *,
        mode: str,
        quiet_start_minute: int,
        quiet_end_minute: int,
        daily_limit: int,
        paused_today: bool,
        ai_greetings_enabled: bool,
        contextual_followups_enabled: bool = False,
    ) -> None:
        index = self.mode_combo.findData(mode)
        selected_index = index if index >= 0 else self.mode_combo.findData("off")
        with QSignalBlocker(self.mode_combo):
            self.mode_combo.setCurrentIndex(selected_index)
        with QSignalBlocker(self.quiet_start):
            self.quiet_start.setTime(_minute_to_time(quiet_start_minute))
        with QSignalBlocker(self.quiet_end):
            self.quiet_end.setTime(_minute_to_time(quiet_end_minute))
        with QSignalBlocker(self.daily_limit):
            self.daily_limit.setValue(max(1, min(2, int(daily_limit))))
        with QSignalBlocker(self.pause_today):
            self.pause_today.setChecked(bool(paused_today))
        with QSignalBlocker(self.ai_greetings_enabled):
            self.ai_greetings_enabled.setChecked(bool(ai_greetings_enabled))
        with QSignalBlocker(self.contextual_followups_enabled):
            self.contextual_followups_enabled.setChecked(bool(contextual_followups_enabled))

    def set_paused_local_date(
        self,
        paused_local_date: str | date | None,
        *,
        today: date | None = None,
    ) -> None:
        current = today or date.today()
        value = (
            paused_local_date.isoformat()
            if isinstance(paused_local_date, date)
            else paused_local_date
        )
        with QSignalBlocker(self.pause_today):
            self.pause_today.setChecked(value == current.isoformat())

    def set_greeting_source(self, text: str | None) -> None:
        source = text.strip() if text and text.strip() else "公开安全的内置短句"
        self.greeting_source_value.setText(source)

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("error", error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    @Slot(int)
    def _emit_mode(self, _index: int) -> None:
        self.mode_changed.emit(str(self.mode_combo.currentData() or "off"))

    @Slot(QTime)
    def _emit_quiet_hours(self, _time: QTime) -> None:
        self.quiet_hours_changed.emit(
            _time_to_minute(self.quiet_start.time()),
            _time_to_minute(self.quiet_end.time()),
        )

    def _choose_greeting_file(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择本地问候文件",
            "",
            "JSON (*.json);;所有文件 (*)",
        )
        if path:
            self.greeting_file_requested.emit(path)


def _minute_to_time(value: int) -> QTime:
    try:
        minute = int(value)
    except (TypeError, ValueError):
        minute = 0
    minute = min(24 * 60 - 1, max(0, minute))
    return QTime(minute // 60, minute % 60)


def _time_to_minute(value: QTime) -> int:
    return value.hour() * 60 + value.minute()
