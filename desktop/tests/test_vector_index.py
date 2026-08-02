from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from amadeus_desktop.data_runtime import SerialDataThread
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.embedding_backend import CPU_PROVIDER, EmbeddingUnavailableError
from amadeus_desktop.embedding_model import PINNED_MODEL
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.persona_repository import PersonaRepository
from amadeus_desktop.storage_models import MemoryVersionOrigin
from amadeus_desktop.vector_index import (
    VectorIndexCoordinator,
    VectorIndexRepositories,
)
from amadeus_desktop.vector_runtime import PriorityVectorRuntime
from amadeus_desktop.vector_store import VectorStore


def _unit_vector(index: int = 0) -> tuple[float, ...]:
    values = [0.0] * PINNED_MODEL.dimension
    values[index] = 1.0
    return tuple(values)


@dataclass(slots=True)
class _Resource:
    database: SQLiteDatabase
    memories: MemoryStore
    personas: PersonaRepository
    vectors: VectorStore
    owner_thread_id: int

    def close(self) -> None:
        self.database.close()


class _FakeBackend:
    model_name = PINNED_MODEL.api_name
    dimension = PINNED_MODEL.dimension
    provider = CPU_PROVIDER

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.thread_ids: list[int] = []
        self.first_batch_started = threading.Event()
        self.release_first_batch = threading.Event()
        self.block_first_batch = False
        self.fail_documents = False
        self.closed = False

    def embed_query(self, query: str) -> tuple[float, ...]:
        self.calls.append("query")
        self.thread_ids.append(threading.get_ident())
        return _unit_vector()

    def embed_documents(self, documents) -> tuple[tuple[float, ...], ...]:
        call_number = sum(call.startswith("batch") for call in self.calls) + 1
        self.calls.append(f"batch{call_number}")
        self.thread_ids.append(threading.get_ident())
        if self.block_first_batch and call_number == 1:
            self.first_batch_started.set()
            assert self.release_first_batch.wait(2)
        if self.fail_documents:
            raise RuntimeError("private model failure")
        return tuple(_unit_vector() for _document in documents)

    def close(self) -> None:
        self.closed = True


def _factory(path, *, seed_active: bool, memory_count: int = 2):
    def create() -> _Resource:
        database = SQLiteDatabase(path).open()
        memories = MemoryStore(database)
        personas = PersonaRepository(database)
        vectors = VectorStore(database)
        records = tuple(
            memories.create_memory(
                "fact",
                f"城市 {index}",
                f"用户在城市 {index} 居住",
                origin=MemoryVersionOrigin.MANUAL,
                memory_id=f"memory-{index}",
            )
            for index in range(memory_count)
        )
        knowledge = personas.upsert_knowledge(
            "kurisu",
            "角色在研究所进行实验。",
            tags=("背景",),
            source_ref="synthetic:test",
            source_hash="a" * 64,
            knowledge_id="persona-1",
        )
        if seed_active:
            user_generation = vectors.begin_memory_generation(
                generation_id="user-old",
                model_name=PINNED_MODEL.api_name,
                model_commit=PINNED_MODEL.revision,
                model_sha256=PINNED_MODEL.onnx_sha256,
                calibration_threshold=0.6,
            )
            persona_generation = vectors.begin_persona_generation(
                generation_id="persona-old",
                persona_id="kurisu",
                model_name=PINNED_MODEL.api_name,
                model_commit=PINNED_MODEL.revision,
                model_sha256=PINNED_MODEL.onnx_sha256,
                calibration_threshold=0.6,
            )
            vectors.activate_memory_generation(
                user_generation.generation_id,
                {record.current_version.version_id: _unit_vector() for record in records},
            )
            vectors.activate_persona_generation(
                persona_generation.generation_id,
                {knowledge.knowledge_id: _unit_vector()},
            )
        return _Resource(
            database,
            memories,
            personas,
            vectors,
            threading.get_ident(),
        )

    return create


def _repositories(resource: object) -> VectorIndexRepositories:
    assert isinstance(resource, _Resource)
    return VectorIndexRepositories(resource.memories, resource.personas, resource.vectors)


def _start_runtime(qtbot, tmp_path, *, seed_active: bool, memory_count: int = 2):
    resources: list[_Resource] = []

    def factory() -> _Resource:
        resource = _factory(
            tmp_path / "amadeus.sqlite3",
            seed_active=seed_active,
            memory_count=memory_count,
        )()
        resources.append(resource)
        return resource

    data = SerialDataThread(factory, resource_close=lambda resource: resource.close())
    vectors = PriorityVectorRuntime(thread_name="test-vector-index")
    backend = _FakeBackend()
    coordinator = VectorIndexCoordinator(data, vectors, lambda: backend, _repositories)
    data.start()
    qtbot.waitUntil(lambda: data.is_ready)
    return data, vectors, backend, coordinator, resources


def _shutdown(data, vectors, coordinator) -> None:
    coordinator.close().result(timeout=2)
    vectors.close(timeout=2)
    assert data.shutdown(2_000)


def test_start_loads_persisted_generations_then_queries_both_caches(qtbot, tmp_path) -> None:
    data, runtime, backend, coordinator, resources = _start_runtime(
        qtbot, tmp_path, seed_active=True
    )
    statuses = []
    coordinator.status_changed.connect(statuses.append)
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        assert coordinator.status.user_generation_id == "user-old"
        assert coordinator.status.user_count == 2
        assert coordinator.status.persona_generation_id == "persona-old"
        assert coordinator.status.persona_count == 1

        result = coordinator.query("上海").result(timeout=2)
        assert len(result.user_hits) == 2
        assert len(result.persona_hits) == 1
        persona_only = coordinator.query("上海", include_user=False).result(timeout=2)
        assert persona_only.user_hits == ()
        assert len(persona_only.persona_hits) == 1
        assert set(backend.thread_ids).isdisjoint({resources[0].owner_thread_id})
        assert all(
            set(status.__dataclass_fields__)
            == {
                "category",
                "available",
                "user_generation_id",
                "user_count",
                "persona_generation_id",
                "persona_count",
            }
            for status in statuses
        )
    finally:
        _shutdown(data, runtime, coordinator)


def test_rebuild_batches_yield_to_query_and_atomically_refresh_caches(qtbot, tmp_path) -> None:
    data, runtime, backend, coordinator, _resources = _start_runtime(
        qtbot, tmp_path, seed_active=False, memory_count=2
    )
    backend.block_first_batch = True
    coordinator = VectorIndexCoordinator(
        data,
        runtime,
        lambda: backend,
        _repositories,
        batch_size=1,
    )
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        assert coordinator.rebuild()
        qtbot.waitUntil(backend.first_batch_started.is_set, timeout=2_000)
        query = coordinator.query("重建时查询")
        backend.release_first_batch.set()
        assert query.result(timeout=2).user_hits == ()
        qtbot.waitUntil(
            lambda: (
                coordinator.status.category == "ready"
                and coordinator.status.user_count == 2
                and coordinator.status.persona_count == 1
            ),
            timeout=3_000,
        )
        assert backend.calls.index("query") < backend.calls.index("batch2")
        result = coordinator.query("重建后查询").result(timeout=2)
        assert len(result.user_hits) == 2
        assert len(result.persona_hits) == 1
    finally:
        _shutdown(data, runtime, coordinator)


def test_rebuild_failure_marks_new_generations_failed_and_preserves_old_cache(
    qtbot, tmp_path
) -> None:
    data, runtime, backend, coordinator, resources = _start_runtime(
        qtbot, tmp_path, seed_active=True
    )
    generation_states: list[tuple[str, str, int]] = []
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        backend.fail_documents = True
        assert coordinator.rebuild()
        qtbot.waitUntil(lambda: coordinator.status.category == "embedding_unavailable")
        assert coordinator.status.user_generation_id == "user-old"
        assert coordinator.status.persona_generation_id == "persona-old"
        with pytest.raises(EmbeddingUnavailableError):
            coordinator.query("降级").result(timeout=2)
        backend.fail_documents = False
        assert coordinator.retry_backend().result(timeout=2)
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        assert coordinator.query("显式恢复").result(timeout=2).user_hits

        def inspect(resource: _Resource):
            return tuple(
                resource.database.connection.execute(
                    """
                    SELECT status, COUNT(*)
                    FROM memory_embedding_generations
                    GROUP BY status ORDER BY status
                    """
                ).fetchall()
            )

        request_id = data.submit(
            inspect,
            on_success=lambda rows: generation_states.extend(
                (str(row[0]), "memory", int(row[1])) for row in rows
            ),
        )
        assert request_id is not None
        qtbot.waitUntil(lambda: bool(generation_states))
        assert ("active", "memory", 1) in generation_states
        assert ("failed", "memory", 1) in generation_states
        assert resources[0].owner_thread_id not in backend.thread_ids
    finally:
        _shutdown(data, runtime, coordinator)


def test_incremental_refresh_embeds_new_current_version_without_rebuilding(qtbot, tmp_path) -> None:
    data, runtime, backend, coordinator, _resources = _start_runtime(
        qtbot, tmp_path, seed_active=True, memory_count=2
    )
    completed: list[bool] = []
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        original_generation = coordinator.status.user_generation_id

        def add_memory(resource: _Resource) -> None:
            resource.memories.create_memory(
                "fact",
                "新增城市",
                "用户后来迁居到新的合成城市",
                origin=MemoryVersionOrigin.MANUAL,
                memory_id="memory-added",
            )

        assert data.submit(add_memory, on_success=lambda _value: completed.append(True))
        qtbot.waitUntil(lambda: bool(completed), timeout=2_000)
        assert coordinator.refresh_incremental()
        qtbot.waitUntil(
            lambda: coordinator.status.category == "ready" and coordinator.status.user_count == 3,
            timeout=3_000,
        )
        assert coordinator.status.user_generation_id == original_generation
        assert any(call.startswith("batch") for call in backend.calls)
        assert len(coordinator.query("迁居").result(timeout=2).user_hits) == 3
    finally:
        _shutdown(data, runtime, coordinator)


def test_retry_after_startup_failure_reloads_persisted_generations(qtbot, tmp_path) -> None:
    resources: list[_Resource] = []

    def resource_factory() -> _Resource:
        resource = _factory(
            tmp_path / "amadeus.sqlite3",
            seed_active=True,
        )()
        resources.append(resource)
        return resource

    backend = _FakeBackend()
    attempts = 0

    def backend_factory() -> _FakeBackend:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise EmbeddingUnavailableError("model_missing")
        return backend

    data = SerialDataThread(resource_factory, resource_close=lambda resource: resource.close())
    runtime = PriorityVectorRuntime(thread_name="test-vector-index-retry")
    coordinator = VectorIndexCoordinator(data, runtime, backend_factory, _repositories)
    data.start()
    qtbot.waitUntil(lambda: data.is_ready)
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "model_missing")
        assert coordinator.status.user_count == 0
        assert coordinator.status.persona_count == 0

        assert coordinator.retry_backend().result(timeout=2)
        qtbot.waitUntil(
            lambda: (
                coordinator.status.category == "ready"
                and coordinator.status.user_generation_id == "user-old"
                and coordinator.status.user_count == 2
                and coordinator.status.persona_generation_id == "persona-old"
                and coordinator.status.persona_count == 1
            ),
            timeout=3_000,
        )
        assert attempts == 2
        assert coordinator.query("显式重试后召回").result(timeout=2).user_hits
    finally:
        _shutdown(data, runtime, coordinator)


def test_removal_only_incremental_refresh_drops_stale_cache_rows(qtbot, tmp_path) -> None:
    data, runtime, _backend, coordinator, _resources = _start_runtime(
        qtbot, tmp_path, seed_active=True, memory_count=2
    )
    completed: list[bool] = []
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        assert coordinator.status.user_count == 2
        assert coordinator.status.persona_count == 1

        def deactivate(resource: _Resource) -> None:
            resource.memories.archive("memory-0")
            resource.personas.set_active("persona-1", False)

        assert data.submit(deactivate, on_success=lambda _value: completed.append(True))
        qtbot.waitUntil(lambda: bool(completed), timeout=2_000)
        assert coordinator.refresh_incremental()
        qtbot.waitUntil(
            lambda: (
                coordinator.status.category == "ready"
                and coordinator.status.user_count == 1
                and coordinator.status.persona_count == 0
            ),
            timeout=3_000,
        )
        result = coordinator.query("删除后召回").result(timeout=2)
        assert len(result.user_hits) == 1
        assert result.persona_hits == ()
    finally:
        _shutdown(data, runtime, coordinator)
