from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QSystemTrayIcon

from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.ui.control_window import ControlWindow
from amadeus_desktop.ui.tray import TrayController, create_app_icon


class FakeInstanceGuard(QObject):
    activation_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def make_logger() -> logging.Logger:
    logger = logging.getLogger("amadeus.test.ui")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def test_generic_icon_requires_no_external_asset(qapp) -> None:
    assert create_app_icon().isNull() is False


def test_control_window_close_hides_when_tray_exists(qtbot) -> None:
    window = ControlWindow(tray_available=True)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible)

    window.close()

    qtbot.waitUntil(lambda: not window.isVisible())


def test_control_window_close_requests_exit_without_tray(qtbot) -> None:
    window = ControlWindow(tray_available=False)
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.exit_requested, timeout=1000):
        window.close()


def test_tray_double_click_requests_show(qtbot) -> None:
    tray = TrayController()
    with qtbot.waitSignal(tray.show_requested, timeout=1000):
        tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)


def test_controller_tray_path_toggles_window(qapp, qtbot) -> None:
    guard = FakeInstanceGuard()
    controller = ApplicationController(
        qapp,
        guard,  # type: ignore[arg-type]
        make_logger(),
        tray_available=True,
    )
    qtbot.addWidget(controller.window)
    try:
        assert controller.tray is not None
        assert not controller.window.isVisible()

        controller.tray.toggle_action.trigger()
        qtbot.waitUntil(controller.window.isVisible)
        assert controller.tray.toggle_action.text() == "隐藏控制窗口"

        controller.window.close()
        qtbot.waitUntil(lambda: not controller.window.isVisible())
        assert controller.tray.toggle_action.text() == "显示控制窗口"
    finally:
        controller.request_exit()
        assert guard.closed is True


def test_controller_fallback_path_shows_exit_window(qapp, qtbot) -> None:
    guard = FakeInstanceGuard()
    controller = ApplicationController(
        qapp,
        guard,  # type: ignore[arg-type]
        make_logger(),
        tray_available=False,
    )
    qtbot.addWidget(controller.window)
    try:
        qtbot.waitUntil(controller.window.isVisible)
        assert controller.tray is None
        assert not controller.window.hide_button.isVisible()
    finally:
        controller.request_exit()
        assert guard.closed is True


def test_instance_activation_shows_existing_control_window(qapp, qtbot) -> None:
    guard = FakeInstanceGuard()
    controller = ApplicationController(
        qapp,
        guard,  # type: ignore[arg-type]
        make_logger(),
        tray_available=True,
    )
    qtbot.addWidget(controller.window)
    try:
        assert not controller.window.isVisible()
        guard.activation_requested.emit()
        qtbot.waitUntil(controller.window.isVisible)
    finally:
        controller.request_exit()
