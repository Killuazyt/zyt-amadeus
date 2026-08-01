"""SQLite schema v1, consistent migration backups, and fail-closed opening."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000


class DatabaseError(RuntimeError):
    """Base class for database lifecycle failures."""


class DatabaseClosedError(DatabaseError):
    """Raised when a repository uses a database outside its lifetime."""


class DatabaseReadOnlyError(DatabaseError):
    """Raised when a write is attempted after migration failed closed."""


class DatabaseMigrationError(DatabaseError):
    """A privacy-safe description of a migration failure."""


Migration = Callable[[sqlite3.Connection], None]


_SCHEMA_V1: tuple[str, ...] = (
    """
    CREATE TABLE profiles (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'normal'
            CHECK (status IN ('normal', 'archived')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_activity_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX conversations_activity_idx
    ON conversations(profile_id, status, last_activity_at DESC, id DESC)
    """,
    """
    CREATE TABLE messages (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        conversation_id TEXT NOT NULL
            REFERENCES conversations(id) ON DELETE CASCADE,
        turn_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL
            CHECK (status IN ('pending', 'streaming', 'completed', 'stopped', 'failed')),
        attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
        terminal_reason TEXT,
        provider_name TEXT,
        model_name TEXT,
        failure_code TEXT,
        participates_in_memory INTEGER NOT NULL DEFAULT 1
            CHECK (participates_in_memory IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        UNIQUE (conversation_id, turn_id, role)
    )
    """,
    """
    CREATE INDEX messages_conversation_sequence_idx
    ON messages(conversation_id, sequence)
    """,
    """
    CREATE TABLE conversation_summaries (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL
            REFERENCES conversations(id) ON DELETE CASCADE,
        content TEXT NOT NULL,
        covers_through_sequence INTEGER NOT NULL CHECK (covers_through_sequence >= 0),
        message_count INTEGER NOT NULL CHECK (message_count >= 0),
        character_count INTEGER NOT NULL CHECK (character_count >= 0),
        created_at TEXT NOT NULL,
        UNIQUE (conversation_id, covers_through_sequence)
    )
    """,
    """
    CREATE INDEX summaries_latest_idx
    ON conversation_summaries(conversation_id, covers_through_sequence DESC)
    """,
    """
    CREATE TABLE memory_groups (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('fact', 'preference', 'event', 'relationship')),
        topic_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'archived')),
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        current_version_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (current_version_id) REFERENCES memory_versions(id)
            ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE INDEX memory_groups_profile_state_idx
    ON memory_groups(profile_id, status, pinned DESC, updated_at DESC)
    """,
    """
    CREATE INDEX memory_groups_topic_idx
    ON memory_groups(profile_id, kind, topic_key)
    """,
    """
    CREATE TABLE memory_versions (
        id TEXT PRIMARY KEY,
        memory_id TEXT NOT NULL REFERENCES memory_groups(id) ON DELETE CASCADE,
        version_number INTEGER NOT NULL CHECK (version_number >= 1),
        content TEXT NOT NULL,
        normalized_content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        search_text TEXT NOT NULL,
        importance REAL NOT NULL CHECK (importance >= 0.0 AND importance <= 1.0),
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        origin TEXT NOT NULL CHECK (origin IN ('automatic', 'manual')),
        operation TEXT NOT NULL
            CHECK (operation IN ('add', 'supplement', 'correct', 'manual_edit')),
        supersedes_version_id TEXT REFERENCES memory_versions(id)
            DEFERRABLE INITIALLY DEFERRED,
        created_at TEXT NOT NULL,
        UNIQUE (memory_id, version_number)
    )
    """,
    """
    CREATE INDEX memory_versions_hash_idx ON memory_versions(content_hash)
    """,
    """
    CREATE TRIGGER memory_versions_are_immutable
    BEFORE UPDATE ON memory_versions
    BEGIN
        SELECT RAISE(ABORT, 'memory versions are immutable');
    END
    """,
    """
    CREATE TRIGGER memory_versions_no_individual_delete
    BEFORE DELETE ON memory_versions
    WHEN EXISTS (SELECT 1 FROM memory_groups WHERE id = OLD.memory_id)
    BEGIN
        SELECT RAISE(ABORT, 'memory versions can only be deleted with their group');
    END
    """,
    """
    CREATE TABLE memory_sources (
        id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL REFERENCES memory_versions(id) ON DELETE CASCADE,
        source_message_id TEXT NOT NULL,
        source_conversation_id TEXT,
        live_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
        live_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
        extraction_method TEXT NOT NULL CHECK (extraction_method IN ('automatic', 'manual')),
        created_at TEXT NOT NULL,
        UNIQUE (version_id, source_message_id)
    )
    """,
    """
    CREATE INDEX memory_sources_message_idx ON memory_sources(live_message_id)
    """,
    """
    CREATE TABLE background_jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL
            CHECK (status IN ('pending', 'running', 'retry', 'failed', 'completed')),
        payload_json TEXT NOT NULL DEFAULT '{}',
        profile_id TEXT REFERENCES profiles(id) ON DELETE CASCADE,
        conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
        message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        run_after TEXT NOT NULL,
        last_error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX background_jobs_ready_idx
    ON background_jobs(status, run_after, created_at)
    """,
    """
    CREATE VIRTUAL TABLE memory_fts USING fts5(
        version_id UNINDEXED,
        memory_id UNINDEXED,
        profile_id UNINDEXED,
        search_text,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
)


def _migrate_to_v1(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V1:
        connection.execute(statement)


_DEFAULT_MIGRATIONS: Mapping[int, Migration] = {1: _migrate_to_v1}
_REQUIRED_TABLES = {
    "profiles",
    "conversations",
    "messages",
    "conversation_summaries",
    "memory_groups",
    "memory_versions",
    "memory_sources",
    "background_jobs",
    "memory_fts",
}


class SQLiteDatabase:
    """Own one SQLite connection intended for a single background data thread.

    Construction is side-effect free. ``open`` performs migration and returns a
    readable, query-only connection instead of a writable one if migration fails.
    Callers can therefore keep the pet and non-database settings operational while
    disabling sends that cannot be persisted.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        backup_dir: str | Path | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        migrations: Mapping[int, Migration] | None = None,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path)
        self.backup_dir = (
            Path(backup_dir) if backup_dir is not None else self.path.parent / "migration-backups"
        )
        self.busy_timeout_ms = busy_timeout_ms
        self._migrations = dict(_DEFAULT_MIGRATIONS)
        if migrations is not None:
            self._migrations.update(migrations)
        self._connection: sqlite3.Connection | None = None
        self._read_only = False
        self._migration_error: DatabaseMigrationError | None = None
        self._last_backup_path: Path | None = None

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def migration_error(self) -> DatabaseMigrationError | None:
        return self._migration_error

    @property
    def last_backup_path(self) -> Path | None:
        return self._last_backup_path

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseClosedError("database is not open")
        return self._connection

    @property
    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def open(self) -> SQLiteDatabase:
        if self._connection is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed_with_data = self.path.exists() and self.path.stat().st_size > 0
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        self._connection = connection
        self._configure_common(connection)

        current_version = self.schema_version
        if current_version == SCHEMA_VERSION:
            try:
                self._validate_schema(connection)
                self._enable_writable_pragmas(connection)
            except Exception as exc:  # noqa: BLE001 - expose only a safe category
                self._fail_closed(
                    DatabaseMigrationError(
                        f"database schema validation failed: {type(exc).__name__}"
                    )
                )
            return self
        if current_version > SCHEMA_VERSION:
            self._fail_closed(
                DatabaseMigrationError("database schema is newer than this application")
            )
            return self

        try:
            if existed_with_data:
                self._last_backup_path = self.create_backup()
            self._enable_writable_pragmas(connection)
            self._apply_migrations(current_version)
        except Exception as exc:  # noqa: BLE001 - expose only a stable local error category
            self._fail_closed(
                DatabaseMigrationError(f"database migration failed: {type(exc).__name__}")
            )
        return self

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def __enter__(self) -> SQLiteDatabase:
        return self.open()

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._read_only:
            raise DatabaseReadOnlyError("database is read-only after migration failure")
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def create_backup(self, target: str | Path | None = None) -> Path:
        """Create a consistent backup using SQLite's online backup API."""

        connection = self.connection
        if target is None:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            target_path = self.backup_dir / (
                f"{self.path.stem}.schema-{self.schema_version}.{stamp}.{uuid4().hex[:8]}.sqlite3"
            )
        else:
            target_path = Path(target)
            target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            raise FileExistsError(target_path)
        destination = sqlite3.connect(target_path)
        try:
            connection.backup(destination)
        finally:
            destination.close()
        return target_path

    def purge_deleted_content(self) -> None:
        """Checkpoint secure deletions and truncate WAL payload pages.

        P5A stores private chat and memory text. ``secure_delete`` overwrites
        deleted cells in the current database image; a truncating checkpoint
        then removes older WAL frames that could still contain the text.  This
        method must be called outside a transaction on the owning data thread.
        """

        if self._read_only:
            raise DatabaseReadOnlyError("database is read-only after migration failure")
        result = self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or int(result[0]) != 0:
            raise DatabaseError("secure deletion checkpoint could not complete")

    def pragma_value(self, name: str) -> object:
        """Expose a small allow-listed PRAGMA reader for diagnostics/tests."""

        if name not in {
            "journal_mode",
            "foreign_keys",
            "busy_timeout",
            "query_only",
            "secure_delete",
        }:
            raise ValueError("unsupported pragma")
        return self.connection.execute(f"PRAGMA {name}").fetchone()[0]

    def _apply_migrations(self, current_version: int) -> None:
        with self.transaction() as connection:
            for target_version in range(current_version + 1, SCHEMA_VERSION + 1):
                migration = self._migrations.get(target_version)
                if migration is None:
                    raise DatabaseMigrationError(f"missing migration for schema {target_version}")
                migration(connection)
                connection.execute(f"PRAGMA user_version = {target_version}")
            self._validate_schema(connection)

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not _REQUIRED_TABLES.issubset(existing):
            raise DatabaseMigrationError("database schema is incomplete")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise DatabaseMigrationError("database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DatabaseMigrationError("database foreign-key check failed")

    def _configure_common(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _enable_writable_pragmas(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA secure_delete = ON")

    def _fail_closed(self, error: DatabaseMigrationError) -> None:
        if self._connection is not None:
            self._connection.close()
        uri = self.path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        self._connection = connection
        self._read_only = True
        self._migration_error = error


def table_names(connection: sqlite3.Connection) -> Sequence[str]:
    """Return user-visible schema objects for deterministic acceptance checks."""

    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
    ).fetchall()
    return tuple(str(row[0]) for row in rows)
