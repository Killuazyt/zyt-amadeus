from __future__ import annotations

import pytest

from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.persona_repository import PersonaRepository
from amadeus_desktop.storage_models import PersonaKnowledgeDraft, StorageValidationError


def test_persona_replace_fts_revalidation_and_recall_are_isolated(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    persona = PersonaRepository(database)
    try:
        first = PersonaKnowledgeDraft(
            content="角色喜欢进行脑科学实验。",
            tags=("研究", "实验"),
            source_ref="synthetic:first",
            source_hash="a" * 64,
            knowledge_id="k1",
        )
        second = PersonaKnowledgeDraft(
            content="角色习惯喝无糖红茶。",
            tags=("偏好",),
            source_ref="synthetic:second",
            source_hash="b" * 64,
            knowledge_id="k2",
        )
        imported_ids = {
            item.knowledge_id for item in persona.replace_persona("kurisu", (first, second))
        }
        assert imported_ids == {
            "k1",
            "k2",
        }
        assert persona.search("kurisu", "红茶")[0].knowledge.knowledge_id == "k2"
        injected = persona.search("kurisu", '红茶") OR knowledge_id:*')
        assert injected[0].knowledge.knowledge_id == "k2"
        assert persona.search("another", "红茶") == ()
        assert persona.get_active_by_ids("kurisu", ("k2", "missing"))[0].knowledge_id == "k2"

        assert persona.replace_persona("kurisu", (second,))[0].knowledge_id == "k2"
        assert not persona.get("k1").active
        assert persona.search("kurisu", "脑科学") == ()

        assert (
            persona.record_successful_recall(
                "ticket-no-first",
                "kurisu",
                ("k2",),
                terminal_status="completed",
                first_chunk_received=False,
            )
            == 0
        )
        assert (
            persona.record_successful_recall(
                "ticket-success",
                "kurisu",
                ("k2",),
                terminal_status="user_stopped",
                first_chunk_received=True,
            )
            == 1
        )
        assert (
            persona.record_successful_recall(
                "ticket-success",
                "kurisu",
                ("k2",),
                terminal_status="user_stopped",
                first_chunk_received=True,
            )
            == 0
        )
        stats = persona.recall_stats("kurisu", ("k2",))[0]
        assert stats.successful_recall_count == 1
        assert stats.last_recalled_at is not None
        assert (
            database.connection.execute("SELECT COUNT(*) FROM memory_recall_events").fetchone()[0]
            == 0
        )
        assert (
            database.connection.execute("SELECT COUNT(*) FROM persona_recall_events").fetchone()[0]
            == 1
        )
    finally:
        database.close()


def test_persona_replace_rejects_duplicate_explicit_knowledge_ids_before_writing(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    persona = PersonaRepository(database)
    try:
        first = PersonaKnowledgeDraft(
            content="第一条合成角色资料。",
            tags=("合成",),
            source_ref="synthetic:first",
            source_hash="c" * 64,
            knowledge_id="duplicate-id",
        )
        second = PersonaKnowledgeDraft(
            content="第二条不同的合成角色资料。",
            tags=("合成",),
            source_ref="synthetic:second",
            source_hash="d" * 64,
            knowledge_id="duplicate-id",
        )

        with pytest.raises(StorageValidationError, match="duplicate knowledge_id"):
            persona.replace_persona("kurisu", (first, second))

        assert persona.list_active_documents("kurisu") == ()
    finally:
        database.close()
