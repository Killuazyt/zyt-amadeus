from __future__ import annotations

import sqlite3

import pytest

from amadeus_desktop.database import (
    SCHEMA_VERSION,
    DatabaseReadOnlyError,
    SQLiteDatabase,
    table_names,
)


def test_schema_v1_enables_required_pragmas_and_entities(tmp_path) -> None:
    path = tmp_path / "data" / "amadeus.sqlite3"
    with SQLiteDatabase(path, busy_timeout_ms=3_210) as database:
        assert database.schema_version == SCHEMA_VERSION == 1
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
        }.issubset(table_names(database.connection))

    assert path.exists()
    assert path.parent.name == "data"


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
