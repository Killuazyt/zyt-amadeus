"""SQLite schema v8, consistent migration backups, and fail-closed opening."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 8
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


_SCHEMA_V6: tuple[str, ...] = (
    """
    ALTER TABLE memory_groups
    ADD COLUMN subject_scope TEXT NOT NULL DEFAULT 'user'
        CHECK (subject_scope IN ('user', 'relationship'))
    """,
    """
    ALTER TABLE memory_versions
    ADD COLUMN event_started_at TEXT
    """,
    """
    ALTER TABLE memory_versions
    ADD COLUMN event_ended_at TEXT
    """,
    """
    ALTER TABLE memory_versions
    ADD COLUMN time_confidence REAL
        CHECK (time_confidence IS NULL OR (time_confidence >= 0.0 AND time_confidence <= 1.0))
    """,
    """
    ALTER TABLE memory_versions
    ADD COLUMN deep_memory_eligible INTEGER NOT NULL DEFAULT 0
        CHECK (deep_memory_eligible IN (0, 1))
    """,
    """
    CREATE TABLE memory_reflections (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        subject_scope TEXT NOT NULL
            CHECK (subject_scope IN ('user', 'companion', 'relationship')),
        topic_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'tentative'
            CHECK (status IN (
                'tentative', 'confirmed', 'promoted', 'merged',
                'disputed', 'denied', 'archived'
            )),
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        current_version_id TEXT,
        archive_candidate_since TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (current_version_id) REFERENCES memory_reflection_versions(id)
            ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE INDEX memory_reflections_profile_state_idx
    ON memory_reflections(profile_id, status, pinned DESC, updated_at DESC)
    """,
    """
    CREATE INDEX memory_reflections_topic_idx
    ON memory_reflections(profile_id, subject_scope, topic_key)
    """,
    """
    CREATE TABLE memory_reflection_versions (
        id TEXT PRIMARY KEY,
        reflection_id TEXT NOT NULL REFERENCES memory_reflections(id) ON DELETE CASCADE,
        version_number INTEGER NOT NULL CHECK (version_number >= 1),
        content TEXT NOT NULL,
        normalized_content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        search_text TEXT NOT NULL,
        importance REAL NOT NULL CHECK (importance >= 0.0 AND importance <= 1.0),
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        origin TEXT NOT NULL CHECK (origin IN ('automatic', 'manual')),
        operation TEXT NOT NULL
            CHECK (operation IN ('add', 'merge', 'correct', 'manual_edit', 'rollback')),
        supersedes_version_id TEXT REFERENCES memory_reflection_versions(id)
            DEFERRABLE INITIALLY DEFERRED,
        created_at TEXT NOT NULL,
        UNIQUE (reflection_id, version_number)
    )
    """,
    """
    CREATE INDEX memory_reflection_versions_hash_idx
    ON memory_reflection_versions(content_hash)
    """,
    """
    CREATE TRIGGER memory_reflection_versions_are_immutable
    BEFORE UPDATE ON memory_reflection_versions
    BEGIN
        SELECT RAISE(ABORT, 'reflection versions are immutable');
    END
    """,
    """
    CREATE TRIGGER memory_reflection_versions_no_individual_delete
    BEFORE DELETE ON memory_reflection_versions
    WHEN EXISTS (SELECT 1 FROM memory_reflections WHERE id = OLD.reflection_id)
    BEGIN
        SELECT RAISE(ABORT, 'reflection versions can only be deleted with their group');
    END
    """,
    """
    CREATE TABLE memory_reflection_sources (
        id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL REFERENCES memory_reflection_versions(id) ON DELETE CASCADE,
        fact_version_id TEXT REFERENCES memory_versions(id) ON DELETE CASCADE,
        source_message_id TEXT NOT NULL,
        live_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
        extraction_method TEXT NOT NULL CHECK (extraction_method IN ('automatic', 'manual')),
        created_at TEXT NOT NULL,
        UNIQUE (version_id, fact_version_id, source_message_id)
    )
    """,
    """
    CREATE INDEX memory_reflection_sources_fact_idx
    ON memory_reflection_sources(fact_version_id)
    """,
    """
    CREATE INDEX memory_reflection_sources_message_idx
    ON memory_reflection_sources(live_message_id)
    """,
    """
    CREATE VIRTUAL TABLE memory_reflection_fts USING fts5(
        version_id UNINDEXED,
        reflection_id UNINDEXED,
        profile_id UNINDEXED,
        search_text,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE memory_reflection_embedding_generations (
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
    CREATE UNIQUE INDEX memory_reflection_embedding_one_active_idx
    ON memory_reflection_embedding_generations(profile_id)
    WHERE status = 'active'
    """,
    """
    CREATE TABLE memory_reflection_vectors (
        generation_id TEXT NOT NULL
            REFERENCES memory_reflection_embedding_generations(id) ON DELETE CASCADE,
        version_id TEXT NOT NULL REFERENCES memory_reflection_versions(id) ON DELETE CASCADE,
        vector_blob BLOB NOT NULL CHECK (length(vector_blob) > 0),
        vector_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (generation_id, version_id)
    )
    """,
    """
    CREATE INDEX memory_reflection_vectors_version_idx
    ON memory_reflection_vectors(version_id)
    """,
    """
    CREATE TABLE memory_reflection_recall_events (
        id TEXT PRIMARY KEY,
        retrieval_ticket_id TEXT NOT NULL,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        version_id TEXT NOT NULL REFERENCES memory_reflection_versions(id) ON DELETE CASCADE,
        conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
        assistant_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
        attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
        terminal_status TEXT NOT NULL CHECK (terminal_status IN ('completed', 'user_stopped')),
        recalled_at TEXT NOT NULL,
        UNIQUE (retrieval_ticket_id, version_id)
    )
    """,
    """
    CREATE INDEX memory_reflection_recall_version_time_idx
    ON memory_reflection_recall_events(version_id, recalled_at DESC)
    """,
    """
    CREATE TABLE memory_persona_impressions (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        subject_scope TEXT NOT NULL
            CHECK (subject_scope IN ('user', 'companion', 'relationship')),
        topic_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'disputed', 'denied', 'archived')),
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        current_version_id TEXT,
        archive_candidate_since TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (current_version_id) REFERENCES memory_persona_impression_versions(id)
            ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE INDEX memory_persona_impressions_profile_state_idx
    ON memory_persona_impressions(profile_id, status, pinned DESC, updated_at DESC)
    """,
    """
    CREATE INDEX memory_persona_impressions_topic_idx
    ON memory_persona_impressions(profile_id, subject_scope, topic_key)
    """,
    """
    CREATE TABLE memory_persona_impression_versions (
        id TEXT PRIMARY KEY,
        impression_id TEXT NOT NULL
            REFERENCES memory_persona_impressions(id) ON DELETE CASCADE,
        version_number INTEGER NOT NULL CHECK (version_number >= 1),
        content TEXT NOT NULL,
        normalized_content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        search_text TEXT NOT NULL,
        importance REAL NOT NULL CHECK (importance >= 0.0 AND importance <= 1.0),
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        origin TEXT NOT NULL CHECK (origin IN ('automatic', 'manual')),
        operation TEXT NOT NULL
            CHECK (operation IN ('add', 'merge', 'correct', 'manual_edit', 'rollback')),
        supersedes_version_id TEXT REFERENCES memory_persona_impression_versions(id)
            DEFERRABLE INITIALLY DEFERRED,
        created_at TEXT NOT NULL,
        UNIQUE (impression_id, version_number)
    )
    """,
    """
    CREATE INDEX memory_persona_impression_versions_hash_idx
    ON memory_persona_impression_versions(content_hash)
    """,
    """
    CREATE TRIGGER memory_persona_impression_versions_are_immutable
    BEFORE UPDATE ON memory_persona_impression_versions
    BEGIN
        SELECT RAISE(ABORT, 'persona impression versions are immutable');
    END
    """,
    """
    CREATE TRIGGER memory_persona_impression_versions_no_individual_delete
    BEFORE DELETE ON memory_persona_impression_versions
    WHEN EXISTS (
        SELECT 1 FROM memory_persona_impressions WHERE id = OLD.impression_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'persona impression versions can only be deleted with their group');
    END
    """,
    """
    CREATE TABLE memory_persona_impression_sources (
        id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL
            REFERENCES memory_persona_impression_versions(id) ON DELETE CASCADE,
        reflection_version_id TEXT
            REFERENCES memory_reflection_versions(id) ON DELETE CASCADE,
        source_message_id TEXT NOT NULL,
        live_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
        extraction_method TEXT NOT NULL CHECK (extraction_method IN ('automatic', 'manual')),
        created_at TEXT NOT NULL,
        UNIQUE (version_id, reflection_version_id, source_message_id)
    )
    """,
    """
    CREATE INDEX memory_persona_impression_sources_reflection_idx
    ON memory_persona_impression_sources(reflection_version_id)
    """,
    """
    CREATE INDEX memory_persona_impression_sources_message_idx
    ON memory_persona_impression_sources(live_message_id)
    """,
    """
    CREATE VIRTUAL TABLE memory_persona_impression_fts USING fts5(
        version_id UNINDEXED,
        impression_id UNINDEXED,
        profile_id UNINDEXED,
        search_text,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE memory_persona_impression_embedding_generations (
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
    CREATE UNIQUE INDEX memory_persona_impression_embedding_one_active_idx
    ON memory_persona_impression_embedding_generations(profile_id)
    WHERE status = 'active'
    """,
    """
    CREATE TABLE memory_persona_impression_vectors (
        generation_id TEXT NOT NULL
            REFERENCES memory_persona_impression_embedding_generations(id) ON DELETE CASCADE,
        version_id TEXT NOT NULL
            REFERENCES memory_persona_impression_versions(id) ON DELETE CASCADE,
        vector_blob BLOB NOT NULL CHECK (length(vector_blob) > 0),
        vector_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (generation_id, version_id)
    )
    """,
    """
    CREATE INDEX memory_persona_impression_vectors_version_idx
    ON memory_persona_impression_vectors(version_id)
    """,
    """
    CREATE TABLE memory_persona_impression_recall_events (
        id TEXT PRIMARY KEY,
        retrieval_ticket_id TEXT NOT NULL,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        version_id TEXT NOT NULL
            REFERENCES memory_persona_impression_versions(id) ON DELETE CASCADE,
        conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
        assistant_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
        attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
        terminal_status TEXT NOT NULL CHECK (terminal_status IN ('completed', 'user_stopped')),
        recalled_at TEXT NOT NULL,
        UNIQUE (retrieval_ticket_id, version_id)
    )
    """,
    """
    CREATE INDEX memory_persona_impression_recall_version_time_idx
    ON memory_persona_impression_recall_events(version_id, recalled_at DESC)
    """,
    """
    CREATE TABLE memory_evidence_signals (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        target_layer TEXT NOT NULL CHECK (target_layer IN ('fact', 'reflection', 'persona')),
        target_group_id TEXT NOT NULL,
        target_version_id TEXT NOT NULL,
        source_message_id TEXT,
        source_fact_version_id TEXT REFERENCES memory_versions(id) ON DELETE CASCADE,
        signal_kind TEXT NOT NULL CHECK (signal_kind IN (
            'initial', 'indirect_support', 'indirect_refute',
            'direct_confirm', 'direct_rebut'
        )),
        reinforcement_delta REAL NOT NULL DEFAULT 0.0,
        disputation_delta REAL NOT NULL DEFAULT 0.0 CHECK (disputation_delta >= 0.0),
        correlation_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX memory_evidence_target_idx
    ON memory_evidence_signals(target_layer, target_version_id, created_at)
    """,
    """
    CREATE TRIGGER memory_evidence_signals_are_immutable
    BEFORE UPDATE ON memory_evidence_signals
    BEGIN
        SELECT RAISE(ABORT, 'memory evidence signals are immutable');
    END
    """,
    """
    CREATE TABLE memory_conflicts (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        target_layer TEXT NOT NULL CHECK (target_layer IN ('fact', 'reflection', 'persona')),
        target_group_id TEXT NOT NULL,
        incumbent_version_id TEXT NOT NULL,
        challenger_version_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
        resolution TEXT CHECK (resolution IN ('keep', 'accept', 'merge')),
        source_message_id TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )
    """,
    """
    CREATE INDEX memory_conflicts_open_idx
    ON memory_conflicts(profile_id, target_layer, status, created_at DESC)
    """,
    """
    CREATE UNIQUE INDEX memory_conflicts_one_open_idx
    ON memory_conflicts(target_layer, target_group_id)
    WHERE status = 'open'
    """,
    """
    CREATE TABLE memory_audit_events (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        owner_layer TEXT NOT NULL CHECK (owner_layer IN ('fact', 'reflection', 'persona')),
        owner_group_id TEXT NOT NULL,
        version_id TEXT,
        source_message_id TEXT,
        event_type TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        reinforcement_delta REAL NOT NULL DEFAULT 0.0,
        disputation_delta REAL NOT NULL DEFAULT 0.0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        occurred_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX memory_audit_owner_time_idx
    ON memory_audit_events(owner_layer, owner_group_id, occurred_at DESC)
    """,
    """
    CREATE INDEX memory_audit_profile_time_idx
    ON memory_audit_events(profile_id, occurred_at DESC)
    """,
    """
    CREATE TRIGGER memory_audit_events_are_immutable
    BEFORE UPDATE ON memory_audit_events
    BEGIN
        SELECT RAISE(ABORT, 'memory audit events are immutable');
    END
    """,
    """
    CREATE TABLE memory_pipeline_state (
        profile_id TEXT PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
        completed_turn_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_turn_count >= 0),
        last_signal_message_sequence INTEGER NOT NULL DEFAULT 0
            CHECK (last_signal_message_sequence >= 0),
        last_signal_at TEXT,
        last_maintenance_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)


def _migrate_to_v6(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V6:
        connection.execute(statement)


_SCHEMA_V7: tuple[str, ...] = (
    """
    CREATE TABLE companion_cues (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('conversation_followup', 'memory_followup')),
        topic TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 120),
        frozen_text TEXT NOT NULL CHECK (length(frozen_text) BETWEEN 1 AND 240),
        status TEXT NOT NULL CHECK (status IN (
            'proposed', 'active', 'surfaced', 'resolved', 'rejected', 'expired'
        )),
        reason TEXT NOT NULL CHECK (reason IN (
            'explicit_return', 'pending_result', 'user_promised_update', 'memory_authorized'
        )),
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        keep_until_resolved INTEGER NOT NULL DEFAULT 0
            CHECK (keep_until_resolved IN (0, 1)),
        dedupe_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        confirmed_at TEXT,
        expires_at TEXT,
        surfaced_at TEXT,
        resolved_at TEXT,
        CHECK (
            status IN ('proposed', 'rejected', 'expired')
            OR confirmed_at IS NOT NULL
        ),
        CHECK (keep_until_resolved = 0 OR expires_at IS NULL)
    )
    """,
    """
    CREATE INDEX companion_cues_selection_idx
    ON companion_cues(profile_id, status, kind, expires_at, created_at)
    """,
    """
    CREATE UNIQUE INDEX companion_cues_live_dedupe_idx
    ON companion_cues(profile_id, dedupe_key)
    WHERE status IN ('proposed', 'active', 'surfaced')
    """,
    """
    CREATE TRIGGER companion_cues_frozen_after_confirmation
    BEFORE UPDATE OF topic, frozen_text, dedupe_key ON companion_cues
    WHEN OLD.status <> 'proposed'
    BEGIN
        SELECT RAISE(ABORT, 'confirmed companion cue text is immutable');
    END
    """,
    """
    CREATE TABLE companion_cue_sources (
        id TEXT PRIMARY KEY,
        cue_id TEXT NOT NULL REFERENCES companion_cues(id) ON DELETE CASCADE,
        source_kind TEXT NOT NULL CHECK (source_kind IN (
            'user_message', 'fact_version', 'reflection_version', 'persona_version'
        )),
        source_message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
        fact_version_id TEXT REFERENCES memory_versions(id) ON DELETE CASCADE,
        reflection_version_id TEXT
            REFERENCES memory_reflection_versions(id) ON DELETE CASCADE,
        persona_version_id TEXT
            REFERENCES memory_persona_impression_versions(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        CHECK (
            (source_message_id IS NOT NULL)
            + (fact_version_id IS NOT NULL)
            + (reflection_version_id IS NOT NULL)
            + (persona_version_id IS NOT NULL) = 1
        ),
        CHECK (
            (source_kind = 'user_message' AND source_message_id IS NOT NULL)
            OR (source_kind = 'fact_version' AND fact_version_id IS NOT NULL)
            OR (source_kind = 'reflection_version' AND reflection_version_id IS NOT NULL)
            OR (source_kind = 'persona_version' AND persona_version_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX companion_cue_sources_cue_idx
    ON companion_cue_sources(cue_id)
    """,
    """
    CREATE INDEX companion_cue_sources_message_idx
    ON companion_cue_sources(source_message_id)
    """,
    """
    CREATE INDEX companion_cue_sources_fact_idx
    ON companion_cue_sources(fact_version_id)
    """,
    """
    CREATE INDEX companion_cue_sources_reflection_idx
    ON companion_cue_sources(reflection_version_id)
    """,
    """
    CREATE INDEX companion_cue_sources_persona_idx
    ON companion_cue_sources(persona_version_id)
    """,
    """
    CREATE TABLE companion_cue_audit_events (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        cue_id TEXT NOT NULL,
        cue_kind TEXT NOT NULL CHECK (cue_kind IN ('conversation_followup', 'memory_followup')),
        event_type TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        previous_status TEXT,
        resulting_status TEXT,
        occurred_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX companion_cue_audit_profile_time_idx
    ON companion_cue_audit_events(profile_id, occurred_at DESC)
    """,
    """
    CREATE INDEX companion_cue_audit_cue_time_idx
    ON companion_cue_audit_events(cue_id, occurred_at DESC)
    """,
    """
    CREATE TRIGGER companion_cue_audit_events_are_immutable
    BEFORE UPDATE ON companion_cue_audit_events
    BEGIN
        SELECT RAISE(ABORT, 'companion cue audit events are immutable');
    END
    """,
    """
    CREATE TRIGGER companion_cues_audit_status_change
    AFTER UPDATE OF status ON companion_cues
    WHEN OLD.status <> NEW.status
    BEGIN
        INSERT INTO companion_cue_audit_events (
            id, profile_id, cue_id, cue_kind, event_type, reason_code,
            previous_status, resulting_status, occurred_at
        ) VALUES (
            lower(hex(randomblob(16))), NEW.profile_id, NEW.id, NEW.kind, 'status_changed',
            CASE WHEN NEW.status = 'expired' THEN 'source_or_time_invalidated'
                 ELSE 'user_or_runtime_transition' END,
            OLD.status, NEW.status,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        );
    END
    """,
    """
    CREATE TRIGGER companion_cues_audit_delete
    BEFORE DELETE ON companion_cues
    BEGIN
        INSERT INTO companion_cue_audit_events (
            id, profile_id, cue_id, cue_kind, event_type, reason_code,
            previous_status, resulting_status, occurred_at
        ) VALUES (
            lower(hex(randomblob(16))), OLD.profile_id, OLD.id, OLD.kind, 'deleted',
            'content_and_sources_removed', OLD.status, NULL,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        );
    END
    """,
    """
    CREATE TRIGGER companion_cue_source_delete_removes_cue
    AFTER DELETE ON companion_cue_sources
    WHEN EXISTS (SELECT 1 FROM companion_cues WHERE id = OLD.cue_id)
    BEGIN
        DELETE FROM companion_cues WHERE id = OLD.cue_id;
    END
    """,
    """
    CREATE TRIGGER companion_cues_invalidate_fact_version
    AFTER UPDATE OF current_version_id, status ON memory_groups
    BEGIN
        UPDATE companion_cues
        SET status = 'expired', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            expires_at = COALESCE(expires_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        WHERE status IN ('proposed', 'active', 'surfaced')
          AND id IN (
              SELECT cue_id FROM companion_cue_sources
              WHERE fact_version_id IS NOT NULL
                AND fact_version_id IN (
                    SELECT id FROM memory_versions WHERE memory_id = NEW.id
                )
          )
          AND (NEW.status <> 'active' OR NEW.current_version_id IS NULL
               OR id IN (
                   SELECT cue_id FROM companion_cue_sources
                   WHERE fact_version_id IS NOT NEW.current_version_id
               ));
    END
    """,
    """
    CREATE TRIGGER companion_cues_invalidate_reflection_version
    AFTER UPDATE OF current_version_id, status ON memory_reflections
    BEGIN
        UPDATE companion_cues
        SET status = 'expired', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            expires_at = COALESCE(expires_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        WHERE status IN ('proposed', 'active', 'surfaced')
          AND id IN (
              SELECT cue_id FROM companion_cue_sources
              WHERE reflection_version_id IS NOT NULL
                AND reflection_version_id IN (
                    SELECT id FROM memory_reflection_versions WHERE reflection_id = NEW.id
                )
          )
          AND (NEW.status NOT IN ('confirmed', 'promoted') OR NEW.current_version_id IS NULL
               OR id IN (
                   SELECT cue_id FROM companion_cue_sources
                   WHERE reflection_version_id IS NOT NEW.current_version_id
               ));
    END
    """,
    """
    CREATE TRIGGER companion_cues_invalidate_persona_version
    AFTER UPDATE OF current_version_id, status ON memory_persona_impressions
    BEGIN
        UPDATE companion_cues
        SET status = 'expired', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            expires_at = COALESCE(expires_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        WHERE status IN ('proposed', 'active', 'surfaced')
          AND id IN (
              SELECT cue_id FROM companion_cue_sources
              WHERE persona_version_id IS NOT NULL
                AND persona_version_id IN (
                    SELECT id FROM memory_persona_impression_versions
                    WHERE impression_id = NEW.id
                )
          )
          AND (NEW.status <> 'active' OR NEW.current_version_id IS NULL
               OR id IN (
                   SELECT cue_id FROM companion_cue_sources
                   WHERE persona_version_id IS NOT NEW.current_version_id
               ));
    END
    """,
    """
    CREATE TRIGGER companion_cues_invalidate_open_conflict
    AFTER INSERT ON memory_conflicts
    WHEN NEW.status = 'open'
    BEGIN
        UPDATE companion_cues
        SET status = 'expired', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            expires_at = COALESCE(expires_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        WHERE status IN ('proposed', 'active', 'surfaced')
          AND id IN (
              SELECT cue_id FROM companion_cue_sources
              WHERE (NEW.target_layer = 'fact' AND fact_version_id = NEW.incumbent_version_id)
                 OR (NEW.target_layer = 'reflection'
                     AND reflection_version_id = NEW.incumbent_version_id)
                 OR (NEW.target_layer = 'persona'
                     AND persona_version_id = NEW.incumbent_version_id)
          );
    END
    """,
    "ALTER TABLE proactive_events ADD COLUMN cue_id TEXT",
    """
    ALTER TABLE messages ADD COLUMN companion_cue_id TEXT
        REFERENCES companion_cues(id) ON DELETE SET NULL
    """,
    """
    CREATE INDEX messages_companion_cue_idx
    ON messages(companion_cue_id)
    """,
)


def _migrate_to_v7(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V7:
        connection.execute(statement)


_SCHEMA_V8: tuple[str, ...] = (
    """
    CREATE TABLE temporal_commitments (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        source_kind TEXT NOT NULL CHECK (source_kind IN ('chat', 'manual')),
        source_message_id TEXT,
        source_conversation_id TEXT,
        live_source_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
        live_source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
        status TEXT NOT NULL CHECK (status IN (
            'draft', 'scheduled', 'due', 'surfaced', 'completed', 'cancelled'
        )),
        current_version_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        confirmed_at TEXT,
        due_detected_at TEXT,
        surfaced_at TEXT,
        completed_at TEXT,
        cancelled_at TEXT,
        FOREIGN KEY (current_version_id) REFERENCES temporal_commitment_versions(id)
            ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED,
        CHECK (
            (source_kind = 'manual' AND source_message_id IS NULL
                AND source_conversation_id IS NULL AND live_source_message_id IS NULL
                AND live_source_conversation_id IS NULL)
            OR
            (source_kind = 'chat' AND source_message_id IS NOT NULL
                AND source_conversation_id IS NOT NULL)
        ),
        CHECK (status = 'draft' OR confirmed_at IS NOT NULL),
        CHECK (status <> 'completed' OR completed_at IS NOT NULL),
        CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL)
    )
    """,
    """
    CREATE TABLE temporal_commitment_versions (
        id TEXT PRIMARY KEY,
        commitment_id TEXT NOT NULL
            REFERENCES temporal_commitments(id) ON DELETE CASCADE,
        version_number INTEGER NOT NULL CHECK (version_number >= 1),
        kind TEXT NOT NULL CHECK (kind IN ('reminder', 'scheduled_followup')),
        content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 500),
        due_at_utc TEXT,
        original_local_time TEXT,
        timezone_name TEXT,
        utc_offset_minutes INTEGER CHECK (
            utc_offset_minutes IS NULL OR utc_offset_minutes BETWEEN -840 AND 840
        ),
        show_content INTEGER NOT NULL DEFAULT 0 CHECK (show_content IN (0, 1)),
        origin TEXT NOT NULL CHECK (origin IN ('chat', 'manual', 'edit', 'snooze')),
        supersedes_version_id TEXT REFERENCES temporal_commitment_versions(id)
            DEFERRABLE INITIALLY DEFERRED,
        created_at TEXT NOT NULL,
        UNIQUE (commitment_id, version_number),
        CHECK (
            (due_at_utc IS NULL AND original_local_time IS NULL)
            OR (due_at_utc IS NOT NULL AND original_local_time IS NOT NULL
                AND timezone_name IS NOT NULL AND utc_offset_minutes IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX temporal_commitments_due_idx
    ON temporal_commitments(profile_id, status, updated_at, id)
    """,
    """
    CREATE INDEX temporal_commitment_versions_due_idx
    ON temporal_commitment_versions(due_at_utc, commitment_id)
    """,
    """
    CREATE INDEX temporal_commitments_source_conversation_idx
    ON temporal_commitments(source_conversation_id, status)
    """,
    """
    CREATE UNIQUE INDEX temporal_commitments_source_message_idx
    ON temporal_commitments(source_message_id)
    WHERE source_message_id IS NOT NULL
    """,
    """
    CREATE TRIGGER temporal_commitment_versions_are_immutable
    BEFORE UPDATE ON temporal_commitment_versions
    BEGIN
        SELECT RAISE(ABORT, 'temporal commitment versions are immutable');
    END
    """,
    """
    CREATE TRIGGER temporal_commitment_versions_no_individual_delete
    BEFORE DELETE ON temporal_commitment_versions
    WHEN EXISTS (SELECT 1 FROM temporal_commitments WHERE id = OLD.commitment_id)
    BEGIN
        SELECT RAISE(ABORT, 'temporal commitment versions can only be deleted with their group');
    END
    """,
    """
    CREATE TABLE temporal_commitment_audit_events (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        commitment_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        previous_status TEXT,
        resulting_status TEXT,
        version_id TEXT,
        occurred_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX temporal_commitment_audit_profile_time_idx
    ON temporal_commitment_audit_events(profile_id, occurred_at DESC)
    """,
    """
    CREATE INDEX temporal_commitment_audit_commitment_time_idx
    ON temporal_commitment_audit_events(commitment_id, occurred_at DESC)
    """,
    """
    CREATE TRIGGER temporal_commitment_audit_events_are_immutable
    BEFORE UPDATE ON temporal_commitment_audit_events
    BEGIN
        SELECT RAISE(ABORT, 'temporal commitment audit events are immutable');
    END
    """,
    """
    CREATE TRIGGER temporal_commitments_audit_delete
    BEFORE DELETE ON temporal_commitments
    BEGIN
        INSERT INTO temporal_commitment_audit_events (
            id, profile_id, commitment_id, event_type, reason_code,
            previous_status, resulting_status, version_id, occurred_at
        ) VALUES (
            lower(hex(randomblob(16))), OLD.profile_id, OLD.id, 'deleted',
            'content_and_sources_removed', OLD.status, NULL, OLD.current_version_id,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        );
    END
    """,
    """
    ALTER TABLE messages ADD COLUMN temporal_commitment_id TEXT
        REFERENCES temporal_commitments(id) ON DELETE SET NULL
    """,
    """
    CREATE INDEX messages_temporal_commitment_idx
    ON messages(temporal_commitment_id)
    """,
)


def _migrate_to_v8(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V8:
        connection.execute(statement)


_DEFAULT_MIGRATIONS: Mapping[int, Migration] = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
    6: _migrate_to_v6,
    7: _migrate_to_v7,
    8: _migrate_to_v8,
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
    "memory_reflections",
    "memory_reflection_versions",
    "memory_reflection_sources",
    "memory_reflection_fts",
    "memory_reflection_embedding_generations",
    "memory_reflection_vectors",
    "memory_reflection_recall_events",
    "memory_persona_impressions",
    "memory_persona_impression_versions",
    "memory_persona_impression_sources",
    "memory_persona_impression_fts",
    "memory_persona_impression_embedding_generations",
    "memory_persona_impression_vectors",
    "memory_persona_impression_recall_events",
    "memory_evidence_signals",
    "memory_conflicts",
    "memory_audit_events",
    "memory_pipeline_state",
    "companion_cues",
    "companion_cue_sources",
    "companion_cue_audit_events",
    "temporal_commitments",
    "temporal_commitment_versions",
    "temporal_commitment_audit_events",
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
    "memory_reflection_versions_are_immutable": (
        "before update on memory_reflection_versions",
        "raise(abort, 'reflection versions are immutable')",
    ),
    "memory_reflection_versions_no_individual_delete": (
        "before delete on memory_reflection_versions",
        "raise(abort, 'reflection versions can only be deleted with their group')",
    ),
    "memory_persona_impression_versions_are_immutable": (
        "before update on memory_persona_impression_versions",
        "raise(abort, 'persona impression versions are immutable')",
    ),
    "memory_persona_impression_versions_no_individual_delete": (
        "before delete on memory_persona_impression_versions",
        "raise(abort, 'persona impression versions can only be deleted with their group')",
    ),
    "memory_evidence_signals_are_immutable": (
        "before update on memory_evidence_signals",
        "raise(abort, 'memory evidence signals are immutable')",
    ),
    "memory_audit_events_are_immutable": (
        "before update on memory_audit_events",
        "raise(abort, 'memory audit events are immutable')",
    ),
    "companion_cue_audit_events_are_immutable": (
        "before update on companion_cue_audit_events",
        "raise(abort, 'companion cue audit events are immutable')",
    ),
    "companion_cues_frozen_after_confirmation": (
        "before update of topic, frozen_text, dedupe_key on companion_cues",
        "when old.status <> 'proposed'",
        "raise(abort, 'confirmed companion cue text is immutable')",
    ),
    "temporal_commitment_versions_are_immutable": (
        "before update on temporal_commitment_versions",
        "raise(abort, 'temporal commitment versions are immutable')",
    ),
    "temporal_commitment_versions_no_individual_delete": (
        "before delete on temporal_commitment_versions",
        "raise(abort, 'temporal commitment versions can only be deleted with their group')",
    ),
    "temporal_commitment_audit_events_are_immutable": (
        "before update on temporal_commitment_audit_events",
        "raise(abort, 'temporal commitment audit events are immutable')",
    ),
    "temporal_commitments_audit_delete": (
        "before delete on temporal_commitments",
        "insert into temporal_commitment_audit_events",
        "'content_and_sources_removed'",
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
    "memory_reflection_embedding_one_active_idx": (
        "create unique index",
        "on memory_reflection_embedding_generations(profile_id)",
        "where status = 'active'",
    ),
    "memory_persona_impression_embedding_one_active_idx": (
        "create unique index",
        "on memory_persona_impression_embedding_generations(profile_id)",
        "where status = 'active'",
    ),
    "memory_conflicts_one_open_idx": (
        "create unique index",
        "on memory_conflicts(target_layer, target_group_id)",
        "where status = 'open'",
    ),
    "companion_cues_live_dedupe_idx": (
        "create unique index",
        "on companion_cues(profile_id, dedupe_key)",
        "where status in ('proposed', 'active', 'surfaced')",
    ),
    "temporal_commitments_source_message_idx": (
        "create unique index",
        "on temporal_commitments(source_message_id)",
        "where source_message_id is not null",
    ),
}
_REQUIRED_INDEX_SQL_MARKERS = {
    "proactive_events_profile_date_idx": (
        "create index",
        "on proactive_events(profile_id, local_date, displayed_at)",
    ),
    "temporal_commitments_due_idx": (
        "create index",
        "on temporal_commitments(profile_id, status, updated_at, id)",
    ),
    "temporal_commitment_versions_due_idx": (
        "create index",
        "on temporal_commitment_versions(due_at_utc, commitment_id)",
    ),
    "temporal_commitments_source_conversation_idx": (
        "create index",
        "on temporal_commitments(source_conversation_id, status)",
    ),
    "temporal_commitment_audit_profile_time_idx": (
        "create index",
        "on temporal_commitment_audit_events(profile_id, occurred_at desc)",
    ),
    "temporal_commitment_audit_commitment_time_idx": (
        "create index",
        "on temporal_commitment_audit_events(commitment_id, occurred_at desc)",
    ),
    "messages_temporal_commitment_idx": (
        "create index",
        "on messages(temporal_commitment_id)",
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
        "companion_cue_id",
        "temporal_commitment_id",
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
        "cue_id",
    },
    "memory_groups": {
        "id",
        "profile_id",
        "kind",
        "topic_key",
        "status",
        "pinned",
        "current_version_id",
        "created_at",
        "updated_at",
        "subject_scope",
    },
    "memory_versions": {
        "id",
        "memory_id",
        "version_number",
        "content",
        "normalized_content",
        "content_hash",
        "search_text",
        "importance",
        "confidence",
        "origin",
        "operation",
        "supersedes_version_id",
        "created_at",
        "event_started_at",
        "event_ended_at",
        "time_confidence",
        "deep_memory_eligible",
    },
    "memory_reflections": {
        "id",
        "profile_id",
        "subject_scope",
        "topic_key",
        "status",
        "pinned",
        "current_version_id",
        "archive_candidate_since",
        "created_at",
        "updated_at",
    },
    "memory_reflection_versions": {
        "id",
        "reflection_id",
        "version_number",
        "content",
        "normalized_content",
        "content_hash",
        "search_text",
        "importance",
        "confidence",
        "origin",
        "operation",
        "supersedes_version_id",
        "created_at",
    },
    "memory_reflection_sources": {
        "id",
        "version_id",
        "fact_version_id",
        "source_message_id",
        "live_message_id",
        "extraction_method",
        "created_at",
    },
    "memory_reflection_embedding_generations": {
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
    "memory_reflection_vectors": {
        "generation_id",
        "version_id",
        "vector_blob",
        "vector_hash",
        "created_at",
    },
    "memory_reflection_recall_events": {
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
    "memory_persona_impressions": {
        "id",
        "profile_id",
        "subject_scope",
        "topic_key",
        "status",
        "pinned",
        "current_version_id",
        "archive_candidate_since",
        "created_at",
        "updated_at",
    },
    "memory_persona_impression_versions": {
        "id",
        "impression_id",
        "version_number",
        "content",
        "normalized_content",
        "content_hash",
        "search_text",
        "importance",
        "confidence",
        "origin",
        "operation",
        "supersedes_version_id",
        "created_at",
    },
    "memory_persona_impression_sources": {
        "id",
        "version_id",
        "reflection_version_id",
        "source_message_id",
        "live_message_id",
        "extraction_method",
        "created_at",
    },
    "memory_persona_impression_embedding_generations": {
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
    "memory_persona_impression_vectors": {
        "generation_id",
        "version_id",
        "vector_blob",
        "vector_hash",
        "created_at",
    },
    "memory_persona_impression_recall_events": {
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
    "memory_evidence_signals": {
        "id",
        "profile_id",
        "target_layer",
        "target_group_id",
        "target_version_id",
        "source_message_id",
        "source_fact_version_id",
        "signal_kind",
        "reinforcement_delta",
        "disputation_delta",
        "correlation_key",
        "created_at",
    },
    "memory_conflicts": {
        "id",
        "profile_id",
        "target_layer",
        "target_group_id",
        "incumbent_version_id",
        "challenger_version_id",
        "status",
        "resolution",
        "source_message_id",
        "created_at",
        "resolved_at",
    },
    "memory_audit_events": {
        "id",
        "profile_id",
        "owner_layer",
        "owner_group_id",
        "version_id",
        "source_message_id",
        "event_type",
        "reason_code",
        "reinforcement_delta",
        "disputation_delta",
        "metadata_json",
        "occurred_at",
    },
    "memory_pipeline_state": {
        "profile_id",
        "completed_turn_count",
        "last_signal_message_sequence",
        "last_signal_at",
        "last_maintenance_at",
        "created_at",
        "updated_at",
    },
    "companion_cues": {
        "id",
        "profile_id",
        "conversation_id",
        "kind",
        "topic",
        "frozen_text",
        "status",
        "reason",
        "confidence",
        "keep_until_resolved",
        "dedupe_key",
        "created_at",
        "updated_at",
        "confirmed_at",
        "expires_at",
        "surfaced_at",
        "resolved_at",
    },
    "companion_cue_sources": {
        "id",
        "cue_id",
        "source_kind",
        "source_message_id",
        "fact_version_id",
        "reflection_version_id",
        "persona_version_id",
        "created_at",
    },
    "companion_cue_audit_events": {
        "id",
        "profile_id",
        "cue_id",
        "cue_kind",
        "event_type",
        "reason_code",
        "previous_status",
        "resulting_status",
        "occurred_at",
    },
    "temporal_commitments": {
        "id",
        "profile_id",
        "source_kind",
        "source_message_id",
        "source_conversation_id",
        "live_source_message_id",
        "live_source_conversation_id",
        "status",
        "current_version_id",
        "created_at",
        "updated_at",
        "confirmed_at",
        "due_detected_at",
        "surfaced_at",
        "completed_at",
        "cancelled_at",
    },
    "temporal_commitment_versions": {
        "id",
        "commitment_id",
        "version_number",
        "kind",
        "content",
        "due_at_utc",
        "original_local_time",
        "timezone_name",
        "utc_offset_minutes",
        "show_content",
        "origin",
        "supersedes_version_id",
        "created_at",
    },
    "temporal_commitment_audit_events": {
        "id",
        "profile_id",
        "commitment_id",
        "event_type",
        "reason_code",
        "previous_status",
        "resulting_status",
        "version_id",
        "occurred_at",
    },
}
_REQUIRED_FTS_COLUMNS = {
    "memory_fts": {"version_id", "memory_id", "profile_id", "search_text"},
    "persona_fts": {"knowledge_id", "persona_id", "search_text"},
    "memory_reflection_fts": {
        "version_id",
        "reflection_id",
        "profile_id",
        "search_text",
    },
    "memory_persona_impression_fts": {
        "version_id",
        "impression_id",
        "profile_id",
        "search_text",
    },
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
