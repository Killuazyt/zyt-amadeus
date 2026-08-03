"""General P6 settings and destructive data-management entry points."""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class GeneralSettingsPage(QWidget):
    always_on_top_changed = Signal(bool)
    launch_at_login_changed = Signal(bool)
    open_data_requested = Signal()
    open_logs_requested = Signal()
    backup_requested = Signal()
    restore_requested = Signal()
    factory_reset_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        title = QLabel("常规")
        title.setStyleSheet("font-size: 18px; font-weight: 650;")
        summary = QLabel("控制日常启动、窗口行为和本机数据。")
        summary.setWordWrap(True)

        self.launch_at_login = QCheckBox("登录 Windows 后启动 Amadeus")
        self.always_on_top = QCheckBox("桌宠、聊天和问候气泡始终置顶")
        behavior = QGroupBox("启动与窗口")
        behavior_layout = QVBoxLayout(behavior)
        behavior_layout.addWidget(self.launch_at_login)
        behavior_layout.addWidget(self.always_on_top)

        language = QLabel("简体中文")
        language.setObjectName("languageValue")
        language_form = QFormLayout()
        language_form.addRow("界面语言", language)
        language_group = QGroupBox("界面")
        language_group.setLayout(language_form)

        self.open_data_button = QPushButton("打开数据目录")
        self.open_logs_button = QPushButton("打开日志目录")
        self.data_path_value = QLabel()
        self.data_path_value.setObjectName("dataPathValue")
        self.data_path_value.setWordWrap(True)
        self.log_path_value = QLabel()
        self.log_path_value.setObjectName("logPathValue")
        self.log_path_value.setWordWrap(True)
        directory_paths = QFormLayout()
        directory_paths.addRow("数据目录", self.data_path_value)
        directory_paths.addRow("日志目录", self.log_path_value)
        directory_buttons = QHBoxLayout()
        directory_buttons.addWidget(self.open_data_button)
        directory_buttons.addWidget(self.open_logs_button)
        directory_buttons.addStretch(1)

        self.backup_button = QPushButton("创建备份…")
        self.restore_button = QPushButton("从备份恢复…")
        self.factory_reset_button = QPushButton("清除全部本地数据…")
        self.factory_reset_button.setObjectName("factoryResetButton")
        data_buttons = QHBoxLayout()
        data_buttons.addWidget(self.backup_button)
        data_buttons.addWidget(self.restore_button)
        data_buttons.addStretch(1)
        data_buttons.addWidget(self.factory_reset_button)

        privacy = QLabel(
            "备份包含聊天、记忆、来源、索引元数据和设置快照，不包含 API 密钥、"
            "日志、模型缓存、导入宠物或原始角色资料。"
        )
        privacy.setWordWrap(True)
        data_group = QGroupBox("本地数据")
        data_layout = QVBoxLayout(data_group)
        data_layout.addLayout(directory_paths)
        data_layout.addLayout(directory_buttons)
        data_layout.addLayout(data_buttons)
        data_layout.addWidget(privacy)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(summary)
        layout.addWidget(behavior)
        layout.addWidget(language_group)
        layout.addWidget(data_group)
        layout.addStretch(1)

        self.always_on_top.toggled.connect(self.always_on_top_changed.emit)
        self.launch_at_login.toggled.connect(self.launch_at_login_changed.emit)
        self.open_data_button.clicked.connect(self.open_data_requested.emit)
        self.open_logs_button.clicked.connect(self.open_logs_requested.emit)
        self.backup_button.clicked.connect(self.backup_requested.emit)
        self.restore_button.clicked.connect(self.restore_requested.emit)
        self.factory_reset_button.clicked.connect(self.factory_reset_requested.emit)

    def apply_settings(self, *, always_on_top: bool, launch_at_login: bool) -> None:
        with QSignalBlocker(self.always_on_top):
            self.always_on_top.setChecked(bool(always_on_top))
        with QSignalBlocker(self.launch_at_login):
            self.launch_at_login.setChecked(bool(launch_at_login))

    def set_paths(self, *, data_path: str, log_path: str) -> None:
        self.data_path_value.setText(str(data_path))
        self.log_path_value.setText(str(log_path))

    def set_launch_at_login_result(self, enabled: bool, *, error: str | None = None) -> None:
        with QSignalBlocker(self.launch_at_login):
            self.launch_at_login.setChecked(bool(enabled))
        self.launch_at_login.setToolTip(error or "")
