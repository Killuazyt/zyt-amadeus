"""Minimal P1 system tray controller."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


def create_app_icon() -> QIcon:
    """Create a generic redistributable icon without private character assets."""

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#111827"))
    painter.setPen(QColor("#22d3ee"))
    painter.drawEllipse(4, 4, 56, 56)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(34)
    painter.setFont(font)
    painter.setPen(QColor("#67e8f9"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "A")
    painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    toggle_requested = Signal()
    show_requested = Signal()
    exit_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._menu = QMenu()
        self.toggle_action = self._menu.addAction("显示控制窗口")
        self._menu.addSeparator()
        self.exit_action = self._menu.addAction("退出")

        self._tray = QSystemTrayIcon(create_app_icon(), self)
        self._tray.setToolTip("Amadeus")
        self._tray.setContextMenu(self._menu)

        self.toggle_action.triggered.connect(self.toggle_requested.emit)
        self.exit_action.triggered.connect(self.exit_requested.emit)
        self._tray.activated.connect(self._on_activated)

    @property
    def is_visible(self) -> bool:
        return self._tray.isVisible()

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def set_control_visible(self, visible: bool) -> None:
        self.toggle_action.setText("隐藏控制窗口" if visible else "显示控制窗口")

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_requested.emit()
