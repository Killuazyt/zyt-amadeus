"""Interactive Windows tray acceptance runner used by the P1 release check."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.logging_config import close_logger, configure_logging
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.settings import SettingsRepository
from amadeus_desktop.single_instance import SingleInstance


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-app-data", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--visible-ms", type=int, default=5_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    application = QApplication([])
    application.setApplicationName("Amadeus P1 acceptance")
    application.setQuitOnLastWindowClosed(False)

    instance = SingleInstance(f"amadeus-tray-acceptance-{uuid4().hex}")
    if not instance.acquire():
        return 2

    paths = AppPaths.for_current_user(args.local_app_data)
    paths.initialize()
    SettingsRepository(paths.settings_file).load_or_create()
    logger = configure_logging(paths.log_file, logger_name="amadeus.tray.acceptance")

    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    controller = ApplicationController(
        application,
        instance,
        logger,
        tray_available=tray_available,
        status_message="P1 Windows 托盘与控制窗口验收正在运行。",
    )

    result = {
        "system_tray_available": tray_available,
        "tray_visible": controller.tray is not None and controller.tray.is_visible,
        "window_initially_hidden": not controller.window.isVisible() if tray_available else False,
        "window_visible_after_show": False,
        "window_hidden_after_toggle": False,
        "window_visible_after_double_click": False,
        "graceful_exit": False,
    }

    def show_window() -> None:
        controller.show_control_window()
        result["window_visible_after_show"] = controller.window.isVisible()
        args.ready_file.write_text(str(os.getpid()), encoding="utf-8")

    def toggle_hidden() -> None:
        controller.toggle_control_window()
        result["window_hidden_after_toggle"] = not controller.window.isVisible()

    def simulate_double_click() -> None:
        if controller.tray is not None:
            controller.tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
        result["window_visible_after_double_click"] = controller.window.isVisible()

    def finish() -> None:
        result["graceful_exit"] = True
        args.result_file.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        controller.request_exit()

    QTimer.singleShot(200, show_window)
    QTimer.singleShot(max(400, args.visible_ms), toggle_hidden)
    QTimer.singleShot(max(600, args.visible_ms + 200), simulate_double_click)
    QTimer.singleShot(max(1_000, args.visible_ms + 1_000), finish)

    exit_code = application.exec()
    controller._cleanup()
    close_logger(logger)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
