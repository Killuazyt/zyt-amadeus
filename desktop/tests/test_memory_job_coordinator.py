from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QTimer

from amadeus_desktop.background_generation import BackgroundGenerationRunner
from amadeus_desktop.chat_models import (
    ChatRequest,
    GenerationPurpose,
    PromptMessage,
    PromptRole,
    TurnTerminalReason,
)
from amadeus_desktop.chat_provider import (
    CancellationRequested,
    CancellationToken,
    ChatProviderError,
    ProviderErrorCode,
    ScriptedChatProvider,
    ScriptedScenario,
)
from amadeus_desktop.conversation_store import BackgroundJobStore, ConversationStore
from amadeus_desktop.data_runtime import SerialDataThread
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.memory_job_coordinator import (
    MEMORY_EXTRACTION_JOB_KIND,
    SUMMARY_JOB_KIND,
    JobRepositoryBundle,
    MemoryJobCoordinator,
    extraction_terminal_is_eligible,
    summary_is_due,
)
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.storage_models import SummaryProgress


@dataclass
class MutableClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs: int) -> None:
        self.current += timedelta(**kwargs)


@dataclass
class _Resource:
    database: SQLiteDatabase
    repositories: JobRepositoryBundle


class SequenceProvider:
    def __init__(self, responses: list[str | ProviderErrorCode]) -> None:
        self._responses = responses
        self._requests = []
        self._lock = threading.Lock()

    @property
    def requests(self):
        with self._lock:
            return tuple(self._requests)

    @property
    def call_count(self) -> int:
        return len(self.requests)

    async def stream(self, request, cancellation: CancellationToken):
        cancellation.bind_current_task()
        try:
            with self._lock:
                self._requests.append(request)
                response = self._responses.pop(0)
            await asyncio.sleep(0)
            cancellation.raise_if_cancelled()
            if isinstance(response, ProviderErrorCode):
                raise ChatProviderError(response)
            yield response
        except asyncio.CancelledError as exc:
            raise CancellationRequested from exc
        finally:
            cancellation.unbind_current_task()


class SlowCancellationProvider:
    def __init__(self) -> None:
        self.started = threading.Event()

    async def stream(self, _request, _cancellation):
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.25)
        yield "late synthetic output"


class GatedResponseProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.started = threading.Event()
        self.release = threading.Event()
        self.call_count = 0

    async def stream(self, _request, cancellation: CancellationToken):
        cancellation.bind_current_task()
        self.call_count += 1
        self.started.set()
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.005)
                cancellation.raise_if_cancelled()
            yield self.response
        except asyncio.CancelledError as exc:
            raise CancellationRequested from exc
        finally:
            cancellation.unbind_current_task()


def _repositories(database: SQLiteDatabase, clock: MutableClock) -> JobRepositoryBundle:
    return JobRepositoryBundle(
        conversations=ConversationStore(database, clock=clock),
        jobs=BackgroundJobStore(database, clock=clock),
        memories=MemoryStore(database, clock=clock),
    )


def _seed(path: Path, clock: MutableClock, operation) -> object:
    database = SQLiteDatabase(path).open()
    try:
        return operation(_repositories(database, clock))
    finally:
        database.close()


def _start(
    qtbot,
    path: Path,
    clock: MutableClock,
    provider,
    *,
    memory_enabled: bool = True,
) -> tuple[SerialDataThread, BackgroundGenerationRunner, MemoryJobCoordinator]:
    def factory() -> _Resource:
        database = SQLiteDatabase(path).open()
        return _Resource(database, _repositories(database, clock))

    runtime = SerialDataThread(
        factory,
        resource_close=lambda resource: resource.database.close(),
    )
    runtime.start()
    qtbot.waitUntil(lambda: runtime.is_ready)
    runner = BackgroundGenerationRunner(provider)
    coordinator = MemoryJobCoordinator(
        runtime,
        runner,
        repositories=lambda resource: resource.repositories,
        memory_enabled=memory_enabled,
        clock=clock,
        poll_interval_ms=60_000,
    )
    coordinator.start()
    return runtime, runner, coordinator


def _stop(
    qtbot,
    runtime: SerialDataThread,
    runner: BackgroundGenerationRunner,
    coordinator: MemoryJobCoordinator,
) -> None:
    assert coordinator.shutdown(1_000)
    qtbot.waitUntil(lambda: not runner.is_running)
    assert runtime.shutdown(1_000)


def _job_row(path: Path, dedupe_key: str) -> tuple[str, int, str, str | None]:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT status, attempt_count, run_after, last_error_code
            FROM background_jobs WHERE dedupe_key = ?
            """,
            (dedupe_key,),
        ).fetchone()
    assert row is not None
    return row


def _job_status(path: Path, dedupe_key: str) -> str:
    return _job_row(path, dedupe_key)[0]


def _valid_candidate(source_message_id: str, content: str = "用户喜欢咖啡") -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "type": "preference",
                    "operation": "add",
                    "content": content,
                    "topic_key": "饮料 咖啡",
                    "importance": 0.7,
                    "confidence": 0.9,
                    "source_message_ids": [source_message_id],
                }
            ]
        },
        ensure_ascii=False,
    )


def _seed_extraction(
    repositories: JobRepositoryBundle,
    *,
    dedupe_key: str = "extract:turn-1",
    text: str = "我喜欢咖啡",
) -> tuple[str, str]:
    conversation = repositories.conversations.create_conversation()
    source = repositories.conversations.save_user_message(
        conversation.conversation_id,
        "turn-1",
        "user-1",
        text,
    )
    repositories.jobs.enqueue(
        MEMORY_EXTRACTION_JOB_KIND,
        dedupe_key,
        profile_id=conversation.profile_id,
        conversation_id=conversation.conversation_id,
        message_id=source.message_id,
        payload={
            "source_message_ids": [source.message_id],
            "turn_id": source.turn_id,
            "terminal_reason": TurnTerminalReason.COMPLETED.value,
        },
    )
    return conversation.conversation_id, source.message_id


def test_trigger_filters_and_summary_thresholds() -> None:
    assert extraction_terminal_is_eligible(TurnTerminalReason.COMPLETED)
    assert extraction_terminal_is_eligible(TurnTerminalReason.USER_STOPPED)
    assert not extraction_terminal_is_eligible(TurnTerminalReason.PROVIDER_ERROR)
    assert not extraction_terminal_is_eligible(TurnTerminalReason.SHUTDOWN)
    assert not extraction_terminal_is_eligible(TurnTerminalReason.COMPLETED, memory_enabled=False)
    assert summary_is_due(SummaryProgress(12, 10, 12))
    assert summary_is_due(SummaryProgress(1, 6_000, 1))
    assert not summary_is_due(SummaryProgress(11, 5_999, 11))


def test_pause_resume_before_start_does_not_leave_scheduler_or_runner_paused() -> None:
    runtime = SerialDataThread(lambda: object())
    runner = BackgroundGenerationRunner(ScriptedChatProvider())
    coordinator = MemoryJobCoordinator(
        runtime,
        runner,
        repositories=lambda _resource: (_ for _ in ()).throw(AssertionError),
    )

    assert coordinator.pause()
    assert coordinator.is_paused
    assert runner.is_paused

    coordinator.resume()

    assert not coordinator.is_paused
    assert not runner.is_paused


def test_foreground_preemption_of_slow_provider_never_blocks_qt(qtbot) -> None:
    provider = SlowCancellationProvider()
    runtime = SerialDataThread(lambda: object())
    runner = BackgroundGenerationRunner(provider)
    coordinator = MemoryJobCoordinator(
        runtime,
        runner,
        repositories=lambda _resource: (_ for _ in ()).throw(AssertionError),
    )
    heartbeats = 0
    timer = QTimer()
    timer.setInterval(10)

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    timer.timeout.connect(heartbeat)
    request = ChatRequest(
        request_id="foreground-preemption-test",
        turn_id="foreground-preemption-test",
        attempt=1,
        messages=(PromptMessage(PromptRole.USER, "synthetic"),),
    )
    try:
        assert runner.start(
            request,
            on_success=lambda _content: None,
            on_failure=lambda _category: None,
        )
        qtbot.waitUntil(provider.started.is_set, timeout=1_000)
        timer.start()

        started = time.perf_counter()
        coordinator.set_foreground_active(True)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.1
        qtbot.waitUntil(lambda: not runner.is_running, timeout=1_000)
        assert heartbeats >= 5
    finally:
        timer.stop()
        coordinator.shutdown(1_000)


def test_summary_claim_excludes_unknown_kind_and_persists_only_on_success(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 8, tzinfo=UTC))

    def seed(repositories: JobRepositoryBundle) -> str:
        conversation = repositories.conversations.create_conversation()
        for index in range(12):
            repositories.conversations.save_user_message(
                conversation.conversation_id,
                f"turn-{index}",
                f"user-{index}",
                f"合成消息 {index}",
            )
        repositories.jobs.enqueue(
            SUMMARY_JOB_KIND,
            "summary:conversation",
            conversation_id=conversation.conversation_id,
            profile_id=conversation.profile_id,
            payload={"after_sequence": 0},
        )
        repositories.jobs.enqueue("not-a-p5a-job", "unknown:1")
        return conversation.conversation_id

    conversation_id = _seed(path, clock, seed)
    provider = SequenceProvider(["稳定摘要"])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(
            lambda: _job_status(path, "summary:conversation") == "completed",
            timeout=3_000,
        )
        assert _job_status(path, "unknown:1") == "pending"
        assert provider.requests[0].options.purpose is GenerationPurpose.CONVERSATION_SUMMARY
        assert provider.requests[0].options.temperature == 0.2
        assert provider.requests[0].options.max_output_tokens == 1_200
        with sqlite3.connect(path) as connection:
            summary = connection.execute(
                """
                SELECT content, message_count, covers_through_sequence
                FROM conversation_summaries WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
        assert summary == ("稳定摘要", 12, 12)
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_extraction_repairs_strict_json_once_then_upserts_memory(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 9, tzinfo=UTC))
    _conversation_id, source_id = _seed(path, clock, _seed_extraction)
    provider = SequenceProvider(["```json\n{}\n```", _valid_candidate(source_id)])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(
            lambda: _job_status(path, "extract:turn-1") == "completed",
            timeout=3_000,
        )
        assert [request.options.purpose for request in provider.requests] == [
            GenerationPurpose.MEMORY_EXTRACTION,
            GenerationPurpose.STRUCTURE_REPAIR,
        ]
        with sqlite3.connect(path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM memory_groups").fetchone()[0]
            source_count = connection.execute(
                "SELECT COUNT(*) FROM memory_sources WHERE source_message_id = ?",
                (source_id,),
            ).fetchone()[0]
        assert count == 1
        assert source_count == 1
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_second_invalid_structure_retries_without_a_second_repair(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 9, 30, tzinfo=UTC))
    _seed(path, clock, _seed_extraction)
    provider = SequenceProvider(["not json", "still not json"])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(lambda: _job_status(path, "extract:turn-1") == "retry", timeout=3_000)
        assert [request.options.purpose for request in provider.requests] == [
            GenerationPurpose.MEMORY_EXTRACTION,
            GenerationPurpose.STRUCTURE_REPAIR,
        ]
        assert _job_row(path, "extract:turn-1")[1] == 1
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_groups").fetchone()[0] == 0
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_summary_provider_failure_keeps_previous_summary(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 9, 45, tzinfo=UTC))

    def seed(repositories: JobRepositoryBundle) -> None:
        conversation = repositories.conversations.create_conversation()
        first = repositories.conversations.save_user_message(
            conversation.conversation_id, "turn-0", "user-0", "旧消息"
        )
        repositories.conversations.save_summary(
            conversation.conversation_id,
            "仍然有效的旧摘要",
            first.sequence,
            message_count=1,
            character_count=3,
        )
        for index in range(1, 13):
            repositories.conversations.save_user_message(
                conversation.conversation_id,
                f"turn-{index}",
                f"user-{index}",
                f"新消息 {index}",
            )
        repositories.jobs.enqueue(
            SUMMARY_JOB_KIND,
            "summary:keep-old",
            profile_id=conversation.profile_id,
            conversation_id=conversation.conversation_id,
            payload={"after_sequence": first.sequence},
        )

    _seed(path, clock, seed)
    provider = SequenceProvider([ProviderErrorCode.NETWORK])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(lambda: _job_status(path, "summary:keep-old") == "retry", timeout=3_000)
        with sqlite3.connect(path) as connection:
            summaries = connection.execute(
                "SELECT content FROM conversation_summaries ORDER BY covers_through_sequence"
            ).fetchall()
        assert summaries == [("仍然有效的旧摘要",)]
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_provider_failures_retry_after_one_and_ten_minutes_then_fail(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 10, tzinfo=UTC))
    _seed(path, clock, _seed_extraction)
    provider = SequenceProvider(
        [ProviderErrorCode.NETWORK, ProviderErrorCode.NETWORK, ProviderErrorCode.NETWORK]
    )
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(lambda: _job_status(path, "extract:turn-1") == "retry", timeout=3_000)
        first = _job_row(path, "extract:turn-1")
        assert first[1] == 1
        assert datetime.fromisoformat(first[2].replace("Z", "+00:00")) == clock() + timedelta(
            minutes=1
        )

        clock.advance(seconds=59)
        coordinator.poll()
        qtbot.wait(30)
        assert provider.call_count == 1

        clock.advance(seconds=1)
        coordinator.poll()
        qtbot.waitUntil(
            lambda: (
                _job_row(path, "extract:turn-1")[1] == 2
                and _job_status(path, "extract:turn-1") == "retry"
            ),
            timeout=3_000,
        )
        second = _job_row(path, "extract:turn-1")
        assert datetime.fromisoformat(second[2].replace("Z", "+00:00")) == clock() + timedelta(
            minutes=10
        )

        clock.advance(minutes=10)
        coordinator.poll()
        qtbot.waitUntil(lambda: _job_status(path, "extract:turn-1") == "failed", timeout=3_000)
        final = _job_row(path, "extract:turn-1")
        assert final[1] == 3
        assert final[3] == "provider_network"
        assert provider.call_count == 3
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_memory_disabled_leaves_extraction_pending_but_runs_summary(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 11, tzinfo=UTC))

    def seed(repositories: JobRepositoryBundle) -> str:
        conversation_id, source_id = _seed_extraction(repositories)
        for index in range(1, 12):
            repositories.conversations.save_user_message(
                conversation_id,
                f"summary-turn-{index}",
                f"summary-user-{index}",
                f"摘要合成消息 {index}",
            )
        repositories.jobs.enqueue(
            SUMMARY_JOB_KIND,
            "summary:disabled-memory",
            profile_id="default",
            conversation_id=conversation_id,
            payload={"after_sequence": 0},
        )
        return source_id

    source_id = _seed(path, clock, seed)
    provider = SequenceProvider(["摘要仍运行", _valid_candidate(source_id)])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider, memory_enabled=False)
    try:
        qtbot.waitUntil(
            lambda: _job_status(path, "summary:disabled-memory") == "completed",
            timeout=3_000,
        )
        assert _job_status(path, "extract:turn-1") == "pending"
        assert provider.call_count == 1

        coordinator.set_memory_enabled(True)
        qtbot.waitUntil(
            lambda: _job_status(path, "extract:turn-1") == "completed",
            timeout=3_000,
        )
        assert provider.call_count == 2
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_exact_repeated_message_adds_provenance_without_provider_call(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 11, 10, tzinfo=UTC))

    def seed(repositories: JobRepositoryBundle) -> None:
        conversation = repositories.conversations.create_conversation()
        first = repositories.conversations.save_user_message(
            conversation.conversation_id,
            "turn-first",
            "user-first",
            "我更喜欢无糖咖啡",
        )
        repositories.memories.create_memory(
            "preference",
            "合成饮料偏好",
            "用户更喜欢无糖咖啡",
            source_message_ids=(first.message_id,),
        )
        repeated = repositories.conversations.save_user_message(
            conversation.conversation_id,
            "turn-repeat",
            "user-repeat",
            "我更喜欢无糖咖啡",
        )
        repositories.jobs.enqueue(
            MEMORY_EXTRACTION_JOB_KIND,
            "extract:exact-repeat",
            profile_id=conversation.profile_id,
            conversation_id=conversation.conversation_id,
            message_id=repeated.message_id,
            payload={
                "source_message_ids": [repeated.message_id],
                "turn_id": repeated.turn_id,
                "terminal_reason": TurnTerminalReason.COMPLETED.value,
            },
        )

    _seed(path, clock, seed)
    provider = SequenceProvider(["provider must not be called"])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(
            lambda: _job_status(path, "extract:exact-repeat") == "completed",
            timeout=3_000,
        )
        assert provider.call_count == 0
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_groups").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 2
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_disabling_memory_drops_candidates_waiting_for_persistence(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 11, 15, tzinfo=UTC))
    _conversation_id, source_id = _seed(path, clock, _seed_extraction)
    provider = GatedResponseProvider(_valid_candidate(source_id))
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    data_blocked = threading.Event()
    release_data = threading.Event()

    def block_data_thread(_resource: object) -> None:
        data_blocked.set()
        release_data.wait(timeout=3)

    try:
        qtbot.waitUntil(provider.started.is_set, timeout=3_000)
        assert runtime.submit(block_data_thread) is not None
        qtbot.waitUntil(data_blocked.is_set, timeout=3_000)

        provider.release.set()
        qtbot.waitUntil(
            lambda: (
                not runner.is_running
                and coordinator._current is not None
                and coordinator._current.finishing
            ),
            timeout=3_000,
        )
        coordinator.set_memory_enabled(False)
        release_data.set()

        qtbot.waitUntil(
            lambda: _job_status(path, "extract:turn-1") == "completed",
            timeout=3_000,
        )
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_groups").fetchone()[0] == 0
        assert provider.call_count == 1
    finally:
        provider.release.set()
        release_data.set()
        _stop(qtbot, runtime, runner, coordinator)


def test_stale_summary_job_does_not_summarize_a_subthreshold_tail(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 11, 30, tzinfo=UTC))

    def seed(repositories: JobRepositoryBundle) -> None:
        conversation = repositories.conversations.create_conversation()
        for index in range(12):
            repositories.conversations.save_user_message(
                conversation.conversation_id,
                f"turn-{index}",
                f"user-{index}",
                f"已摘要消息 {index}",
            )
        repositories.conversations.save_summary(
            conversation.conversation_id,
            "现有摘要",
            12,
            message_count=12,
            character_count=60,
        )
        for index in range(12, 14):
            repositories.conversations.save_user_message(
                conversation.conversation_id,
                f"turn-{index}",
                f"user-{index}",
                f"尾部消息 {index}",
            )
        repositories.jobs.enqueue(
            SUMMARY_JOB_KIND,
            "summary:stale-tail",
            profile_id=conversation.profile_id,
            conversation_id=conversation.conversation_id,
            payload={"after_sequence": 0},
        )

    _seed(path, clock, seed)
    provider = SequenceProvider(["不应使用"])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(
            lambda: _job_status(path, "summary:stale-tail") == "completed",
            timeout=3_000,
        )
        assert provider.call_count == 0
        with sqlite3.connect(path) as connection:
            summaries = connection.execute(
                "SELECT content FROM conversation_summaries ORDER BY covers_through_sequence"
            ).fetchall()
        assert summaries == [("现有摘要",)]
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_foreground_preemption_persists_retry_and_restart_completes_once(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 1, 12, tzinfo=UTC))
    _conversation_id, source_id = _seed(path, clock, _seed_extraction)
    blocked = ScriptedChatProvider(
        ScriptedScenario.NEVER,
        chunks=("不会返回",),
        first_delay_ms=0,
    )
    first_runtime, first_runner, first_coordinator = _start(qtbot, path, clock, blocked)
    try:
        qtbot.waitUntil(lambda: blocked.call_count == 1, timeout=3_000)
        first_coordinator.set_foreground_active(True)
        qtbot.waitUntil(lambda: _job_status(path, "extract:turn-1") == "retry", timeout=3_000)
        assert _job_row(path, "extract:turn-1")[1] == 1
    finally:
        _stop(qtbot, first_runtime, first_runner, first_coordinator)

    provider = SequenceProvider([_valid_candidate(source_id)])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(
            lambda: _job_status(path, "extract:turn-1") == "completed",
            timeout=3_000,
        )
        assert _job_row(path, "extract:turn-1")[1] == 2
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_groups").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 1
    finally:
        _stop(qtbot, runtime, runner, coordinator)
