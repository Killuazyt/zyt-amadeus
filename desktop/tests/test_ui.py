from __future__ import annotations

import logging
from copy import deepcopy

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QSystemTrayIcon

from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsRepository
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


def make_controller(qapp, tmp_path, *, tray_available: bool) -> ApplicationController:
    paths = AppPaths.for_current_user(tmp_path)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = deepcopy(DEFAULT_SETTINGS)
    repository.save(settings)
    return ApplicationController(
        qapp,
        FakeInstanceGuard(),  # type: ignore[arg-type]
        make_logger(),
        paths=paths,
        settings_repository=repository,
        settings=settings,
        tray_available=tray_available,
    )


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


def test_controller_tray_path_toggles_pet(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=True)
    guard = controller.instance_guard
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    try:
        assert controller.tray is not None
        assert not controller.window.isVisible()
        assert controller.pet_window.isVisible()

        controller.tray.toggle_action.trigger()
        qtbot.waitUntil(lambda: not controller.pet_window.isVisible())
        assert controller.tray.toggle_action.text() == "显示宠物"

        controller.tray.toggle_action.trigger()
        qtbot.waitUntil(controller.pet_window.isVisible)
        assert controller.tray.toggle_action.text() == "隐藏宠物"
    finally:
        controller.request_exit()
        assert guard.closed is True


def test_controller_fallback_path_shows_exit_window(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=False)
    guard = controller.instance_guard
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    try:
        qtbot.waitUntil(controller.window.isVisible)
        assert controller.tray is None
        assert not controller.window.hide_button.isVisible()
        assert controller.pet_window.isVisible()
    finally:
        controller.request_exit()
        assert guard.closed is True


def test_instance_activation_shows_existing_pet(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=True)
    guard = controller.instance_guard
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    try:
        controller.pet_window.hide()
        assert not controller.pet_window.isVisible()
        guard.activation_requested.emit()
        qtbot.waitUntil(controller.pet_window.isVisible)
    finally:
        controller.request_exit()


def test_drag_finish_persists_relative_position(qapp, qtbot, tmp_path) -> None:
    controller = make_controller(qapp, tmp_path, tray_available=True)
    qtbot.addWidget(controller.window)
    qtbot.addWidget(controller.pet_window)
    try:
        controller.pet_window.move(100, 100)
        controller.pet_window.drag_finished.emit(controller.pet_window.pos())

        saved = controller.settings_repository.load()
        assert saved["pet"]["position"] is not None
        assert 0 <= saved["pet"]["position"]["x_ratio"] <= 1
        assert 0 <= saved["pet"]["position"]["y_ratio"] <= 1
    finally:
        controller.request_exit()
