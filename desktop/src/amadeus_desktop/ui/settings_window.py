"""Single-instance P6 settings center with left navigation and eight pages."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QHideEvent, QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from amadeus_desktop.ui.diagnostics_page import DiagnosticsPage
from amadeus_desktop.ui.general_page import GeneralSettingsPage
from amadeus_desktop.ui.history_page import HistoryPage
from amadeus_desktop.ui.memory_page import MemoryPage
from amadeus_desktop.ui.persona_page import PersonaPage
from amadeus_desktop.ui.pet_settings_page import PetSettingsPage
from amadeus_desktop.ui.proactive_page import ProactivePage

_PAGE_SPECS = (
    ("general", "常规"),
    ("pet", "桌宠"),
    ("model", "对话模型"),
    ("persona", "角色"),
    ("history", "聊天历史"),
    ("memory", "长期记忆"),
    ("proactive", "主动互动"),
    ("diagnostics", "诊断"),
)
_PAGE_INDEX = {name: index for index, (name, _label) in enumerate(_PAGE_SPECS)}
_INDEX_PAGE = {index: name for name, index in _PAGE_INDEX.items()}


class SettingsWindow(QDialog):
    """Reusable non-modal settings center; its owner retains the single instance."""

    page_changed = Signal(str)
    visibility_changed = Signal(bool)

    def __init__(
        self,
        model_page: QWidget,
        *,
        general_page: GeneralSettingsPage | None = None,
        pet_page: PetSettingsPage | None = None,
        persona_page: PersonaPage | None = None,
        history_page: HistoryPage | None = None,
        memory_page: MemoryPage | None = None,
        proactive_page: ProactivePage | None = None,
        diagnostics_page: DiagnosticsPage | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsWindow")
        self.setWindowTitle("Amadeus 设置")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setModal(False)
        self.resize(1080, 760)
        self.setMinimumSize(820, 580)

        self.general_page = general_page or GeneralSettingsPage()
        self.pet_page = pet_page or PetSettingsPage()
        self.model_page = model_page
        self.persona_page = persona_page or PersonaPage()
        self.history_page = history_page or HistoryPage()
        self.memory_page = memory_page or MemoryPage()
        self.proactive_page = proactive_page or ProactivePage()
        self.diagnostics_page = diagnostics_page or DiagnosticsPage()
        self._pages: tuple[QWidget, ...] = (
            self.general_page,
            self.pet_page,
            self.model_page,
            self.persona_page,
            self.history_page,
            self.memory_page,
            self.proactive_page,
            self.diagnostics_page,
        )
        for page in self._pages:
            self._prepare_embedded_page(page)

        title = QLabel("Amadeus 设置")
        title.setObjectName("settingsTitle")
        title.setStyleSheet("font-size: 20px; font-weight: 650;")

        self.navigation = QListWidget()
        self.navigation.setObjectName("settingsNavigation")
        self.navigation.setAccessibleName("设置页面")
        self.navigation.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.navigation.setMinimumWidth(154)
        self.navigation.setMaximumWidth(210)
        for name, label in _PAGE_SPECS:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.navigation.addItem(item)

        self.stack = QStackedWidget()
        self.stack.setObjectName("settingsPages")
        for page in self._pages:
            self.stack.addWidget(page)

        body = QHBoxLayout()
        body.addWidget(self.navigation)
        body.addWidget(self.stack, 1)

        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(body, 1)
        layout.addLayout(footer)

        embedded_close = getattr(self.model_page, "close_button", None)
        if isinstance(embedded_close, QWidget):
            embedded_close.hide()

        self.navigation.currentRowChanged.connect(self._on_page_changed)
        self.navigation.setCurrentRow(0)
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    @property
    def current_page(self) -> str:
        return _INDEX_PAGE.get(self.stack.currentIndex(), "general")

    @property
    def page_names(self) -> tuple[str, ...]:
        return tuple(name for name, _label in _PAGE_SPECS)

    def page(self, name: str) -> QWidget:
        try:
            return self._pages[_PAGE_INDEX[name]]
        except KeyError:
            raise ValueError(f"Unsupported settings page: {name!r}") from None

    def show_page(self, page: str) -> None:
        try:
            index = _PAGE_INDEX[page]
        except KeyError:
            raise ValueError(f"Unsupported settings page: {page!r}") from None
        self.navigation.setCurrentRow(index)
        self.stack.setCurrentIndex(index)

    def show_and_activate(self, page: str | None = None) -> None:
        if page is not None:
            self.show_page(page)
        current_widget = self.stack.currentWidget()
        if current_widget is not None:
            current_widget.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def shutdown(self, wait_ms: int = 2_000) -> bool:
        """Cancel optional embedded background work before application shutdown."""

        clean = True
        for page in self._pages:
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
        """Escape and close hide rather than destroy the shared settings instance."""

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
        if page is None:
            return
        self.stack.setCurrentIndex(index)
        self.page_changed.emit(page)

    def _cancel_embedded_transient_work(self) -> None:
        cancel = getattr(self.model_page, "cancel_test", None)
        if callable(cancel):
            cancel()
        for page in self._pages:
            cancel_transient = getattr(page, "cancel_transient_work", None)
            if callable(cancel_transient):
                cancel_transient()

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
