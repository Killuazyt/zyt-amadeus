from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest

from amadeus_desktop import database as database_module
from amadeus_desktop.companion_cues import (
    CONTEXTUAL_PREVIEW_TEXT,
    CompanionCueStore,
    CompanionCueUnavailableError,
)
from amadeus_desktop.conversation_store import (
    ConversationStore,
    ProactiveInteractionStore,
)
from amadeus_desktop.data_management import (
    BACKUP_FORMAT,
    CHAT_EXPORT_FORMAT,
    MEMORY_EXPORT_FORMAT,
    SQLiteExportRepository,
    export_chat_json,
    export_memory_json,
)
from amadeus_desktop.database import SCHEMA_VERSION, SQLiteDatabase, table_names
from amadeus_desktop.deep_memory_store import DeepMemoryStore
from amadeus_desktop.memory_extraction import (
    CompanionCueRejectionReason,
    ExtractionPayloadError,
    parse_and_validate_extraction_bundle,
)
from amadeus_desktop.memory_models import ExtractionSource, SourceRole
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.settings import CURRENT_SCHEMA_VERSION, DEFAULT_SETTINGS, SettingsRepository
from amadeus_desktop.storage_models import (
    CompanionCueKind,
    CompanionCueReason,
    CompanionCueSourceKind,
    CompanionCueStatus,
    MemoryVersionOrigin,
    StorageNotFoundError,
    StorageValidationError,
)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def cue_fixture(tmp_path):
    clock = _Clock(datetime(2026, 8, 18, 2, 0, tzinfo=UTC))
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    conversations = ConversationStore(database, clock=clock)
    conversation = conversations.create_conversation("P7H")
    source = conversations.save_user_message(
        conversation.conversation_id,
        "turn-1",
        "user-1",
        "结果出来后我会回来告诉你。",
    )
    cues = CompanionCueStore(database, clock=clock)
    try:
        yield clock, database, conversations, conversation, source, cues
    finally:
        database.close()


def _cue_payload(**overrides: object) -> str:
    cue: dict[str, object] = {
        "topic": "项目结果",
        "follow_up_text": "你之前说结果出来后会回来更新，要继续聊吗？",
        "reason": "user_promised_update",
        "confidence": 0.91,
        "source_message_ids": ["user-1"],
    }
    cue.update(overrides)
    return json.dumps({"candidates": [], "companion_cues": [cue]}, ensure_ascii=False)


def test_extraction_accepts_only_explicit_user_supported_cues_and_deduplicates() -> None:
    sources = {"user-1": ExtractionSource("user-1", SourceRole.USER, "结果出来后我会回来告诉你。")}
    cue = json.loads(_cue_payload())["companion_cues"][0]
    raw = json.dumps(
        {"candidates": [], "companion_cues": [cue, deepcopy(cue)]},
        ensure_ascii=False,
    )

    result = parse_and_validate_extraction_bundle(raw, sources)

    assert len(result.companion_cues) == 1
    assert result.companion_cues[0].reason is CompanionCueReason.USER_PROMISED_UPDATE
    assert [item.reason for item in result.rejected_companion_cues] == [
        CompanionCueRejectionReason.DUPLICATE
    ]


@pytest.mark.parametrize(
    ("source", "overrides", "expected"),
    (
        (
            ExtractionSource("user-1", SourceRole.USER, "我们聊点别的。"),
            {},
            CompanionCueRejectionReason.UNSUPPORTED_INTENT,
        ),
        (
            ExtractionSource("user-1", SourceRole.ASSISTANT, "我会回来告诉你。"),
            {},
            CompanionCueRejectionReason.NON_USER_SOURCE,
        ),
        (
            ExtractionSource("user-1", SourceRole.USER, "结果出来后我会回来告诉你。"),
            {"confidence": 0.79},
            CompanionCueRejectionReason.LOW_CONFIDENCE,
        ),
        (
            ExtractionSource(
                "user-1", SourceRole.USER, "结果出来后我会回来告诉你，密码是 test-1234。"
            ),
            {},
            CompanionCueRejectionReason.SENSITIVE_CONTENT,
        ),
        (
            ExtractionSource("user-1", SourceRole.USER, "结果出来后我会回来告诉你，但不要记录。"),
            {},
            CompanionCueRejectionReason.DO_NOT_REMEMBER,
        ),
    ),
)
def test_extraction_rejects_unsupported_non_user_low_confidence_sensitive_and_opt_out(
    source: ExtractionSource,
    overrides: dict[str, object],
    expected: CompanionCueRejectionReason,
) -> None:
    result = parse_and_validate_extraction_bundle(
        _cue_payload(**overrides),
        {"user-1": source},
    )

    assert result.companion_cues == ()
    assert result.rejected_companion_cues[0].reason is expected


def test_extraction_contract_caps_companion_cues_at_two() -> None:
    cue = json.loads(_cue_payload())["companion_cues"][0]
    with pytest.raises(ExtractionPayloadError):
        parse_and_validate_extraction_bundle(
            json.dumps({"candidates": [], "companion_cues": [cue, cue, cue]}),
            {"user-1": ExtractionSource("user-1", SourceRole.USER, "结果出来后我会回来告诉你。")},
        )


def test_confirmation_freezes_text_expires_in_30_days_and_is_one_time(
    cue_fixture,
) -> None:
    clock, database, conversations, conversation, source, cues = cue_fixture
    proposed = cues.propose_conversation_followup(
        conversation_id=conversation.conversation_id,
        topic="草稿主题",
        frozen_text="草稿",
        reason=CompanionCueReason.USER_PROMISED_UPDATE,
        confidence=0.9,
        source_message_ids=(source.message_id,),
    )

    assert proposed.status is CompanionCueStatus.PROPOSED
    assert cues.select_proactive_presentation() is None
    confirmed = cues.confirm(
        proposed.cue_id,
        topic="确认后的项目结果",
        frozen_text="你之前说会回来更新项目结果。现在想继续聊吗？",
    )
    assert confirmed.status is CompanionCueStatus.ACTIVE
    assert confirmed.expires_at == clock.value + timedelta(days=30)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        database.connection.execute(
            "UPDATE companion_cues SET frozen_text = '越权修改' WHERE id = ?",
            (confirmed.cue_id,),
        )

    presentation = cues.select_proactive_presentation()
    assert presentation is not None
    assert presentation.preview_text == CONTEXTUAL_PREVIEW_TEXT
    assert "项目结果" not in presentation.preview_text
    assert presentation.expanded_text == confirmed.frozen_text

    proactive = ProactiveInteractionStore(database, clock=clock, companion_cues=cues)
    event = proactive.record_displayed(
        "idle",
        date(2026, 8, 18),
        event_id="event-1",
        cue_id=confirmed.cue_id,
    )
    assert event.cue_id == confirmed.cue_id
    assert cues.get(confirmed.cue_id).status is CompanionCueStatus.SURFACED
    assert cues.select_proactive_presentation() is None

    with pytest.raises(CompanionCueUnavailableError, match="does not match"):
        proactive.persist_greeting_on_click(
            event.event_id,
            conversation.conversation_id,
            CONTEXTUAL_PREVIEW_TEXT,
        )
    clicked, message = proactive.persist_greeting_on_click(
        event.event_id,
        conversation.conversation_id,
        confirmed.frozen_text,
        message_id="cue-message-1",
    )
    assert clicked.cue_id == confirmed.cue_id
    assert message.content == confirmed.frozen_text
    loaded = conversations.load_message_page(conversation.conversation_id, limit=20).items
    clicked_message = next(item for item in loaded if item.message_id == message.message_id)
    assert clicked_message.companion_source_label == "待续话题"

    event_row = database.connection.execute(
        "SELECT * FROM proactive_events WHERE id = ?", (event.event_id,)
    ).fetchone()
    assert set(event_row.keys()) == {
        "id",
        "profile_id",
        "local_date",
        "trigger_kind",
        "displayed_at",
        "disposition",
        "message_id",
        "cue_id",
    }
    assert confirmed.frozen_text not in repr(dict(event_row))


def test_selection_prioritizes_conversation_then_memory_and_honors_retention(
    cue_fixture,
) -> None:
    _clock, database, _conversations, conversation, source, cues = cue_fixture
    memory = MemoryStore(database).create_memory(
        "preference",
        "饮品",
        "用户偏好红茶",
        origin=MemoryVersionOrigin.MANUAL,
    )
    memory_cue = cues.propose_memory_followup(
        CompanionCueSourceKind.FACT_VERSION,
        memory.current_version.version_id,
    )
    memory_cue = cues.confirm(
        memory_cue.cue_id,
        topic=memory_cue.topic,
        frozen_text="你允许我主动提起这条饮品偏好。",
        keep_until_resolved=True,
    )
    conversation_cue = cues.propose_conversation_followup(
        conversation_id=conversation.conversation_id,
        topic="项目结果",
        frozen_text="你说会回来更新项目结果。",
        reason=CompanionCueReason.USER_PROMISED_UPDATE,
        confidence=0.95,
        source_message_ids=(source.message_id,),
    )
    conversation_cue = cues.confirm(
        conversation_cue.cue_id,
        topic=conversation_cue.topic,
        frozen_text=conversation_cue.frozen_text,
    )

    first = cues.select_proactive_presentation()
    assert first is not None and first.cue_id == conversation_cue.cue_id
    cues.mark_surfaced(conversation_cue.cue_id)
    second = cues.select_proactive_presentation()
    assert second is not None and second.cue_id == memory_cue.cue_id
    assert second.source_label == "已授权记忆"
    assert memory_cue.keep_until_resolved
    assert memory_cue.expires_at is None


def test_exact_memory_version_update_conflict_archive_and_delete_revoke_authority(
    cue_fixture,
) -> None:
    clock, database, conversations, conversation, _source, cues = cue_fixture
    memories = MemoryStore(database, clock=clock)
    memory = memories.create_memory(
        "preference",
        "饮品",
        "用户偏好红茶",
        origin=MemoryVersionOrigin.MANUAL,
    )
    cue = cues.propose_memory_followup(
        CompanionCueSourceKind.FACT_VERSION,
        memory.current_version.version_id,
    )
    cue = cues.confirm(cue.cue_id, topic=cue.topic, frozen_text=cue.frozen_text)
    memory = memories.edit_memory(memory.memory_id, "用户现在偏好咖啡")
    assert cues.get(cue.cue_id).status is CompanionCueStatus.EXPIRED

    conflict_cue = cues.propose_memory_followup(
        CompanionCueSourceKind.FACT_VERSION,
        memory.current_version.version_id,
    )
    conflict_cue = cues.confirm(
        conflict_cue.cue_id,
        topic=conflict_cue.topic,
        frozen_text=conflict_cue.frozen_text,
    )
    conflict_source = conversations.save_user_message(
        conversation.conversation_id,
        "turn-conflict",
        "user-conflict",
        "其实我现在不喝咖啡。",
    )
    DeepMemoryStore(database, clock=clock).open_fact_conflict(
        memory.memory_id,
        "用户现在不喝咖啡",
        source_message_id=conflict_source.message_id,
        importance=0.8,
        confidence=0.9,
    )
    assert cues.get(conflict_cue.cue_id).status is CompanionCueStatus.EXPIRED

    assert memories.delete_memory(memory.memory_id)
    with pytest.raises(StorageNotFoundError):
        cues.get(conflict_cue.cue_id)
    deleted = [event for event in cues.list_audit_events() if event.event_type == "deleted"]
    assert deleted
    audit_columns = {
        row[1]
        for row in database.connection.execute("PRAGMA table_info(companion_cue_audit_events)")
    }
    assert "topic" not in audit_columns
    assert "frozen_text" not in audit_columns
    assert "source_message_id" not in audit_columns


def test_static_persona_and_user_message_cannot_be_memory_authorization_sources(
    cue_fixture,
) -> None:
    _clock, _database, _conversations, _conversation, source, cues = cue_fixture
    with pytest.raises(StorageValidationError):
        cues.propose_memory_followup(
            CompanionCueSourceKind.USER_MESSAGE,
            source.message_id,
        )
    with pytest.raises(ValueError):
        CompanionCueSourceKind("static_persona")


def test_due_cues_expire_but_keep_until_resolved_survives(cue_fixture) -> None:
    clock, _database, _conversations, conversation, source, cues = cue_fixture
    expiring = cues.propose_conversation_followup(
        conversation_id=conversation.conversation_id,
        topic="项目结果",
        frozen_text="回来更新项目结果。",
        reason=CompanionCueReason.USER_PROMISED_UPDATE,
        confidence=0.9,
        source_message_ids=(source.message_id,),
    )
    expiring = cues.confirm(
        expiring.cue_id,
        topic=expiring.topic,
        frozen_text=expiring.frozen_text,
    )
    clock.value += timedelta(days=31)

    assert cues.expire_due() == 1
    assert cues.get(expiring.cue_id).status is CompanionCueStatus.EXPIRED


def test_v6_database_migrates_to_v8_with_empty_cue_and_temporal_tables(tmp_path) -> None:
    path = tmp_path / "legacy-v6.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute("PRAGMA foreign_keys = ON")
    for migration in (
        database_module._migrate_to_v1,
        database_module._migrate_to_v2,
        database_module._migrate_to_v3,
        database_module._migrate_to_v4,
        database_module._migrate_to_v5,
        database_module._migrate_to_v6,
    ):
        migration(legacy)
    legacy.execute("PRAGMA user_version = 6")
    timestamp = "2026-08-18T00:00:00.000000Z"
    legacy.execute(
        "INSERT INTO profiles VALUES ('legacy', '旧用户', ?, ?)",
        (timestamp, timestamp),
    )
    legacy.commit()
    legacy.close()

    database = SQLiteDatabase(path, backup_dir=tmp_path / "backups").open()
    try:
        assert database.schema_version == SCHEMA_VERSION == 8
        assert (
            database.connection.execute(
                "SELECT display_name FROM profiles WHERE id = 'legacy'"
            ).fetchone()[0]
            == "旧用户"
        )
        assert {
            "companion_cues",
            "companion_cue_sources",
            "companion_cue_audit_events",
            "temporal_commitments",
            "temporal_commitment_versions",
            "temporal_commitment_audit_events",
        }.issubset(table_names(database.connection))
        assert database.connection.execute("SELECT COUNT(*) FROM companion_cues").fetchone()[0] == 0
        assert database.last_backup_path is not None
    finally:
        database.close()
    backup = sqlite3.connect(database.last_backup_path)
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        backup.close()


def test_v10_settings_migrate_to_v11_with_contextual_followups_off(tmp_path) -> None:
    path = tmp_path / "settings.json"
    legacy = deepcopy(DEFAULT_SETTINGS)
    legacy["schema_version"] = 10
    legacy["proactive"].pop("contextual_followups_enabled")
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    loaded = SettingsRepository(path).load()

    assert loaded["schema_version"] == CURRENT_SCHEMA_VERSION == 11
    assert loaded["proactive"]["contextual_followups_enabled"] is False


def test_v3_exports_split_chat_and_memory_cues_without_vectors_or_private_delete_audit(
    cue_fixture,
    tmp_path,
) -> None:
    _clock, database, conversations, conversation, source, cues = cue_fixture
    conversation_cue = cues.propose_conversation_followup(
        conversation_id=conversation.conversation_id,
        topic="项目结果",
        frozen_text="回来更新项目结果。",
        reason=CompanionCueReason.USER_PROMISED_UPDATE,
        confidence=0.9,
        source_message_ids=(source.message_id,),
    )
    cues.confirm(
        conversation_cue.cue_id,
        topic=conversation_cue.topic,
        frozen_text=conversation_cue.frozen_text,
    )
    memory = MemoryStore(database).create_memory(
        "preference",
        "饮品",
        "用户偏好红茶",
        origin=MemoryVersionOrigin.MANUAL,
    )
    memory_cue = cues.propose_memory_followup(
        CompanionCueSourceKind.FACT_VERSION,
        memory.current_version.version_id,
    )
    cues.confirm(
        memory_cue.cue_id,
        topic=memory_cue.topic,
        frozen_text=memory_cue.frozen_text,
    )
    deleted_secret = "只应存在于已删除线索正文"
    deleted_source = conversations.save_user_message(
        conversation.conversation_id,
        "turn-delete",
        "user-delete",
        "晚点继续说这个删除测试。",
    )
    deleted_cue = cues.propose_conversation_followup(
        conversation_id=conversation.conversation_id,
        topic="删除测试",
        frozen_text=deleted_secret,
        reason=CompanionCueReason.USER_PROMISED_UPDATE,
        confidence=0.9,
        source_message_ids=(deleted_source.message_id,),
    )
    cues.delete(deleted_cue.cue_id)

    repository = SQLiteExportRepository(database.connection)
    chat_path = export_chat_json(tmp_path / "chat.json", repository.load_chat_bundle)
    memory_path = export_memory_json(tmp_path / "memory.json", repository.load_memory_bundle)
    chat = json.loads(chat_path.read_text(encoding="utf-8"))
    exported_memory = json.loads(memory_path.read_text(encoding="utf-8"))

    assert chat["format"] == CHAT_EXPORT_FORMAT == "amadeus-chat-export/v4"
    assert exported_memory["format"] == MEMORY_EXPORT_FORMAT == "amadeus-memory-export/v3"
    assert {cue["kind"] for cue in chat["companion_cues"]} == {
        CompanionCueKind.CONVERSATION_FOLLOWUP.value
    }
    assert {cue["kind"] for cue in exported_memory["companion_followups"]["cues"]} == {
        CompanionCueKind.MEMORY_FOLLOWUP.value
    }
    serialized = chat_path.read_text(encoding="utf-8") + memory_path.read_text(encoding="utf-8")
    assert deleted_secret not in serialized
    assert "vector_blob" not in serialized
    assert BACKUP_FORMAT == "amadeus-backup/v2"
