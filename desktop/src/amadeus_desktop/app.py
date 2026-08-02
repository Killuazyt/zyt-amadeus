"""Composition root for the Amadeus desktop application."""

from __future__ import annotations

import re
import socket
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from amadeus_desktop import __version__
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.logging_config import close_logger, configure_logging
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsError, SettingsRepository
from amadeus_desktop.single_instance import DEFAULT_SERVER_NAME, SingleInstance


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    if "--embedding-model-probe" in arguments[1:]:
        return _run_embedding_model_probe()
    if "--fts-degraded-probe" in arguments[1:]:
        return _run_fts_degraded_probe()
    mock_chat = "--mock-chat" in arguments[1:]
    auto_exit_ms = _auto_exit_delay(arguments[1:])
    embedding_lifecycle_mode = _embedding_lifecycle_mode(arguments[1:])
    acceptance_instance_name = _acceptance_instance_name(arguments[1:])
    qt_arguments = [
        argument
        for argument in arguments
        if argument != "--mock-chat"
        and not argument.startswith("--auto-exit-ms=")
        and not argument.startswith("--embedding-lifecycle-probe=")
        and not argument.startswith("--acceptance-instance-name=")
    ]
    application = QApplication(qt_arguments)
    application.setApplicationName("Amadeus")
    application.setApplicationDisplayName("Amadeus")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("Amadeus")
    application.setQuitOnLastWindowClosed(False)

    instance_guard = SingleInstance(acceptance_instance_name or DEFAULT_SERVER_NAME)
    if not instance_guard.acquire():
        return 0

    paths = AppPaths.for_current_user()
    paths.initialize()
    logger = configure_logging(paths.log_file)
    logger.info("Application starting version=%s", __version__)

    status_message: str | None = None
    settings_trusted = True
    repository = SettingsRepository(paths.settings_file)
    try:
        settings = repository.load_or_create()
    except SettingsError as exc:
        logger.warning("Settings unavailable error_type=%s", type(exc).__name__)
        status_message = "设置文件无法读取，当前使用临时默认值；原文件没有被覆盖。详情请查看日志。"
        settings = deepcopy(DEFAULT_SETTINGS)
        settings_trusted = False

    controller = ApplicationController(
        application,
        instance_guard,
        logger,
        paths=paths,
        settings_repository=repository,
        settings=settings,
        status_message=status_message,
        mock_chat=mock_chat,
        allow_saved_provider=settings_trusted,
    )
    if auto_exit_ms is not None:
        QTimer.singleShot(auto_exit_ms, controller.request_exit)
    lifecycle_result: list[bool] = []
    if embedding_lifecycle_mode is not None:
        _configure_embedding_lifecycle_probe(
            application,
            controller,
            embedding_lifecycle_mode,
            lifecycle_result,
        )
    exit_code = application.exec()
    controller._cleanup()
    logger.info("Application stopped exit_code=%s", exit_code)
    close_logger(logger)
    if embedding_lifecycle_mode is not None and (
        lifecycle_result != [True] or controller.shutdown_clean is not True
    ):
        return 3
    return exit_code


def _auto_exit_delay(arguments: Sequence[str]) -> int | None:
    """Parse the bounded, hidden lifecycle-probe switch used by packaging checks."""

    values = [value.partition("=")[2] for value in arguments if value.startswith("--auto-exit-ms=")]
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit():
        return None
    delay = int(values[0])
    return delay if 1 <= delay <= 60_000 else None


def _embedding_lifecycle_mode(arguments: Sequence[str]) -> str | None:
    values = [
        value.partition("=")[2]
        for value in arguments
        if value.startswith("--embedding-lifecycle-probe=")
    ]
    if len(values) != 1 or values[0] not in {"ready", "degraded"}:
        return None
    return values[0]


def _acceptance_instance_name(arguments: Sequence[str]) -> str | None:
    values = [
        value.partition("=")[2]
        for value in arguments
        if value.startswith("--acceptance-instance-name=")
    ]
    if len(values) != 1 or re.fullmatch(r"amadeus-acceptance-[0-9a-f]{32}", values[0]) is None:
        return None
    return values[0]


def _configure_embedding_lifecycle_probe(
    application: QApplication,
    controller: ApplicationController,
    mode: str,
    result: list[bool],
) -> None:
    """Exit a full hidden app lifecycle only after a definitive vector state."""

    failure_categories = {
        "model_missing",
        "model_corrupt",
        "model_version_mismatch",
        "model_runtime_unavailable",
        "model_inference_failed",
        "generation_model_mismatch",
        "invalid_vector",
        "storage_error",
        "storage_unavailable",
        "runtime_unavailable",
    }

    def finish(succeeded: bool) -> None:
        if result:
            return
        result.append(succeeded)
        QTimer.singleShot(0, application, controller.request_exit)

    def status_changed(status: object) -> None:
        category = str(getattr(status, "category", ""))
        if category == "ready":
            finish(mode == "ready")
        elif category in failure_categories:
            finish(mode == "degraded")

    controller.vector_index.status_changed.connect(status_changed)
    # Startup may complete before this acceptance-only observer is attached,
    # especially in a frozen build. Observe the immutable current snapshot as
    # well as subsequent signals so a terminal state cannot be missed.
    status_changed(controller.vector_index.status)
    QTimer.singleShot(30_000, application, lambda: finish(False))


def _run_embedding_model_probe() -> int:
    """Exercise the packaged fixed model without Qt, user data, or network access."""

    from amadeus_desktop.embedding_backend import CPU_PROVIDER
    from amadeus_desktop.embedding_model import (
        MODEL_DIMENSION,
        resolve_runtime_model_directory,
        verify_model,
    )

    paths = AppPaths.for_current_user()
    try:
        with _network_denied():
            verification = verify_model(
                resolve_runtime_model_directory(paths.embedding_model_directory)
            )
    except Exception:
        return 2
    return int(
        not (
            verification.ready
            and verification.dimension == MODEL_DIMENSION
            and verification.provider == CPU_PROVIDER
        )
    )


def _run_fts_degraded_probe() -> int:
    """Prove packaged SQLite/FTS recall works while the vector model is absent."""

    from amadeus_desktop.database import SQLiteDatabase
    from amadeus_desktop.embedding_model import inspect_model, resolve_runtime_model_directory
    from amadeus_desktop.memory_store import MemoryStore
    from amadeus_desktop.storage_models import MemoryVersionOrigin

    paths = AppPaths.for_current_user()
    try:
        with _network_denied():
            candidate = resolve_runtime_model_directory(paths.embedding_model_directory)
            if inspect_model(candidate).ready:
                return 2
            with tempfile.TemporaryDirectory(prefix="amadeus-fts-probe-") as root:
                database = SQLiteDatabase(f"{root}/probe.sqlite3").open()
                try:
                    store = MemoryStore(database)
                    created = store.create_memory(
                        "preference",
                        "synthetic:drink",
                        "用户偏好合成咖啡测试数据",
                        origin=MemoryVersionOrigin.MANUAL,
                        memory_id="synthetic-fts-probe",
                    )
                    hits = store.search("咖啡")
                finally:
                    database.close()
        return int(not hits or hits[0].memory.memory_id != created.memory_id)
    except Exception:
        return 2


@contextmanager
def _network_denied() -> Iterator[None]:
    """Reject socket creation during the hidden packaged-model acceptance probe."""

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_sendto = socket.socket.sendto

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise OSError("network disabled for embedding probe")

    socket.socket.connect = blocked  # type: ignore[method-assign]
    socket.socket.connect_ex = blocked  # type: ignore[method-assign]
    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    socket.socket.sendto = blocked  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo
        socket.socket.sendto = original_sendto  # type: ignore[method-assign]
