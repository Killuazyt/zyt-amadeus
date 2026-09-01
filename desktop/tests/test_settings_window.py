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


def test_settings_shell_has_p7i_navigation_and_embeds_provider_center(qtbot) -> None:
    window = make_window(qtbot)

    assert window.page_names == (
        "general",
        "pet",
        "model",
        "voice",
        "visual",
        "persona",
        "history",
        "memory",
        "reminders",
        "proactive",
        "diagnostics",
    )
    assert [window.navigation.item(index).text() for index in range(11)] == [
        "常规",
        "桌宠",
        "模型提供商",
        "语音",
        "屏幕与相机",
        "角色",
        "聊天历史",
        "长期记忆",
        "提醒",
        "主动互动",
        "诊断",
    ]
    assert window.stack.count() == 11
    assert window.stack.widget(2) is window.model_page
    assert window.stack.widget(3) is window.voice_page
    assert window.stack.widget(4) is window.visual_page
    assert window.stack.widget(6) is window.history_page
    assert window.stack.widget(7) is window.memory_page
    assert window.stack.widget(8) is window.reminders_page
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


def test_invalid_deep_link_is_rejected_without_changing_page(qtbot) -> None:
    window = make_window(qtbot)
    window.show_page("persona")

    try:
        window.show_page("tools")
    except ValueError as exc:
        assert "tools" in str(exc)
    else:
        raise AssertionError("unsupported settings page must fail closed")

    assert window.current_page == "persona"
