"""Data-thread stages for P5B hybrid prompt retrieval.

SQLite-backed candidate collection and final revalidation deliberately live in
separate operations.  The vector inference between them is performed by the
single vector runtime and never owns a database connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from amadeus_desktop.chat_models import (
    AttachmentKind,
    AttachmentSnapshot,
    AttachmentSource,
    CompanionContextSnapshot,
    ImagePart,
    InputModality,
    PreparedPrompt,
    PromptContent,
    PromptMessage,
    PromptRole,
    ProviderRoute,
)
from amadeus_desktop.companion_context import build_companion_context_snapshot
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
    MemoryLayer,
    PromptDerivedMemory,
    PromptMemory,
    PromptPersonaKnowledge,
    WorkingMemoryItem,
    WorkingMemorySnapshot,
)
from amadeus_desktop.persona import (
    build_capability_safety_boundary,
    build_persona_core_prompt,
)
from amadeus_desktop.prompt_context import DefaultPromptContextService, PromptContextInput
from amadeus_desktop.storage_models import (
    DEFAULT_PROFILE_ID,
    MemoryRecord,
    MemoryVersionOrigin,
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

    def recent_recalled_version_ids(self, *, profile_id: str, response_limit: int): ...


class _DeepMemoryRepository(Protocol):
    def search(self, layer, query: str, *, profile_id: str, limit: int): ...

    def get_active_by_version_ids(self, layer, version_ids, *, profile_id: str): ...

    def recent_recalled_version_ids(self, layer, *, profile_id: str, response_limit: int): ...


class _PersonaRepository(Protocol):
    def search(self, persona_id: str, query: str, *, limit: int): ...

    def get_active_by_ids(self, persona_id: str, knowledge_ids): ...

    def recall_stats(self, persona_id: str, knowledge_ids=None): ...

    def recent_recalled_knowledge_ids(self, persona_id: str, *, response_limit: int): ...


class _AttachmentRepository(Protocol):
    def prompt_parts(self, user_text: str, attachments: tuple[AttachmentSnapshot, ...]): ...


class RetrievalStores(Protocol):
    conversations: _ConversationRepository
    memories: _MemoryRepository
    deep_memories: _DeepMemoryRepository
    personas: _PersonaRepository
    attachments: _AttachmentRepository


@dataclass(frozen=True, slots=True)
class PromptRetrievalSeed:
    """FTS and conversation data captured after the user row is durable."""

    conversation_id: str
    current_user_message: str
    retrieval_query: str
    recent_messages: tuple[PromptMessage, ...]
    summary: str | None
    current_prompt_content: PromptContent
    attachments: tuple[AttachmentSnapshot, ...] = ()
    provider_route: ProviderRoute = ProviderRoute.TEXT
    companion_context: CompanionContextSnapshot = CompanionContextSnapshot()
    user_fts_hits: tuple[RankedRetrievalHit, ...] = ()
    reflection_fts_hits: tuple[RankedRetrievalHit, ...] = ()
    persona_impression_fts_hits: tuple[RankedRetrievalHit, ...] = ()
    persona_fts_hits: tuple[RankedRetrievalHit, ...] = ()


@dataclass(frozen=True, slots=True)
class VectorRetrievalResult:
    """Vector-thread output safe to pass back to the serialized data thread."""

    user_hits: tuple[RankedRetrievalHit, ...] = ()
    reflection_hits: tuple[RankedRetrievalHit, ...] = ()
    persona_impression_hits: tuple[RankedRetrievalHit, ...] = ()
    persona_hits: tuple[RankedRetrievalHit, ...] = ()
    threshold: float = 1.0
    degraded_category: str | None = None


def collect_prompt_retrieval_seed(
    stores: RetrievalStores,
    conversation_id: str,
    current_user_message: str,
    *,
    memory_enabled: bool,
    deep_memory_enabled: bool = True,
    persona_id: str = DEFAULT_PERSONA_ID,
    companion_context: CompanionContextSnapshot | None = None,
) -> PromptRetrievalSeed:
    """Collect recent context and independent FTS candidate lists."""

    recent_stored = stores.conversations.load_recent_valid_messages(
        conversation_id,
        limit=PROMPT_RECENT_MESSAGE_LIMIT,
    )
    recent: list[PromptMessage] = []
    retrieval_context: list[str] = []
    prompt_attachments: list[AttachmentSnapshot] = []
    user_positions = [
        position
        for position, message in enumerate(recent_stored)
        if message.role is StoredMessageRole.USER
    ]
    attachment_positions = set(user_positions[-3:])
    current_position = user_positions[-1] if user_positions else None
    current_prompt_content: PromptContent = current_user_message
    current_attachments: tuple[AttachmentSnapshot, ...] = ()
    current_input_modality = InputModality.TEXT
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
        content: PromptContent = message.content
        if role is PromptRole.USER:
            user_text = message.content if message.content.strip() else "请查看我附上的资料。"
            content = user_text
            if position in attachment_positions and message.attachments:
                snapshots = tuple(_attachment_snapshot(item) for item in message.attachments)
                parts = tuple(stores.attachments.prompt_parts(user_text, snapshots))
                if any(isinstance(part, ImagePart) for part in parts):
                    content = parts
                elif parts:
                    content = parts[0].text
                prompt_attachments.extend(snapshots)
            if position == current_position:
                current_attachments = tuple(
                    _attachment_snapshot(item) for item in message.attachments
                )
                current_input_modality = InputModality(message.input_modality.value)
                current_prompt_content = content
                continue
        recent.append(PromptMessage(role, content))
        retrieval_context.append(message.content)

    retrieval_query = build_retrieval_query(current_user_message, retrieval_context)
    user_fts: tuple[RankedRetrievalHit, ...] = ()
    reflection_fts: tuple[RankedRetrievalHit, ...] = ()
    impression_fts: tuple[RankedRetrievalHit, ...] = ()
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
        if deep_memory_enabled:
            try:
                reflection_results = stores.deep_memories.search(
                    MemoryLayer.REFLECTION,
                    retrieval_query,
                    profile_id=DEFAULT_PROFILE_ID,
                    limit=MAX_SOURCE_HITS,
                )
                reflection_fts = tuple(
                    RankedRetrievalHit(
                        record.current_version.version_id,
                        rank,
                        fts_rank,
                    )
                    for rank, (record, fts_rank) in enumerate(
                        reflection_results,
                        start=1,
                    )
                )
            except Exception:
                reflection_fts = ()
            try:
                impression_results = stores.deep_memories.search(
                    MemoryLayer.PERSONA,
                    retrieval_query,
                    profile_id=DEFAULT_PROFILE_ID,
                    limit=MAX_SOURCE_HITS,
                )
                impression_fts = tuple(
                    RankedRetrievalHit(
                        record.current_version.version_id,
                        rank,
                        fts_rank,
                    )
                    for rank, (record, fts_rank) in enumerate(
                        impression_results,
                        start=1,
                    )
                )
            except Exception:
                impression_fts = ()

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
        current_prompt_content=current_prompt_content,
        attachments=tuple(
            {attachment.attachment_id: attachment for attachment in prompt_attachments}.values()
        ),
        provider_route=(
            ProviderRoute.MULTIMODAL
            if any(
                isinstance(message.content, tuple)
                and any(isinstance(part, ImagePart) for part in message.content)
                for message in (*recent, PromptMessage(PromptRole.USER, current_prompt_content))
            )
            else ProviderRoute.TEXT
        ),
        companion_context=(
            companion_context
            if companion_context is not None
            else build_companion_context_snapshot(
                input_modality=current_input_modality,
                attachments=current_attachments,
            )
        ),
        user_fts_hits=user_fts,
        reflection_fts_hits=reflection_fts,
        persona_impression_fts_hits=impression_fts,
        persona_fts_hits=persona_fts,
    )


def finalize_prepared_prompt(
    stores: RetrievalStores,
    seed: PromptRetrievalSeed,
    *,
    turn_id: str,
    attempt: int,
    memory_enabled: bool,
    deep_memory_enabled: bool = True,
    vector_result: VectorRetrievalResult | None,
    prompt_service: DefaultPromptContextService,
    persona_id: str = DEFAULT_PERSONA_ID,
    follow_user_language: bool = True,
    message_id: str = "",
) -> PreparedPrompt:
    """Revalidate candidates, fuse the two corpora, and build one immutable prompt."""

    vectors = vector_result or VectorRetrievalResult(degraded_category="vector_unavailable")
    user_vector_hits = vectors.user_hits if memory_enabled else ()
    reflection_vector_hits = (
        vectors.reflection_hits if memory_enabled and deep_memory_enabled else ()
    )
    impression_vector_hits = (
        vectors.persona_impression_hits if memory_enabled and deep_memory_enabled else ()
    )
    user_fts_hits = seed.user_fts_hits if memory_enabled else ()
    reflection_fts_hits = seed.reflection_fts_hits if memory_enabled and deep_memory_enabled else ()
    impression_fts_hits = (
        seed.persona_impression_fts_hits if memory_enabled and deep_memory_enabled else ()
    )
    user_version_ids = _candidate_ids(user_fts_hits, user_vector_hits)
    reflection_version_ids = _candidate_ids(reflection_fts_hits, reflection_vector_hits)
    impression_version_ids = _candidate_ids(impression_fts_hits, impression_vector_hits)
    persona_ids = _candidate_ids(seed.persona_fts_hits, vectors.persona_hits)

    user_records: tuple[MemoryRecord, ...] = ()
    if memory_enabled and user_version_ids:
        user_records = stores.memories.get_active_by_version_ids(
            user_version_ids,
            profile_id=DEFAULT_PROFILE_ID,
        )
    reflection_records = ()
    impression_records = ()
    if memory_enabled and deep_memory_enabled and reflection_version_ids:
        reflection_records = stores.deep_memories.get_active_by_version_ids(
            MemoryLayer.REFLECTION,
            reflection_version_ids,
            profile_id=DEFAULT_PROFILE_ID,
        )
    if memory_enabled and deep_memory_enabled and impression_version_ids:
        impression_records = stores.deep_memories.get_active_by_version_ids(
            MemoryLayer.PERSONA,
            impression_version_ids,
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
    reflection_items = {
        record.current_version.version_id: _derived_item(record) for record in reflection_records
    }
    impression_items = {
        record.current_version.version_id: _derived_item(record) for record in impression_records
    }
    recent = {
        RetrievalCorpus.USER_MEMORY: stores.memories.recent_recalled_version_ids(
            profile_id=DEFAULT_PROFILE_ID,
            response_limit=3,
        ),
        RetrievalCorpus.MEMORY_REFLECTION: (
            stores.deep_memories.recent_recalled_version_ids(
                MemoryLayer.REFLECTION,
                profile_id=DEFAULT_PROFILE_ID,
                response_limit=3,
            )
            if deep_memory_enabled
            else ()
        ),
        RetrievalCorpus.MEMORY_PERSONA_IMPRESSION: (
            stores.deep_memories.recent_recalled_version_ids(
                MemoryLayer.PERSONA,
                profile_id=DEFAULT_PROFILE_ID,
                response_limit=3,
            )
            if deep_memory_enabled
            else ()
        ),
        RetrievalCorpus.PERSONA_KNOWLEDGE: stores.personas.recent_recalled_knowledge_ids(
            persona_id,
            response_limit=3,
        ),
    }
    bundle = build_retrieval_bundle(
        user_fts_hits=user_fts_hits,
        user_vector_hits=user_vector_hits,
        user_items=user_items,
        reflection_fts_hits=reflection_fts_hits,
        reflection_vector_hits=reflection_vector_hits,
        reflection_items=reflection_items,
        persona_impression_fts_hits=impression_fts_hits,
        persona_impression_vector_hits=impression_vector_hits,
        persona_impression_items=impression_items,
        persona_fts_hits=seed.persona_fts_hits,
        persona_vector_hits=vectors.persona_hits,
        persona_items=persona_items,
        vector_threshold=vectors.threshold,
        query_text=seed.current_user_message,
        recently_recalled=recent,
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
            user_confirmed=(
                memories_by_version[result.item.target_id].kind.value == "relationship"
                and memories_by_version[result.item.target_id].current_version.origin
                is MemoryVersionOrigin.MANUAL
            ),
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
    reflections_by_version = {
        record.current_version.version_id: record for record in reflection_records
    }
    impressions_by_version = {
        record.current_version.version_id: record for record in impression_records
    }
    reflections = tuple(
        _prompt_derived(reflections_by_version[result.item.target_id])
        for result in bundle.reflections
    )
    impressions = tuple(
        _prompt_derived(impressions_by_version[result.item.target_id])
        for result in bundle.persona_impressions
    )
    context = prompt_service.build(
        PromptContextInput(
            safety_boundary=build_capability_safety_boundary(seed.companion_context),
            persona=build_persona_core_prompt(follow_user_language=follow_user_language),
            current_date=date.today(),
            current_user_message=seed.current_user_message,
            memories=memories,
            reflections=reflections,
            persona_impressions=impressions,
            persona_knowledge=persona,
            summary=seed.summary,
            recent_messages=seed.recent_messages,
        )
    )
    messages = (*context.messages[:-1], PromptMessage(PromptRole.USER, seed.current_prompt_content))
    working_items: list[WorkingMemoryItem] = []
    selected_ids = {
        *context.selected_memory_version_ids,
        *context.selected_reflection_version_ids,
        *context.selected_persona_impression_version_ids,
        *context.selected_persona_knowledge_ids,
    }
    for layer, results in (
        (MemoryLayer.FACT, bundle.user_memories),
        (MemoryLayer.REFLECTION, bundle.reflections),
        (MemoryLayer.PERSONA, bundle.persona_impressions),
        (MemoryLayer.STATIC_PERSONA, bundle.persona_knowledge),
    ):
        for result in results:
            if result.item.target_id not in selected_ids:
                continue
            sources = "+".join(
                name
                for name, present in (
                    ("fts", result.fts_rank is not None),
                    ("vector", result.vector_rank is not None),
                )
                if present
            )
            working_items.append(
                WorkingMemoryItem(
                    layer=layer,
                    target_id=result.item.target_id,
                    version_id=result.item.target_id,
                    score=result.final_score,
                    reason=(
                        f"{sources or 'revalidated'}; relevance gated; "
                        f"evidence={result.evidence_weight:.2f}; "
                        f"repeat={result.repetition_weight:.2f}"
                    ),
                )
            )
    return PreparedPrompt(
        messages=messages,
        user_memory_version_ids=context.selected_memory_version_ids,
        reflection_version_ids=context.selected_reflection_version_ids,
        persona_impression_version_ids=context.selected_persona_impression_version_ids,
        persona_knowledge_ids=context.selected_persona_knowledge_ids,
        retrieval_ticket_id=f"{turn_id}:{attempt}",
        attempt=attempt,
        attachments=seed.attachments,
        provider_route=seed.provider_route,
        working_memory_snapshot=WorkingMemorySnapshot(
            conversation_id=seed.conversation_id,
            message_id=message_id or turn_id,
            query=seed.current_user_message,
            selected=tuple(working_items),
        ),
        companion_context=seed.companion_context,
    )


def _attachment_snapshot(value) -> AttachmentSnapshot:
    return AttachmentSnapshot(
        attachment_id=value.attachment_id,
        kind=AttachmentKind(value.kind.value),
        source=AttachmentSource(value.source.value),
        display_name=value.display_name,
        mime_type=value.mime_type,
        size_bytes=value.size_bytes,
        sha256=value.sha256,
        relative_path=value.relative_path,
        extracted_text=value.extracted_text,
        text_truncated=value.text_truncated,
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
        topic_key=record.topic_key,
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


def _derived_item(record) -> RetrievalItem:
    version = record.current_version
    return RetrievalItem(
        target_id=version.version_id,
        corpus=(
            RetrievalCorpus.MEMORY_REFLECTION
            if record.layer is MemoryLayer.REFLECTION
            else RetrievalCorpus.MEMORY_PERSONA_IMPRESSION
        ),
        content=version.content,
        kind=record.layer.value,
        topic_key=record.topic_key,
        importance=version.importance,
        confidence=version.confidence,
        evidence_score=record.evidence_score,
        status_weight=1.0,
        pinned=record.pinned,
        created_at=version.created_at,
        active=not record.conflicted,
        current=True,
    )


def _prompt_derived(record) -> PromptDerivedMemory:
    version = record.current_version
    return PromptDerivedMemory(
        group_id=record.group_id,
        version_id=version.version_id,
        layer=record.layer,
        subject_scope=record.subject_scope,
        content=version.content,
        topic_key=record.topic_key,
        importance=version.importance,
        confidence=version.confidence,
        evidence_score=record.evidence_score,
        pinned=record.pinned,
    )
