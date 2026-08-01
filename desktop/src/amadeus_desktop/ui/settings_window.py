"""P5 settings-window shell exposing only model, history, and memory pages."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QHideEvent, QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from amadeus_desktop.ui.history_page import HistoryPage
from amadeus_desktop.ui.memory_page import MemoryPage

_PAGE_INDEX = {"model": 0, "history": 1, "memory": 2}
_INDEX_PAGE = {index: name for name, index in _PAGE_INDEX.items()}


class SettingsWindow(QDialog):
    """Reusable non-modal settings window; its owner keeps the single instance alive."""

    page_changed = Signal(str)
    visibility_changed = Signal(bool)

    def __init__(
        self,
        model_page: QWidget,
        *,
        history_page: HistoryPage | None = None,
        memory_page: MemoryPage | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsWindow")
        self.setWindowTitle("Amadeus 设置")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setModal(False)
        self.resize(1020, 720)
        self.setMinimumSize(760, 540)

        self.model_page = model_page
        self.history_page = history_page or HistoryPage()
        self.memory_page = memory_page or MemoryPage()
        self._prepare_embedded_page(self.model_page)
        self._prepare_embedded_page(self.history_page)
        self._prepare_embedded_page(self.memory_page)

        title = QLabel("Amadeus 设置")
        title.setObjectName("settingsTitle")
        title.setStyleSheet("font-size: 20px; font-weight: 650;")
        scope = QLabel("P5A 仅开放对话模型、聊天历史与长期记忆。")
        scope.setObjectName("settingsScopeNotice")
        scope.setWordWrap(True)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsTabs")
        self.tabs.addTab(self.model_page, "对话模型")
        self.tabs.addTab(self.history_page, "聊天历史")
        self.tabs.addTab(self.memory_page, "长期记忆")

        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(scope)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(footer)

        embedded_close = getattr(self.model_page, "close_button", None)
        if isinstance(embedded_close, QWidget):
            embedded_close.hide()

        self.tabs.currentChanged.connect(self._on_page_changed)
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    @property
    def current_page(self) -> str:
        return _INDEX_PAGE.get(self.tabs.currentIndex(), "model")

    def show_page(self, page: str) -> None:
        try:
            index = _PAGE_INDEX[page]
        except KeyError:
            raise ValueError(f"Unsupported settings page: {page!r}") from None
        self.tabs.setCurrentIndex(index)

    def show_and_activate(self, page: str | None = None) -> None:
        if page is not None:
            self.show_page(page)
        current_widget = self.tabs.currentWidget()
        if current_widget is not None:
            current_widget.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def shutdown(self, wait_ms: int = 2_000) -> bool:
        """Cancel optional embedded background work before application shutdown."""

        clean = True
        for page in (self.model_page, self.history_page, self.memory_page):
            callback = getattr(page, "shutdown", None)
            if not callable(callback):
                continue
            clean = _call_shutdown(callback, wait_ms) and clean
        self.hide()
        return clean

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name
        self._cancel_embedded_transient_work()
        self.hide()
        event.ignore()

    def reject(self) -> None:
        """Escape and the window close button hide rather than destroy the shared instance."""

        self._cancel_embedded_transient_work()
        self.hide()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API name
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt API name
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt API name
        if (
            event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
        ):
            self.reject()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    @Slot(int)
    def _on_page_changed(self, index: int) -> None:
        page = _INDEX_PAGE.get(index)
        if page is not None:
            self.page_changed.emit(page)

    def _cancel_embedded_transient_work(self) -> None:
        cancel = getattr(self.model_page, "cancel_test", None)
        if callable(cancel):
            cancel()

    @staticmethod
    def _prepare_embedded_page(page: QWidget) -> None:
        page.hide()
        page.setParent(None)
        page.setWindowFlags(Qt.WindowType.Widget)
        page.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


def _call_shutdown(callback: Callable[..., object], wait_ms: int) -> bool:
    try:
        result = callback(wait_ms=wait_ms)
    except TypeError:
        result = callback()
    return result is not False
