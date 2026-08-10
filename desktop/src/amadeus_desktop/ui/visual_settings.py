"""Visual source selection and opt-in active-vision policy settings."""

from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from amadeus_desktop.visual_runtime import CameraCaptureSource, WindowCaptureSource


class VisualSettingsPage(QWidget):
    save_requested = Signal(object)

    def __init__(self, settings: dict[str, object]) -> None:
        super().__init__()
        self._settings = deepcopy(settings)
        self.active_vision = QCheckBox("允许既有主动互动机会取样最新帧")
        self.active_vision.setChecked(bool(settings["active_vision_enabled"]))
        self.preferred_source = QComboBox()
        self.preferred_source.addItem("屏幕", "screen")
        self.preferred_source.addItem("窗口", "window")
        self.preferred_source.addItem("相机", "camera")
        self.screen_combo = QComboBox()
        self.window_combo = QComboBox()
        self.camera_combo = QComboBox()
        self.refresh_button = QPushButton("刷新来源列表")
        self.refresh_button.clicked.connect(self.reload_sources)
        self.notice = QLabel(
            "实时来源只在本地以 1 FPS 更新一个最新帧槽，不会每秒调用模型。"
            "每次应用启动都保持关闭；必须在聊天面板中显式开始。"
        )
        self.notice.setWordWrap(True)
        self.retention = QLabel(
            "用户文字/语音触发的实际画面最多随该轮保存一张；主动视觉分析帧不会进入历史或长期记忆。"
        )
        self.retention.setWordWrap(True)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.save_button = QPushButton("保存视觉设置")
        self.save_button.clicked.connect(self._save)

        form = QFormLayout()
        form.addRow("默认来源", self.preferred_source)
        form.addRow("屏幕", self.screen_combo)
        form.addRow("窗口", self.window_combo)
        form.addRow("相机", self.camera_combo)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.active_vision)
        layout.addWidget(self.notice)
        layout.addWidget(self.retention)
        layout.addWidget(self.status)
        layout.addWidget(self.save_button)
        layout.addStretch(1)
        self.reload_sources()
        self._select(self.preferred_source, str(settings["preferred_source"]))

    def reload_sources(self) -> None:
        self._fill(
            self.screen_combo,
            tuple((screen.name(), screen.name()) for screen in QGuiApplication.screens()),
            str(self._settings["screen_id"]),
            "系统主屏幕",
        )
        self._fill(
            self.window_combo,
            tuple((item.window_id, item.title) for item in WindowCaptureSource.windows()),
            str(self._settings["window_id"]),
            "请选择窗口",
        )
        self._fill(
            self.camera_combo,
            tuple((item.camera_id, item.description) for item in CameraCaptureSource.cameras()),
            str(self._settings["camera_id"]),
            "系统默认相机",
        )

    def apply_save_result(self, *, success: bool, message: str) -> None:
        self.save_button.setEnabled(True)
        if success:
            self._settings = self.current_settings()
        self.status.setText(message)

    def current_settings(self) -> dict[str, object]:
        return {
            "active_vision_enabled": self.active_vision.isChecked(),
            "latest_frame_fps": 1,
            "preferred_source": str(self.preferred_source.currentData()),
            "screen_id": str(self.screen_combo.currentData() or ""),
            "window_id": str(self.window_combo.currentData() or ""),
            "camera_id": str(self.camera_combo.currentData() or ""),
        }

    def _save(self) -> None:
        self.save_button.setEnabled(False)
        self.save_requested.emit(self.current_settings())

    @staticmethod
    def _fill(
        combo: QComboBox,
        values: tuple[tuple[str, str], ...],
        selected: str,
        default_label: str,
    ) -> None:
        combo.clear()
        combo.addItem(default_label, "")
        for value, label in values:
            combo.addItem(label, value)
        if selected and not VisualSettingsPage._select(combo, selected):
            combo.addItem("已保存来源（当前不可用）", selected)
            VisualSettingsPage._select(combo, selected)

    @staticmethod
    def _select(combo: QComboBox, value: str) -> bool:
        index = combo.findData(value)
        if index < 0:
            return False
        combo.setCurrentIndex(index)
        return True
