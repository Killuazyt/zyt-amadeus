"""P6 system tray menu and state synchronization."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
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
    """Own the exact eight-item P6 tray menu and expose intent-only signals."""

    toggle_requested = Signal()
    open_chat_requested = Signal()
    memory_requested = Signal()
    settings_requested = Signal()
    always_on_top_changed = Signal(bool)
    pause_proactive_today_changed = Signal(bool)
    launch_at_login_changed = Signal(bool)
    exit_requested = Signal()

    # Transitional P5 signal names remain available until controller composition is
    # switched atomically to the P6 entry points.
    show_requested = Signal()
    model_settings_requested = Signal()

    def __init__(
        self,
        *,
        system_tray_factory: Callable[[QIcon, QObject], QSystemTrayIcon] = QSystemTrayIcon,
    ) -> None:
        super().__init__()
        self._closed = False
        self._menu = QMenu()

        # Product order is intentionally exact; do not insert separators or placeholders.
        self.toggle_action = self._menu.addAction("隐藏宠物")
        self.open_chat_action = self._menu.addAction("打开对话")
        self.memory_action = self._menu.addAction("记忆管理…")
        self.settings_action = self._menu.addAction("设置…")
        self.always_on_top_action = self._checkable_action("始终置顶")
        self.pause_proactive_today_action = self._checkable_action("今天暂停主动互动")
        self.launch_at_login_action = self._checkable_action("开机启动")
        self.exit_action = self._menu.addAction("退出")

        # The old action attribute is retained as a strict alias, not a ninth item.
        self.model_settings_action = self.settings_action

        self._tray = system_tray_factory(create_app_icon(), self)
        self._tray.setToolTip("Amadeus")
        self._tray.setContextMenu(self._menu)

        self.toggle_action.triggered.connect(self.toggle_requested.emit)
        self.open_chat_action.triggered.connect(self.open_chat_requested.emit)
        self.memory_action.triggered.connect(self.memory_requested.emit)
        self.settings_action.triggered.connect(self._request_settings)
        self.always_on_top_action.toggled.connect(self.always_on_top_changed.emit)
        self.pause_proactive_today_action.toggled.connect(self.pause_proactive_today_changed.emit)
        self.launch_at_login_action.toggled.connect(self.launch_at_login_changed.emit)
        self.exit_action.triggered.connect(self.exit_requested.emit)
        self._tray.activated.connect(self._on_activated)

    @property
    def is_visible(self) -> bool:
        return self._tray.isVisible()

    @property
    def actions(self) -> tuple[QAction, ...]:
        return tuple(self._menu.actions())

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def close(self) -> None:
        """Detach native tray/menu resources in a deterministic order."""

        if self._closed:
            return
        self._closed = True
        self._tray.hide()
        self._tray.setContextMenu(None)
        self._menu.close()

    def set_pet_visible(self, visible: bool) -> None:
        self.toggle_action.setText("隐藏宠物" if visible else "显示宠物")

    def set_always_on_top(self, enabled: bool) -> None:
        _set_checked(self.always_on_top_action, enabled)

    def set_proactive_paused_today(self, paused: bool) -> None:
        _set_checked(self.pause_proactive_today_action, paused)

    def set_launch_at_login(self, enabled: bool) -> None:
        _set_checked(self.launch_at_login_action, enabled)

    def apply_state(
        self,
        *,
        pet_visible: bool,
        always_on_top: bool,
        proactive_paused_today: bool,
        launch_at_login: bool,
    ) -> None:
        """Synchronize all check states without reporting them as user input."""

        self.set_pet_visible(pet_visible)
        self.set_always_on_top(always_on_top)
        self.set_proactive_paused_today(proactive_paused_today)
        self.set_launch_at_login(launch_at_login)

    def _checkable_action(self, text: str) -> QAction:
        action = self._menu.addAction(text)
        action.setCheckable(True)
        return action

    def _request_settings(self) -> None:
        self.settings_requested.emit()
        self.model_settings_requested.emit()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_chat_requested.emit()
            self.show_requested.emit()


def _set_checked(action: QAction, checked: bool) -> None:
    with QSignalBlocker(action):
        action.setChecked(bool(checked))
