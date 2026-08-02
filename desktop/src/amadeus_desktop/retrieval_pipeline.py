"""Data-thread stages for P5B hybrid prompt retrieval.

SQLite-backed candidate collection and final revalidation deliberately live in
separate operations.  The vector inference between them is performed by the
single vector runtime and never owns a database connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from amadeus_desktop.chat_models import PreparedPrompt, PromptMessage, PromptRole
from amadeus_desktop.hybrid_retrieval import (
    MAX_SOURCE_HITS,
    RankedRetrievalHit,
    RetrievalCorpus,
    RetrievalItem,
    build_retrieval_bundle,
    build_retrieval_query,
)
from amadeus_desktop.memory_models import (
    MemoryKind as DomainMemoryKind,
)
from amadeus_desktop.memory_models import (
    PromptMemory,
    PromptPersonaKnowledge,
)
from amadeus_desktop.persona import (
    build_capability_safety_boundary,
    build_persona_core_prompt,
)
from amadeus_desktop.prompt_context import DefaultPromptContextService, PromptContextInput
from amadeus_desktop.storage_models import (
    DEFAULT_PROFILE_ID,
    MemoryRecord,
    PersonaKnowledge,
    RecallStats,
    StoredMessageRole,
    StoredMessageStatus,
)

DEFAULT_PERSONA_ID = "kurisu"
# The current user message is already durable when retrieval starts.  Fetch it
# plus 20 historical messages; PromptContextService removes the duplicate
# current row before applying its 20-message history cap.
PROMPT_RECENT_MESSAGE_LIMIT = 21


class _ConversationRepository(Protocol):
    def load_recent_valid_messages(self, conversation_id: str, *, limit: int): ...

    def latest_summary(self, conversation_id: str): ...


class _MemoryRepository(Protocol):
    def search(self, query: str, *, profile_id: str, limit: int): ...

    def get_active_by_version_ids(self, version_ids, *, profile_id: str): ...

    def recall_stats(self, *, profile_id: str, memory_ids=None): ...


class _PersonaRepository(Protocol):
    def search(self, persona_id: str, query: str, *, limit: int): ...

    def get_active_by_ids(self, persona_id: str, knowledge_ids): ...

    def recall_stats(self, persona_id: str, knowledge_ids=None): ...


class RetrievalStores(Protocol):
    conversations: _ConversationRepository
    memories: _MemoryRepository
    personas: _PersonaRepository


@dataclass(frozen=True, slots=True)
class PromptRetrievalSeed:
    """FTS and conversation data captured after the user row is durable."""

    conversation_id: str
    current_user_message: str
    retrieval_query: str
    recent_messages: tuple[PromptMessage, ...]
    summary: str | None
    user_fts_hits: tuple[RankedRetrievalHit, ...] = ()
    persona_fts_hits: tuple[RankedRetrievalHit, ...] = ()


@dataclass(frozen=True, slots=True)
class VectorRetrievalResult:
    """Vector-thread output safe to pass back to the serialized data thread."""

    user_hits: tuple[RankedRetrievalHit, ...] = ()
    persona_hits: tuple[RankedRetrievalHit, ...] = ()
    threshold: float = 1.0
    degraded_category: str | None = None


def collect_prompt_retrieval_seed(
    stores: RetrievalStores,
    conversation_id: str,
    current_user_message: str,
    *,
    memory_enabled: bool,
    persona_id: str = DEFAULT_PERSONA_ID,
) -> PromptRetrievalSeed:
    """Collect recent context and independent FTS candidate lists."""

    recent_stored = stores.conversations.load_recent_valid_messages(
        conversation_id,
        limit=PROMPT_RECENT_MESSAGE_LIMIT,
    )
    recent: list[PromptMessage] = []
    retrieval_context: list[str] = []
    for position, message in enumerate(recent_stored):
        if message.role is StoredMessageRole.USER:
            role = PromptRole.USER
        elif (
            message.status
            in {
                StoredMessageStatus.COMPLETED,
                StoredMessageStatus.STOPPED,
            }
            and message.content
        ):
            role = PromptRole.ASSISTANT
        else:
            continue
        recent.append(PromptMessage(role, message.content))
        if not (
            position == len(recent_stored) - 1
            and role is PromptRole.USER
            and message.content == current_user_message
        ):
            retrieval_context.append(message.content)

    retrieval_query = build_retrieval_query(current_user_message, retrieval_context)
    user_fts: tuple[RankedRetrievalHit, ...] = ()
    if memory_enabled:
        try:
            results = stores.memories.search(
                retrieval_query,
                profile_id=DEFAULT_PROFILE_ID,
                limit=MAX_SOURCE_HITS,
            )
            user_fts = tuple(
                RankedRetrievalHit(
                    result.memory.current_version.version_id,
                    rank,
                    result.rank,
                )
                for rank, result in enumerate(results, start=1)
            )
        except Exception:
            user_fts = ()

    try:
        persona_results = stores.personas.search(
            persona_id,
            retrieval_query,
            limit=MAX_SOURCE_HITS,
        )
        persona_fts = tuple(
            RankedRetrievalHit(result.knowledge.knowledge_id, rank, result.rank)
            for rank, result in enumerate(persona_results, start=1)
        )
    except Exception:
        persona_fts = ()

    summary = stores.conversations.latest_summary(conversation_id)
    return PromptRetrievalSeed(
        conversation_id=conversation_id,
        current_user_message=current_user_message,
        retrieval_query=retrieval_query,
        recent_messages=tuple(recent),
        summary=None if summary is None else summary.content,
        user_fts_hits=user_fts,
        persona_fts_hits=persona_fts,
    )


def finalize_prepared_prompt(
    stores: RetrievalStores,
    seed: PromptRetrievalSeed,
    *,
    turn_id: str,
    attempt: int,
    memory_enabled: bool,
    vector_result: VectorRetrievalResult | None,
    prompt_service: DefaultPromptContextService,
    persona_id: str = DEFAULT_PERSONA_ID,
) -> PreparedPrompt:
    """Revalidate candidates, fuse the two corpora, and build one immutable prompt."""

    vectors = vector_result or VectorRetrievalResult(degraded_category="vector_unavailable")
    user_vector_hits = vectors.user_hits if memory_enabled else ()
    user_fts_hits = seed.user_fts_hits if memory_enabled else ()
    user_version_ids = _candidate_ids(user_fts_hits, user_vector_hits)
    persona_ids = _candidate_ids(seed.persona_fts_hits, vectors.persona_hits)

    user_records: tuple[MemoryRecord, ...] = ()
    if memory_enabled and user_version_ids:
        user_records = stores.memories.get_active_by_version_ids(
            user_version_ids,
            profile_id=DEFAULT_PROFILE_ID,
        )
    persona_records: tuple[PersonaKnowledge, ...] = ()
    if persona_ids:
        persona_records = stores.personas.get_active_by_ids(persona_id, persona_ids)

    user_stats = _user_stats(stores, user_records)
    persona_stats = _persona_stats(stores, persona_id, persona_records)
    user_items = {
        record.current_version.version_id: _user_item(record, user_stats.get(record.memory_id))
        for record in user_records
    }
    persona_items = {
        record.knowledge_id: _persona_item(record, persona_stats.get(record.knowledge_id))
        for record in persona_records
    }
    bundle = build_retrieval_bundle(
        user_fts_hits=user_fts_hits,
        user_vector_hits=user_vector_hits,
        user_items=user_items,
        persona_fts_hits=seed.persona_fts_hits,
        persona_vector_hits=vectors.persona_hits,
        persona_items=persona_items,
        vector_threshold=vectors.threshold,
    )

    memories_by_version = {record.current_version.version_id: record for record in user_records}
    memories = tuple(
        PromptMemory(
            memory_id=memories_by_version[result.item.target_id].memory_id,
            memory_version_id=result.item.target_id,
            kind=DomainMemoryKind(result.item.kind),
            content=result.item.content,
            topic_key=memories_by_version[result.item.target_id].topic_key,
            importance=result.item.importance,
            confidence=result.item.confidence,
            pinned=result.item.pinned,
        )
        for result in bundle.user_memories
    )
    persona = tuple(
        PromptPersonaKnowledge(
            knowledge_id=result.item.target_id,
            persona_id=persona_id,
            content=result.item.content,
        )
        for result in bundle.persona_knowledge
    )
    context = prompt_service.build(
        PromptContextInput(
            safety_boundary=build_capability_safety_boundary(),
            persona=build_persona_core_prompt(),
            current_date=date.today(),
            current_user_message=seed.current_user_message,
            memories=memories,
            persona_knowledge=persona,
            summary=seed.summary,
            recent_messages=seed.recent_messages,
        )
    )
    return PreparedPrompt(
        messages=context.messages,
        user_memory_version_ids=context.selected_memory_version_ids,
        persona_knowledge_ids=context.selected_persona_knowledge_ids,
        retrieval_ticket_id=f"{turn_id}:{attempt}",
        attempt=attempt,
    )


def _candidate_ids(*groups: tuple[RankedRetrievalHit, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(hit.target_id for group in groups for hit in group))


def _user_stats(
    stores: RetrievalStores,
    records: tuple[MemoryRecord, ...],
) -> dict[str, RecallStats]:
    if not records:
        return {}
    values = stores.memories.recall_stats(
        profile_id=DEFAULT_PROFILE_ID,
        memory_ids=tuple(record.memory_id for record in records),
    )
    return {value.target_id: value for value in values}


def _persona_stats(
    stores: RetrievalStores,
    persona_id: str,
    records: tuple[PersonaKnowledge, ...],
) -> dict[str, RecallStats]:
    if not records:
        return {}
    values = stores.personas.recall_stats(
        persona_id,
        tuple(record.knowledge_id for record in records),
    )
    return {value.target_id: value for value in values}


def _user_item(record: MemoryRecord, stats: RecallStats | None) -> RetrievalItem:
    version = record.current_version
    return RetrievalItem(
        target_id=version.version_id,
        corpus=RetrievalCorpus.USER_MEMORY,
        content=version.content,
        kind=record.kind.value,
        importance=version.importance,
        confidence=version.confidence,
        pinned=record.pinned,
        successful_recall_count=0 if stats is None else stats.successful_recall_count,
        # Pin/archive metadata updates must not reset event decay.  The current
        # immutable version timestamp is the stable fallback when no successful
        # recall event exists.
        created_at=version.created_at,
        last_successful_recall_at=None if stats is None else stats.last_recalled_at,
        active=True,
        current=True,
    )


def _persona_item(record: PersonaKnowledge, stats: RecallStats | None) -> RetrievalItem:
    return RetrievalItem(
        target_id=record.knowledge_id,
        corpus=RetrievalCorpus.PERSONA_KNOWLEDGE,
        content=record.content,
        successful_recall_count=0 if stats is None else stats.successful_recall_count,
        created_at=record.updated_at,
        last_successful_recall_at=None if stats is None else stats.last_recalled_at,
        active=record.active,
        current=record.active,
    )
