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

from amadeus_desktop.build_info import BuildInfo, BuildInfoError, load_build_info
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.credential_store import run_wincred_acceptance_probe
from amadeus_desktop.data_management import DataManagementError, recover_interrupted_restore
from amadeus_desktop.installation_mutex import InstallationMutex, InstallationMutexError
from amadeus_desktop.logging_config import close_logger, configure_logging
from amadeus_desktop.maintenance import (
    UNINSTALL_DELETE_DATA_ARGUMENT,
    MaintenanceError,
    delete_all_local_data_for_uninstall,
)
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsError, SettingsRepository
from amadeus_desktop.single_instance import DEFAULT_SERVER_NAME, SingleInstance
from amadeus_desktop.ui.tray import create_app_icon


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    internal_arguments = arguments[1:]
    if any(value.startswith("--uninstall-cleanup") for value in internal_arguments):
        if internal_arguments != [UNINSTALL_DELETE_DATA_ARGUMENT] or not _is_frozen():
            return 6
        return _run_uninstall_cleanup()
    if any(value.startswith("--wincred-acceptance-probe") for value in internal_arguments):
        probe_id = _wincred_acceptance_probe_id(internal_arguments)
        if probe_id is None or not _is_frozen():
            return 6
        return int(not run_wincred_acceptance_probe(probe_id))
    if "--embedding-model-probe" in arguments[1:]:
        return _run_embedding_model_probe()
    if "--fts-degraded-probe" in arguments[1:]:
        return _run_fts_degraded_probe()
    lifecycle_mutex = InstallationMutex()
    try:
        lifecycle_mutex.acquire()
    except InstallationMutexError:
        return 5
    try:
        try:
            build_info = load_build_info()
        except BuildInfoError:
            return 5
        return _run_application(arguments, build_info)
    finally:
        lifecycle_mutex.close()


def _run_application(arguments: list[str], build_info: BuildInfo) -> int:
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
    application.setApplicationVersion(build_info.version)
    application.setOrganizationName("Amadeus")
    application.setWindowIcon(create_app_icon())
    application.setQuitOnLastWindowClosed(False)

    instance_guard = SingleInstance(acceptance_instance_name or DEFAULT_SERVER_NAME)
    if not instance_guard.acquire():
        return 0

    paths = AppPaths.for_current_user()
    paths.initialize()
    logger = configure_logging(paths.log_file)
    logger.info(
        "Application starting version=%s commit_sha=%s build_date_utc=%s",
        build_info.version,
        build_info.commit_sha,
        build_info.build_date_utc,
    )

    try:
        restore_recovered = recover_interrupted_restore(paths)
    except DataManagementError as exc:
        logger.critical(
            "Interrupted restore recovery failed closed error_type=%s",
            type(exc).__name__,
        )
        instance_guard.close()
        close_logger(logger)
        return 4
    if restore_recovered:
        logger.warning("Interrupted restore transaction recovered before startup")

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
        build_info=build_info,
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


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _wincred_acceptance_probe_id(arguments: Sequence[str]) -> str | None:
    if len(arguments) != 1:
        return None
    prefix = "--wincred-acceptance-probe="
    value = arguments[0]
    if not value.startswith(prefix):
        return None
    probe_id = value.removeprefix(prefix)
    return probe_id if re.fullmatch(r"[0-9a-f]{32}", probe_id) is not None else None


def _run_uninstall_cleanup() -> int:
    try:
        delete_all_local_data_for_uninstall()
    except MaintenanceError:
        return 6
    return 0


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
            finish(mode == "ready" and _ready_lifecycle_surface_is_valid(application, controller))
        elif category in failure_categories:
            finish(mode == "degraded")

    controller.vector_index.status_changed.connect(status_changed)
    # Startup may complete before this acceptance-only observer is attached,
    # especially in a frozen build. Observe the immutable current snapshot as
    # well as subsequent signals so a terminal state cannot be missed.
    status_changed(controller.vector_index.status)
    QTimer.singleShot(30_000, application, lambda: finish(False))


def _ready_lifecycle_surface_is_valid(
    application: QApplication,
    controller: ApplicationController,
) -> bool:
    """Require the real Windows Sandbox probe to exercise public pet and tray assets."""

    if application.platformName().casefold() != "windows":
        return True
    try:
        tray = controller.tray
        return bool(
            tray is not None
            and tray.is_visible
            and controller.pet_window.isVisible()
            and controller.pet_window.asset.manifest.pet_id == "builtin-amadeus"
        )
    except (AttributeError, RuntimeError):
        return False


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
