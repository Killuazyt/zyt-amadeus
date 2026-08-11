from __future__ import annotations

import math
import sqlite3

import pytest

from amadeus_desktop import vector_store as vector_store_module
from amadeus_desktop.conversation_store import ConversationStore
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.deep_memory_store import DeepMemoryStore
from amadeus_desktop.memory_models import MemoryLayer
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.persona_repository import PersonaRepository
from amadeus_desktop.storage_models import (
    EmbeddingGenerationStatus,
    MemoryVersionOrigin,
    StorageValidationError,
)
from amadeus_desktop.vector_store import VECTOR_BLOB_BYTES, VectorStore

MODEL = {
    "model_name": "BAAI/bge-small-zh-v1.5",
    "model_commit": "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59",
    "model_sha256": "a" * 64,
    "calibration_threshold": 0.6,
}


def _vector(seed: float = 1.0) -> tuple[float, ...]:
    return tuple(seed + index / 1_000 for index in range(512))


def test_memory_generation_is_normalized_validated_and_switched_atomically(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    memory = MemoryStore(database)
    vectors = VectorStore(database)
    try:
        record = memory.create_memory(
            "fact",
            "城市",
            "用户住在上海",
            origin=MemoryVersionOrigin.MANUAL,
        )
        first = vectors.begin_memory_generation(generation_id="g1", **MODEL)
        assert first.status is EmbeddingGenerationStatus.BUILDING
        active = vectors.activate_memory_generation(
            first.generation_id,
            {record.current_version.version_id: _vector()},
        )
        assert active.status is EmbeddingGenerationStatus.ACTIVE
        assert active.item_count == 1
        loaded = vectors.load_memory_vectors()
        assert len(loaded) == 1
        assert len(loaded[0].vector) == 512
        assert math.isclose(sum(value * value for value in loaded[0].vector), 1.0, rel_tol=1e-4)
        assert (
            database.connection.execute(
                "SELECT length(vector_blob) FROM memory_vectors"
            ).fetchone()[0]
            == VECTOR_BLOB_BYTES
        )

        second = vectors.begin_memory_generation(generation_id="g2", **MODEL)
        with pytest.raises(StorageValidationError, match="exactly match"):
            vectors.activate_memory_generation(second.generation_id, {})
        assert vectors.get_active_memory_generation().generation_id == "g1"
        failed = vectors.fail_memory_generation(second.generation_id, "injected_failure")
        assert failed.status is EmbeddingGenerationStatus.FAILED
        assert vectors.get_active_memory_generation().generation_id == "g1"
    finally:
        database.close()


def test_corrupt_or_non_finite_vector_blob_is_rejected(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    memory = MemoryStore(database)
    vectors = VectorStore(database)
    try:
        record = memory.create_memory(
            "fact",
            "城市",
            "用户住在上海",
            origin=MemoryVersionOrigin.MANUAL,
        )
        generation = vectors.begin_memory_generation(generation_id="g", **MODEL)
        vectors.activate_memory_generation(
            generation.generation_id,
            {record.current_version.version_id: _vector()},
        )
        with database.transaction() as connection:
            connection.execute(
                "UPDATE memory_vectors SET vector_blob = ?",
                (b"broken",),
            )
        with pytest.raises(StorageValidationError, match="invalid length"):
            vectors.load_memory_vectors()

        with pytest.raises(StorageValidationError, match="finite"):
            vectors.upsert_memory_vector(
                generation.generation_id,
                record.current_version.version_id,
                (float("nan"),) + (0.0,) * 511,
            )
    finally:
        database.close()


def test_memory_and_persona_generations_are_physically_isolated(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    memory = MemoryStore(database)
    persona = PersonaRepository(database)
    vectors = VectorStore(database)
    try:
        record = memory.create_memory(
            "fact",
            "城市",
            "用户住在上海",
            origin=MemoryVersionOrigin.MANUAL,
        )
        knowledge = persona.upsert_knowledge(
            "kurisu",
            "角色在研究所工作。",
            tags=("背景",),
            source_ref="synthetic:test",
            source_hash="b" * 64,
        )
        memory_generation = vectors.begin_memory_generation(generation_id="gm", **MODEL)
        persona_generation = vectors.begin_persona_generation(
            persona_id="kurisu", generation_id="gp", **MODEL
        )
        vectors.activate_memory_generation(
            memory_generation.generation_id,
            {record.current_version.version_id: _vector()},
        )
        vectors.activate_persona_generation(
            persona_generation.generation_id,
            {knowledge.knowledge_id: _vector(2.0)},
        )

        assert [item.target_id for item in vectors.load_memory_vectors()] == [
            record.current_version.version_id
        ]
        assert [item.target_id for item in vectors.load_persona_vectors("kurisu")] == [
            knowledge.knowledge_id
        ]
        memory_count = database.connection.execute(
            "SELECT COUNT(*) FROM memory_vectors"
        ).fetchone()[0]
        persona_count = database.connection.execute(
            "SELECT COUNT(*) FROM persona_vectors"
        ).fetchone()[0]
        assert memory_count == persona_count == 1
    finally:
        database.close()


def test_reflection_and_persona_impression_vectors_are_independent_corpora(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    conversations = ConversationStore(database)
    memory = MemoryStore(database)
    deep = DeepMemoryStore(database)
    vectors = VectorStore(database)
    try:
        conversation = conversations.create_conversation()
        fact_ids: list[str] = []
        for index in range(5):
            message = conversations.save_user_message(
                conversation.conversation_id,
                f"turn-{index}",
                f"user-{index}",
                f"我第 {index + 1} 次表示重视稳定互动",
            )
            fact = memory.create_memory(
                "relationship",
                f"稳定互动:{index}",
                f"用户第 {index + 1} 次表示重视稳定互动",
                source_message_ids=(message.message_id,),
            )
            fact_ids.append(fact.current_version.version_id)
        active_reflection = deep.create_reflection(
            "用户重视稳定互动",
            "稳定互动",
            fact_version_ids=fact_ids,
            importance=0.8,
        )
        active_reflection = deep.confirm(MemoryLayer.REFLECTION, active_reflection.group_id)
        promoted_reflection = deep.create_reflection(
            "关系信任来自长期可预期交流",
            "关系信任",
            fact_version_ids=fact_ids,
            importance=1.0,
        )
        deep.confirm(MemoryLayer.REFLECTION, promoted_reflection.group_id)
        deep.confirm(MemoryLayer.REFLECTION, promoted_reflection.group_id)
        impression = deep.promote_reflection(promoted_reflection.group_id)

        reflection_generation = vectors.begin_reflection_generation(
            generation_id="reflection-generation",
            **MODEL,
        )
        impression_generation = vectors.begin_persona_impression_generation(
            generation_id="impression-generation",
            **MODEL,
        )
        vectors.activate_reflection_generation(
            reflection_generation.generation_id,
            {active_reflection.current_version.version_id: _vector(3.0)},
        )
        vectors.activate_persona_impression_generation(
            impression_generation.generation_id,
            {impression.current_version.version_id: _vector(4.0)},
        )

        assert [item.target_id for item in vectors.load_reflection_vectors()] == [
            active_reflection.current_version.version_id
        ]
        assert [item.target_id for item in vectors.load_persona_impression_vectors()] == [
            impression.current_version.version_id
        ]
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM memory_reflection_vectors"
            ).fetchone()[0]
            == 1
        )
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM memory_persona_impression_vectors"
            ).fetchone()[0]
            == 1
        )
        static_count = database.connection.execute(
            "SELECT COUNT(*) FROM persona_vectors"
        ).fetchone()[0]
        assert static_count == 0
    finally:
        database.close()


def test_interrupted_builds_become_retryable_failures_without_moving_active(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    memory = MemoryStore(database)
    persona = PersonaRepository(database)
    vectors = VectorStore(database)
    try:
        record = memory.create_memory(
            "fact",
            "城市",
            "用户住在上海",
            origin=MemoryVersionOrigin.MANUAL,
        )
        knowledge = persona.upsert_knowledge(
            "kurisu",
            "角色在研究所工作。",
            tags=("背景",),
            source_ref="synthetic:test",
            source_hash="c" * 64,
        )
        active_user = vectors.begin_memory_generation(generation_id="active-u", **MODEL)
        active_persona = vectors.begin_persona_generation(
            persona_id="kurisu", generation_id="active-p", **MODEL
        )
        vectors.activate_memory_generation(
            active_user.generation_id,
            {record.current_version.version_id: _vector()},
        )
        vectors.activate_persona_generation(
            active_persona.generation_id,
            {knowledge.knowledge_id: _vector()},
        )
        interrupted_user = vectors.begin_memory_generation(generation_id="interrupted-u", **MODEL)
        interrupted_persona = vectors.begin_persona_generation(
            persona_id="kurisu", generation_id="interrupted-p", **MODEL
        )

        assert vectors.recover_interrupted_generations() == (1, 0, 0, 1)
        assert (
            vectors.get_memory_generation(interrupted_user.generation_id).status
            is EmbeddingGenerationStatus.FAILED
        )
        assert (
            vectors.get_persona_generation(interrupted_persona.generation_id).status
            is EmbeddingGenerationStatus.FAILED
        )
        assert vectors.get_active_memory_generation().generation_id == "active-u"
        assert vectors.get_active_persona_generation("kurisu").generation_id == "active-p"
        assert vectors.recover_interrupted_generations() == (0, 0, 0, 0)
    finally:
        database.close()


def test_dual_corpus_activation_rolls_back_both_switches_on_failure(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    memory = MemoryStore(database)
    persona = PersonaRepository(database)
    vectors = VectorStore(database)
    try:
        record = memory.create_memory(
            "fact",
            "城市",
            "用户住在上海",
            origin=MemoryVersionOrigin.MANUAL,
        )
        knowledge = persona.upsert_knowledge(
            "kurisu",
            "角色在研究所工作。",
            tags=("背景",),
            source_ref="synthetic:test",
            source_hash="d" * 64,
        )
        old_user = vectors.begin_memory_generation(generation_id="old-u", **MODEL)
        old_persona = vectors.begin_persona_generation(
            persona_id="kurisu", generation_id="old-p", **MODEL
        )
        vectors.activate_generations_atomically(
            old_user.generation_id,
            {record.current_version.version_id: _vector()},
            old_persona.generation_id,
            {knowledge.knowledge_id: _vector()},
        )
        new_user = vectors.begin_memory_generation(generation_id="new-u", **MODEL)
        new_persona = vectors.begin_persona_generation(
            persona_id="kurisu", generation_id="new-p", **MODEL
        )
        database.connection.execute(
            """
            CREATE TRIGGER inject_persona_activation_failure
            BEFORE UPDATE ON persona_embedding_generations
            WHEN NEW.id = 'new-p' AND NEW.status = 'active'
            BEGIN
                SELECT RAISE(ABORT, 'injected activation failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected activation failure"):
            vectors.activate_generations_atomically(
                new_user.generation_id,
                {record.current_version.version_id: _vector(2.0)},
                new_persona.generation_id,
                {knowledge.knowledge_id: _vector(2.0)},
            )

        assert vectors.get_active_memory_generation().generation_id == "old-u"
        assert vectors.get_active_persona_generation("kurisu").generation_id == "old-p"
        assert vectors.get_memory_generation("new-u").status is EmbeddingGenerationStatus.BUILDING
        assert vectors.get_persona_generation("new-p").status is EmbeddingGenerationStatus.BUILDING
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM memory_vectors WHERE generation_id = 'new-u'"
            ).fetchone()[0]
            == 0
        )
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM persona_vectors WHERE generation_id = 'new-p'"
            ).fetchone()[0]
            == 0
        )
    finally:
        database.close()


def test_generation_activation_uses_same_bounded_document_window(tmp_path, monkeypatch) -> None:
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    memory = MemoryStore(database)
    vectors = VectorStore(database)
    try:
        for index in range(3):
            memory.create_memory(
                "fact",
                f"主题 {index}",
                f"合成记忆 {index}",
                origin=MemoryVersionOrigin.MANUAL,
                memory_id=f"memory-{index}",
            )
        selected = memory.list_active_documents(limit=2)
        monkeypatch.setattr(vector_store_module, "MAX_INDEX_DOCUMENTS", 2)
        generation = vectors.begin_memory_generation(generation_id="bounded", **MODEL)

        active = vectors.activate_memory_generation(
            generation.generation_id,
            {record.current_version.version_id: _vector() for record in selected},
        )

        assert active.item_count == 2
        assert {item.target_id for item in vectors.load_memory_vectors()} == {
            record.current_version.version_id for record in selected
        }
    finally:
        database.close()
