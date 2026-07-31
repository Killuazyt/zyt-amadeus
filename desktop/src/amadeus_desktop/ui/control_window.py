"""Minimal ordinary control window used before the desktop pet exists."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent, QHideEvent, QShowEvent
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class ControlWindow(QWidget):
    exit_requested = Signal()
    visibility_changed = Signal(bool)

    def __init__(self, *, tray_available: bool, status_message: str | None = None) -> None:
        super().__init__()
        self._tray_available = tray_available
        self._allow_close = False

        self.setWindowTitle("Amadeus")
        self.setMinimumSize(360, 180)

        title = QLabel("Amadeus 桌面应用基础")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")

        status = QLabel(status_message or "P1 基础进程正在运行。桌宠与聊天将在后续阶段实现。")
        status.setWordWrap(True)

        self.hide_button = QPushButton("隐藏窗口")
        self.hide_button.setVisible(tray_available)
        self.hide_button.clicked.connect(self.hide)

        self.exit_button = QPushButton("退出 Amadeus")
        self.exit_button.clicked.connect(self.exit_requested.emit)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(status)
        layout.addStretch(1)
        layout.addWidget(self.hide_button)
        layout.addWidget(self.exit_button)

    def show_and_activate(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def prepare_to_exit(self) -> None:
        self._allow_close = True

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name
        if self._allow_close:
            event.accept()
        elif self._tray_available:
            self.hide()
            event.ignore()
        else:
            self.exit_requested.emit()
            event.accept()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API name
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt API name
        super().hideEvent(event)
        self.visibility_changed.emit(False)
