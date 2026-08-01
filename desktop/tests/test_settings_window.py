from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from amadeus_desktop.provider_config import ProviderConfig
from amadeus_desktop.ui.model_settings import ModelSettingsWindow
from amadeus_desktop.ui.settings_window import SettingsWindow


def make_window(qtbot) -> SettingsWindow:
    model_page = ModelSettingsWindow(
        ProviderConfig.default(),
        has_saved_secret=False,
        credential_reader=lambda: None,
    )
    window = SettingsWindow(model_page)
    qtbot.addWidget(window)
    return window


def test_settings_shell_embeds_existing_model_dialog_and_only_exposes_p5_pages(qtbot) -> None:
    window = make_window(qtbot)

    assert window.tabs.count() == 3
    assert [window.tabs.tabText(index) for index in range(3)] == [
        "对话模型",
        "聊天历史",
        "长期记忆",
    ]
    assert window.tabs.widget(0) is window.model_page
    assert window.model_page.windowType() == Qt.WindowType.Widget
    assert window.model_page.close_button.isHidden()
    assert not window.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)


def test_deep_links_emit_page_name_and_reopening_reuses_same_window(qtbot) -> None:
    window = make_window(qtbot)
    pages: list[str] = []
    window.page_changed.connect(pages.append)

    window.show_and_activate("memory")
    qtbot.waitUntil(window.isVisible)
    assert window.current_page == "memory"
    assert pages[-1] == "memory"

    original_identity = id(window)
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.show_and_activate("history")

    assert id(window) == original_identity
    assert window.current_page == "history"
    assert window.isVisible()


def test_escape_hides_without_destroying_embedded_pages(qtbot) -> None:
    window = make_window(qtbot)
    model_page = window.model_page
    window.show_and_activate("model")
    qtbot.waitUntil(window.isVisible)

    qtbot.keyClick(model_page.model_edit, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not window.isVisible())

    assert window.model_page is model_page
    assert window.shutdown()


def test_shutdown_delegates_to_optional_page_hook(qtbot) -> None:
    class ModelPage(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.wait_values: list[int] = []

        def shutdown(self, wait_ms: int = 0) -> bool:
            self.wait_values.append(wait_ms)
            return True

    model_page = ModelPage()
    window = SettingsWindow(model_page)
    qtbot.addWidget(window)

    assert window.shutdown(321)
    assert model_page.wait_values == [321]
