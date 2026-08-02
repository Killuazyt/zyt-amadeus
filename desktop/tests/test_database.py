from __future__ import annotations

import sqlite3

import pytest

from amadeus_desktop import database as database_module
from amadeus_desktop.database import (
    SCHEMA_VERSION,
    DatabaseReadOnlyError,
    SQLiteDatabase,
    table_names,
)


def test_schema_v2_enables_required_pragmas_and_entities(tmp_path) -> None:
    path = tmp_path / "data" / "amadeus.sqlite3"
    with SQLiteDatabase(path, busy_timeout_ms=3_210) as database:
        assert database.schema_version == SCHEMA_VERSION == 2
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
        }.issubset(table_names(database.connection))

    assert path.exists()
    assert path.parent.name == "data"


def test_schema_v1_is_backed_up_and_migrated_to_v2_without_losing_data(tmp_path) -> None:
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
        assert database.schema_version == 2
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
    ),
    ids=("trigger-definition", "partial-unique-index", "fts-table", "v2-columns"),
)
def test_current_schema_with_corrupted_v2_invariant_fails_closed(tmp_path, corruption: str) -> None:
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
