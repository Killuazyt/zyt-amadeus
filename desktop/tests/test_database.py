from __future__ import annotations

import sqlite3

import pytest

from amadeus_desktop import database as database_module
from amadeus_desktop.database import (
    AMADEUS_APPLICATION_ID,
    SCHEMA_VERSION,
    DatabaseReadOnlyError,
    SQLiteDatabase,
    table_names,
)
from amadeus_desktop.memory_store import MemoryStore


def test_schema_v6_enables_required_pragmas_and_entities(tmp_path) -> None:
    path = tmp_path / "data" / "amadeus.sqlite3"
    with SQLiteDatabase(path, busy_timeout_ms=3_210) as database:
        assert database.schema_version == SCHEMA_VERSION == 6
        assert database.pragma_value("application_id") == AMADEUS_APPLICATION_ID
        assert str(database.pragma_value("journal_mode")).lower() == "wal"
        assert database.pragma_value("foreign_keys") == 1
        assert database.pragma_value("busy_timeout") == 3_210
        assert database.pragma_value("secure_delete") == 1
        assert not database.read_only
        assert {
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
            "memory_persona_impressions",
            "memory_persona_impression_versions",
            "memory_evidence_signals",
            "memory_conflicts",
            "memory_audit_events",
            "memory_pipeline_state",
        }.issubset(table_names(database.connection))
        message_columns = {
            row[1] for row in database.connection.execute("PRAGMA table_info(messages)")
        }
        assert "input_modality" in message_columns

    assert path.exists()
    assert path.parent.name == "data"


def test_schema_v1_is_backed_up_and_migrated_to_v6_without_losing_data(tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute("PRAGMA foreign_keys = ON")
    database_module._migrate_to_v1(legacy)
    legacy.execute("PRAGMA user_version = 1")
    legacy.execute(
        "INSERT INTO profiles VALUES ('p', 'name', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    legacy.commit()
    legacy.close()

    database = SQLiteDatabase(path, backup_dir=tmp_path / "backups").open()
    try:
        assert not database.read_only
        assert database.schema_version == 6
        assert database.pragma_value("application_id") == AMADEUS_APPLICATION_ID
        assert database.last_backup_path is not None
        assert (
            database.connection.execute(
                "SELECT display_name FROM profiles WHERE id = 'p'"
            ).fetchone()[0]
            == "name"
        )
        assert {
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
            "memory_persona_impressions",
            "memory_evidence_signals",
            "memory_conflicts",
            "memory_audit_events",
        }.issubset(table_names(database.connection))
    finally:
        database.close()

    backup = sqlite3.connect(database.last_backup_path)
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("SELECT display_name FROM profiles WHERE id = 'p'").fetchone() == (
            "name",
        )
        assert (
            backup.execute("SELECT 1 FROM sqlite_master WHERE name = 'memory_vectors'").fetchone()
            is None
        )
    finally:
        backup.close()


def test_schema_v2_migrates_messages_and_application_identity_without_data_loss(
    tmp_path,
) -> None:
    path = tmp_path / "amadeus.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute("PRAGMA foreign_keys = ON")
    database_module._migrate_to_v1(legacy)
    database_module._migrate_to_v2(legacy)
    legacy.execute("PRAGMA user_version = 2")
    timestamp = "2026-08-03T00:00:00.000000Z"
    legacy.execute("INSERT INTO profiles VALUES ('p', 'name', ?, ?)", (timestamp, timestamp))
    legacy.execute(
        """
        INSERT INTO conversations(
            id, profile_id, title, status, created_at, updated_at, last_activity_at
        ) VALUES ('c', 'p', 'legacy', 'normal', ?, ?, ?)
        """,
        (timestamp, timestamp, timestamp),
    )
    legacy.execute(
        """
        INSERT INTO messages(
            id, conversation_id, turn_id, role, content, status, attempt,
            participates_in_memory, created_at, updated_at, completed_at
        ) VALUES ('m', 'c', 't', 'user', 'preserved', 'completed', 1, 1, ?, ?, ?)
        """,
        (timestamp, timestamp, timestamp),
    )
    legacy.commit()
    legacy.close()

    database = SQLiteDatabase(path, backup_dir=tmp_path / "backups").open()
    backup_path = database.last_backup_path
    try:
        assert not database.read_only
        assert database.schema_version == 6
        assert database.pragma_value("application_id") == AMADEUS_APPLICATION_ID
        row = database.connection.execute(
            "SELECT content, origin, input_modality FROM messages WHERE id = 'm'"
        ).fetchone()
        assert tuple(row) == ("preserved", "conversation", "text")
        assert "proactive_events" in table_names(database.connection)
        assert "attachments" in table_names(database.connection)
    finally:
        database.close()

    assert backup_path is not None
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
        assert backup.execute("PRAGMA application_id").fetchone()[0] == 0
        assert backup.execute("SELECT content FROM messages WHERE id = 'm'").fetchone() == (
            "preserved",
        )
        assert {row[1] for row in backup.execute("PRAGMA table_info(messages)")} == {
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
        }
    finally:
        backup.close()


def test_schema_v5_to_v6_keeps_history_out_of_deep_backfill_until_user_edit(tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute("PRAGMA foreign_keys = ON")
    for migrate in (
        database_module._migrate_to_v1,
        database_module._migrate_to_v2,
        database_module._migrate_to_v3,
        database_module._migrate_to_v4,
        database_module._migrate_to_v5,
    ):
        migrate(legacy)
    legacy.execute("PRAGMA user_version = 5")
    timestamp = "2026-08-01T00:00:00.000000Z"
    legacy.execute("INSERT INTO profiles VALUES ('p', 'name', ?, ?)", (timestamp, timestamp))
    legacy.execute(
        """
        INSERT INTO conversations(
            id, profile_id, title, status, created_at, updated_at, last_activity_at
        ) VALUES ('c', 'p', 'legacy', 'normal', ?, ?, ?)
        """,
        (timestamp, timestamp, timestamp),
    )
    legacy.execute(
        """
        INSERT INTO messages(
            id, conversation_id, turn_id, role, content, status, attempt,
            participates_in_memory, created_at, updated_at, completed_at, origin,
            input_modality
        ) VALUES ('m', 'c', 't', 'user', '我偏好红茶', 'completed', 1, 1,
                  ?, ?, ?, 'conversation', 'text')
        """,
        (timestamp, timestamp, timestamp),
    )
    legacy.execute(
        """
        INSERT INTO memory_groups(
            id, profile_id, kind, topic_key, status, pinned, current_version_id,
            created_at, updated_at
        ) VALUES ('fact', 'p', 'preference', '饮料', 'active', 0, NULL, ?, ?)
        """,
        (timestamp, timestamp),
    )
    legacy.execute(
        """
        INSERT INTO memory_versions(
            id, memory_id, version_number, content, normalized_content, content_hash,
            search_text, importance, confidence, origin, operation,
            supersedes_version_id, created_at
        ) VALUES ('fact-v1', 'fact', 1, '用户偏好红茶', '用户偏好红茶', ?,
                  '饮料 用户偏好红茶', 0.7, 0.9, 'automatic', 'add', NULL, ?)
        """,
        ("a" * 64, timestamp),
    )
    legacy.execute("UPDATE memory_groups SET current_version_id = 'fact-v1' WHERE id = 'fact'")
    legacy.execute(
        """
        INSERT INTO memory_sources(
            id, version_id, source_message_id, source_conversation_id,
            live_message_id, live_conversation_id, extraction_method, created_at
        ) VALUES ('source', 'fact-v1', 'm', 'c', 'm', 'c', 'automatic', ?)
        """,
        (timestamp,),
    )
    legacy.commit()
    legacy.close()

    database = SQLiteDatabase(path, backup_dir=tmp_path / "backups").open()
    try:
        assert database.schema_version == 6
        assert (
            database.connection.execute(
                "SELECT deep_memory_eligible FROM memory_versions WHERE id = 'fact-v1'"
            ).fetchone()[0]
            == 0
        )
        edited = MemoryStore(database).edit_memory("fact", "用户确认仍偏好红茶")
        assert edited.current_version.deep_memory_eligible
        source_ids = {
            row[0]
            for row in database.connection.execute(
                "SELECT source_message_id FROM memory_sources WHERE version_id = ?",
                (edited.current_version.version_id,),
            )
        }
        assert "m" in source_ids
        assert any(value.startswith("manual:") for value in source_ids)
    finally:
        backup_path = database.last_backup_path
        database.close()

    assert backup_path is not None
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "deep_memory_eligible" not in {
            row[1] for row in backup.execute("PRAGMA table_info(memory_versions)")
        }


def test_migration_failure_rolls_back_and_reopens_read_only_with_consistent_backup(
    tmp_path,
) -> None:
    path = tmp_path / "amadeus.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE legacy_value(value TEXT NOT NULL)")
    legacy.execute("INSERT INTO legacy_value VALUES ('preserved')")
    legacy.commit()
    legacy.close()

    def fail_after_write(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE must_rollback(value TEXT)")
        connection.execute("INSERT INTO legacy_value VALUES ('must-rollback')")
        raise RuntimeError("injected migration failure")

    database = SQLiteDatabase(
        path,
        backup_dir=tmp_path / "backups",
        migrations={1: fail_after_write},
    ).open()
    try:
        assert database.read_only
        assert database.pragma_value("query_only") == 1
        assert database.migration_error is not None
        assert database.last_backup_path is not None
        assert database.last_backup_path.exists()
        assert [
            row[0]
            for row in database.connection.execute("SELECT value FROM legacy_value").fetchall()
        ] == ["preserved"]
        assert (
            database.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'must_rollback'"
            ).fetchone()
            is None
        )
        with pytest.raises(DatabaseReadOnlyError), database.transaction():
            pass

        backup = sqlite3.connect(database.last_backup_path)
        try:
            assert backup.execute("SELECT value FROM legacy_value").fetchall() == [("preserved",)]
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
        finally:
            backup.close()
    finally:
        database.close()

    original = sqlite3.connect(path)
    try:
        assert original.execute("SELECT value FROM legacy_value").fetchall() == [("preserved",)]
        assert (
            original.execute("SELECT 1 FROM sqlite_master WHERE name = 'must_rollback'").fetchone()
            is None
        )
    finally:
        original.close()


def test_explicit_backup_refuses_overwrite_and_contains_committed_state(tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    target = tmp_path / "snapshot.sqlite3"
    with SQLiteDatabase(path) as database:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO profiles VALUES ('p', 'name', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')"
            )
        assert database.create_backup(target) == target
        with pytest.raises(FileExistsError):
            database.create_backup(target)

    snapshot = sqlite3.connect(target)
    try:
        assert snapshot.execute("SELECT display_name FROM profiles WHERE id='p'").fetchone() == (
            "name",
        )
        assert snapshot.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        snapshot.close()


def test_claimed_current_but_incomplete_schema_fails_closed(tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.close()

    database = SQLiteDatabase(path).open()
    try:
        assert database.read_only
        assert database.migration_error is not None
        assert "validation" in str(database.migration_error)
        with pytest.raises(sqlite3.OperationalError):
            database.connection.execute("INSERT INTO unrelated VALUES ('blocked')")
    finally:
        database.close()


@pytest.mark.parametrize(
    "corruption",
    (
        """
        DROP TRIGGER memory_versions_are_immutable;
        CREATE TRIGGER memory_versions_are_immutable
        AFTER INSERT ON profiles BEGIN SELECT 1; END;
        """,
        """
        DROP INDEX memory_embedding_one_active_idx;
        CREATE INDEX memory_embedding_one_active_idx
        ON memory_embedding_generations(profile_id);
        """,
        """
        DROP TABLE persona_fts;
        CREATE TABLE persona_fts(
            knowledge_id TEXT, persona_id TEXT, search_text TEXT
        );
        """,
        """
        ALTER TABLE memory_recall_events RENAME TO memory_recall_events_full;
        CREATE TABLE memory_recall_events(id TEXT PRIMARY KEY);
        """,
        """
        DROP TABLE proactive_events;
        CREATE TABLE proactive_events(id TEXT PRIMARY KEY);
        """,
        """
        DROP TABLE message_attachments;
        CREATE TABLE message_attachments(message_id TEXT PRIMARY KEY);
        """,
        """
        ALTER TABLE messages DROP COLUMN input_modality;
        """,
        """
        PRAGMA application_id = 0;
        """,
    ),
    ids=(
        "trigger-definition",
        "partial-unique-index",
        "fts-table",
        "v2-columns",
        "v3-columns",
        "v4-columns",
        "v5-columns",
        "application-id",
    ),
)
def test_current_schema_with_corrupted_invariant_fails_closed(tmp_path, corruption: str) -> None:
    path = tmp_path / "amadeus.sqlite3"
    SQLiteDatabase(path).open().close()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(corruption)
        connection.commit()
    finally:
        connection.close()

    database = SQLiteDatabase(path).open()
    try:
        assert database.read_only
        assert database.migration_error is not None
        assert "validation" in str(database.migration_error)
        assert database.pragma_value("query_only") == 1
    finally:
        database.close()
