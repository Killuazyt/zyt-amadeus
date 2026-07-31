"""Composition root for the P1 desktop application."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from copy import deepcopy

from PySide6.QtWidgets import QApplication

from amadeus_desktop import __version__
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.logging_config import close_logger, configure_logging
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsError, SettingsRepository
from amadeus_desktop.single_instance import DEFAULT_SERVER_NAME, SingleInstance


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    application = QApplication(arguments)
    application.setApplicationName("Amadeus")
    application.setApplicationDisplayName("Amadeus")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("Amadeus")
    application.setQuitOnLastWindowClosed(False)

    instance_guard = SingleInstance(DEFAULT_SERVER_NAME)
    if not instance_guard.acquire():
        return 0

    paths = AppPaths.for_current_user()
    paths.initialize()
    logger = configure_logging(paths.log_file)
    logger.info("Application starting version=%s", __version__)

    status_message: str | None = None
    repository = SettingsRepository(paths.settings_file)
    try:
        settings = repository.load_or_create()
    except SettingsError as exc:
        logger.warning("Settings unavailable error_type=%s", type(exc).__name__)
        status_message = "设置文件无法读取，当前使用临时默认值；原文件没有被覆盖。详情请查看日志。"
        settings = deepcopy(DEFAULT_SETTINGS)

    controller = ApplicationController(
        application,
        instance_guard,
        logger,
        paths=paths,
        settings_repository=repository,
        settings=settings,
        status_message=status_message,
    )
    exit_code = application.exec()
    controller._cleanup()
    logger.info("Application stopped exit_code=%s", exit_code)
    close_logger(logger)
    return exit_code
