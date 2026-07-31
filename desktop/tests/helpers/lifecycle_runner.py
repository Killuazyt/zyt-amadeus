"""Test-only launcher for process lifecycle and single-instance acceptance."""

from __future__ import annotations

import argparse
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.logging_config import close_logger, configure_logging
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.settings import SettingsRepository
from amadeus_desktop.single_instance import SingleInstance


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-name", required=True)
    parser.add_argument("--local-app-data", required=True, type=Path)
    parser.add_argument("--auto-exit-ms", type=int, default=10_000)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--activated-file", type=Path)
    parser.add_argument("--exit-on-activation", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    application = QApplication([])
    application.setQuitOnLastWindowClosed(False)

    instance = SingleInstance(args.instance_name)
    if not instance.acquire():
        return 0

    paths = AppPaths.for_current_user(args.local_app_data)
    paths.initialize()
    SettingsRepository(paths.settings_file).load_or_create()
    logger = configure_logging(paths.log_file, logger_name=f"amadeus.test.{args.instance_name}")
    controller = ApplicationController(
        application,
        instance,
        logger,
        tray_available=False,
    )

    def on_activation() -> None:
        if args.activated_file is not None:
            args.activated_file.write_text("activated", encoding="utf-8")
        if args.exit_on_activation:
            QTimer.singleShot(0, controller.request_exit)

    instance.activation_requested.connect(on_activation)
    if args.ready_file is not None:
        args.ready_file.write_text("ready", encoding="utf-8")
    QTimer.singleShot(args.auto_exit_ms, controller.request_exit)

    exit_code = application.exec()
    controller._cleanup()
    close_logger(logger)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
