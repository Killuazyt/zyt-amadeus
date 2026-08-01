"""Privacy-safe P5A DeepSeek smoke runner.

The default invocation performs preflight only and never starts a provider
request.  ``--execute-live`` is the explicit network opt-in.  Live execution
copies only the validated, non-sensitive provider configuration into a
temporary LOCALAPPDATA-equivalent root.  The API credential stays in the
fixed Windows Credential Manager target and is never copied, logged, or
included in the JSON report.

The report intentionally contains only counts, booleans, latency buckets, and
stable error categories.  Temporary chat and memory content is deleted when
the process leaves the temporary-directory context.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

import amadeus_desktop.settings as settings_module
from amadeus_desktop.chat_models import ConversationState, MessageStatus
from amadeus_desktop.chat_provider import ChatProvider
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.credential_store import (
    CredentialStore,
    CredentialStoreError,
    WinCredentialStore,
)
from amadeus_desktop.local_data_service import MemoryListSnapshot
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.provider_config import ProviderConfig, ProviderPreset
from amadeus_desktop.settings import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_SETTINGS,
    SettingsError,
    SettingsRepository,
)

_MAX_SOURCE_SETTINGS_BYTES = 256 * 1024
_SCENARIOS = (
    ("initial", "合成验收信息：我更喜欢无糖咖啡。请简短确认。"),
    ("exact_duplicate", "合成验收信息：我更喜欢无糖咖啡。请简短确认。"),
    (
        "explicit_correction",
        "更正合成验收信息：我不喜欢无糖咖啡，我更喜欢无糖茶。请简短确认。",
    ),
    (
        "do_not_remember",
        "不要记住这条合成验收信息：我喜欢紫色文件夹。请简短确认。",
    ),
)
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed"})


class _SmokeInstanceGuard(QObject):
    """Process-local guard sufficient for an isolated acceptance controller."""

    activation_requested = Signal()

    def close(self) -> None:
        return


class _SmokeTimeout(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass(frozen=True, slots=True)
class SmokePreflight:
    """Internal preflight state; ``configuration`` is never serialized directly."""

    ready: bool
    category: str
    provider_enabled: bool
    credential_configured: bool
    deepseek_contract: bool
    configuration: ProviderConfig | None = None

    def public_report(self, *, live_requested: bool) -> dict[str, object]:
        return {
            "mode": "live" if live_requested else "preflight",
            "status": "ready" if self.ready else "blocked",
            "category": self.category,
            "checks": {
                "provider_enabled": self.provider_enabled,
                "credential_configured": self.credential_configured,
                "deepseek_contract": self.deepseek_contract,
            },
            "live_execution_requested": live_requested,
            "network_requests_started": 0,
        }


@dataclass(frozen=True, slots=True)
class _DatabaseSnapshot:
    memory_groups: int
    memory_versions: int
    memory_sources: int
    memory_fts: int
    operation_counts: Mapping[str, int]
    job_status_counts: Mapping[str, int]
    message_status_counts: Mapping[str, int]

    @property
    def extraction_job_count(self) -> int:
        return sum(self.job_status_counts.values())

    def public_counts(self) -> dict[str, object]:
        return {
            "memory_groups": self.memory_groups,
            "memory_versions": self.memory_versions,
            "memory_sources": self.memory_sources,
            "memory_fts": self.memory_fts,
            "memory_operation_counts": dict(sorted(self.operation_counts.items())),
            "extraction_job_status_counts": dict(sorted(self.job_status_counts.items())),
            "message_status_counts": dict(sorted(self.message_status_counts.items())),
        }


@dataclass(frozen=True, slots=True)
class _ScenarioDelta:
    memory_groups: int
    memory_versions: int
    memory_sources: int
    correct_versions: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight P5A DeepSeek smoke safely; pass --execute-live to opt in to network calls."
        )
    )
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help="Explicitly permit synthetic DeepSeek requests using the existing WinCred secret.",
    )
    parser.add_argument(
        "--source-local-app-data",
        type=Path,
        default=None,
        help="Optional LOCALAPPDATA base containing the existing Amadeus settings.",
    )
    parser.add_argument("--turn-timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--job-timeout-seconds",
        type=float,
        default=900.0,
        help="Wait budget covering the P5A 1-minute and 10-minute retry schedule.",
    )
    return parser.parse_args(argv)


def preflight(
    source_local_app_data: Path | None = None,
    *,
    credential_store: CredentialStore | None = None,
) -> SmokePreflight:
    """Validate only non-sensitive settings and WinCred presence."""

    source_paths = AppPaths.for_current_user(source_local_app_data)
    settings_path = source_paths.settings_file
    try:
        stat = settings_path.stat()
    except FileNotFoundError:
        return _blocked("settings_missing")
    except OSError:
        return _blocked("settings_unavailable")
    if not settings_path.is_file() or not 0 < stat.st_size <= _MAX_SOURCE_SETTINGS_BYTES:
        return _blocked("settings_size_invalid")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise TypeError
        SettingsRepository._reject_sensitive_keys(settings)
        version = settings.get("schema_version", 0)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ValueError
        if version > CURRENT_SCHEMA_VERSION:
            raise ValueError
        settings = deepcopy(settings)
        while version < CURRENT_SCHEMA_VERSION:
            migration = settings_module._MIGRATIONS.get(version)
            if migration is None:
                raise ValueError
            settings = migration(settings)
            version = settings.get("schema_version")
            if not isinstance(version, int):
                raise ValueError
        SettingsRepository._validate(settings)
        configuration = ProviderConfig.from_mapping(settings["provider"])
    except (OSError, SettingsError, KeyError, TypeError, ValueError):
        return _blocked("settings_invalid")

    provider_enabled = settings.get("provider_enabled") is True
    deepseek_contract = configuration.preset is ProviderPreset.DEEPSEEK_PAYG
    store = credential_store or WinCredentialStore()
    try:
        credential_configured = store.has_secret()
    except CredentialStoreError:
        return SmokePreflight(
            ready=False,
            category="credential_store_unavailable",
            provider_enabled=provider_enabled,
            credential_configured=False,
            deepseek_contract=deepseek_contract,
            configuration=None,
        )
    if not provider_enabled:
        category = "provider_disabled"
    elif not deepseek_contract:
        category = "provider_not_deepseek"
    elif not credential_configured:
        category = "credential_missing"
    else:
        category = "ready"
    return SmokePreflight(
        ready=category == "ready",
        category=category,
        provider_enabled=provider_enabled,
        credential_configured=credential_configured,
        deepseek_contract=deepseek_contract,
        configuration=configuration if category == "ready" else None,
    )


def execute_smoke(
    preflight_result: SmokePreflight,
    *,
    application: QApplication | None = None,
    credential_store: CredentialStore | None = None,
    provider_override: ChatProvider | None = None,
    temporary_parent: Path | None = None,
    turn_timeout_seconds: float = 120.0,
    job_timeout_seconds: float = 900.0,
) -> dict[str, object]:
    """Run the isolated production-controller smoke after explicit caller opt-in.

    ``provider_override`` exists only so the orchestration and privacy contract
    can be exercised offline.  The command-line path never supplies it.
    """

    if not preflight_result.ready or preflight_result.configuration is None:
        return preflight_result.public_report(live_requested=True)
    if turn_timeout_seconds <= 0 or job_timeout_seconds <= 0:
        return _failure_report("invalid_timeout")

    previous_qt_handler = qInstallMessageHandler(_discard_qt_message)
    try:
        app = application or QApplication.instance() or QApplication([])
        app.setApplicationName("Amadeus P5A isolated live smoke")
        app.setQuitOnLastWindowClosed(False)
    except Exception as exc:  # noqa: BLE001 - retain only a stable type category
        qInstallMessageHandler(previous_qt_handler)
        return _failure_report(_exception_category(exc))
    store = credential_store or WinCredentialStore()
    terminal_events: list[dict[str, object]] = []
    scenario_checks: dict[str, bool] = {}
    scenario_deltas: dict[str, dict[str, int]] = {}
    latencies_ms: list[int] = []
    foreground_send_count = 0
    controller: ApplicationController | None = None
    restart_controller: ApplicationController | None = None
    lifecycle_clean = True
    final_snapshot = _empty_snapshot()
    error_category = "none"
    success_report: dict[str, object] | None = None
    temporary_context: TemporaryDirectory[str] | None = None
    temporary_data_cleaned = False

    try:
        temporary_context = TemporaryDirectory(
            prefix="amadeus-p5a-live-smoke-",
            dir=str(temporary_parent) if temporary_parent is not None else None,
        )
        temporary = temporary_context.name
        if temporary:
            isolated_base = Path(temporary) / "LocalAppData"
            paths = AppPaths.for_current_user(isolated_base)
            paths.initialize()
            repository = SettingsRepository(paths.settings_file)
            isolated_settings = deepcopy(DEFAULT_SETTINGS)
            isolated_settings["provider_enabled"] = True
            isolated_settings["provider"] = preflight_result.configuration.to_mapping()
            isolated_settings["memory"]["enabled"] = True
            repository.save(isolated_settings)

            controller = _create_controller(
                app,
                paths,
                repository,
                isolated_settings,
                store,
                provider_override,
            )
            _wait_until(
                app,
                lambda: controller is not None and controller._data_initialized,
                timeout_seconds=10.0,
                timeout_category="data_startup_timeout",
            )
            if not controller._data_writable:
                raise _SmokeTimeout("data_not_writable")

            started_at = 0.0

            def record_terminal(_request_id: str, turn: object, state: object) -> None:
                nonlocal started_at
                elapsed = max(0, round((time.perf_counter() - started_at) * 1_000))
                assistant = getattr(turn, "assistant_message", None)
                message_status = getattr(getattr(assistant, "status", None), "value", "unknown")
                state_value = getattr(state, "value", "unknown")
                category_value = getattr(turn, "provider_error_code", None)
                terminal_reason = getattr(turn, "terminal_reason", None)
                category = category_value or getattr(terminal_reason, "value", "unknown")
                terminal_events.append(
                    {
                        "state": _safe_value(state_value),
                        "message_status": _safe_value(message_status),
                        "category": _safe_value(category),
                    }
                )
                latencies_ms.append(elapsed)

            controller.conversation.request_finished.connect(record_terminal)
            previous = _database_snapshot(paths.database_file)
            for index, (scenario_name, synthetic_text) in enumerate(_SCENARIOS, start=1):
                event_count = len(terminal_events)
                started_at = time.perf_counter()
                foreground_send_count += 1
                controller.chat_panel.send_requested.emit(synthetic_text)
                _wait_until(
                    app,
                    lambda expected_events=event_count + 1: len(terminal_events) >= expected_events,
                    timeout_seconds=turn_timeout_seconds,
                    timeout_category="foreground_timeout",
                )
                _wait_until(
                    app,
                    lambda: (
                        controller is not None
                        and controller.conversation.state is ConversationState.IDLE
                        and not controller.conversation.has_running_worker
                    ),
                    timeout_seconds=10.0,
                    timeout_category="foreground_cleanup_timeout",
                )
                latest_event = terminal_events[-1]
                if latest_event["message_status"] != MessageStatus.COMPLETED.value:
                    raise _SmokeTimeout(f"foreground_{latest_event['category']}")

                current = _wait_for_extraction_job(
                    app,
                    paths.database_file,
                    expected_count=index,
                    timeout_seconds=job_timeout_seconds,
                )
                delta = _snapshot_delta(previous, current)
                scenario_deltas[scenario_name] = {
                    "memory_groups": delta.memory_groups,
                    "memory_versions": delta.memory_versions,
                    "memory_sources": delta.memory_sources,
                    "correct_versions": delta.correct_versions,
                }
                scenario_checks[scenario_name] = _scenario_passed(scenario_name, delta, current)
                previous = current
                final_snapshot = current

            lifecycle_clean = _close_controller(app, controller)
            controller = None

            restart_controller = _create_controller(
                app,
                paths,
                repository,
                isolated_settings,
                store,
                provider_override,
            )
            _wait_until(
                app,
                lambda: restart_controller is not None and restart_controller._data_initialized,
                timeout_seconds=10.0,
                timeout_category="restart_timeout",
            )
            restored_turn_count = len(restart_controller.conversation.turns)
            recovery_passed = (
                restart_controller._data_writable
                and restored_turn_count == len(_SCENARIOS)
                and all(
                    turn.assistant_message.status is MessageStatus.COMPLETED
                    for turn in restart_controller.conversation.turns
                )
            )
            management_report, management_passed = _run_local_management(
                app,
                restart_controller,
                paths.database_file,
            )
            lifecycle_clean = _close_controller(app, restart_controller) and lifecycle_clean
            restart_controller = None

            passed = (
                len(terminal_events) == len(_SCENARIOS)
                and all(scenario_checks.values())
                and recovery_passed
                and management_passed
                and lifecycle_clean
                and final_snapshot.job_status_counts.get("failed", 0) == 0
            )
            live_model_report = {
                "status": "passed" if all(scenario_checks.values()) else "failed",
                "foreground_send_count": foreground_send_count,
                "foreground_terminal_count": len(terminal_events),
                "scenario_status_counts": dict(
                    Counter("passed" if value else "failed" for value in scenario_checks.values())
                ),
                "scenario_checks": dict(sorted(scenario_checks.items())),
                "scenario_count_deltas": scenario_deltas,
                "foreground_status_counts": _terminal_status_counts(terminal_events),
                "foreground_error_category_counts": _terminal_category_counts(terminal_events),
                "latency_ms": _latency_summary(latencies_ms),
                "database_counts": final_snapshot.public_counts(),
            }
            success_report = {
                "mode": "live",
                "status": "passed" if passed else "failed",
                "category": "none" if passed else "acceptance_mismatch",
                "live_model": live_model_report,
                "local_management": management_report,
                "restart": {
                    "recovered": recovery_passed,
                    "turn_count": restored_turn_count,
                },
                "lifecycle_clean": lifecycle_clean,
                "temporary_data_cleaned": False,
            }
    except _SmokeTimeout as exc:
        error_category = exc.category
    except Exception as exc:  # noqa: BLE001 - only the exception type category is retained
        error_category = _exception_category(exc)
    finally:
        if controller is not None:
            lifecycle_clean = _close_controller(app, controller) and lifecycle_clean
        if restart_controller is not None:
            lifecycle_clean = _close_controller(app, restart_controller) and lifecycle_clean
        if temporary_context is not None:
            try:
                temporary_context.cleanup()
                temporary_data_cleaned = True
            except OSError:
                error_category = "temporary_cleanup_failed"
                success_report = None
        qInstallMessageHandler(previous_qt_handler)

    if success_report is not None:
        success_report["lifecycle_clean"] = lifecycle_clean
        success_report["temporary_data_cleaned"] = temporary_data_cleaned
        return success_report

    report = _failure_report(error_category)
    report.update(
        {
            "live_model": {
                "status": "failed",
                "foreground_send_count": foreground_send_count,
                "foreground_terminal_count": len(terminal_events),
                "scenario_status_counts": dict(
                    Counter("passed" if value else "failed" for value in scenario_checks.values())
                ),
                "scenario_checks": dict(sorted(scenario_checks.items())),
                "scenario_count_deltas": scenario_deltas,
                "foreground_status_counts": _terminal_status_counts(terminal_events),
                "foreground_error_category_counts": _terminal_category_counts(terminal_events),
                "latency_ms": _latency_summary(latencies_ms),
                "database_counts": final_snapshot.public_counts(),
            },
            "local_management": {"status": "not_run", "checks": {}},
            "lifecycle_clean": lifecycle_clean,
            "temporary_data_cleaned": temporary_data_cleaned,
        }
    )
    return report


def _run_local_management(
    application: QApplication,
    controller: ApplicationController,
    database_path: Path,
) -> tuple[dict[str, object], bool]:
    """Exercise memory administration through ``LocalDataService`` only."""

    service = controller.data_service
    memory_snapshots: list[MemoryListSnapshot] = []
    source_snapshots: list[tuple[object, ...]] = []
    source_contexts: list[object] = []
    operation_failures: list[str] = []
    checks = {
        "source_rows_available": False,
        "source_context_resolvable": False,
        "manual_edit_created_version": False,
        "manual_confidence_locked": False,
        "pin_persisted": False,
        "archive_persisted": False,
        "restore_persisted": False,
        "delete_cascaded": False,
    }
    manual_version_delta = 0
    source_count = 0

    service.memories_loaded.connect(
        lambda value: (
            memory_snapshots.append(value) if isinstance(value, MemoryListSnapshot) else None
        )
    )
    service.memory_sources_loaded.connect(
        lambda _memory_id, rows: source_snapshots.append(tuple(rows))
    )
    service.source_context_loaded.connect(
        lambda snapshot, _message_id: source_contexts.append(snapshot)
    )
    service.operation_failed.connect(
        lambda _operation, category: operation_failures.append(_safe_value(category))
    )

    def refreshed(
        predicate: Callable[[tuple[dict[str, object], ...]], bool],
    ) -> tuple[dict[str, object], ...]:
        snapshot_count = len(memory_snapshots)
        failure_count = len(operation_failures)
        service.refresh_memories()
        _wait_until(
            application,
            lambda: (
                len(operation_failures) > failure_count
                or (len(memory_snapshots) > snapshot_count and predicate(memory_snapshots[-1].rows))
            ),
            timeout_seconds=10.0,
            timeout_category="management_refresh_timeout",
        )
        if len(operation_failures) > failure_count:
            raise _SmokeTimeout("management_operation_failed")
        return memory_snapshots[-1].rows

    def after_write(
        operation: Callable[[], None],
        predicate: Callable[[tuple[dict[str, object], ...]], bool],
    ) -> tuple[dict[str, object], ...]:
        snapshot_count = len(memory_snapshots)
        failure_count = len(operation_failures)
        operation()
        _wait_until(
            application,
            lambda: (
                len(operation_failures) > failure_count
                or (len(memory_snapshots) > snapshot_count and predicate(memory_snapshots[-1].rows))
            ),
            timeout_seconds=10.0,
            timeout_category="management_write_timeout",
        )
        if len(operation_failures) > failure_count:
            raise _SmokeTimeout("management_operation_failed")
        return memory_snapshots[-1].rows

    category = "none"
    post_delete = _empty_snapshot()
    try:
        rows = refreshed(lambda values: len(values) == 1)
        memory_id = str(rows[0]["memory_id"])
        original_version = int(rows[0]["version_number"])

        source_count_before = len(source_snapshots)
        failure_count = len(operation_failures)
        service.load_memory_sources(memory_id)
        _wait_until(
            application,
            lambda: (
                len(operation_failures) > failure_count
                or len(source_snapshots) > source_count_before
            ),
            timeout_seconds=10.0,
            timeout_category="management_sources_timeout",
        )
        if len(operation_failures) > failure_count:
            raise _SmokeTimeout("management_operation_failed")
        sources = source_snapshots[-1]
        source_count = len(sources)
        available = [
            source
            for source in sources
            if isinstance(source, Mapping)
            and source.get("available") is True
            and isinstance(source.get("content"), str)
            and bool(str(source["content"]).strip())
            and isinstance(source.get("conversation_id"), str)
            and isinstance(source.get("message_id"), str)
        ]
        checks["source_rows_available"] = bool(available)
        if available:
            context_count = len(source_contexts)
            failure_count = len(operation_failures)
            service.load_source_context(
                str(available[0]["conversation_id"]),
                str(available[0]["message_id"]),
            )
            _wait_until(
                application,
                lambda: (
                    len(operation_failures) > failure_count or len(source_contexts) > context_count
                ),
                timeout_seconds=10.0,
                timeout_category="management_source_context_timeout",
            )
            if len(operation_failures) > failure_count:
                raise _SmokeTimeout("management_operation_failed")
            checks["source_context_resolvable"] = len(source_contexts) > context_count

        manual_rows = after_write(
            lambda: service.edit_memory(
                memory_id,
                "手工编辑后的合成验收记忆：偏好无糖茶。",
            ),
            lambda values: any(
                str(row.get("memory_id")) == memory_id
                and int(row.get("version_number", 0)) > original_version
                for row in values
            ),
        )
        manual_row = next(row for row in manual_rows if str(row["memory_id"]) == memory_id)
        manual_version_delta = int(manual_row["version_number"]) - original_version
        checks["manual_edit_created_version"] = manual_version_delta == 1
        checks["manual_confidence_locked"] = float(manual_row["confidence"]) == 1.0

        pinned_rows = after_write(
            lambda: service.set_memory_pinned(memory_id, True),
            lambda values: any(
                str(row.get("memory_id")) == memory_id and row.get("pinned") is True
                for row in values
            ),
        )
        checks["pin_persisted"] = any(
            str(row.get("memory_id")) == memory_id and row.get("pinned") is True
            for row in pinned_rows
        )

        archived_rows = after_write(
            lambda: service.archive_memory(memory_id),
            lambda values: any(
                str(row.get("memory_id")) == memory_id and row.get("status") == "archived"
                for row in values
            ),
        )
        checks["archive_persisted"] = any(
            str(row.get("memory_id")) == memory_id and row.get("status") == "archived"
            for row in archived_rows
        )

        restored_rows = after_write(
            lambda: service.restore_memory(memory_id),
            lambda values: any(
                str(row.get("memory_id")) == memory_id and row.get("status") == "active"
                for row in values
            ),
        )
        checks["restore_persisted"] = any(
            str(row.get("memory_id")) == memory_id and row.get("status") == "active"
            for row in restored_rows
        )

        after_write(
            lambda: service.delete_memory(memory_id),
            lambda values: all(str(row.get("memory_id")) != memory_id for row in values),
        )
        post_delete = _database_snapshot(database_path)
        checks["delete_cascaded"] = (
            post_delete.memory_groups == 0
            and post_delete.memory_versions == 0
            and post_delete.memory_sources == 0
            and post_delete.memory_fts == 0
        )
    except _SmokeTimeout as exc:
        category = exc.category
    except Exception as exc:  # noqa: BLE001 - retain only a stable type category
        category = _exception_category(exc)

    passed = all(checks.values()) and not operation_failures and category == "none"
    return (
        {
            "status": "passed" if passed else "failed",
            "category": category if not passed else "none",
            "checks": dict(sorted(checks.items())),
            "source_count": source_count,
            "manual_version_delta": manual_version_delta,
            "operation_failure_category_counts": dict(sorted(Counter(operation_failures).items())),
            "post_delete_counts": {
                "memory_groups": post_delete.memory_groups,
                "memory_versions": post_delete.memory_versions,
                "memory_sources": post_delete.memory_sources,
                "memory_fts": post_delete.memory_fts,
            },
        },
        passed,
    )


def _create_controller(
    application: QApplication,
    paths: AppPaths,
    repository: SettingsRepository,
    settings: dict[str, Any],
    credential_store: CredentialStore,
    provider_override: ChatProvider | None,
) -> ApplicationController:
    logger = logging.getLogger(f"amadeus.p5a.live-smoke.{time.monotonic_ns()}")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return ApplicationController(
        application,
        _SmokeInstanceGuard(),  # type: ignore[arg-type]
        logger,
        paths=paths,
        settings_repository=repository,
        settings=deepcopy(settings),
        tray_available=False,
        chat_provider=provider_override,
        allow_saved_provider=True,
        credential_store=credential_store,
        background_jobs_enabled=True,
    )


def _wait_until(
    application: QApplication,
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float,
    timeout_category: str,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    application.processEvents()
    if not predicate():
        raise _SmokeTimeout(timeout_category)


def _wait_for_extraction_job(
    application: QApplication,
    database_path: Path,
    *,
    expected_count: int,
    timeout_seconds: float,
) -> _DatabaseSnapshot:
    latest = _empty_snapshot()

    def terminal() -> bool:
        nonlocal latest
        latest = _database_snapshot(database_path)
        statuses = latest.job_status_counts
        terminal_count = sum(
            count for status, count in statuses.items() if status in _TERMINAL_JOB_STATUSES
        )
        return latest.extraction_job_count == expected_count and terminal_count == expected_count

    _wait_until(
        application,
        terminal,
        timeout_seconds=timeout_seconds,
        timeout_category="background_timeout",
    )
    return latest


def _database_snapshot(database_path: Path) -> _DatabaseSnapshot:
    if not database_path.is_file():
        return _empty_snapshot()
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        connection.execute("PRAGMA query_only = ON")
        groups = int(connection.execute("SELECT COUNT(*) FROM memory_groups").fetchone()[0])
        versions = int(connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0])
        sources = int(connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0])
        fts_rows = int(connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0])
        operations = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT operation, COUNT(*) FROM memory_versions GROUP BY operation"
            )
        }
        jobs = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT status, COUNT(*) FROM background_jobs
                WHERE kind = 'memory_extraction'
                GROUP BY status
                """
            )
        }
        messages = {
            f"{row[0]}:{row[1]}": int(row[2])
            for row in connection.execute(
                "SELECT role, status, COUNT(*) FROM messages GROUP BY role, status"
            )
        }
    finally:
        connection.close()
    return _DatabaseSnapshot(groups, versions, sources, fts_rows, operations, jobs, messages)


def _snapshot_delta(before: _DatabaseSnapshot, after: _DatabaseSnapshot) -> _ScenarioDelta:
    return _ScenarioDelta(
        memory_groups=after.memory_groups - before.memory_groups,
        memory_versions=after.memory_versions - before.memory_versions,
        memory_sources=after.memory_sources - before.memory_sources,
        correct_versions=(
            after.operation_counts.get("correct", 0) - before.operation_counts.get("correct", 0)
        ),
    )


def _scenario_passed(
    scenario: str,
    delta: _ScenarioDelta,
    snapshot: _DatabaseSnapshot,
) -> bool:
    if snapshot.job_status_counts.get("failed", 0):
        return False
    if scenario == "initial":
        return delta.memory_groups >= 1 and delta.memory_versions >= 1 and delta.memory_sources >= 1
    if scenario == "exact_duplicate":
        return delta.memory_groups == 0 and delta.memory_versions == 0 and delta.memory_sources >= 1
    if scenario == "explicit_correction":
        return (
            delta.memory_groups == 0
            and delta.memory_versions >= 1
            and delta.memory_sources >= 1
            and delta.correct_versions >= 1
        )
    if scenario == "do_not_remember":
        return delta.memory_groups == 0 and delta.memory_versions == 0 and delta.memory_sources == 0
    return False


def _close_controller(application: QApplication, controller: ApplicationController) -> bool:
    clean = controller._shutdown_background_tasks(timeout_ms=5_000)
    controller._exiting = True
    controller.chat_panel.hide()
    controller.settings_window.hide()
    controller.pet_window.hide()
    controller.window.hide()
    if controller.tray is not None:
        controller.tray.close()
    controller.instance_guard.close()
    application.processEvents()
    return clean


def _latency_summary(values: list[int]) -> dict[str, object]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "median": None,
            "max": None,
            "bucket_counts": {},
        }
    buckets = Counter()
    for value in values:
        if value < 5_000:
            buckets["under_5s"] += 1
        elif value < 15_000:
            buckets["5s_to_15s"] += 1
        else:
            buckets["15s_or_more"] += 1
    return {
        "count": len(values),
        "min": min(values),
        "median": round(median(values)),
        "max": max(values),
        "bucket_counts": dict(sorted(buckets.items())),
    }


def _terminal_status_counts(events: list[dict[str, object]]) -> dict[str, int]:
    return dict(sorted(Counter(str(event["message_status"]) for event in events).items()))


def _terminal_category_counts(events: list[dict[str, object]]) -> dict[str, int]:
    return dict(sorted(Counter(str(event["category"]) for event in events).items()))


def _safe_value(value: object) -> str:
    text = str(value).lower()
    return (
        "".join(character for character in text if character.isalnum() or character in "_.:-")[:96]
        or "unknown"
    )


def _discard_qt_message(*_args: object) -> None:
    """Keep the command-line contract to one aggregate JSON document."""


def _exception_category(exc: BaseException) -> str:
    return _safe_value(type(exc).__name__)


def _empty_snapshot() -> _DatabaseSnapshot:
    return _DatabaseSnapshot(0, 0, 0, 0, {}, {}, {})


def _blocked(category: str) -> SmokePreflight:
    return SmokePreflight(False, category, False, False, False, None)


def _failure_report(category: str) -> dict[str, object]:
    return {
        "mode": "live",
        "status": "failed",
        "category": _safe_value(category),
        "temporary_data_cleaned": True,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        result = preflight(args.source_local_app_data)
        if not args.execute_live:
            report = result.public_report(live_requested=False)
            exit_code = 0 if result.ready else 2
        elif not result.ready:
            report = result.public_report(live_requested=True)
            exit_code = 2
        else:
            report = execute_smoke(
                result,
                turn_timeout_seconds=args.turn_timeout_seconds,
                job_timeout_seconds=args.job_timeout_seconds,
            )
            exit_code = 0 if report.get("status") == "passed" else 1
    except Exception as exc:  # noqa: BLE001 - no raw exception data may reach stdout
        report = _failure_report(_exception_category(exc))
        exit_code = 1
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
