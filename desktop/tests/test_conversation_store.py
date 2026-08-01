from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from amadeus_desktop.conversation_store import BackgroundJobStore, ConversationStore
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.storage_models import (
    BackgroundJobStatus,
    StorageConflictError,
    StorageNotFoundError,
    StoredMessageStatus,
)


@pytest.fixture
def stores(tmp_path):
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    conversations = ConversationStore(database)
    jobs = BackgroundJobStore(database)
    try:
        yield database, conversations, jobs
    finally:
        database.close()


def test_conversation_create_restore_rename_archive_and_clear(stores) -> None:
    _database, store, _jobs = stores
    first = store.get_or_create_active_conversation()
    assert store.get_or_create_active_conversation().conversation_id == first.conversation_id

    renamed = store.rename_conversation(first.conversation_id, "长期计划")
    assert renamed.title == "长期计划"
    assert store.set_conversation_archived(first.conversation_id, True).status == "archived"

    second = store.get_or_create_active_conversation()
    assert second.conversation_id != first.conversation_id
    assert [item.conversation_id for item in store.list_conversations()] == [second.conversation_id]
    assert len(store.list_conversations(include_archived=True)) == 2
    assert store.clear_conversations() == 2
    assert store.list_conversations(include_archived=True) == ()


def test_conversation_delete_scrubs_message_body_from_database_and_wal(tmp_path) -> None:
    database_path = tmp_path / "amadeus.sqlite3"
    marker = "P5A_DELETED_CHAT_BODY_CANARY_7N4M2K9Q"
    database = SQLiteDatabase(database_path).open()
    store = ConversationStore(database)
    try:
        conversation = store.create_conversation()
        store.save_user_message(
            conversation.conversation_id,
            "turn-delete",
            "user-delete",
            marker,
        )
        store.create_assistant_placeholder(
            conversation.conversation_id,
            "turn-delete",
            "assistant-delete",
        )
        store.finalize_assistant(
            "assistant-delete",
            marker,
            status="completed",
            terminal_reason="completed",
            attempt=1,
        )

        assert store.delete_conversation(conversation.conversation_id)
    finally:
        database.close()

    encoded = marker.encode("utf-8")
    sqlite_files = (
        database_path,
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
    )
    assert all(encoded not in path.read_bytes() for path in sqlite_files if path.exists())


def test_user_commit_checkpoint_terminal_and_retry_reuse_stable_ids(stores) -> None:
    _database, store, _jobs = stores
    conversation = store.create_conversation()
    user = store.save_user_message(
        conversation.conversation_id,
        "turn-1",
        "user-1",
        "先持久化",
        participates_in_memory=False,
    )
    assert user.status is StoredMessageStatus.COMPLETED
    assert not user.participates_in_memory

    assistant = store.create_assistant_placeholder(
        conversation.conversation_id, "turn-1", "assistant-1"
    )
    assert assistant.content == ""
    assert assistant.status is StoredMessageStatus.PENDING
    checkpoint = store.checkpoint_assistant("assistant-1", "部分", attempt=1)
    assert checkpoint.content == "部分"
    assert checkpoint.status is StoredMessageStatus.STREAMING
    failed = store.finalize_assistant(
        "assistant-1",
        "部分回答",
        status="failed",
        terminal_reason="provider_error",
        attempt=1,
        failure_code="protocol",
    )
    assert failed.failure_code == "protocol"

    retried = store.begin_assistant_attempt("assistant-1", 2)
    assert retried.message_id == "assistant-1"
    assert retried.content == ""
    assert retried.attempt == 2
    completed = store.finalize_assistant(
        "assistant-1",
        "重试完成",
        status="completed",
        terminal_reason="completed",
        attempt=2,
        provider_name="fake",
        model_name="fake-model",
    )
    assert completed.content == "重试完成"
    assert completed.attempt == 2
    assert len(store.load_recent_messages(conversation.conversation_id)) == 2


def test_save_turn_is_atomic_if_placeholder_insert_conflicts(stores) -> None:
    _database, store, _jobs = stores
    first = store.create_conversation()
    store.save_user_message(first.conversation_id, "existing-turn", "taken-id", "已有")
    second = store.create_conversation()

    with pytest.raises(StorageConflictError):
        store.save_turn(
            second.conversation_id,
            "new-turn",
            "new-user-id",
            "不能留下半轮",
            "taken-id",
        )

    with pytest.raises(StorageNotFoundError, match="message does not exist"):
        store.get_message("new-user-id")
    assert store.load_message_page(second.conversation_id).items == ()


def test_recent_valid_messages_filters_before_applying_limit(stores) -> None:
    _database, store, _jobs = stores
    conversation = store.create_conversation()
    for index in range(12):
        store.save_user_message(
            conversation.conversation_id,
            f"valid-turn-{index}",
            f"valid-user-{index}",
            f"有效 {index}",
        )
    store.create_assistant_placeholder(
        conversation.conversation_id,
        "pending-turn",
        "pending-assistant",
    )
    valid = store.load_recent_valid_messages(conversation.conversation_id, limit=10)
    assert len(valid) == 10
    assert [message.message_id for message in valid] == [
        f"valid-user-{index}" for index in range(2, 12)
    ]


def test_pagination_is_stable_and_context_page_contains_source(stores) -> None:
    _database, store, _jobs = stores
    conversation = store.create_conversation()
    for index in range(85):
        store.save_user_message(
            conversation.conversation_id,
            f"turn-{index}",
            f"message-{index}",
            f"内容 {index}",
        )

    first = store.load_message_page(conversation.conversation_id)
    second = store.load_message_page(
        conversation.conversation_id, before_sequence=first.next_before_sequence
    )
    third = store.load_message_page(
        conversation.conversation_id, before_sequence=second.next_before_sequence
    )
    assert [len(first.items), len(second.items), len(third.items)] == [40, 40, 5]
    assert first.next_before_sequence is not None
    assert second.next_before_sequence is not None
    assert third.next_before_sequence is None
    all_ids = {item.message_id for page in (first, second, third) for item in page.items}
    assert len(all_ids) == 85
    assert all(
        list(page.items) == sorted(page.items, key=lambda message: message.sequence)
        for page in (first, second, third)
    )

    context = store.load_message_context(conversation.conversation_id, "message-44", limit=40)
    assert context.items[-1].message_id == "message-44"
    assert len(context.items) == 40


def test_summary_progress_excludes_empty_stop_and_loads_increment(stores) -> None:
    _database, store, _jobs = stores
    conversation = store.create_conversation()
    user = store.save_user_message(conversation.conversation_id, "turn-1", "user-1", "一二三")
    store.create_assistant_placeholder(conversation.conversation_id, "turn-1", "assistant-1")
    store.finalize_assistant(
        "assistant-1",
        "",
        status="stopped",
        terminal_reason="user_stopped",
        attempt=1,
    )
    progress = store.summary_progress(conversation.conversation_id)
    assert progress.message_count == 1
    assert progress.character_count == 3
    incremental = store.load_messages_after(conversation.conversation_id)
    assert [item.message_id for item in incremental] == ["user-1"]

    summary = store.save_summary(
        conversation.conversation_id,
        "已有摘要",
        user.sequence,
        message_count=1,
        character_count=3,
    )
    assert store.latest_summary(conversation.conversation_id) == summary
    assert store.summary_progress(conversation.conversation_id).message_count == 0


def test_recover_interrupted_assistant_messages_is_idempotent(stores) -> None:
    _database, store, _jobs = stores
    conversation = store.create_conversation()
    store.save_user_message(conversation.conversation_id, "turn-1", "user-1", "问题")
    store.create_assistant_placeholder(conversation.conversation_id, "turn-1", "assistant-1")
    store.checkpoint_assistant("assistant-1", "检查点", attempt=1)

    assert store.recover_interrupted_messages() == 1
    recovered = store.get_message("assistant-1")
    assert recovered.status is StoredMessageStatus.STOPPED
    assert recovered.terminal_reason == "shutdown"
    assert recovered.content == "检查点"
    assert recovered.attempt == 1
    assert store.recover_interrupted_messages() == 0


def test_jobs_are_idempotent_filterable_recoverable_and_manually_retryable(stores) -> None:
    _database, _conversations, jobs = stores
    now = datetime.now(UTC)
    extraction = jobs.enqueue("memory_extraction", "extract:1", run_after=now)
    assert jobs.enqueue("memory_extraction", "extract:1", run_after=now) == extraction
    summary = jobs.enqueue("summary", "summary:1", run_after=now)

    claimed_summary = jobs.claim_ready(kinds=("summary",))
    assert [job.job_id for job in claimed_summary] == [summary.job_id]
    assert claimed_summary[0].attempt_count == 1
    jobs.mark_completed(summary.job_id)

    claimed_extraction = jobs.claim_ready(kinds=("memory_extraction",))
    assert [job.job_id for job in claimed_extraction] == [extraction.job_id]
    failed = jobs.mark_failed(extraction.job_id, error_code="invalid-json")
    assert failed.status is BackgroundJobStatus.FAILED
    retried = jobs.retry_failed(
        extraction.job_id,
        run_after=now - timedelta(seconds=1),
    )
    assert retried.status is BackgroundJobStatus.RETRY
    assert retried.attempt_count == 0
    assert jobs.claim_ready(kinds=()) == ()
    running = jobs.claim_ready(kinds=("memory_extraction",))[0]
    assert running.attempt_count == 1
    assert jobs.recover_interrupted() == 1
    assert jobs.recover_interrupted() == 0

    with pytest.raises(StorageConflictError):
        jobs.retry_failed(extraction.job_id)
