from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amadeus_desktop.background_generation import BackgroundGenerationRunner
from amadeus_desktop.chat_provider import (
    CancellationRequested,
    CancellationToken,
    ChatProviderError,
    ProviderErrorCode,
)
from amadeus_desktop.conversation_store import BackgroundJobStore, ConversationStore
from amadeus_desktop.data_runtime import SerialDataThread
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.deep_memory_coordinator import (
    DEEP_MEMORY_CYCLE_JOB_KIND,
    DeepMemoryJobCoordinator,
    DeepMemoryRepositoryBundle,
)
from amadeus_desktop.deep_memory_store import DeepMemoryStore
from amadeus_desktop.memory_store import MemoryStore


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class SequenceProvider:
    def __init__(self, responses: list[str | ProviderErrorCode]) -> None:
        self._responses = responses
        self._requests = []
        self._lock = threading.Lock()

    @property
    def requests(self):
        with self._lock:
            return tuple(self._requests)

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


@dataclass
class _Resource:
    database: SQLiteDatabase
    repositories: DeepMemoryRepositoryBundle

    def close(self) -> None:
        self.database.close()


def _repositories(database: SQLiteDatabase, clock: MutableClock) -> DeepMemoryRepositoryBundle:
    return DeepMemoryRepositoryBundle(
        jobs=BackgroundJobStore(database, clock=clock),
        memories=MemoryStore(database, clock=clock),
        deep_memories=DeepMemoryStore(database, clock=clock),
    )


def _seed(path: Path, clock: MutableClock) -> tuple[str, ...]:
    database = SQLiteDatabase(path).open()
    try:
        conversations = ConversationStore(database, clock=clock)
        memories = MemoryStore(database, clock=clock)
        jobs = BackgroundJobStore(database, clock=clock)
        conversation = conversations.create_conversation()
        fact_ids: list[str] = []
        for index in range(5):
            message = conversations.save_user_message(
                conversation.conversation_id,
                f"turn-{index}",
                f"user-{index}",
                f"我第 {index + 1} 次表示重视稳定互动",
            )
            fact = memories.create_memory(
                "relationship",
                f"稳定互动:{index}",
                f"用户第 {index + 1} 次表示重视稳定互动",
                source_message_ids=(message.message_id,),
            )
            fact_ids.append(fact.current_version.version_id)
        jobs.enqueue(
            DEEP_MEMORY_CYCLE_JOB_KIND,
            "deep-memory:integration",
            profile_id=conversation.profile_id,
            conversation_id=conversation.conversation_id,
            payload={"source_message_ids": [f"user-{index}" for index in range(5)]},
        )
        return tuple(fact_ids)
    finally:
        database.close()


def _response(fact_ids: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "reflections": [
                {
                    "content": "用户长期通过稳定互动建立关系信任",
                    "topic_key": "稳定互动 信任",
                    "subject_scope": "relationship",
                    "importance": 0.8,
                    "confidence": 0.85,
                    "fact_version_ids": list(fact_ids),
                }
            ]
        },
        ensure_ascii=False,
    )


def _start(qtbot, path: Path, clock: MutableClock, provider, *, enabled: bool = True):
    def factory() -> _Resource:
        database = SQLiteDatabase(path).open()
        return _Resource(database, _repositories(database, clock))

    runtime = SerialDataThread(factory, resource_close=lambda resource: resource.close())
    runtime.start()
    qtbot.waitUntil(lambda: runtime.is_ready)
    runner = BackgroundGenerationRunner(provider)
    coordinator = DeepMemoryJobCoordinator(
        runtime,
        runner,
        repositories=lambda resource: resource.repositories,
        deep_memory_enabled=enabled,
        clock=clock,
        poll_interval_ms=60_000,
    )
    coordinator.start()
    return runtime, runner, coordinator


def _stop(qtbot, runtime, runner, coordinator) -> None:
    assert coordinator.shutdown()
    qtbot.waitUntil(lambda: not runner.is_running)
    assert runtime.shutdown(2_000)


def _job_state(path: Path) -> tuple[str, int, str | None]:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT status, attempt_count, last_error_code FROM background_jobs
            WHERE dedupe_key = 'deep-memory:integration'
            """
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), row[2]


def _completed_deep_job_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM background_jobs "
                "WHERE kind = 'deep_memory_cycle' AND status = 'completed'"
            ).fetchone()[0]
        )


def test_deep_cycle_repairs_once_then_atomically_synthesizes(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 11, tzinfo=UTC))
    fact_ids = _seed(path, clock)
    provider = SequenceProvider(["invalid", _response(fact_ids)])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(lambda: _job_state(path)[0] == "completed", timeout=4_000)
        assert len(provider.requests) == 2
        assert provider.requests[0].options.purpose.value == "reflection_synthesis"
        assert provider.requests[1].options.purpose.value == "structure_repair"
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_reflections").fetchone()[0] == 1
            assert (
                connection.execute("SELECT COUNT(*) FROM memory_reflection_sources").fetchone()[0]
                == 5
            )
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_cycle_evaluates_evidence_before_followup_synthesis(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 11, tzinfo=UTC))
    database = SQLiteDatabase(path).open()
    try:
        conversations = ConversationStore(database, clock=clock)
        facts = MemoryStore(database, clock=clock)
        deep = DeepMemoryStore(database, clock=clock)
        jobs = BackgroundJobStore(database, clock=clock)
        conversation = conversations.create_conversation()
        old_message = conversations.save_user_message(
            conversation.conversation_id,
            "old-turn",
            "old-user",
            "我重视稳定互动",
        )
        old_fact = facts.create_memory(
            "relationship",
            "稳定互动:old",
            "用户重视稳定互动",
            source_message_ids=(old_message.message_id,),
        )
        existing = deep.create_reflection(
            "用户长期重视稳定互动",
            "稳定互动",
            fact_version_ids=(old_fact.current_version.version_id,),
            importance=0.7,
        )
        deep.confirm("reflection", existing.group_id)
        fact_ids: list[str] = []
        source_ids: list[str] = []
        for index in range(5):
            message = conversations.save_user_message(
                conversation.conversation_id,
                f"new-turn-{index}",
                f"new-user-{index}",
                f"我第 {index + 1} 次继续重视稳定互动",
            )
            fact = facts.create_memory(
                "relationship",
                f"稳定互动:new:{index}",
                f"用户第 {index + 1} 次继续重视稳定互动",
                source_message_ids=(message.message_id,),
            )
            fact_ids.append(fact.current_version.version_id)
            source_ids.append(message.message_id)
        jobs.enqueue(
            DEEP_MEMORY_CYCLE_JOB_KIND,
            "deep-memory:evidence-before-synthesis",
            profile_id=conversation.profile_id,
            conversation_id=conversation.conversation_id,
            payload={"source_message_ids": source_ids, "trigger": "turn"},
        )
    finally:
        database.close()

    evidence_response = json.dumps(
        {
            "signals": [
                {
                    "target_layer": "reflection",
                    "target_group_id": existing.group_id,
                    "source_fact_version_id": fact_ids[0],
                    "signal": "reinforces",
                }
            ]
        },
        ensure_ascii=False,
    )
    provider = SequenceProvider([evidence_response, _response(tuple(fact_ids))])
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(lambda: _completed_deep_job_count(path) == 2, timeout=6_000)
        assert [request.options.purpose.value for request in provider.requests] == [
            "memory_evidence",
            "reflection_synthesis",
        ]
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_reflections").fetchone()[0] == 2
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM memory_evidence_signals "
                    "WHERE signal_kind = 'indirect_support'"
                ).fetchone()[0]
                == 1
            )
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_deep_toggle_pauses_existing_jobs_without_creating_fallback(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 11, tzinfo=UTC))
    fact_ids = _seed(path, clock)
    provider = SequenceProvider([_response(fact_ids)])
    runtime, runner, coordinator = _start(
        qtbot,
        path,
        clock,
        provider,
        enabled=False,
    )
    try:
        qtbot.wait(50)
        assert provider.requests == ()
        assert _job_state(path)[0] == "pending"
        coordinator.set_deep_memory_enabled(True)
        qtbot.waitUntil(lambda: _job_state(path)[0] == "completed", timeout=4_000)
        assert len(provider.requests) == 1
        with sqlite3.connect(path) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM memory_persona_impressions").fetchone()[0]
                == 0
            )
    finally:
        _stop(qtbot, runtime, runner, coordinator)


def test_deep_provider_failure_uses_one_minute_then_ten_minute_retry(qtbot, tmp_path) -> None:
    path = tmp_path / "amadeus.sqlite3"
    clock = MutableClock(datetime(2026, 8, 11, tzinfo=UTC))
    _seed(path, clock)
    provider = SequenceProvider(
        [
            ProviderErrorCode.NETWORK,
            ProviderErrorCode.NETWORK,
            ProviderErrorCode.NETWORK,
        ]
    )
    runtime, runner, coordinator = _start(qtbot, path, clock, provider)
    try:
        qtbot.waitUntil(lambda: _job_state(path)[0] == "retry", timeout=4_000)
        assert _job_state(path)[1] == 1
        qtbot.waitUntil(lambda: not coordinator.has_active_job and not runner.is_running)
        clock.advance(seconds=60)
        coordinator.poll()
        qtbot.waitUntil(
            lambda: _job_state(path)[:2] == ("retry", 2),
            timeout=4_000,
        )
        qtbot.waitUntil(lambda: not coordinator.has_active_job and not runner.is_running)
        clock.advance(seconds=600)
        coordinator.poll()
        qtbot.waitUntil(lambda: _job_state(path)[0] == "failed", timeout=10_000)
        assert _job_state(path) == ("failed", 3, "network")
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_reflections").fetchone()[0] == 0
    finally:
        _stop(qtbot, runtime, runner, coordinator)
