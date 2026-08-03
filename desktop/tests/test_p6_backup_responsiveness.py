from __future__ import annotations

import threading
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer

from amadeus_desktop import __version__
from amadeus_desktop.conversation_store import ConversationStore
from amadeus_desktop.data_management import create_backup_archive, validate_backup_archive
from amadeus_desktop.data_runtime import DataPriority, SerialDataThread
from amadeus_desktop.database import SCHEMA_VERSION, SQLiteDatabase
from amadeus_desktop.settings import CURRENT_SCHEMA_VERSION, DEFAULT_SETTINGS

_FIXED_TIME = datetime(2026, 8, 3, 4, 5, 6, tzinfo=UTC)
_SYNTHETIC_TURN_COUNT = 448
_SYNTHETIC_MESSAGE_CHARS = 4_096
_CONTROLLED_DELAY_SECONDS = 0.18


def _seed_large_database(database_path: Path, backup_directory: Path) -> int:
    database = SQLiteDatabase(database_path, backup_dir=backup_directory).open()
    try:
        conversations = ConversationStore(database, clock=lambda: _FIXED_TIME)
        conversation = conversations.create_conversation(
            "大数据备份心跳验收",
            conversation_id="backup-heartbeat-conversation",
        )
        timestamp = "2026-08-03T04:05:06.000000Z"
        rows: list[tuple[object, ...]] = []
        for index in range(_SYNTHETIC_TURN_COUNT):
            for role in ("user", "assistant"):
                marker = f"synthetic-{index:04d}-{role}-"
                content = marker + ("x" * (_SYNTHETIC_MESSAGE_CHARS - len(marker)))
                rows.append(
                    (
                        f"message-{index:04d}-{role}",
                        conversation.conversation_id,
                        f"turn-{index:04d}",
                        role,
                        content,
                        int(role == "user"),
                        timestamp,
                        timestamp,
                        timestamp,
                    )
                )
        with database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO messages(
                    id, conversation_id, turn_id, role, origin, content, status, attempt,
                    participates_in_memory, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, 'conversation', ?, 'completed', 1, ?, ?, ?, ?)
                """,
                rows,
            )
        checkpoint = database.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert checkpoint is not None
        assert int(checkpoint[0]) == 0
        return database_path.stat().st_size
    finally:
        database.close()


def test_large_online_backup_keeps_qt_heartbeat_responsive_and_validates(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    del qapp
    database_path = tmp_path / "source.sqlite3"
    migration_backups = tmp_path / "migration-backups"
    database_size = _seed_large_database(database_path, migration_backups)
    assert database_size >= 3 * 1024**2

    data_runtime = SerialDataThread(
        lambda: SQLiteDatabase(database_path, backup_dir=migration_backups).open(),
        resource_close=lambda database: database.close(),
    )
    destination = tmp_path / "large-data.amadeus-backup"
    operation_started = threading.Event()
    operation_finished = threading.Event()
    completed: list[tuple[Path, float, int]] = []
    failures: list[str] = []
    ui_thread_id = threading.get_ident()
    heartbeats = 0
    heartbeats_during_backup = 0
    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(2)

    def heartbeat() -> None:
        nonlocal heartbeats, heartbeats_during_backup
        heartbeats += 1
        if operation_started.is_set() and not operation_finished.is_set():
            heartbeats_during_backup += 1

    def backup_operation(database: SQLiteDatabase) -> tuple[Path, float, int]:
        operation_started.set()
        started_at = time.perf_counter()
        worker_thread_id = threading.get_ident()

        def delayed_online_backup(target: Path) -> Path:
            snapshot = database.create_backup(target)
            time.sleep(_CONTROLLED_DELAY_SECONDS)
            return snapshot

        try:
            archive = create_backup_archive(
                destination,
                database_backup=delayed_online_backup,
                settings_snapshot=deepcopy(DEFAULT_SETTINGS),
                app_version=__version__,
                created_at=_FIXED_TIME,
            )
            return archive, time.perf_counter() - started_at, worker_thread_id
        finally:
            operation_finished.set()

    timer.timeout.connect(heartbeat)
    try:
        data_runtime.start()
        qtbot.waitUntil(lambda: data_runtime.is_ready, timeout=3_000)
        timer.start()
        request_id = data_runtime.submit(
            backup_operation,
            priority=DataPriority.INTERACTIVE,
            on_success=completed.append,
            on_failure=failures.append,
        )
        assert request_id is not None
        qtbot.waitUntil(lambda: bool(completed) or bool(failures), timeout=10_000)

        assert failures == []
        assert len(completed) == 1
        archive, elapsed_seconds, worker_thread_id = completed[0]
        assert archive == destination
        assert worker_thread_id != ui_thread_id
        assert elapsed_seconds >= _CONTROLLED_DELAY_SECONDS
        assert heartbeats >= 5
        assert heartbeats_during_backup >= 5

        metadata = validate_backup_archive(archive)
        assert metadata.app_version == __version__
        assert metadata.database_schema == SCHEMA_VERSION
        assert metadata.settings_schema == CURRENT_SCHEMA_VERSION
        assert metadata.database_size >= 3 * 1024**2
        assert len(metadata.database_sha256) == 64
        assert len(metadata.settings_sha256) == 64
    finally:
        timer.stop()
        assert data_runtime.shutdown(3_000)
