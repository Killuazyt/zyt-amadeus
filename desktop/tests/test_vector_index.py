from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from amadeus_desktop.conversation_store import ConversationStore
from amadeus_desktop.data_runtime import SerialDataThread
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.deep_memory_store import DeepMemoryStore
from amadeus_desktop.embedding_backend import CPU_PROVIDER, EmbeddingUnavailableError
from amadeus_desktop.embedding_model import PINNED_MODEL
from amadeus_desktop.memory_models import MemoryLayer
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.persona_repository import PersonaRepository
from amadeus_desktop.storage_models import MemoryVersionOrigin, PersonaKnowledgeDraft
from amadeus_desktop.vector_index import (
    VectorIndexCoordinator,
    VectorIndexRepositories,
)
from amadeus_desktop.vector_runtime import PriorityVectorRuntime, VectorCorpus
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
    deep_memories: DeepMemoryStore
    owner_thread_id: int

    def close(self) -> None:
        self.database.close()


class _FakeBackend:
    model_name = PINNED_MODEL.api_name
    dimension = PINNED_MODEL.dimension
    provider = CPU_PROVIDER

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.document_batches: list[tuple[str, ...]] = []
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
        documents = tuple(documents)
        call_number = sum(call.startswith("batch") for call in self.calls) + 1
        self.calls.append(f"batch{call_number}")
        self.document_batches.append(documents)
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
        deep_memories = DeepMemoryStore(database)
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
            reflection_generation = vectors.begin_reflection_generation(
                generation_id="reflection-old",
                model_name=PINNED_MODEL.api_name,
                model_commit=PINNED_MODEL.revision,
                model_sha256=PINNED_MODEL.onnx_sha256,
                calibration_threshold=0.6,
            )
            impression_generation = vectors.begin_persona_impression_generation(
                generation_id="impression-old",
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
            vectors.activate_reflection_generation(reflection_generation.generation_id, {})
            vectors.activate_persona_impression_generation(
                impression_generation.generation_id,
                {},
            )
        return _Resource(
            database,
            memories,
            personas,
            vectors,
            deep_memories,
            threading.get_ident(),
        )

    return create


def _repositories(resource: object) -> VectorIndexRepositories:
    assert isinstance(resource, _Resource)
    return VectorIndexRepositories(
        resource.memories,
        resource.personas,
        resource.vectors,
        resource.deep_memories,
    )


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
                "reflection_generation_id",
                "reflection_count",
                "persona_impression_generation_id",
                "persona_impression_count",
            }
            for status in statuses
        )
    finally:
        _shutdown(data, runtime, coordinator)


def test_persisted_model_mismatch_automatically_rebuilds_both_corpora(
    qtbot,
    tmp_path,
) -> None:
    data, runtime, _backend, coordinator, _resources = _start_runtime(
        qtbot,
        tmp_path,
        seed_active=True,
    )
    corrupted: list[bool] = []
    inspections: list[tuple[object, object, int, int]] = []
    categories: list[str] = []
    coordinator.status_changed.connect(lambda status: categories.append(status.category))
    try:

        def corrupt_persisted_model_identity(resource: _Resource) -> None:
            resource.database.connection.execute(
                "UPDATE memory_embedding_generations SET model_commit = ?",
                ("outdated-memory-commit",),
            )
            resource.database.connection.execute(
                "UPDATE persona_embedding_generations SET model_commit = ?",
                ("outdated-persona-commit",),
            )

        assert data.submit(
            corrupt_persisted_model_identity,
            on_success=lambda _value: corrupted.append(True),
        )
        qtbot.waitUntil(lambda: corrupted == [True], timeout=2_000)

        assert coordinator.start()
        qtbot.waitUntil(
            lambda: (
                coordinator.status.category == "ready"
                and coordinator.status.user_generation_id not in {None, "user-old"}
                and coordinator.status.user_count == 2
                and coordinator.status.persona_generation_id not in {None, "persona-old"}
                and coordinator.status.persona_count == 1
            ),
            timeout=4_000,
        )

        assert "generation_model_mismatch" in categories
        assert "rebuilding" in categories
        assert categories.index("generation_model_mismatch") < categories.index("rebuilding")
        result = coordinator.query("自动重建后召回").result(timeout=2)
        assert len(result.user_hits) == 2
        assert len(result.persona_hits) == 1

        def inspect(resource: _Resource) -> tuple[object, object, int, int]:
            user = resource.vectors.get_active_memory_generation()
            persona = resource.vectors.get_active_persona_generation("kurisu")
            memory_active = int(
                resource.database.connection.execute(
                    "SELECT COUNT(*) FROM memory_embedding_generations WHERE status = 'active'"
                ).fetchone()[0]
            )
            persona_active = int(
                resource.database.connection.execute(
                    "SELECT COUNT(*) FROM persona_embedding_generations WHERE status = 'active'"
                ).fetchone()[0]
            )
            return user, persona, memory_active, persona_active

        assert data.submit(inspect, on_success=inspections.append)
        qtbot.waitUntil(lambda: bool(inspections), timeout=2_000)
        user, persona, memory_active, persona_active = inspections[0]
        assert user is not None
        assert persona is not None
        assert user.model_commit == PINNED_MODEL.revision
        assert user.model_sha256 == PINNED_MODEL.onnx_sha256
        assert persona.model_commit == PINNED_MODEL.revision
        assert persona.model_sha256 == PINNED_MODEL.onnx_sha256
        assert memory_active == 1
        assert persona_active == 1
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


def test_persona_rebuild_switches_only_persona_generation_and_cache(qtbot, tmp_path) -> None:
    data, runtime, backend, coordinator, _resources = _start_runtime(
        qtbot,
        tmp_path,
        seed_active=True,
    )
    changed: list[bool] = []
    inspection: list[tuple[str, str, int]] = []
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        old_user_generation = coordinator.status.user_generation_id
        old_persona_generation = coordinator.status.persona_generation_id
        old_user_cache = coordinator._caches.snapshot(VectorCorpus.USER_MEMORY)
        old_persona_cache = coordinator._caches.snapshot(VectorCorpus.PERSONA_KNOWLEDGE)

        def add_persona_document(resource: _Resource) -> None:
            resource.personas.upsert_knowledge(
                "kurisu",
                "角色会在实验结束后整理研究笔记。",
                tags=("习惯",),
                source_ref="synthetic:test:updated",
                source_hash="b" * 64,
                knowledge_id="persona-2",
            )

        assert data.submit(
            add_persona_document,
            on_success=lambda _value: changed.append(True),
        )
        qtbot.waitUntil(lambda: bool(changed), timeout=2_000)

        assert coordinator.rebuild_persona()
        qtbot.waitUntil(
            lambda: (
                coordinator.status.category == "ready"
                and coordinator.status.persona_count == 2
                and coordinator.status.persona_generation_id != old_persona_generation
            ),
            timeout=3_000,
        )

        assert coordinator.status.user_generation_id == old_user_generation
        assert coordinator.status.user_count == 2
        assert coordinator._caches.snapshot(VectorCorpus.USER_MEMORY) is old_user_cache
        assert coordinator._caches.snapshot(VectorCorpus.PERSONA_KNOWLEDGE) is not old_persona_cache
        assert {document for batch in backend.document_batches for document in batch} == {
            "角色在研究所进行实验。",
            "角色会在实验结束后整理研究笔记。",
        }

        def inspect(resource: _Resource) -> tuple[str, str, int]:
            user = resource.vectors.get_active_memory_generation()
            persona = resource.vectors.get_active_persona_generation("kurisu")
            memory_generation_count = int(
                resource.database.connection.execute(
                    "SELECT COUNT(*) FROM memory_embedding_generations"
                ).fetchone()[0]
            )
            assert user is not None
            assert persona is not None
            return user.generation_id, persona.generation_id, memory_generation_count

        assert data.submit(inspect, on_success=inspection.append)
        qtbot.waitUntil(lambda: bool(inspection), timeout=2_000)
        assert inspection == [
            (
                old_user_generation,
                coordinator.status.persona_generation_id,
                1,
            )
        ]
    finally:
        _shutdown(data, runtime, coordinator)


def test_persona_rebuild_failure_preserves_old_dual_caches_and_marks_only_persona_failed(
    qtbot,
    tmp_path,
) -> None:
    data, runtime, backend, coordinator, _resources = _start_runtime(
        qtbot,
        tmp_path,
        seed_active=True,
    )
    generation_states: list[tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...]]] = []
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        old_user_cache = coordinator._caches.snapshot(VectorCorpus.USER_MEMORY)
        old_persona_cache = coordinator._caches.snapshot(VectorCorpus.PERSONA_KNOWLEDGE)
        backend.fail_documents = True

        assert coordinator.rebuild_persona()
        qtbot.waitUntil(lambda: coordinator.status.category == "embedding_unavailable")

        assert coordinator.status.user_generation_id == "user-old"
        assert coordinator.status.persona_generation_id == "persona-old"
        assert coordinator._caches.snapshot(VectorCorpus.USER_MEMORY) is old_user_cache
        assert coordinator._caches.snapshot(VectorCorpus.PERSONA_KNOWLEDGE) is old_persona_cache

        def inspect(
            resource: _Resource,
        ) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...]]:
            memory = tuple(
                (str(row[0]), int(row[1]))
                for row in resource.database.connection.execute(
                    """
                    SELECT status, COUNT(*) FROM memory_embedding_generations
                    GROUP BY status ORDER BY status
                    """
                ).fetchall()
            )
            persona = tuple(
                (str(row[0]), int(row[1]))
                for row in resource.database.connection.execute(
                    """
                    SELECT status, COUNT(*) FROM persona_embedding_generations
                    GROUP BY status ORDER BY status
                    """
                ).fetchall()
            )
            return memory, persona

        assert data.submit(inspect, on_success=generation_states.append)
        qtbot.waitUntil(lambda: bool(generation_states), timeout=2_000)
        assert generation_states == [
            (
                (("active", 1),),
                (("active", 1), ("failed", 1)),
            )
        ]

        backend.fail_documents = False
        assert coordinator.retry_backend().result(timeout=2)
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        result = coordinator.query("旧双库仍可用").result(timeout=2)
        assert len(result.user_hits) == 2
        assert len(result.persona_hits) == 1
    finally:
        _shutdown(data, runtime, coordinator)


def test_persona_rebuild_shares_gate_and_runs_pending_incremental_refresh(
    qtbot,
    tmp_path,
) -> None:
    data, runtime, backend, coordinator, _resources = _start_runtime(
        qtbot,
        tmp_path,
        seed_active=True,
    )
    backend.block_first_batch = True
    categories: list[str] = []
    coordinator.status_changed.connect(lambda status: categories.append(status.category))
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        assert coordinator.rebuild_persona()
        qtbot.waitUntil(backend.first_batch_started.is_set, timeout=2_000)

        assert coordinator.rebuild_persona()
        assert not coordinator.rebuild()
        assert not coordinator.refresh_incremental()

        backend.release_first_batch.set()
        qtbot.waitUntil(
            lambda: "incremental" in categories and coordinator.status.category == "ready",
            timeout=3_000,
        )
        rebuilding_index = categories.index("rebuilding")
        incremental_index = categories.index("incremental", rebuilding_index + 1)
        assert categories.count("rebuilding") >= 2
        assert "ready" in categories[incremental_index + 1 :]
    finally:
        _shutdown(data, runtime, coordinator)


def test_queued_persona_rebuild_activates_latest_corpus_after_first_snapshot_changes(
    qtbot,
    tmp_path,
) -> None:
    data, runtime, backend, coordinator, _resources = _start_runtime(
        qtbot,
        tmp_path,
        seed_active=True,
    )
    backend.block_first_batch = True
    replaced: list[tuple[str, ...]] = []
    inspections: list[tuple[tuple[str, ...], tuple[tuple[str, int], ...]]] = []
    categories: list[str] = []
    coordinator.status_changed.connect(lambda status: categories.append(status.category))
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        old_user_generation = coordinator.status.user_generation_id
        old_user_cache = coordinator._caches.snapshot(VectorCorpus.USER_MEMORY)

        assert coordinator.rebuild_persona()
        qtbot.waitUntil(backend.first_batch_started.is_set, timeout=2_000)

        def replace_persona(resource: _Resource) -> tuple[str, ...]:
            records = resource.personas.replace_persona(
                "kurisu",
                (
                    PersonaKnowledgeDraft(
                        content="这是并发替换后的最新角色知识。",
                        tags=("最新",),
                        source_ref="synthetic:test:latest",
                        source_hash="c" * 64,
                        knowledge_id="persona-latest",
                    ),
                ),
            )
            return tuple(record.knowledge_id for record in records)

        assert data.submit(replace_persona, on_success=replaced.append)
        qtbot.waitUntil(lambda: replaced == [("persona-latest",)], timeout=2_000)
        assert coordinator.rebuild_persona()

        backend.release_first_batch.set()
        qtbot.waitUntil(
            lambda: (
                coordinator.status.category == "ready"
                and coordinator.status.persona_count == 1
                and backend.document_batches[-1:] == [("这是并发替换后的最新角色知识。",)]
            ),
            timeout=4_000,
        )

        assert categories.count("rebuilding") >= 2
        assert coordinator.status.user_generation_id == old_user_generation
        assert coordinator._caches.snapshot(VectorCorpus.USER_MEMORY) is old_user_cache
        result = coordinator.query("最新角色知识", include_user=False).result(timeout=2)
        assert tuple(hit.target_id for hit in result.persona_hits) == ("persona-latest",)

        def inspect(
            resource: _Resource,
        ) -> tuple[tuple[str, ...], tuple[tuple[str, int], ...]]:
            active_ids = tuple(
                document.knowledge_id
                for document in resource.personas.list_active_documents("kurisu")
            )
            states = tuple(
                (str(row[0]), int(row[1]))
                for row in resource.database.connection.execute(
                    """
                    SELECT status, COUNT(*) FROM persona_embedding_generations
                    GROUP BY status ORDER BY status
                    """
                ).fetchall()
            )
            return active_ids, states

        assert data.submit(inspect, on_success=inspections.append)
        qtbot.waitUntil(lambda: bool(inspections), timeout=2_000)
        assert inspections[0][0] == ("persona-latest",)
        assert ("active", 1) in inspections[0][1]
        assert ("failed", 1) in inspections[0][1]
    finally:
        backend.release_first_batch.set()
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


def test_incremental_refresh_queries_reflection_and_impression_caches(qtbot, tmp_path) -> None:
    data, runtime, _backend, coordinator, _resources = _start_runtime(
        qtbot,
        tmp_path,
        seed_active=True,
        memory_count=0,
    )
    seeded: list[tuple[str, str]] = []
    try:
        assert coordinator.start()
        qtbot.waitUntil(lambda: coordinator.status.category == "ready")
        reflection_generation = coordinator.status.reflection_generation_id
        impression_generation = coordinator.status.persona_impression_generation_id

        def seed_derived(resource: _Resource) -> tuple[str, str]:
            conversations = ConversationStore(resource.database)
            conversation = conversations.create_conversation()
            fact_ids: list[str] = []
            for index in range(5):
                message = conversations.save_user_message(
                    conversation.conversation_id,
                    f"deep-turn-{index}",
                    f"deep-user-{index}",
                    f"我第 {index + 1} 次表示重视稳定互动",
                )
                fact = resource.memories.create_memory(
                    "relationship",
                    f"稳定互动:{index}",
                    f"用户第 {index + 1} 次表示重视稳定互动",
                    source_message_ids=(message.message_id,),
                )
                fact_ids.append(fact.current_version.version_id)
            reflection = resource.deep_memories.create_reflection(
                "用户重视稳定互动",
                "稳定互动",
                fact_version_ids=fact_ids,
                importance=0.8,
            )
            reflection = resource.deep_memories.confirm(
                MemoryLayer.REFLECTION,
                reflection.group_id,
            )
            promotable = resource.deep_memories.create_reflection(
                "关系信任来自稳定互动",
                "关系信任",
                fact_version_ids=fact_ids,
                importance=1.0,
            )
            resource.deep_memories.confirm(MemoryLayer.REFLECTION, promotable.group_id)
            resource.deep_memories.confirm(MemoryLayer.REFLECTION, promotable.group_id)
            impression = resource.deep_memories.promote_reflection(promotable.group_id)
            return (
                reflection.current_version.version_id,
                impression.current_version.version_id,
            )

        assert data.submit(seed_derived, on_success=seeded.append)
        qtbot.waitUntil(lambda: bool(seeded), timeout=2_000)
        assert coordinator.refresh_incremental()
        qtbot.waitUntil(
            lambda: (
                coordinator.status.category == "ready"
                and coordinator.status.reflection_count == 1
                and coordinator.status.persona_impression_count == 1
            ),
            timeout=4_000,
        )
        assert coordinator.status.reflection_generation_id == reflection_generation
        assert coordinator.status.persona_impression_generation_id == impression_generation
        result = coordinator.query("稳定互动").result(timeout=2)
        assert tuple(hit.target_id for hit in result.reflection_hits) == (seeded[0][0],)
        assert tuple(hit.target_id for hit in result.persona_impression_hits) == (seeded[0][1],)
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
