from __future__ import annotations

from amadeus_desktop.hybrid_retrieval import RankedRetrievalHit
from amadeus_desktop.local_data_service import create_local_data_stores
from amadeus_desktop.prompt_context import DefaultPromptContextService
from amadeus_desktop.retrieval_pipeline import (
    VectorRetrievalResult,
    _user_item,
    collect_prompt_retrieval_seed,
    finalize_prepared_prompt,
)
from amadeus_desktop.storage_models import PersonaKnowledgeDraft, StoredMessageStatus


def _stores(tmp_path):
    return create_local_data_stores(tmp_path / "amadeus.sqlite3", tmp_path / "backups")


def _seed_user_and_persona(stores):
    conversation = stores.conversations.create_conversation()
    stores.conversations.save_user_message(
        conversation.conversation_id,
        "source-turn",
        "source-user",
        "我偏好手冲咖啡",
    )
    memory = stores.memories.create_memory(
        "preference",
        "饮料 咖啡",
        "用户偏好手冲咖啡。",
        source_message_ids=("source-user",),
    )
    persona = stores.personas.replace_persona(
        "kurisu",
        (
            PersonaKnowledgeDraft(
                content="角色熟悉脑科学实验方法。",
                tags=("研究",),
                source_ref="synthetic:test",
                source_hash="a" * 64,
                knowledge_id="persona-1",
            ),
        ),
    )[0]
    stores.conversations.save_turn(
        conversation.conversation_id,
        "turn-1",
        "user-1",
        "咖啡和实验有什么联系？",
        "assistant-1",
    )
    return conversation, memory, persona


def test_fts_retrieval_keeps_user_and_persona_provenance_separate(tmp_path) -> None:
    stores = _stores(tmp_path)
    try:
        conversation, memory, persona = _seed_user_and_persona(stores)
        seed = collect_prompt_retrieval_seed(
            stores,
            conversation.conversation_id,
            "咖啡和实验有什么联系？",
            memory_enabled=True,
        )
        prepared = finalize_prepared_prompt(
            stores,
            seed,
            turn_id="turn-1",
            attempt=2,
            memory_enabled=True,
            vector_result=None,
            prompt_service=DefaultPromptContextService(),
        )

        assert prepared.user_memory_version_ids == (memory.current_version.version_id,)
        assert prepared.persona_knowledge_ids == (persona.knowledge_id,)
        assert prepared.retrieval_ticket_id == "turn-1:2"
        assert prepared.attempt == 2
        rendered = "\n".join(message.content for message in prepared.messages)
        assert rendered.index("[角色本地知识") < rendered.index("[用户长期记忆")
    finally:
        stores.close()


def test_vector_hits_are_revalidated_against_current_and_active_rows(tmp_path) -> None:
    stores = _stores(tmp_path)
    try:
        conversation, memory, persona = _seed_user_and_persona(stores)
        seed = collect_prompt_retrieval_seed(
            stores,
            conversation.conversation_id,
            "完全不相干的检索文本",
            memory_enabled=True,
        )
        old_version_id = memory.current_version.version_id
        stores.memories.edit_memory(memory.memory_id, "用户现在偏好绿茶。")
        stores.personas.set_active(persona.knowledge_id, False)
        prepared = finalize_prepared_prompt(
            stores,
            seed,
            turn_id="turn-1",
            attempt=1,
            memory_enabled=True,
            vector_result=VectorRetrievalResult(
                user_hits=(RankedRetrievalHit(old_version_id, 1, 0.9),),
                persona_hits=(RankedRetrievalHit(persona.knowledge_id, 1, 0.9),),
                threshold=0.5,
            ),
            prompt_service=DefaultPromptContextService(),
        )
        assert prepared.user_memory_version_ids == ()
        assert prepared.persona_knowledge_ids == ()
    finally:
        stores.close()


def test_disabling_user_memory_still_allows_persona_retrieval(tmp_path) -> None:
    stores = _stores(tmp_path)
    try:
        conversation, _memory, persona = _seed_user_and_persona(stores)
        seed = collect_prompt_retrieval_seed(
            stores,
            conversation.conversation_id,
            "咖啡和实验有什么联系？",
            memory_enabled=False,
        )
        prepared = finalize_prepared_prompt(
            stores,
            seed,
            turn_id="turn-1",
            attempt=1,
            memory_enabled=False,
            vector_result=None,
            prompt_service=DefaultPromptContextService(),
        )
        assert prepared.user_memory_version_ids == ()
        assert prepared.persona_knowledge_ids == (persona.knowledge_id,)
    finally:
        stores.close()


def test_prompt_keeps_twenty_historical_messages_after_removing_current(tmp_path) -> None:
    stores = _stores(tmp_path)
    try:
        conversation = stores.conversations.create_conversation()
        for index in range(10):
            stores.conversations.save_turn(
                conversation.conversation_id,
                f"history-turn-{index}",
                f"history-user-{index}",
                f"历史用户消息 {index}",
                f"history-assistant-{index}",
            )
            stores.conversations.finalize_assistant(
                f"history-assistant-{index}",
                f"历史助手消息 {index}",
                status=StoredMessageStatus.COMPLETED,
                terminal_reason="completed",
                attempt=1,
            )
        current = "当前问题"
        stores.conversations.save_turn(
            conversation.conversation_id,
            "current-turn",
            "current-user",
            current,
            "current-assistant",
        )

        seed = collect_prompt_retrieval_seed(
            stores,
            conversation.conversation_id,
            current,
            memory_enabled=False,
        )
        prepared = finalize_prepared_prompt(
            stores,
            seed,
            turn_id="current-turn",
            attempt=1,
            memory_enabled=False,
            vector_result=None,
            prompt_service=DefaultPromptContextService(),
        )

        assert len(seed.recent_messages) == 21
        dialogue = tuple(message for message in prepared.messages if message.role.value != "system")
        assert len(dialogue[:-1]) == 20
        assert dialogue[-1].content == current
    finally:
        stores.close()


def test_user_retrieval_decay_anchor_uses_immutable_version_timestamp(tmp_path) -> None:
    stores = _stores(tmp_path)
    try:
        conversation = stores.conversations.create_conversation()
        stores.conversations.save_user_message(
            conversation.conversation_id,
            "source-turn",
            "source-user",
            "今天跑步",
        )
        record = stores.memories.create_memory(
            "event",
            "运动",
            "用户今天跑步。",
            source_message_ids=("source-user",),
        )
        updated = stores.memories.set_pinned(record.memory_id, True)

        item = _user_item(updated, None)
        assert item.created_at == updated.current_version.created_at
    finally:
        stores.close()
