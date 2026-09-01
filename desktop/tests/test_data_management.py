from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

import amadeus_desktop.data_management as data_management
import amadeus_desktop.database as database_module
from amadeus_desktop import __version__
from amadeus_desktop.attachments import AttachmentStore
from amadeus_desktop.conversation_store import ConversationStore
from amadeus_desktop.data_management import (
    BACKUP_DATABASE_MEMBER,
    BACKUP_FORMAT,
    BACKUP_MANIFEST_MEMBER,
    BACKUP_MEMBERS,
    BACKUP_SETTINGS_MEMBER,
    CHAT_EXPORT_FORMAT,
    MEMORY_EXPORT_FORMAT,
    BackupError,
    BackupLimits,
    BackupValidationError,
    ExportError,
    RestoreError,
    SQLiteExportRepository,
    UnsafeResetPlanError,
    apply_validated_restore,
    create_backup_archive,
    disable_provider_credential_reuse_for_restore,
    discard_staged_restore,
    export_chat_json,
    export_memory_json,
    plan_factory_reset,
    stage_backup_for_restore,
    validate_backup_archive,
)
from amadeus_desktop.database import AMADEUS_APPLICATION_ID, SCHEMA_VERSION, SQLiteDatabase
from amadeus_desktop.deep_memory_store import DeepMemoryStore
from amadeus_desktop.memory_models import MemoryLayer
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.paths import AppDirectory, AppPaths
from amadeus_desktop.persona_repository import PersonaRepository
from amadeus_desktop.settings import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_SETTINGS,
    SettingsRepository,
)
from amadeus_desktop.storage_models import (
    StoredAttachment,
    StoredAttachmentKind,
    StoredAttachmentSource,
)
from amadeus_desktop.vector_store import VectorStore

FIXED_TIME = datetime(2026, 8, 3, 4, 5, 6, tzinfo=UTC)
MODEL = {
    "model_name": "BAAI/bge-small-zh-v1.5",
    "model_commit": "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59",
    "model_sha256": "a" * 64,
    "calibration_threshold": 0.6,
}


def _vector(seed: float) -> tuple[float, ...]:
    return tuple(seed + index / 1_000 for index in range(512))


def _seed_database(tmp_path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(
        tmp_path / "source.sqlite3",
        backup_dir=tmp_path / "migration-backups",
    ).open()
    conversations = ConversationStore(database, clock=lambda: FIXED_TIME)
    conversation = conversations.create_conversation("测试会话", conversation_id="conv-1")
    user, assistant = conversations.save_turn(
        conversation.conversation_id,
        "turn-1",
        "user-1",
        "我喜欢无糖咖啡",
        "assistant-1",
    )
    conversations.finalize_assistant(
        assistant.message_id,
        "记住了。",
        status="completed",
        terminal_reason="completed",
        attempt=1,
        provider_name="test-provider",
        model_name="test-model",
    )
    conversations.save_summary(
        conversation.conversation_id,
        "用户谈到饮品偏好。",
        user.sequence,
        message_count=1,
        character_count=8,
    )
    memories = MemoryStore(database, clock=lambda: FIXED_TIME)
    memory = memories.create_memory(
        "preference",
        "drink:coffee",
        "用户喜欢无糖咖啡",
        source_message_ids=(user.message_id,),
        memory_id="memory-1",
    )
    memory = memories.edit_memory(memory.memory_id, "用户偏好无糖咖啡")
    persona = PersonaRepository(database)
    knowledge = persona.upsert_knowledge(
        "kurisu",
        "角色在公开研究机构工作。",
        tags=("背景",),
        source_ref="synthetic:p6-backup",
        source_hash="b" * 64,
        knowledge_id="persona-1",
    )
    vectors = VectorStore(database)
    user_generation = vectors.begin_memory_generation(generation_id="user-p6", **MODEL)
    persona_generation = vectors.begin_persona_generation(
        persona_id="kurisu",
        generation_id="persona-p6",
        **MODEL,
    )
    vectors.activate_memory_generation(
        user_generation.generation_id,
        {memory.current_version.version_id: _vector(1.0)},
    )
    vectors.activate_persona_generation(
        persona_generation.generation_id,
        {knowledge.knowledge_id: _vector(2.0)},
    )
    return database


def _settings() -> dict[str, object]:
    return deepcopy(DEFAULT_SETTINGS)


def _make_backup(tmp_path: Path, database: SQLiteDatabase) -> Path:
    return create_backup_archive(
        tmp_path / "backup.amadeus-backup",
        database_backup=lambda target: database.create_backup(target),
        settings_snapshot=_settings(),
        app_version=__version__,
        created_at=FIXED_TIME,
    )


def _seed_image_attachment(
    database: SQLiteDatabase,
    attachment_root: Path,
) -> tuple[StoredAttachment, bytes]:
    output = io.BytesIO()
    Image.new("RGB", (19, 13), (20, 90, 130)).save(output, format="PNG")
    payload = output.getvalue()
    imported = AttachmentStore(attachment_root).import_bytes(
        payload,
        display_name="private-view.png",
    )
    attachment = StoredAttachment(
        attachment_id=imported.attachment_id,
        kind=StoredAttachmentKind.IMAGE,
        source=StoredAttachmentSource.FILE_PICKER,
        display_name=imported.display_name,
        mime_type=imported.mime_type,
        size_bytes=imported.size_bytes,
        sha256=imported.sha256,
        relative_path=imported.relative_path,
        status="ready",
        extracted_text="",
        text_truncated=False,
        created_at=FIXED_TIME,
    )
    conversations = ConversationStore(database, clock=lambda: FIXED_TIME)
    conversations.save_user_message(
        "conv-1",
        "turn-image",
        "user-image",
        "请看图片",
        attachments=(attachment,),
    )
    return attachment, payload


def _rewrite_archive(
    source: Path,
    destination: Path,
    replacements: dict[str, bytes],
    *,
    extra: tuple[str, bytes] | None = None,
) -> Path:
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members.update(replacements)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        if extra is not None:
            archive.writestr(*extra)
    return destination


def _manual_archive(
    destination: Path,
    database_path: Path,
    settings: dict[str, object],
) -> Path:
    database_bytes = database_path.read_bytes()
    settings_bytes = (json.dumps(settings, ensure_ascii=False, sort_keys=True) + "\n").encode()
    with sqlite3.connect(database_path) as connection:
        database_schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
    manifest = {
        "format": BACKUP_FORMAT,
        "created_at": "2026-08-03T04:05:06Z",
        "app_version": __version__,
        "schemas": {
            "database": database_schema,
            "settings": settings["schema_version"],
        },
        "files": {
            BACKUP_DATABASE_MEMBER: {
                "size": len(database_bytes),
                "sha256": hashlib.sha256(database_bytes).hexdigest(),
            },
            BACKUP_SETTINGS_MEMBER: {
                "size": len(settings_bytes),
                "sha256": hashlib.sha256(settings_bytes).hexdigest(),
            },
        },
    }
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(BACKUP_MANIFEST_MEMBER, json.dumps(manifest).encode())
        archive.writestr(BACKUP_DATABASE_MEMBER, database_bytes)
        archive.writestr(BACKUP_SETTINGS_MEMBER, settings_bytes)
    return destination


def test_sqlite_export_repository_reads_chat_and_auditable_memory(tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    try:
        repository = SQLiteExportRepository(database.connection)
        chat = repository.load_chat_bundle()
        memory = repository.load_memory_bundle()
    finally:
        database.close()

    assert chat.database_schema == SCHEMA_VERSION
    assert [item["id"] for item in chat.conversations] == ["conv-1"]
    assert [item["origin"] for item in chat.messages] == ["conversation", "conversation"]
    assert chat.messages[1]["terminal_reason"] == "completed"
    assert chat.messages[1]["provider_name"] == "test-provider"
    assert chat.summaries[0]["covers_through_sequence"] == 1

    assert memory.database_schema == SCHEMA_VERSION
    assert memory.groups[0]["current_version_id"] == memory.versions[-1]["id"]
    assert [item["version_number"] for item in memory.versions] == [1, 2]
    assert "user-1" in {item["source_message_id"] for item in memory.sources}
    assert "vector_blob" not in memory.versions[0]


def test_versioned_exports_are_atomic_utf8_and_do_not_read_settings_or_credentials(
    tmp_path: Path,
) -> None:
    database = _seed_database(tmp_path)
    repository = SQLiteExportRepository(database.connection)
    chat_calls = 0
    memory_calls = 0

    def load_chat():
        nonlocal chat_calls
        chat_calls += 1
        return repository.load_chat_bundle()

    def load_memory():
        nonlocal memory_calls
        memory_calls += 1
        return repository.load_memory_bundle()

    try:
        chat_path = export_chat_json(tmp_path / "聊天.json", load_chat, exported_at=FIXED_TIME)
        memory_path = export_memory_json(
            tmp_path / "记忆.json", load_memory, exported_at=FIXED_TIME
        )
    finally:
        database.close()

    assert chat_calls == memory_calls == 1
    chat = json.loads(chat_path.read_text(encoding="utf-8"))
    memory = json.loads(memory_path.read_text(encoding="utf-8"))
    assert chat["format"] == CHAT_EXPORT_FORMAT
    assert memory["format"] == MEMORY_EXPORT_FORMAT
    assert chat["exported_at"] == memory["exported_at"] == "2026-08-03T04:05:06Z"
    assert set(chat) == {
        "format",
        "exported_at",
        "database_schema",
        "conversations",
        "messages",
        "summaries",
        "attachments",
        "message_attachments",
        "companion_cues",
        "companion_cue_sources",
        "proactive_events",
        "companion_cue_audit_events",
        "temporal_commitments",
        "temporal_versions",
        "temporal_audit_events",
    }
    assert set(memory) == {
        "format",
        "exported_at",
        "database_schema",
        "layers",
        "evidence_signals",
        "conflicts",
        "audit_events",
        "companion_followups",
    }
    assert set(memory["layers"]) == {
        "recent",
        "facts",
        "reflections",
        "persona_impressions",
    }
    assert "content" not in memory["layers"]["recent"]["message_refs"][0]
    assert "vector_blob" not in json.dumps(memory, ensure_ascii=False)
    combined = chat_path.read_text(encoding="utf-8") + memory_path.read_text(encoding="utf-8")
    assert "provider_enabled" not in combined
    assert "credential" not in combined.casefold()
    assert not list(tmp_path.glob(".*.tmp"))


def test_memory_export_v3_contains_lineage_evidence_conflicts_and_bodyless_audit(
    tmp_path: Path,
) -> None:
    database = _seed_database(tmp_path)
    conversations = ConversationStore(database, clock=lambda: FIXED_TIME)
    memories = MemoryStore(database, clock=lambda: FIXED_TIME)
    deep = DeepMemoryStore(database, clock=lambda: FIXED_TIME)
    try:
        conversation = conversations.list_conversations()[0]
        fact_ids = [memories.get("memory-1").current_version.version_id]
        for index in range(4):
            message = conversations.save_user_message(
                conversation.conversation_id,
                f"deep-export-turn-{index}",
                f"deep-export-user-{index}",
                f"我第 {index + 1} 次表示重视稳定互动",
            )
            fact = memories.create_memory(
                "relationship",
                f"稳定互动:{index}",
                f"用户第 {index + 1} 次表示重视稳定互动",
                source_message_ids=(message.message_id,),
            )
            fact_ids.append(fact.current_version.version_id)
        reflection = deep.create_reflection(
            "用户通过稳定互动建立信任",
            "稳定互动 信任",
            fact_version_ids=fact_ids,
            importance=1.0,
        )
        deep.confirm(MemoryLayer.REFLECTION, reflection.group_id)
        deep.confirm(MemoryLayer.REFLECTION, reflection.group_id)
        impression = deep.promote_reflection(reflection.group_id)
        challenger = conversations.save_user_message(
            conversation.conversation_id,
            "deep-export-conflict-turn",
            "deep-export-conflict-user",
            "我对稳定互动的看法似乎有变化",
        )
        deep.open_fact_conflict(
            memories.list_memories(kind="relationship")[0].memory_id,
            "用户可能不再重视稳定互动",
            source_message_id=challenger.message_id,
            importance=0.7,
            confidence=0.6,
        )
        destination = export_memory_json(
            tmp_path / "deep-memory.json",
            lambda: SQLiteExportRepository(database.connection).load_memory_bundle(),
            exported_at=FIXED_TIME,
        )
    finally:
        database.close()

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["format"] == "amadeus-memory-export/v3"
    reflection_layer = payload["layers"]["reflections"]
    persona_layer = payload["layers"]["persona_impressions"]
    assert reflection_layer["groups"][0]["id"] == reflection.group_id
    assert reflection_layer["sources"][0]["fact_version_id"] in fact_ids
    assert persona_layer["groups"][0]["id"] == impression.group_id
    assert persona_layer["sources"][0]["reflection_version_id"] == (
        reflection.current_version.version_id
    )
    assert payload["evidence_signals"]
    assert payload["conflicts"][0]["status"] == "open"
    assert payload["audit_events"]
    serialized_audit = json.dumps(payload["audit_events"], ensure_ascii=False)
    assert "用户通过稳定互动建立信任" not in serialized_audit
    assert all(
        "content" not in reference for reference in payload["layers"]["recent"]["message_refs"]
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "vector_blob" not in serialized
    assert "角色在公开研究机构工作" not in serialized


def test_export_failure_preserves_existing_destination(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "chat.json"
    destination.write_bytes(b"old-export")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("injected")

    monkeypatch.setattr(data_management.os, "replace", fail_replace)
    bundle = data_management.ChatExportBundle(3, (), (), ())
    with pytest.raises(ExportError, match="atomically"):
        export_chat_json(destination, lambda: bundle, exported_at=FIXED_TIME)

    assert destination.read_bytes() == b"old-export"
    assert not list(tmp_path.glob(".chat.json.*.tmp"))


def test_export_wraps_unavailable_destination_directory(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    bundle = data_management.ChatExportBundle(3, (), (), ())

    with pytest.raises(ExportError, match="atomically"):
        export_chat_json(blocked_parent / "chat.json", lambda: bundle, exported_at=FIXED_TIME)


def test_backup_has_fixed_members_manifest_hashes_and_valid_database(tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()

    with zipfile.ZipFile(backup) as archive:
        assert set(archive.namelist()) == BACKUP_MEMBERS
        manifest = json.loads(archive.read(BACKUP_MANIFEST_MEMBER))
        settings = json.loads(archive.read(BACKUP_SETTINGS_MEMBER))
    metadata = validate_backup_archive(backup)

    assert manifest["format"] == BACKUP_FORMAT
    assert manifest["schemas"] == {
        "database": SCHEMA_VERSION,
        "settings": CURRENT_SCHEMA_VERSION,
    }
    assert metadata.database_schema == SCHEMA_VERSION
    assert metadata.settings_schema == CURRENT_SCHEMA_VERSION
    assert settings == _settings()
    assert "invalid-fake-amadeus-secret" not in json.dumps(settings).casefold()


def test_backup_v2_round_trips_exact_attachment_bytes_and_chat_export_omits_binary(
    tmp_path: Path,
) -> None:
    database = _seed_database(tmp_path)
    attachment_root = tmp_path / "attachments"
    attachment, original = _seed_image_attachment(database, attachment_root)
    export_path = tmp_path / "chat.json"
    try:
        export_chat_json(
            export_path,
            lambda: SQLiteExportRepository(database.connection).load_chat_bundle(),
            exported_at=FIXED_TIME,
        )
        backup = create_backup_archive(
            tmp_path / "with-attachment.amadeus-backup",
            database_backup=lambda target: database.create_backup(target),
            settings_snapshot=_settings(),
            app_version=__version__,
            attachment_root=attachment_root,
            created_at=FIXED_TIME,
        )
    finally:
        database.close()

    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["format"] == CHAT_EXPORT_FORMAT
    assert len(exported["attachments"]) == 1
    exported_attachment = exported["attachments"][0]
    assert exported_attachment["id"] == attachment.attachment_id
    assert exported_attachment["display_name"] == "private-view.png"
    assert exported_attachment["sha256"] == attachment.sha256
    assert exported_attachment["relative_path"] == attachment.relative_path
    assert exported_attachment["size_bytes"] == len(original)
    assert exported_attachment["extracted_text"] == ""
    assert exported["message_attachments"] == [
        {
            "message_id": "user-image",
            "attachment_id": attachment.attachment_id,
            "ordinal": 0,
        }
    ]
    assert "data:image" not in export_path.read_text(encoding="utf-8")

    staged = stage_backup_for_restore(backup, tmp_path / "restore-stage")
    try:
        restored = staged.attachments_path.joinpath(*attachment.relative_path.split("/"))
        assert restored.read_bytes() == original
        assert staged.metadata.format == BACKUP_FORMAT
        assert tuple(item["relative_path"] for item in staged.metadata.attachments) == (
            attachment.relative_path,
        )
    finally:
        discard_staged_restore(staged)


def test_backup_callback_must_produce_current_consistent_snapshot(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="was not created"):
        create_backup_archive(
            tmp_path / "missing.amadeus-backup",
            database_backup=lambda _target: None,
            settings_snapshot=_settings(),
            app_version=__version__,
        )

    assert not (tmp_path / "missing.amadeus-backup").exists()


def test_backup_wraps_unavailable_destination_directory(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(BackupError, match="safely"):
        create_backup_archive(
            blocked_parent / "backup.amadeus-backup",
            database_backup=lambda _target: pytest.fail("backup callback must not run"),
            settings_snapshot=_settings(),
            app_version=__version__,
        )


@pytest.mark.parametrize(
    "sensitive_settings",
    [
        {**_settings(), "api_key": "invalid-test-secret"},
        {**_settings(), "note": "Bearer abcdefghijklmnopqrstuvwxyz"},
    ],
)
def test_backup_rejects_sensitive_settings(
    sensitive_settings: dict[str, object], tmp_path: Path
) -> None:
    database = _seed_database(tmp_path)
    try:
        with pytest.raises(BackupError, match="sensitive"):
            create_backup_archive(
                tmp_path / "secret.amadeus-backup",
                database_backup=lambda target: database.create_backup(target),
                settings_snapshot=sensitive_settings,
                app_version=__version__,
            )
    finally:
        database.close()


def test_validation_rejects_extra_and_path_traversal_members(tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()

    extra = _rewrite_archive(
        backup,
        tmp_path / "extra.amadeus-backup",
        {},
        extra=("../outside.txt", b"no"),
    )
    with pytest.raises(BackupValidationError, match="member"):
        stage_backup_for_restore(extra, tmp_path / "staging")
    assert not (tmp_path / "outside.txt").exists()


def test_validation_rejects_checksum_tampering_and_cleans_stage(tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()

    tampered = _rewrite_archive(
        backup,
        tmp_path / "tampered.amadeus-backup",
        {BACKUP_SETTINGS_MEMBER: b'{"schema_version":5}\n'},
    )
    staging_parent = tmp_path / "staging"
    with pytest.raises(BackupValidationError, match="checksum"):
        stage_backup_for_restore(tampered, staging_parent)
    assert not list(staging_parent.glob(".amadeus-restore-*"))


def test_validation_wraps_unavailable_staging_directory(tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()
    blocked_parent = tmp_path / "blocked-staging"
    blocked_parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(BackupValidationError, match="invalid"):
        stage_backup_for_restore(backup, blocked_parent)


@pytest.mark.parametrize("schema_name", ["database", "settings"])
def test_validation_rejects_future_schemas(schema_name: str, tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()
    with zipfile.ZipFile(backup) as archive:
        manifest = json.loads(archive.read(BACKUP_MANIFEST_MEMBER))
    manifest["schemas"][schema_name] = 999
    future = _rewrite_archive(
        backup,
        tmp_path / f"future-{schema_name}.amadeus-backup",
        {BACKUP_MANIFEST_MEMBER: json.dumps(manifest).encode()},
    )

    with pytest.raises(BackupValidationError, match="newer"):
        validate_backup_archive(future)


def test_validation_rejects_wrong_application_id(tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    snapshot = database.create_backup(tmp_path / "wrong-app.sqlite3")
    database.close()
    with sqlite3.connect(snapshot) as connection:
        connection.execute("PRAGMA application_id = 0")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    archive = _manual_archive(tmp_path / "wrong-app.amadeus-backup", snapshot, _settings())

    with pytest.raises(BackupValidationError, match="application ID"):
        validate_backup_archive(archive)


@pytest.mark.parametrize(
    "schema_damage",
    (
        "DROP TRIGGER memory_versions_are_immutable",
        "DROP INDEX memory_embedding_one_active_idx",
        "ALTER TABLE messages DROP COLUMN origin",
    ),
    ids=("immutable-trigger", "active-generation-index", "required-column"),
)
def test_validation_rejects_current_schema_missing_runtime_invariant(
    schema_damage: str,
    tmp_path: Path,
) -> None:
    database = _seed_database(tmp_path / "source")
    database_path = database.path
    try:
        database.connection.execute(schema_damage)
        assert database.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        database.close()

    backup = _manual_archive(
        tmp_path / "schema-damaged.amadeus-backup",
        database_path,
        _settings(),
    )
    staging_parent = tmp_path / "schema-damaged-staging"

    with pytest.raises(BackupValidationError, match="schema"):
        stage_backup_for_restore(backup, staging_parent)

    assert not staging_parent.exists() or not tuple(staging_parent.iterdir())


def test_validation_rejects_foreign_key_damage(tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    snapshot = database.create_backup(tmp_path / "bad-fk.sqlite3")
    database.close()
    with sqlite3.connect(snapshot) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO messages(
                id, conversation_id, turn_id, role, origin, content, status, attempt,
                participates_in_memory, created_at, updated_at, completed_at
            ) VALUES (
                'orphan', 'missing', 'turn-orphan', 'user', 'conversation', 'x',
                'completed', 1, 1, '2026-08-03T00:00:00Z',
                '2026-08-03T00:00:00Z', '2026-08-03T00:00:00Z'
            )
            """
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    archive = _manual_archive(tmp_path / "bad-fk.amadeus-backup", snapshot, _settings())

    with pytest.raises(BackupValidationError, match="foreign-key"):
        validate_backup_archive(archive)


def test_validation_rejects_corrupt_database_image(tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()
    with zipfile.ZipFile(backup) as archive:
        manifest = json.loads(archive.read(BACKUP_MANIFEST_MEMBER))
        database_bytes = archive.read(BACKUP_DATABASE_MEMBER)
    corrupted_buffer = bytearray(database_bytes)
    corrupted_buffer[4096:8192] = b"\0" * 4096
    corrupted = bytes(corrupted_buffer)
    manifest["files"][BACKUP_DATABASE_MEMBER] = {
        "size": len(corrupted),
        "sha256": hashlib.sha256(corrupted).hexdigest(),
    }
    archive = _rewrite_archive(
        backup,
        tmp_path / "corrupt.amadeus-backup",
        {
            BACKUP_MANIFEST_MEMBER: json.dumps(manifest).encode(),
            BACKUP_DATABASE_MEMBER: corrupted,
        },
    )

    with pytest.raises(BackupValidationError, match="database"):
        validate_backup_archive(archive)


def test_validation_enforces_member_size_limits(tmp_path: Path) -> None:
    database = _seed_database(tmp_path)
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()

    limits = BackupLimits(
        archive_bytes=backup.stat().st_size + 1,
        database_bytes=32,
        settings_bytes=4 * 1024**2,
        manifest_bytes=128 * 1024,
    )
    with pytest.raises(BackupValidationError, match="size"):
        validate_backup_archive(backup, limits=limits)


def test_staged_restore_replaces_database_and_settings_and_drops_sidecars(
    tmp_path: Path,
) -> None:
    database = _seed_database(tmp_path / "source")
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()
    payload = stage_backup_for_restore(backup, tmp_path / "staging")
    paths = AppPaths.for_current_user(tmp_path / "restored-user")
    paths.database_file.parent.mkdir(parents=True)
    paths.settings_file.parent.mkdir(parents=True)
    paths.database_file.write_bytes(b"old-database")
    paths.settings_file.write_bytes(b"old-settings")
    Path(f"{paths.database_file}-wal").write_bytes(b"old-wal")
    Path(f"{paths.database_file}-shm").write_bytes(b"old-shm")

    try:
        apply_validated_restore(payload, paths)
        assert json.loads(paths.settings_file.read_text(encoding="utf-8")) == _settings()
        assert not Path(f"{paths.database_file}-wal").exists()
        assert not Path(f"{paths.database_file}-shm").exists()
        with sqlite3.connect(paths.database_file) as restored:
            assert restored.execute("PRAGMA application_id").fetchone()[0] == AMADEUS_APPLICATION_ID
            assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert restored.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
            assert (
                restored.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0] == 1
            )
            assert restored.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 2
            assert restored.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 3
            assert (
                restored.execute(
                    "SELECT COUNT(*) FROM memory_sources WHERE live_message_id = ?",
                    ("user-1",),
                ).fetchone()[0]
                == 2
            )
            assert (
                restored.execute(
                    "SELECT COUNT(*) FROM memory_sources WHERE extraction_method = 'manual'"
                ).fetchone()[0]
                == 1
            )
            assert restored.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 1
            assert restored.execute("SELECT COUNT(*) FROM persona_vectors").fetchone()[0] == 1
            assert (
                restored.execute(
                    "SELECT id FROM memory_embedding_generations WHERE status = 'active'"
                ).fetchone()[0]
                == "user-p6"
            )
            assert (
                restored.execute(
                    "SELECT id FROM persona_embedding_generations WHERE status = 'active'"
                ).fetchone()[0]
                == "persona-p6"
            )
    finally:
        discard_staged_restore(payload)


def test_restore_without_credential_reuse_disables_profiles_and_profile_voice(
    tmp_path: Path,
) -> None:
    database = _seed_database(tmp_path / "source")
    try:
        settings = _settings()
        profile = settings["model_providers"]["profiles"][0]
        profile["enabled"] = True
        fingerprint = "a" * 64
        profile["test_fingerprints"] = {
            role: fingerprint for role in ("conversation", "summary", "memory", "vision")
        }
        settings["provider_enabled"] = True
        backup = create_backup_archive(
            tmp_path / "provider-backup.amadeus-backup",
            database_backup=lambda target: database.create_backup(target),
            settings_snapshot=settings,
            app_version=__version__,
            created_at=FIXED_TIME,
        )
    finally:
        database.close()
    payload = stage_backup_for_restore(backup, tmp_path / "provider-stage")

    try:
        disabled = disable_provider_credential_reuse_for_restore(payload)
        document = json.loads(disabled.settings_path.read_text(encoding="utf-8"))

        assert all(
            profile["enabled"] is False and set(profile["test_fingerprints"].values()) == {""}
            for profile in document["model_providers"]["profiles"]
        )
        assert document["provider_enabled"] is False
        assert document["multimodal"]["enabled"] is False
        assert disabled.staged_settings_sha256 != payload.staged_settings_sha256
    finally:
        discard_staged_restore(payload)


def test_old_v5_database_and_v8_settings_backup_restores_then_migrates_to_p7f(
    tmp_path: Path,
) -> None:
    legacy_database = tmp_path / "legacy-v5.sqlite3"
    with sqlite3.connect(legacy_database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for migrate in (
            database_module._migrate_to_v1,
            database_module._migrate_to_v2,
            database_module._migrate_to_v3,
            database_module._migrate_to_v4,
            database_module._migrate_to_v5,
        ):
            migrate(connection)
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    legacy_settings = deepcopy(DEFAULT_SETTINGS)
    legacy_settings["schema_version"] = 8
    legacy_settings["memory"] = {"enabled": True}
    archive = _manual_archive(
        tmp_path / "legacy.amadeus-backup",
        legacy_database,
        legacy_settings,
    )
    payload = stage_backup_for_restore(archive, tmp_path / "legacy-stage")
    paths = AppPaths.for_current_user(tmp_path / "legacy-restored")
    try:
        apply_validated_restore(payload, paths)
        migrated_database = SQLiteDatabase(
            paths.database_file,
            backup_dir=paths.directory(AppDirectory.BACKUPS),
        ).open()
        try:
            assert migrated_database.schema_version == 8
            assert "memory_reflections" in {
                row[0]
                for row in migrated_database.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            migrated_database.close()
        settings = SettingsRepository(paths.settings_file).load()
        assert settings["schema_version"] == CURRENT_SCHEMA_VERSION
        assert settings["memory"] == {
            "enabled": True,
            "deep_memory_enabled": True,
        }
    finally:
        discard_staged_restore(payload)


def test_restore_failure_rolls_back_database_settings_wal_and_shm_exactly(
    tmp_path: Path,
) -> None:
    database = _seed_database(tmp_path / "source")
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()
    payload = stage_backup_for_restore(backup, tmp_path / "staging")
    paths = AppPaths.for_current_user(tmp_path / "rollback-user")
    paths.database_file.parent.mkdir(parents=True)
    paths.settings_file.parent.mkdir(parents=True)
    originals = {
        paths.database_file: b"old-database-exact",
        Path(f"{paths.database_file}-wal"): b"old-wal-exact",
        Path(f"{paths.database_file}-shm"): b"old-shm-exact",
        paths.settings_file: b"old-settings-exact",
    }
    for path, data in originals.items():
        path.write_bytes(data)
    failure_injected = False

    def fail_settings_install(source: Path, destination: Path) -> None:
        nonlocal failure_injected
        if (
            not failure_injected
            and source.name.endswith(".new")
            and destination == paths.settings_file
        ):
            failure_injected = True
            raise OSError("injected settings install failure")
        os.replace(source, destination)

    try:
        with pytest.raises(RestoreError, match="previous data was restored"):
            apply_validated_restore(payload, paths, replace_operation=fail_settings_install)
        assert failure_injected
        assert {path: path.read_bytes() for path in originals} == originals
        assert not list(paths.root.rglob("*.rollback"))
        assert not list(paths.root.rglob("*.new"))
    finally:
        discard_staged_restore(payload)


def test_restore_rechecks_staged_content_before_replacement(tmp_path: Path) -> None:
    database = _seed_database(tmp_path / "source")
    try:
        backup = _make_backup(tmp_path, database)
    finally:
        database.close()
    payload = stage_backup_for_restore(backup, tmp_path / "staging")
    payload.settings_path.write_text('{"schema_version": 5}\n', encoding="utf-8")
    paths = AppPaths.for_current_user(tmp_path / "untouched-user")

    try:
        with pytest.raises(RestoreError, match="changed after validation"):
            apply_validated_restore(payload, paths)
        assert not paths.database_file.exists()
        assert not paths.settings_file.exists()
    finally:
        discard_staged_restore(payload)


def test_factory_reset_plan_contains_only_exact_known_children(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    plan = plan_factory_reset(paths)

    assert plan.root == paths.root
    assert plan.targets == tuple(paths.directory(region) for region in AppDirectory)
    assert all(target.parent == plan.root for target in plan.targets)
    assert tmp_path not in plan.targets
    assert plan.root not in plan.targets


def test_factory_reset_plan_rejects_broad_or_wrong_roots(tmp_path: Path) -> None:
    with pytest.raises(UnsafeResetPlanError, match="Amadeus"):
        plan_factory_reset(AppPaths(tmp_path / "DifferentProduct"))

    root = Path(Path.cwd().anchor)
    with pytest.raises(UnsafeResetPlanError):
        plan_factory_reset(AppPaths(root))
