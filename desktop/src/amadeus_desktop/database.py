"""SQLite schema v5, consistent migration backups, and fail-closed opening."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 5
AMADEUS_APPLICATION_ID = int.from_bytes(b"AMDS", "big")
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


_SCHEMA_V2: tuple[str, ...] = (
    """
    CREATE TABLE memory_embedding_generations (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        model_name TEXT NOT NULL,
        model_commit TEXT NOT NULL,
        dimension INTEGER NOT NULL CHECK (dimension > 0),
        model_sha256 TEXT NOT NULL,
        calibration_threshold REAL NOT NULL
            CHECK (calibration_threshold >= -1.0 AND calibration_threshold <= 1.0),
        status TEXT NOT NULL
            CHECK (status IN ('building', 'active', 'retired', 'failed')),
        item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
        failure_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        activated_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX memory_embedding_one_active_idx
    ON memory_embedding_generations(profile_id)
    WHERE status = 'active'
    """,
    """
    CREATE INDEX memory_embedding_generation_state_idx
    ON memory_embedding_generations(profile_id, status, created_at DESC)
    """,
    """
    CREATE TABLE memory_vectors (
        generation_id TEXT NOT NULL
            REFERENCES memory_embedding_generations(id) ON DELETE CASCADE,
        version_id TEXT NOT NULL REFERENCES memory_versions(id) ON DELETE CASCADE,
        vector_blob BLOB NOT NULL CHECK (length(vector_blob) > 0),
        vector_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (generation_id, version_id)
    )
    """,
    """
    CREATE INDEX memory_vectors_version_idx ON memory_vectors(version_id)
    """,
    """
    CREATE TABLE memory_recall_events (
        id TEXT PRIMARY KEY,
        retrieval_ticket_id TEXT NOT NULL,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        version_id TEXT NOT NULL REFERENCES memory_versions(id) ON DELETE CASCADE,
        conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
        assistant_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
        attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
        terminal_status TEXT NOT NULL CHECK (terminal_status IN ('completed', 'user_stopped')),
        recalled_at TEXT NOT NULL,
        UNIQUE (retrieval_ticket_id, version_id)
    )
    """,
    """
    CREATE INDEX memory_recall_version_time_idx
    ON memory_recall_events(version_id, recalled_at DESC)
    """,
    """
    CREATE INDEX memory_recall_profile_time_idx
    ON memory_recall_events(profile_id, recalled_at DESC)
    """,
    """
    CREATE TABLE persona_knowledge (
        id TEXT PRIMARY KEY,
        persona_id TEXT NOT NULL,
        content TEXT NOT NULL,
        search_text TEXT NOT NULL,
        tags_json TEXT NOT NULL DEFAULT '[]',
        source_ref TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (persona_id, content_hash)
    )
    """,
    """
    CREATE INDEX persona_knowledge_state_idx
    ON persona_knowledge(persona_id, active, updated_at DESC, id)
    """,
    """
    CREATE VIRTUAL TABLE persona_fts USING fts5(
        knowledge_id UNINDEXED,
        persona_id UNINDEXED,
        search_text,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE persona_embedding_generations (
        id TEXT PRIMARY KEY,
        persona_id TEXT NOT NULL,
        model_name TEXT NOT NULL,
        model_commit TEXT NOT NULL,
        dimension INTEGER NOT NULL CHECK (dimension > 0),
        model_sha256 TEXT NOT NULL,
        calibration_threshold REAL NOT NULL
            CHECK (calibration_threshold >= -1.0 AND calibration_threshold <= 1.0),
        status TEXT NOT NULL
            CHECK (status IN ('building', 'active', 'retired', 'failed')),
        item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
        failure_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        activated_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX persona_embedding_one_active_idx
    ON persona_embedding_generations(persona_id)
    WHERE status = 'active'
    """,
    """
    CREATE INDEX persona_embedding_generation_state_idx
    ON persona_embedding_generations(persona_id, status, created_at DESC)
    """,
    """
    CREATE TABLE persona_vectors (
        generation_id TEXT NOT NULL
            REFERENCES persona_embedding_generations(id) ON DELETE CASCADE,
        knowledge_id TEXT NOT NULL REFERENCES persona_knowledge(id) ON DELETE CASCADE,
        vector_blob BLOB NOT NULL CHECK (length(vector_blob) > 0),
        vector_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (generation_id, knowledge_id)
    )
    """,
    """
    CREATE INDEX persona_vectors_knowledge_idx ON persona_vectors(knowledge_id)
    """,
    """
    CREATE TABLE persona_recall_events (
        id TEXT PRIMARY KEY,
        retrieval_ticket_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        knowledge_id TEXT NOT NULL REFERENCES persona_knowledge(id) ON DELETE CASCADE,
        conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
        assistant_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
        attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
        terminal_status TEXT NOT NULL CHECK (terminal_status IN ('completed', 'user_stopped')),
        recalled_at TEXT NOT NULL,
        UNIQUE (retrieval_ticket_id, knowledge_id)
    )
    """,
    """
    CREATE INDEX persona_recall_knowledge_time_idx
    ON persona_recall_events(knowledge_id, recalled_at DESC)
    """,
    """
    CREATE INDEX persona_recall_persona_time_idx
    ON persona_recall_events(persona_id, recalled_at DESC)
    """,
)


def _migrate_to_v2(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V2:
        connection.execute(statement)


_SCHEMA_V3: tuple[str, ...] = (
    """
    ALTER TABLE messages
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'conversation'
        CHECK (origin IN ('conversation', 'proactive'))
    """,
    """
    CREATE TABLE proactive_events (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        local_date TEXT NOT NULL,
        trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('startup', 'idle')),
        displayed_at TEXT NOT NULL,
        disposition TEXT NOT NULL DEFAULT 'displayed'
            CHECK (disposition IN ('displayed', 'clicked', 'dismissed')),
        message_id TEXT REFERENCES messages(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX proactive_events_profile_date_idx
    ON proactive_events(profile_id, local_date, displayed_at)
    """,
)


def _migrate_to_v3(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V3:
        connection.execute(statement)
    connection.execute(f"PRAGMA application_id = {AMADEUS_APPLICATION_ID}")


_SCHEMA_V4: tuple[str, ...] = (
    """
    CREATE TABLE attachments (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('image', 'document')),
        source TEXT NOT NULL CHECK (
            source IN ('file_picker', 'drop', 'clipboard', 'screenshot',
                       'screen', 'window', 'camera')
        ),
        display_name TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
        relative_path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'failed')),
        extracted_text TEXT NOT NULL DEFAULT '',
        text_truncated INTEGER NOT NULL DEFAULT 0 CHECK (text_truncated IN (0, 1)),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX attachments_sha256_idx ON attachments(sha256)
    """,
    """
    CREATE INDEX attachments_relative_path_idx ON attachments(relative_path)
    """,
    """
    CREATE TABLE message_attachments (
        message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0 AND ordinal < 5),
        PRIMARY KEY (message_id, attachment_id),
        UNIQUE (message_id, ordinal)
    )
    """,
    """
    CREATE INDEX message_attachments_attachment_idx
    ON message_attachments(attachment_id, message_id)
    """,
)


def _migrate_to_v4(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V4:
        connection.execute(statement)


_SCHEMA_V5: tuple[str, ...] = (
    """
    ALTER TABLE messages
    ADD COLUMN input_modality TEXT NOT NULL DEFAULT 'text'
        CHECK (input_modality IN ('text', 'voice'))
    """,
)


def _migrate_to_v5(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V5:
        connection.execute(statement)


_DEFAULT_MIGRATIONS: Mapping[int, Migration] = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
}
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
    "memory_embedding_generations",
    "memory_vectors",
    "memory_recall_events",
    "persona_knowledge",
    "persona_fts",
    "persona_embedding_generations",
    "persona_vectors",
    "persona_recall_events",
    "proactive_events",
    "attachments",
    "message_attachments",
}
_REQUIRED_TRIGGER_SQL_MARKERS = {
    "memory_versions_are_immutable": (
        "before update on memory_versions",
        "raise(abort, 'memory versions are immutable')",
    ),
    "memory_versions_no_individual_delete": (
        "before delete on memory_versions",
        "when exists (select 1 from memory_groups where id = old.memory_id)",
        "raise(abort, 'memory versions can only be deleted with their group')",
    ),
}
_REQUIRED_PARTIAL_INDEX_SQL_MARKERS = {
    "memory_embedding_one_active_idx": (
        "create unique index",
        "on memory_embedding_generations(profile_id)",
        "where status = 'active'",
    ),
    "persona_embedding_one_active_idx": (
        "create unique index",
        "on persona_embedding_generations(persona_id)",
        "where status = 'active'",
    ),
}
_REQUIRED_INDEX_SQL_MARKERS = {
    "proactive_events_profile_date_idx": (
        "create index",
        "on proactive_events(profile_id, local_date, displayed_at)",
    ),
}
_REQUIRED_COLUMNS = {
    "messages": {
        "sequence",
        "id",
        "conversation_id",
        "turn_id",
        "role",
        "content",
        "status",
        "attempt",
        "terminal_reason",
        "provider_name",
        "model_name",
        "failure_code",
        "participates_in_memory",
        "created_at",
        "updated_at",
        "completed_at",
        "origin",
        "input_modality",
    },
    "attachments": {
        "id",
        "kind",
        "source",
        "display_name",
        "mime_type",
        "size_bytes",
        "sha256",
        "relative_path",
        "status",
        "extracted_text",
        "text_truncated",
        "created_at",
    },
    "message_attachments": {"message_id", "attachment_id", "ordinal"},
    "memory_embedding_generations": {
        "id",
        "profile_id",
        "model_name",
        "model_commit",
        "dimension",
        "model_sha256",
        "calibration_threshold",
        "status",
        "item_count",
        "failure_code",
        "created_at",
        "updated_at",
        "activated_at",
    },
    "memory_vectors": {
        "generation_id",
        "version_id",
        "vector_blob",
        "vector_hash",
        "created_at",
    },
    "memory_recall_events": {
        "id",
        "retrieval_ticket_id",
        "profile_id",
        "version_id",
        "conversation_id",
        "assistant_message_id",
        "attempt",
        "terminal_status",
        "recalled_at",
    },
    "persona_knowledge": {
        "id",
        "persona_id",
        "content",
        "search_text",
        "tags_json",
        "source_ref",
        "source_hash",
        "content_hash",
        "active",
        "created_at",
        "updated_at",
    },
    "persona_embedding_generations": {
        "id",
        "persona_id",
        "model_name",
        "model_commit",
        "dimension",
        "model_sha256",
        "calibration_threshold",
        "status",
        "item_count",
        "failure_code",
        "created_at",
        "updated_at",
        "activated_at",
    },
    "persona_vectors": {
        "generation_id",
        "knowledge_id",
        "vector_blob",
        "vector_hash",
        "created_at",
    },
    "persona_recall_events": {
        "id",
        "retrieval_ticket_id",
        "persona_id",
        "knowledge_id",
        "conversation_id",
        "assistant_message_id",
        "attempt",
        "terminal_status",
        "recalled_at",
    },
    "proactive_events": {
        "id",
        "profile_id",
        "local_date",
        "trigger_kind",
        "displayed_at",
        "disposition",
        "message_id",
    },
}
_REQUIRED_FTS_COLUMNS = {
    "memory_fts": {"version_id", "memory_id", "profile_id", "search_text"},
    "persona_fts": {"knowledge_id", "persona_id", "search_text"},
}
_REQUIRED_FTS_SQL_MARKERS = (
    "virtual table",
    "using fts5",
    "tokenize = 'unicode61 remove_diacritics 2'",
)


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
            "application_id",
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
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if application_id != AMADEUS_APPLICATION_ID:
            raise DatabaseMigrationError("database application ID is invalid")
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not _REQUIRED_TABLES.issubset(existing):
            raise DatabaseMigrationError("database schema is incomplete")
        trigger_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        triggers = {str(row["name"]): str(row["sql"] or "") for row in trigger_rows}
        if not _REQUIRED_TRIGGER_SQL_MARKERS.keys() <= triggers.keys():
            raise DatabaseMigrationError("database schema is missing required triggers")
        for name, markers in _REQUIRED_TRIGGER_SQL_MARKERS.items():
            normalized_sql = " ".join(triggers[name].lower().split())
            if any(marker not in normalized_sql for marker in markers):
                raise DatabaseMigrationError("database immutable-memory trigger is invalid")
        index_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
        indexes = {str(row["name"]): str(row["sql"] or "") for row in index_rows}
        if not _REQUIRED_PARTIAL_INDEX_SQL_MARKERS.keys() <= indexes.keys():
            raise DatabaseMigrationError("database schema is missing required indexes")
        for name, markers in _REQUIRED_PARTIAL_INDEX_SQL_MARKERS.items():
            normalized_sql = " ".join(indexes[name].lower().split())
            if any(marker not in normalized_sql for marker in markers):
                raise DatabaseMigrationError("database active-generation index is invalid")
        if not _REQUIRED_INDEX_SQL_MARKERS.keys() <= indexes.keys():
            raise DatabaseMigrationError("database schema is missing required indexes")
        for name, markers in _REQUIRED_INDEX_SQL_MARKERS.items():
            normalized_sql = " ".join(indexes[name].lower().split())
            if any(marker not in normalized_sql for marker in markers):
                raise DatabaseMigrationError("database required index is invalid")
        for table, required_columns in _REQUIRED_COLUMNS.items():
            columns = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if columns != required_columns:
                raise DatabaseMigrationError("database schema columns are invalid")
        for table, required_columns in _REQUIRED_FTS_COLUMNS.items():
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            normalized_sql = "" if row is None else " ".join(str(row["sql"] or "").lower().split())
            if any(marker not in normalized_sql for marker in _REQUIRED_FTS_SQL_MARKERS):
                raise DatabaseMigrationError("database FTS schema is invalid")
            columns = {
                str(column["name"])
                for column in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if columns != required_columns:
                raise DatabaseMigrationError("database FTS columns are invalid")
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


def validate_database_schema(connection: sqlite3.Connection) -> None:
    """Apply the exact runtime schema/integrity gate to an existing connection."""

    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        SQLiteDatabase._validate_schema(connection)
    finally:
        connection.row_factory = previous_factory
