from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace

from amadeus_desktop.chat_models import (
    ChatMessage,
    ConversationTurn,
    MessageRole,
    MessageStatus,
    PreparedPrompt,
    TurnTerminalReason,
)
from amadeus_desktop.data_runtime import DataPriority, SerialDataThread
from amadeus_desktop.local_data_service import LocalDataService, create_local_data_stores
from amadeus_desktop.retrieval_pipeline import VectorRetrievalResult


def _turn(
    *,
    turn_id: str = "turn-timeout",
    user_id: str = "user-timeout",
    assistant_id: str = "assistant-timeout",
    content: str = "本轮必须先持久化，再允许 FTS 降级。",
) -> ConversationTurn:
    return ConversationTurn(
        turn_id=turn_id,
        user_message=ChatMessage(
            user_id,
            MessageRole.USER,
            content,
            MessageStatus.COMPLETED,
        ),
        assistant_message=ChatMessage(
            assistant_id,
            MessageRole.ASSISTANT,
            "",
            MessageStatus.PENDING,
        ),
    )


def test_slow_vector_query_falls_back_without_delaying_or_duplicating_user_row(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    pending_vector: Future[VectorRetrievalResult] = Future()
    assert pending_vector.set_running_or_notify_cancel()

    def factory():
        return create_local_data_stores(
            tmp_path / "amadeus.sqlite3",
            tmp_path / "backups",
        )

    runtime = SerialDataThread(factory, resource_close=lambda stores: stores.close())
    service = LocalDataService(
        runtime,
        vector_query=lambda _query: pending_vector,
        vector_timeout_ms=25,
    )
    prompts: list[PreparedPrompt] = []
    failures: list[str] = []
    service.start()
    qtbot.waitUntil(lambda: service.is_writable, timeout=2_000)

    assert service.prepare_new_turn(_turn(), prompts.append, failures.append)
    qtbot.waitUntil(lambda: bool(prompts), timeout=2_000)
    assert failures == []
    assert prompts[0].user_memory_version_ids == ()
    assert not pending_vector.cancelled()

    # Completing the timed-out query cannot trigger a second prompt callback.
    pending_vector.set_result(VectorRetrievalResult(threshold=0.5))
    qapp.processEvents()
    assert len(prompts) == 1

    inspections: list[tuple[str, int]] = []
    assert runtime.submit(
        lambda stores: (
            stores.conversations.get_message("user-timeout").content,
            int(
                stores.database.connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE id = 'user-timeout'"
                ).fetchone()[0]
            ),
        ),
        priority=DataPriority.FOREGROUND,
        on_success=inspections.append,
    )
    qtbot.waitUntil(lambda: bool(inspections), timeout=2_000)
    assert inspections[0][0]
    assert inspections[0][1] == 1
    assert service.shutdown(2_000)


def test_queued_vector_query_is_cancelled_at_timeout(qtbot, tmp_path) -> None:
    pending_vector: Future[VectorRetrievalResult] = Future()
    runtime = SerialDataThread(
        lambda: create_local_data_stores(
            tmp_path / "amadeus.sqlite3",
            tmp_path / "backups",
        ),
        resource_close=lambda stores: stores.close(),
    )
    service = LocalDataService(
        runtime,
        vector_query=lambda _query: pending_vector,
        vector_timeout_ms=25,
    )
    prompts: list[PreparedPrompt] = []
    service.start()
    qtbot.waitUntil(lambda: service.is_writable, timeout=2_000)

    assert service.prepare_new_turn(_turn(), prompts.append, lambda _category: None)
    qtbot.waitUntil(lambda: bool(prompts), timeout=2_000)
    assert pending_vector.cancelled()
    assert service._pending_prompt_preparations == {}
    assert service.shutdown(2_000)


def test_shutdown_cancels_queued_vector_query_and_clears_pending(qtbot, tmp_path) -> None:
    pending_vector: Future[VectorRetrievalResult] = Future()
    runtime = SerialDataThread(
        lambda: create_local_data_stores(
            tmp_path / "amadeus.sqlite3",
            tmp_path / "backups",
        ),
        resource_close=lambda stores: stores.close(),
    )
    service = LocalDataService(
        runtime,
        vector_query=lambda _query: pending_vector,
        vector_timeout_ms=5_000,
    )
    prompts: list[PreparedPrompt] = []
    service.start()
    qtbot.waitUntil(lambda: service.is_writable, timeout=2_000)

    assert service.prepare_new_turn(_turn(), prompts.append, lambda _category: None)
    qtbot.waitUntil(lambda: bool(service._pending_prompt_preparations), timeout=2_000)
    assert service.shutdown(2_000)
    assert pending_vector.cancelled()
    assert service._pending_prompt_preparations == {}
    assert prompts == []


def test_disabled_memory_is_forwarded_to_new_vector_query_boundary(qtbot, tmp_path) -> None:
    include_user_values: list[bool] = []

    def vector_query(
        _query: str,
        *,
        include_user: bool,
    ) -> Future[VectorRetrievalResult]:
        include_user_values.append(include_user)
        completed: Future[VectorRetrievalResult] = Future()
        completed.set_result(VectorRetrievalResult(threshold=0.5))
        return completed

    runtime = SerialDataThread(
        lambda: create_local_data_stores(
            tmp_path / "amadeus.sqlite3",
            tmp_path / "backups",
        ),
        resource_close=lambda stores: stores.close(),
    )
    service = LocalDataService(
        runtime,
        memory_enabled=False,
        vector_query=vector_query,
    )
    prompts: list[PreparedPrompt] = []
    service.start()
    qtbot.waitUntil(lambda: service.is_writable, timeout=2_000)

    assert service.prepare_new_turn(_turn(), prompts.append, lambda _category: None)
    qtbot.waitUntil(lambda: bool(prompts), timeout=2_000)
    assert include_user_values == [False]
    assert prompts[0].user_memory_version_ids == ()
    assert service.shutdown(2_000)


def test_memory_disable_epoch_removes_user_memory_before_provider_start(
    qtbot,
    tmp_path,
) -> None:
    pending_vector: Future[VectorRetrievalResult] = Future()
    assert pending_vector.set_running_or_notify_cancel()
    include_user_values: list[bool] = []

    def vector_query(
        _query: str,
        *,
        include_user: bool,
    ) -> Future[VectorRetrievalResult]:
        include_user_values.append(include_user)
        return pending_vector

    runtime = SerialDataThread(
        lambda: create_local_data_stores(
            tmp_path / "amadeus.sqlite3",
            tmp_path / "backups",
        ),
        resource_close=lambda stores: stores.close(),
    )
    service = LocalDataService(
        runtime,
        vector_query=vector_query,
        vector_timeout_ms=2_000,
    )
    service.start()
    qtbot.waitUntil(lambda: service.is_writable, timeout=2_000)
    conversation_id = service.current_conversation_id
    assert conversation_id is not None
    seeded: list[str] = []

    def seed_memory(stores) -> str:
        stores.conversations.save_user_message(
            conversation_id,
            "source-turn",
            "source-user",
            "我偏好手冲咖啡",
        )
        record = stores.memories.create_memory(
            "preference",
            "饮料 咖啡",
            "用户偏好手冲咖啡。",
            source_message_ids=("source-user",),
        )
        return record.current_version.version_id

    assert runtime.submit(
        seed_memory,
        priority=DataPriority.FOREGROUND,
        on_success=seeded.append,
    )
    qtbot.waitUntil(lambda: bool(seeded), timeout=2_000)

    prompts: list[PreparedPrompt] = []
    turn = _turn(
        turn_id="turn-toggle",
        user_id="user-toggle",
        assistant_id="assistant-toggle",
        content="我之前说过喜欢哪种咖啡？",
    )
    assert service.prepare_new_turn(turn, prompts.append, lambda _category: None)
    qtbot.waitUntil(lambda: include_user_values == [True], timeout=2_000)

    # Re-enabling before finalization must not resurrect evidence invalidated by
    # the intervening disable event.
    service.set_memory_enabled(False)
    service.set_memory_enabled(True)
    pending_vector.set_result(VectorRetrievalResult(threshold=0.5))
    qtbot.waitUntil(lambda: bool(prompts), timeout=2_000)
    assert prompts[0].user_memory_version_ids == ()
    assert all("[用户长期记忆" not in message.content for message in prompts[0].messages)

    # A disable after prompt construction also prevents successful-recall stats.
    service.set_memory_enabled(False)
    completed_turn = replace(turn, terminal_reason=TurnTerminalReason.COMPLETED)
    service.record_successful_recall(
        completed_turn,
        replace(prompts[0], user_memory_version_ids=(seeded[0],)),
    )
    stats: list[int] = []
    assert runtime.submit(
        lambda stores: (
            stores.memories.recall_stats(
                memory_ids=(stores.memories.get_active_by_version_ids((seeded[0],))[0].memory_id,)
            )[0].successful_recall_count
        ),
        priority=DataPriority.BACKGROUND,
        on_success=stats.append,
    )
    qtbot.waitUntil(lambda: bool(stats), timeout=2_000)
    assert stats == [0]
    assert service.shutdown(2_000)
