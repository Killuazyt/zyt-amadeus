from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from amadeus_desktop.conversation_store import BackgroundJobStore, ConversationStore
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.memory_models import MemoryOperation
from amadeus_desktop.memory_service import MemoryService
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.storage_models import (
    ManualVersionProtectedError,
    MemoryStatus,
    MemoryVersionOrigin,
    StaleMemorySourceError,
    StorageNotFoundError,
    StorageValidationError,
)


@pytest.fixture
def memory_fixture(tmp_path):
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    conversations = ConversationStore(database)
    memory = MemoryStore(database)
    assert isinstance(memory, MemoryService)
    conversation = conversations.create_conversation()
    for index, text in enumerate(("我喜欢咖啡", "还是咖啡", "我现在喜欢红茶", "这是明确纠正")):
        conversations.save_user_message(
            conversation.conversation_id,
            f"turn-{index}",
            f"user-{index}",
            text,
        )
    try:
        yield database, conversations, memory, conversation
    finally:
        database.close()


def test_exact_duplicate_only_adds_provenance_and_chinese_fts_is_safe(
    memory_fixture,
) -> None:
    database, _conversations, memory, _conversation = memory_fixture
    first = memory.upsert_memory(
        "preference",
        "饮料 咖啡",
        "我喜欢咖啡",
        importance=0.7,
        confidence=0.9,
        source_message_ids=("user-0",),
    )
    duplicate = memory.upsert_memory(
        "preference",
        "别名主题",
        "  我喜欢咖啡  ",
        importance=0.8,
        confidence=0.95,
        source_message_ids=("user-1",),
    )
    assert first.created_group
    assert not duplicate.created_group
    assert duplicate.memory.memory_id == first.memory.memory_id
    assert len(memory.list_memories()) == 1
    assert {source.source_message_id for source in memory.list_sources(first.memory.memory_id)} == {
        "user-0",
        "user-1",
    }
    assert memory.search("咖啡")[0].memory.memory_id == first.memory.memory_id
    assert memory.search('咖啡") OR memory_id:*')[0].memory.memory_id == first.memory.memory_id
    assert database.connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 1


def test_clear_all_memories_keeps_chat_and_removes_extraction_jobs(memory_fixture) -> None:
    database, conversations, memory, conversation = memory_fixture
    first = memory.create_memory(
        "preference",
        "饮料 咖啡",
        "我喜欢咖啡",
        source_message_ids=("user-0",),
    )
    memory.create_memory(
        "fact",
        "所在地",
        "我住在测试城市",
        source_message_ids=("user-1",),
    )
    BackgroundJobStore(database).enqueue(
        "memory_extraction",
        "clear-test",
        profile_id="default",
        conversation_id=conversation.conversation_id,
        message_id="user-0",
    )
    messages_before = conversations.load_message_page(conversation.conversation_id, limit=40).items

    assert memory.clear_all_memories() == 2

    assert memory.list_memories() == ()
    assert database.connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 0
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM background_jobs WHERE kind = 'memory_extraction'"
        ).fetchone()[0]
        == 0
    )
    assert conversations.load_message_page(conversation.conversation_id, limit=40).items == (
        messages_before
    )
    with pytest.raises(StorageNotFoundError):
        memory.get(first.memory_id)


def test_exact_repeated_user_message_attaches_source_without_replacing_version(
    memory_fixture,
) -> None:
    _database, conversations, memory, conversation = memory_fixture
    record = memory.create_memory(
        "preference",
        "饮料 咖啡",
        "我喜欢咖啡",
        source_message_ids=("user-0",),
    )
    repeated = conversations.save_user_message(
        conversation.conversation_id,
        "turn-repeat",
        "user-repeat",
        "我喜欢咖啡",
    )

    assert memory.attach_exact_repeat_source(repeated.message_id) == 1
    assert memory.attach_exact_repeat_source(repeated.message_id) == 0
    unchanged = memory.get(record.memory_id)
    assert unchanged.current_version.version_id == record.current_version.version_id
    assert {source.source_message_id for source in memory.list_sources(record.memory_id)} == {
        "user-0",
        "user-repeat",
    }

    memory.edit_memory(record.memory_id, "用户手工改为喜欢红茶")
    repeated_again = conversations.save_user_message(
        conversation.conversation_id,
        "turn-repeat-again",
        "user-repeat-again",
        "我喜欢咖啡",
    )
    assert memory.attach_exact_repeat_source(repeated_again.message_id) == 0


def test_versions_are_immutable_manual_edits_win_and_corrections_replace_fts(
    memory_fixture,
) -> None:
    database, conversations, memory, conversation = memory_fixture
    record = memory.create_memory(
        "preference",
        "饮料",
        "我喜欢咖啡",
        importance=0.7,
        confidence=0.9,
        source_message_ids=("user-0",),
    )
    supplemented = memory.add_version(
        record.memory_id,
        "我喜欢手冲咖啡",
        importance=0.75,
        confidence=0.9,
        operation=MemoryOperation.SUPPLEMENT,
        source_message_ids=("user-1",),
    )
    assert supplemented.current_version.version_number == 2
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        database.connection.execute(
            "UPDATE memory_versions SET content='篡改' WHERE id=?",
            (record.current_version.version_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="only be deleted"):
        database.connection.execute(
            "DELETE FROM memory_versions WHERE id=?",
            (record.current_version.version_id,),
        )

    manual = memory.edit_memory(record.memory_id, "我更喜欢红茶")
    assert manual.current_version.version_number == 3
    assert manual.current_version.origin is MemoryVersionOrigin.MANUAL
    assert manual.current_version.confidence == 1.0
    with pytest.raises(ManualVersionProtectedError):
        memory.add_version(
            record.memory_id,
            "自动补充不应覆盖",
            importance=0.8,
            confidence=0.8,
            operation=MemoryOperation.SUPPLEMENT,
            source_message_ids=("user-2",),
        )
    with pytest.raises(ManualVersionProtectedError):
        memory.add_version(
            record.memory_id,
            "延迟旧纠正不应覆盖手工版本",
            importance=0.8,
            confidence=0.9,
            operation=MemoryOperation.CORRECT,
            source_message_ids=("user-3",),
        )

    new_correction = conversations.save_user_message(
        conversation.conversation_id,
        "turn-new-correction",
        "user-new-correction",
        "更正：我现在只喝红茶",
        created_at=manual.current_version.created_at + timedelta(microseconds=1),
    )

    corrected = memory.add_version(
        record.memory_id,
        "我现在只喝红茶",
        importance=0.8,
        confidence=0.9,
        operation=MemoryOperation.CORRECT,
        source_message_ids=(new_correction.message_id,),
    )
    assert corrected.current_version.version_number == 4
    assert memory.search("红茶")[0].memory.current_version.content == "我现在只喝红茶"
    assert memory.search("手冲咖啡") == ()
    assert len(memory.list_versions(record.memory_id)) == 4
    assert database.connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 1


def test_delayed_automatic_version_cannot_replace_newer_user_source(memory_fixture) -> None:
    _database, _conversations, memory, _conversation = memory_fixture
    record = memory.create_memory(
        "preference",
        "饮料",
        "我喜欢咖啡",
        source_message_ids=("user-0",),
    )
    current = memory.add_version(
        record.memory_id,
        "我现在喜欢红茶",
        importance=0.8,
        confidence=0.9,
        operation=MemoryOperation.CORRECT,
        source_message_ids=("user-2",),
    )

    with pytest.raises(StaleMemorySourceError):
        memory.add_version(
            record.memory_id,
            "延迟任务试图恢复咖啡",
            importance=0.7,
            confidence=0.9,
            operation=MemoryOperation.CORRECT,
            source_message_ids=("user-1",),
        )
    assert (
        memory.get(record.memory_id).current_version.version_id
        == current.current_version.version_id
    )

    newest = memory.add_version(
        record.memory_id,
        "用户又明确更正了饮料偏好",
        importance=0.8,
        confidence=0.9,
        operation=MemoryOperation.CORRECT,
        source_message_ids=("user-3",),
    )
    assert newest.current_version.version_number == 3


def test_deleted_chat_leaves_content_free_source_tombstone(memory_fixture) -> None:
    database, conversations, memory, conversation = memory_fixture
    record = memory.create_memory(
        "fact",
        "饮料",
        "用户喝咖啡",
        source_message_ids=("user-0",),
    )
    assert conversations.delete_conversation(conversation.conversation_id)

    assert memory.get(record.memory_id).current_version.content == "用户喝咖啡"
    source = memory.list_sources(record.memory_id)[0]
    assert source.source_message_id == "user-0"
    assert source.source_conversation_id == conversation.conversation_id
    assert source.live_message_id is None
    assert source.live_conversation_id is None
    assert source.source_deleted
    columns = {
        row[1]
        for row in database.connection.execute("PRAGMA table_info(memory_sources)").fetchall()
    }
    assert "content" not in columns
    assert database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_disabled_message_cannot_become_provenance_but_stays_in_chat(
    memory_fixture,
) -> None:
    _database, conversations, memory, conversation = memory_fixture
    disabled = conversations.save_user_message(
        conversation.conversation_id,
        "turn-disabled",
        "user-disabled",
        "不要在关闭期间追溯",
        participates_in_memory=False,
    )
    assert not disabled.participates_in_memory
    assert conversations.get_message(disabled.message_id).content
    with pytest.raises(StorageValidationError, match="disabled"):
        memory.create_memory(
            "fact",
            "停用",
            "不应写入",
            source_message_ids=(disabled.message_id,),
        )


def test_archive_restore_and_delete_cascade_all_versions_sources_and_fts(
    memory_fixture,
) -> None:
    database, _conversations, memory, _conversation = memory_fixture
    first_marker = "P5A_DELETED_MEMORY_V1_CANARY_8R3K6T2W"
    second_marker = "P5A_DELETED_MEMORY_V2_CANARY_5H9N4J7X"
    record = memory.create_memory(
        "preference",
        "饮料",
        f"我喜欢咖啡 {first_marker}",
        source_message_ids=("user-0",),
    )
    memory.add_version(
        record.memory_id,
        f"我改喝红茶 {second_marker}",
        importance=0.8,
        confidence=0.9,
        operation=MemoryOperation.CORRECT,
        source_message_ids=("user-2",),
    )
    assert memory.archive(record.memory_id).status is MemoryStatus.ARCHIVED
    assert memory.search("红茶") == ()
    assert memory.search("红茶", include_archived=True)
    assert memory.restore(record.memory_id).status is MemoryStatus.ACTIVE
    assert memory.set_pinned(record.memory_id, True).pinned

    assert memory.delete_memory(record.memory_id)
    assert not memory.delete_memory(record.memory_id)
    with pytest.raises(StorageNotFoundError):
        memory.get(record.memory_id)
    assert database.connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 0
    sqlite_files = (
        database.path,
        database.path.with_name(f"{database.path.name}-wal"),
        database.path.with_name(f"{database.path.name}-shm"),
    )
    raw_files = [path.read_bytes() for path in sqlite_files if path.exists()]
    assert all(first_marker.encode("utf-8") not in raw for raw in raw_files)
    assert all(second_marker.encode("utf-8") not in raw for raw in raw_files)


def test_version_pointer_rolls_back_if_transactional_fts_update_fails(
    memory_fixture,
) -> None:
    database, _conversations, memory, _conversation = memory_fixture
    record = memory.create_memory(
        "preference",
        "饮料",
        "我喜欢咖啡",
        source_message_ids=("user-0",),
    )
    database.connection.execute("DROP TABLE memory_fts")

    with pytest.raises(sqlite3.OperationalError, match="memory_fts"):
        memory.add_version(
            record.memory_id,
            "我现在喝红茶",
            importance=0.8,
            confidence=0.9,
            operation=MemoryOperation.CORRECT,
            source_message_ids=("user-2",),
        )

    restored = memory.get(record.memory_id)
    assert restored.current_version.version_id == record.current_version.version_id
    assert len(memory.list_versions(record.memory_id)) == 1


def test_conversation_and_current_memory_survive_database_reopen(tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    first_database = SQLiteDatabase(path).open()
    conversations = ConversationStore(first_database)
    conversation = conversations.create_conversation("重启恢复")
    conversations.save_user_message(
        conversation.conversation_id,
        "turn-restart",
        "user-restart",
        "我偏爱无糖咖啡",
    )
    record = MemoryStore(first_database).create_memory(
        "preference",
        "咖啡甜度",
        "用户偏爱无糖咖啡",
        source_message_ids=("user-restart",),
    )
    first_database.close()

    second_database = SQLiteDatabase(path).open()
    try:
        restored_conversations = ConversationStore(second_database)
        restored_memory = MemoryStore(second_database)
        assert (
            restored_conversations.get_or_create_active_conversation().conversation_id
            == conversation.conversation_id
        )
        assert restored_conversations.get_message("user-restart").content == "我偏爱无糖咖啡"
        assert restored_memory.get(record.memory_id).current_version.content == "用户偏爱无糖咖啡"
        assert restored_memory.search("无糖咖啡")[0].memory.memory_id == record.memory_id
    finally:
        second_database.close()


def test_active_document_revalidation_and_successful_recall_are_auditable(
    memory_fixture,
) -> None:
    database, _conversations, memory, _conversation = memory_fixture
    active = memory.create_memory(
        "preference",
        "饮料",
        "用户喜欢咖啡",
        source_message_ids=("user-0",),
    )
    archived = memory.create_memory(
        "fact",
        "旧资料",
        "这是一条归档资料",
        source_message_ids=("user-1",),
    )
    memory.archive(archived.memory_id)

    assert [item.current_version.version_id for item in memory.list_active_documents()] == [
        active.current_version.version_id
    ]
    assert memory.get_active_by_version_ids(
        (active.current_version.version_id, archived.current_version.version_id, "missing")
    ) == (active,)

    assert (
        memory.record_successful_recall(
            "ticket-no-first",
            (active.current_version.version_id,),
            terminal_status="completed",
            first_chunk_received=False,
        )
        == 0
    )
    assert (
        memory.record_successful_recall(
            "ticket-failed",
            (active.current_version.version_id,),
            terminal_status="failed",
            first_chunk_received=True,
        )
        == 0
    )
    assert (
        memory.record_successful_recall(
            "ticket-success",
            (active.current_version.version_id,),
            terminal_status="completed",
            first_chunk_received=True,
        )
        == 1
    )
    assert (
        memory.record_successful_recall(
            "ticket-success",
            (active.current_version.version_id,),
            terminal_status="completed",
            first_chunk_received=True,
        )
        == 0
    )
    assert (
        memory.record_successful_recall(
            "ticket-stopped",
            (active.current_version.version_id,),
            terminal_status="user_stopped",
            first_chunk_received=True,
            attempt=2,
        )
        == 1
    )
    stats = memory.recall_stats(memory_ids=(active.memory_id,))[0]
    assert stats.successful_recall_count == 2
    assert stats.last_recalled_at is not None
    assert (
        database.connection.execute("SELECT COUNT(*) FROM memory_recall_events").fetchone()[0] == 2
    )


def test_event_decay_uses_latest_success_and_respects_exemptions(tmp_path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    memory = MemoryStore(database, clock=lambda: start)
    try:
        stale = memory.create_memory(
            "event",
            "旧事件",
            "用户一百天前去过展览",
            importance=0.5,
            origin=MemoryVersionOrigin.MANUAL,
        )
        recalled = memory.create_memory(
            "event",
            "有召回事件",
            "用户一百天前参加过读书会",
            importance=0.5,
            origin=MemoryVersionOrigin.MANUAL,
        )
        pinned = memory.create_memory(
            "event",
            "置顶事件",
            "用户一百天前得到一份礼物",
            importance=0.2,
            origin=MemoryVersionOrigin.MANUAL,
        )
        important = memory.create_memory(
            "event",
            "重要事件",
            "用户一百天前完成毕业答辩",
            importance=0.85,
            origin=MemoryVersionOrigin.MANUAL,
        )
        fact = memory.create_memory(
            "fact",
            "事实",
            "用户的生日是五月一日",
            importance=0.2,
            origin=MemoryVersionOrigin.MANUAL,
        )
        memory.set_pinned(pinned.memory_id, True)
        memory.record_successful_recall(
            "ticket-recent",
            (recalled.current_version.version_id,),
            terminal_status="completed",
            first_chunk_received=True,
            recalled_at=start + timedelta(days=50),
        )

        assert memory.archive_decayed_events(now=start + timedelta(days=100)) == (stale.memory_id,)
        assert memory.get(recalled.memory_id).status is MemoryStatus.ACTIVE
        assert memory.get(pinned.memory_id).status is MemoryStatus.ACTIVE
        assert memory.get(important.memory_id).status is MemoryStatus.ACTIVE
        assert memory.get(fact.memory_id).status is MemoryStatus.ACTIVE

        assert memory.archive_decayed_events(now=start + timedelta(days=151)) == (
            recalled.memory_id,
        )
    finally:
        database.close()


def test_event_decay_anchor_ignores_group_metadata_updates(tmp_path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    memory = MemoryStore(database, clock=lambda: start)
    try:
        record = memory.create_memory(
            "event",
            "旧事件",
            "用户一百天前参加过一次合成活动",
            importance=0.5,
            origin=MemoryVersionOrigin.MANUAL,
        )
        metadata_editor = MemoryStore(
            database,
            clock=lambda: start + timedelta(days=89),
        )
        metadata_editor.set_pinned(record.memory_id, True)
        metadata_editor.set_pinned(record.memory_id, False)

        assert memory.archive_decayed_events(now=start + timedelta(days=100)) == (record.memory_id,)
        assert memory.get(record.memory_id).status is MemoryStatus.ARCHIVED
    finally:
        database.close()
