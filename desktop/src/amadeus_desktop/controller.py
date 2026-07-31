"""Application lifecycle and minimal window coordination."""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from amadeus_desktop.single_instance import SingleInstance
from amadeus_desktop.ui.control_window import ControlWindow
from amadeus_desktop.ui.tray import TrayController


class ApplicationController:
    """Own all P1 UI objects and provide one idempotent exit path."""

    def __init__(
        self,
        application: QApplication,
        instance_guard: SingleInstance,
        logger: logging.Logger,
        *,
        tray_available: bool | None = None,
        status_message: str | None = None,
    ) -> None:
        self.application = application
        self.instance_guard = instance_guard
        self.logger = logger
        self._exiting = False

        if tray_available is None:
            tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray_available = tray_available

        self.window = ControlWindow(
            tray_available=tray_available,
            status_message=status_message,
        )
        self.window.exit_requested.connect(self.request_exit)
        self.instance_guard.activation_requested.connect(self.show_control_window)

        self.tray: TrayController | None = None
        if tray_available:
            self.tray = TrayController()
            self.tray.toggle_requested.connect(self.toggle_control_window)
            self.tray.show_requested.connect(self.show_control_window)
            self.tray.exit_requested.connect(self.request_exit)
            self.window.visibility_changed.connect(self.tray.set_control_visible)
            self.tray.show()
            self.window.hide()
        else:
            self.window.show_and_activate()

        self.application.aboutToQuit.connect(self._cleanup)

    def show_control_window(self) -> None:
        self.window.show_and_activate()

    def toggle_control_window(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        else:
            self.show_control_window()

    def request_exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        self.logger.info("Application exit requested")
        self.window.prepare_to_exit()
        self.window.hide()
        if self.tray is not None:
            self.tray.hide()
        self.instance_guard.close()
        self.application.quit()

    def _cleanup(self) -> None:
        if not self._exiting:
            self._exiting = True
            if self.tray is not None:
                self.tray.hide()
            self.instance_guard.close()
