"""Qt-facing P5A application service over the single serialized data thread."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from inspect import Parameter, signature
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from amadeus_desktop.attachments import AttachmentStore
from amadeus_desktop.chat_models import (
    AttachmentKind,
    AttachmentSnapshot,
    AttachmentSource,
    ChatMessage,
    ConversationTurn,
    InputModality,
    MessageRole,
    MessageStatus,
    PreparedPrompt,
    PromptMessage,
    PromptRole,
    ProviderRoute,
    TurnTerminalReason,
)
from amadeus_desktop.companion_cues import CompanionCueStore
from amadeus_desktop.conversation_store import (
    BackgroundJobStore,
    ConversationStore,
    ProactiveInteractionStore,
)
from amadeus_desktop.data_runtime import DataPriority, SerialDataThread
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.deep_memory_store import DeepMemoryStore
from amadeus_desktop.memory_models import MemoryKind as DomainMemoryKind
from amadeus_desktop.memory_models import MemoryLayer, PromptMemory, WorkingMemorySnapshot
from amadeus_desktop.memory_service import MemoryService
from amadeus_desktop.persona import (
    build_capability_safety_boundary,
    build_persona_core_prompt,
)
from amadeus_desktop.persona_repository import PersonaRepository
from amadeus_desktop.prompt_context import DefaultPromptContextService, PromptContextInput
from amadeus_desktop.retrieval_pipeline import (
    PromptRetrievalSeed,
    VectorRetrievalResult,
    collect_prompt_retrieval_seed,
    finalize_prepared_prompt,
)
from amadeus_desktop.storage_models import (
    DEFAULT_PROFILE_ID,
    BackgroundJob,
    CompanionCueSourceKind,
    CompanionCueStatus,
    Conversation,
    MemoryRecord,
    MemorySource,
    MemoryVersionOperation,
    MemoryVersionOrigin,
    MessagePage,
    ProactiveTrigger,
    StorageNotFoundError,
    StoredAttachment,
    StoredAttachmentKind,
    StoredAttachmentSource,
    StoredInputModality,
    StoredMessage,
    StoredMessageOrigin,
    StoredMessageRole,
    StoredMessageStatus,
    TemporalCommitment,
    TemporalCommitmentKind,
    TemporalCommitmentStatus,
)
from amadeus_desktop.temporal_commitments import (
    TemporalCommitmentStore,
    TemporalDraftSpec,
    TemporalDueSnapshot,
)
from amadeus_desktop.vector_store import VectorStore

SUMMARY_MESSAGE_THRESHOLD = 12
SUMMARY_CHARACTER_THRESHOLD = 6_000
MESSAGE_PAGE_SIZE = 40
# The current durable user row is part of the SQL result and is removed by the
# prompt budgeter, so fetch one extra row to retain 20 historical messages.
PROMPT_RECENT_MESSAGE_LIMIT = 21
_USER_MEMORY_SECTION_PREFIX = "[用户长期记忆："
_REFLECTION_SECTION_PREFIX = "[长期反思："
_PERSONA_IMPRESSION_SECTION_PREFIX = "[互动人格印象："


@dataclass(slots=True)
class LocalDataStores:
    database: SQLiteDatabase
    conversations: ConversationStore
    memories: MemoryService
    deep_memories: DeepMemoryStore
    personas: PersonaRepository
    vectors: VectorStore
    jobs: BackgroundJobStore
    proactive: ProactiveInteractionStore
    attachments: AttachmentStore
    companion_cues: CompanionCueStore
    temporal_commitments: TemporalCommitmentStore

    def close(self) -> None:
        self.database.close()


@dataclass(frozen=True, slots=True)
class ConversationPresentationEntry:
    """One chronologically ordered chat row, including standalone greetings."""

    entry_id: str
    turn_id: str
    user_message: ChatMessage | None
    assistant_message: ChatMessage | None
    origin: StoredMessageOrigin
    first_sequence: int


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    conversation: Conversation | None
    conversations: tuple[Conversation, ...]
    messages: tuple[StoredMessage, ...]
    turns: tuple[ConversationTurn, ...]
    next_before_sequence: int | None
    read_only: bool
    migration_error_category: str | None = None
    presentation_entries: tuple[ConversationPresentationEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class OlderMessagesSnapshot:
    conversation_id: str
    messages: tuple[StoredMessage, ...]
    turns: tuple[ConversationTurn, ...]
    next_before_sequence: int | None
    presentation_entries: tuple[ConversationPresentationEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryListSnapshot:
    rows: tuple[dict[str, object], ...]
    failed_jobs: tuple[dict[str, object], ...]
    working_rows: tuple[dict[str, object], ...] = ()
    recent_rows: tuple[dict[str, object], ...] = ()
    reflection_rows: tuple[dict[str, object], ...] = ()
    persona_rows: tuple[dict[str, object], ...] = ()
    static_persona_rows: tuple[dict[str, object], ...] = ()
    timeline_rows: tuple[dict[str, object], ...] = ()
    audit_rows: tuple[dict[str, object], ...] = ()
    conflict_rows: tuple[dict[str, object], ...] = ()
    cue_rows: tuple[dict[str, object], ...] = ()
    layer_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReminderListSnapshot:
    commitments: tuple[TemporalCommitment, ...]
    query: str = ""
    status: str = ""
    outstanding_count: int = 0


@dataclass(slots=True)
class _PendingPromptPreparation:
    seed: PromptRetrievalSeed
    turn: object
    memory_enabled: bool
    memory_disable_epoch: int
    deep_memory_enabled: bool
    deep_memory_disable_epoch: int
    follow_user_language: bool
    on_success: Callable[[PreparedPrompt], None]
    on_failure: Callable[[str], None]
    vector_future: Future[VectorRetrievalResult] | None = None


def _supports_parameter(
    vector_query: Callable[..., object] | None,
    name: str,
) -> bool:
    """Detect optional vector-query keywords without breaking injected callbacks."""

    if vector_query is None:
        return False
    try:
        parameters = signature(vector_query).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind is Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _without_user_memory(prepared: PreparedPrompt) -> PreparedPrompt:
    """Remove both injected user-memory text and its recall evidence."""

    return replace(
        prepared,
        messages=tuple(
            message
            for message in prepared.messages
            if not (
                message.role is PromptRole.SYSTEM
                and isinstance(message.content, str)
                and message.content.startswith(
                    (
                        _USER_MEMORY_SECTION_PREFIX,
                        _REFLECTION_SECTION_PREFIX,
                        _PERSONA_IMPRESSION_SECTION_PREFIX,
                    )
                )
            )
        ),
        user_memory_version_ids=(),
        reflection_version_ids=(),
        persona_impression_version_ids=(),
        working_memory_snapshot=_filtered_working_snapshot(
            prepared.working_memory_snapshot,
            allowed_layers={MemoryLayer.STATIC_PERSONA},
        ),
    )


def _without_deep_memory(prepared: PreparedPrompt) -> PreparedPrompt:
    return replace(
        prepared,
        messages=tuple(
            message
            for message in prepared.messages
            if not (
                message.role is PromptRole.SYSTEM
                and isinstance(message.content, str)
                and message.content.startswith(
                    (_REFLECTION_SECTION_PREFIX, _PERSONA_IMPRESSION_SECTION_PREFIX)
                )
            )
        ),
        reflection_version_ids=(),
        persona_impression_version_ids=(),
        working_memory_snapshot=_filtered_working_snapshot(
            prepared.working_memory_snapshot,
            allowed_layers={MemoryLayer.FACT, MemoryLayer.STATIC_PERSONA},
        ),
    )


def _filtered_working_snapshot(
    snapshot: WorkingMemorySnapshot | None,
    *,
    allowed_layers: set[MemoryLayer],
) -> WorkingMemorySnapshot | None:
    if snapshot is None:
        return None
    return replace(
        snapshot,
        selected=tuple(item for item in snapshot.selected if item.layer in allowed_layers),
    )


def create_local_data_stores(
    database_path: Path,
    backup_directory: Path,
    attachment_directory: Path | AttachmentStore | None = None,
) -> LocalDataStores:
    """Open and construct all synchronous repositories on the caller's thread."""

    # Imported lazily so the pure database lifecycle remains independently testable.
    from amadeus_desktop.memory_store import MemoryStore

    database = SQLiteDatabase(database_path, backup_dir=backup_directory).open()
    companion_cues = CompanionCueStore(database)
    temporal_commitments = TemporalCommitmentStore(database)
    return LocalDataStores(
        database=database,
        conversations=ConversationStore(database),
        memories=MemoryStore(database),
        deep_memories=DeepMemoryStore(database),
        personas=PersonaRepository(database),
        vectors=VectorStore(database),
        jobs=BackgroundJobStore(database),
        proactive=ProactiveInteractionStore(database, companion_cues=companion_cues),
        attachments=(
            attachment_directory
            if isinstance(attachment_directory, AttachmentStore)
            else AttachmentStore(attachment_directory or database_path.parent / "attachments")
        ),
        companion_cues=companion_cues,
        temporal_commitments=temporal_commitments,
    )


class LocalDataService(QObject):
    """Conversation persistence, history, keyword recall, and memory administration."""

    startup_loaded = Signal(object)
    startup_failed = Signal(str)
    conversation_loaded = Signal(object)
    older_messages_loaded = Signal(object)
    history_loaded = Signal(object, str)
    memories_loaded = Signal(object)
    memories_cleared = Signal(int)
    memory_sources_loaded = Signal(str, object)
    layer_sources_loaded = Signal(str, str, object)
    layer_versions_loaded = Signal(str, str, object)
    deletion_impact_loaded = Signal(str, str, object)
    source_context_loaded = Signal(object, str)
    operation_failed = Signal(str, str)
    write_availability_changed = Signal(bool)
    jobs_enqueued = Signal()
    index_rebuild_requested = Signal(str)
    proactive_count_loaded = Signal(str, int)
    proactive_presentation_loaded = Signal(object)
    proactive_event_displayed = Signal(object)
    proactive_event_dismissed = Signal(object)
    proactive_greeting_persisted = Signal(object, object)
    companion_cue_changed = Signal(object)
    companion_cue_opened = Signal(str)
    reminders_loaded = Signal(object)
    temporal_commitment_changed = Signal(object)
    temporal_chat_draft_created = Signal(object)
    temporal_due_scanned = Signal(object)
    temporal_outstanding_count_changed = Signal(int)
    temporal_active_count_loaded = Signal(str, int)
    temporal_followup_opened = Signal(str)
    _vector_query_completed = Signal(str, object)

    def __init__(
        self,
        runtime: SerialDataThread,
        *,
        memory_enabled: bool = True,
        deep_memory_enabled: bool = True,
        follow_user_language: bool = True,
        prompt_service: DefaultPromptContextService | None = None,
        vector_query: Callable[..., Future[VectorRetrievalResult]] | None = None,
        vector_timeout_ms: int = 500,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self._memory_enabled = bool(memory_enabled)
        self._deep_memory_enabled = bool(deep_memory_enabled)
        self._follow_user_language = bool(follow_user_language)
        self._memory_disable_epoch = 0
        self._deep_memory_disable_epoch = 0
        self._prompt_service = prompt_service or DefaultPromptContextService()
        self._vector_query = vector_query
        self._vector_query_supports_include_user = _supports_parameter(
            vector_query,
            "include_user",
        )
        self._vector_query_supports_include_deep = _supports_parameter(
            vector_query,
            "include_deep",
        )
        self._vector_timeout_ms = max(1, int(vector_timeout_ms))
        self._pending_prompt_preparations: dict[str, _PendingPromptPreparation] = {}
        self._accept_prompt_preparations = True
        self._current_conversation_id: str | None = None
        self._next_before_sequence: int | None = None
        self._working_memory_snapshot: WorkingMemorySnapshot | None = None
        self._writable = False
        self._provider_name: str | None = None
        self._model_name: str | None = None
        self._multimodal_provider_name: str | None = None
        self._multimodal_model_name: str | None = None
        self._turn_provider_metadata: dict[str, tuple[str | None, str | None]] = {}
        self._finalized_provider_metadata: dict[str, tuple[str | None, str | None]] = {}
        self._started = False
        runtime.ready_changed.connect(self._on_runtime_ready)
        runtime.initialization_failed.connect(self.startup_failed.emit)
        self._vector_query_completed.connect(self._on_vector_query_completed)

    @property
    def memory_enabled(self) -> bool:
        return self._memory_enabled

    @property
    def deep_memory_enabled(self) -> bool:
        return self._deep_memory_enabled

    @property
    def current_conversation_id(self) -> str | None:
        return self._current_conversation_id

    @property
    def working_memory_snapshot(self) -> WorkingMemorySnapshot | None:
        return self._working_memory_snapshot

    @property
    def is_writable(self) -> bool:
        return self._writable

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.runtime.start()

    def set_memory_enabled(self, enabled: bool) -> None:
        normalized = bool(enabled)
        if self._memory_enabled and not normalized:
            # A disable event invalidates user-memory evidence already being
            # prepared, even if the setting is enabled again before completion.
            self._memory_disable_epoch += 1
        self._memory_enabled = normalized

    def set_deep_memory_enabled(self, enabled: bool) -> None:
        normalized = bool(enabled)
        if self._deep_memory_enabled and not normalized:
            self._deep_memory_disable_epoch += 1
        self._deep_memory_enabled = normalized

    def set_follow_user_language(self, enabled: bool) -> None:
        self._follow_user_language = bool(enabled)

    def set_provider_metadata(self, provider_name: str | None, model_name: str | None) -> None:
        self._provider_name = provider_name
        self._model_name = model_name

    def set_multimodal_provider_metadata(
        self,
        provider_name: str | None,
        model_name: str | None,
    ) -> None:
        self._multimodal_provider_name = provider_name
        self._multimodal_model_name = model_name

    def bind_turn_provider_metadata(
        self,
        turn_id: str,
        provider_name: str | None,
        model_name: str | None,
    ) -> None:
        """Bind metadata captured at provider-request start to one durable turn."""

        identifier = str(turn_id)
        if identifier in self._turn_provider_metadata:
            self._turn_provider_metadata[identifier] = (provider_name, model_name)

    def pop_finalized_provider_metadata(
        self,
        turn_id: str,
    ) -> tuple[str | None, str | None] | None:
        return self._finalized_provider_metadata.pop(str(turn_id), None)

    @Slot(bool)
    def _on_runtime_ready(self, ready: bool) -> None:
        if not ready:
            self._writable = False
            self.write_availability_changed.emit(False)
            return
        request_id = self.runtime.submit(
            _initialize_stores,
            priority=DataPriority.FOREGROUND,
            on_success=self._on_startup_loaded,
            on_failure=self._on_startup_failed,
        )
        if request_id is None:
            self._on_startup_failed("DataThreadStopped")

    def _on_startup_loaded(self, value: object) -> None:
        if not isinstance(value, ConversationSnapshot):
            self._on_startup_failed("InvalidStartupSnapshot")
            return
        self._current_conversation_id = (
            None if value.conversation is None else value.conversation.conversation_id
        )
        self._next_before_sequence = value.next_before_sequence
        self._writable = not value.read_only
        self.write_availability_changed.emit(self._writable)
        self.startup_loaded.emit(value)

    def _on_startup_failed(self, category: str) -> None:
        self._writable = False
        self.write_availability_changed.emit(False)
        self.startup_failed.emit(category)

    # ConversationPersistence boundary ---------------------------------
    def prepare_new_turn(self, turn, on_success, on_failure) -> bool:
        conversation_id = self._current_conversation_id
        if not self._accept_prompt_preparations or not self._writable or conversation_id is None:
            return False
        memory_enabled = self._memory_enabled
        deep_memory_enabled = self._deep_memory_enabled
        follow_user_language = self._follow_user_language
        memory_disable_epoch = self._memory_disable_epoch
        deep_memory_disable_epoch = self._deep_memory_disable_epoch

        def operation(stores: LocalDataStores) -> PromptRetrievalSeed:
            stored_attachments = tuple(
                _stored_attachment(attachment) for attachment in turn.user_message.attachments
            )
            stores.conversations.save_turn(
                conversation_id,
                turn.turn_id,
                turn.user_message.message_id,
                turn.user_message.content,
                turn.assistant_message.message_id,
                attempt=turn.attempt,
                participates_in_memory=memory_enabled and bool(turn.user_message.content.strip()),
                input_modality=StoredInputModality(turn.user_message.input_modality.value),
                attachments=stored_attachments,
            )
            return collect_prompt_retrieval_seed(
                stores,
                conversation_id,
                _effective_user_text(turn.user_message.content),
                memory_enabled=memory_enabled,
                deep_memory_enabled=deep_memory_enabled,
                companion_context=turn.companion_context,
            )

        return (
            self.runtime.submit(
                operation,
                priority=DataPriority.FOREGROUND,
                on_success=lambda seed: self._start_prompt_retrieval(
                    seed,
                    turn,
                    memory_enabled=memory_enabled,
                    memory_disable_epoch=memory_disable_epoch,
                    deep_memory_enabled=deep_memory_enabled,
                    deep_memory_disable_epoch=deep_memory_disable_epoch,
                    follow_user_language=follow_user_language,
                    on_success=on_success,
                    on_failure=on_failure,
                ),
                on_failure=on_failure,
            )
            is not None
        )

    def prepare_retry(self, turn, on_success, on_failure) -> bool:
        conversation_id = self._current_conversation_id
        if not self._accept_prompt_preparations or not self._writable or conversation_id is None:
            return False
        memory_enabled = self._memory_enabled
        deep_memory_enabled = self._deep_memory_enabled
        follow_user_language = self._follow_user_language
        memory_disable_epoch = self._memory_disable_epoch
        deep_memory_disable_epoch = self._deep_memory_disable_epoch

        def operation(stores: LocalDataStores) -> PromptRetrievalSeed:
            # A previous two-step preparation may have committed only the user
            # message. Recover that exact stable-ID turn without duplicating it.
            try:
                stores.conversations.get_message(turn.user_message.message_id)
            except StorageNotFoundError:
                stores.conversations.save_user_message(
                    conversation_id,
                    turn.turn_id,
                    turn.user_message.message_id,
                    turn.user_message.content,
                    participates_in_memory=memory_enabled
                    and bool(turn.user_message.content.strip()),
                    input_modality=StoredInputModality(turn.user_message.input_modality.value),
                    attachments=tuple(
                        _stored_attachment(attachment)
                        for attachment in turn.user_message.attachments
                    ),
                )
            try:
                stores.conversations.begin_assistant_attempt(
                    turn.assistant_message.message_id,
                    turn.attempt,
                )
            except StorageNotFoundError:
                stores.conversations.create_assistant_placeholder(
                    conversation_id,
                    turn.turn_id,
                    turn.assistant_message.message_id,
                    attempt=turn.attempt,
                )
            return collect_prompt_retrieval_seed(
                stores,
                conversation_id,
                _effective_user_text(turn.user_message.content),
                memory_enabled=memory_enabled,
                deep_memory_enabled=deep_memory_enabled,
                companion_context=turn.companion_context,
            )

        return (
            self.runtime.submit(
                operation,
                priority=DataPriority.FOREGROUND,
                on_success=lambda seed: self._start_prompt_retrieval(
                    seed,
                    turn,
                    memory_enabled=memory_enabled,
                    memory_disable_epoch=memory_disable_epoch,
                    deep_memory_enabled=deep_memory_enabled,
                    deep_memory_disable_epoch=deep_memory_disable_epoch,
                    follow_user_language=follow_user_language,
                    on_success=on_success,
                    on_failure=on_failure,
                ),
                on_failure=on_failure,
            )
            is not None
        )

    def _start_prompt_retrieval(
        self,
        seed: PromptRetrievalSeed,
        turn: object,
        *,
        memory_enabled: bool,
        memory_disable_epoch: int,
        deep_memory_enabled: bool,
        deep_memory_disable_epoch: int,
        follow_user_language: bool,
        on_success: Callable[[PreparedPrompt], None],
        on_failure: Callable[[str], None],
    ) -> None:
        if not self._accept_prompt_preparations:
            return
        token = f"{turn.turn_id}:{turn.attempt}"
        pending = _PendingPromptPreparation(
            seed=seed,
            turn=turn,
            memory_enabled=memory_enabled,
            memory_disable_epoch=memory_disable_epoch,
            deep_memory_enabled=deep_memory_enabled,
            deep_memory_disable_epoch=deep_memory_disable_epoch,
            follow_user_language=follow_user_language,
            on_success=on_success,
            on_failure=on_failure,
        )
        self._pending_prompt_preparations[token] = pending
        if self._vector_query is None:
            self._finish_prompt_retrieval(token, None)
            return
        try:
            include_user = self._user_memory_allowed(pending)
            if self._vector_query_supports_include_user:
                keywords: dict[str, bool] = {"include_user": include_user}
                if self._vector_query_supports_include_deep:
                    keywords["include_deep"] = self._deep_memory_allowed(pending)
                future = self._vector_query(seed.retrieval_query, **keywords)
            else:
                # Compatibility for injected P5A/fake callbacks.  The final
                # data-thread stage still discards any legacy user-vector hits.
                future = self._vector_query(seed.retrieval_query)
        except Exception:
            self._finish_prompt_retrieval(token, None)
            return
        pending.vector_future = future

        def completed(result_future: Future[VectorRetrievalResult]) -> None:
            try:
                result: object = result_future.result()
            except Exception:
                result = VectorRetrievalResult(degraded_category="vector_query_failed")
            self._vector_query_completed.emit(token, result)

        future.add_done_callback(completed)
        QTimer.singleShot(
            self._vector_timeout_ms,
            self,
            lambda: self._finish_prompt_retrieval(token, None),
        )

    @Slot(str, object)
    def _on_vector_query_completed(self, token: str, value: object) -> None:
        result = value if isinstance(value, VectorRetrievalResult) else None
        self._finish_prompt_retrieval(token, result)

    def _finish_prompt_retrieval(
        self,
        token: str,
        vector_result: VectorRetrievalResult | None,
    ) -> None:
        pending = self._pending_prompt_preparations.pop(token, None)
        if pending is None:
            return
        if vector_result is None and pending.vector_future is not None:
            # This cancels a queued vector task.  A task already executing may
            # finish later, but its callback sees no pending token and is ignored.
            pending.vector_future.cancel()
        memory_enabled = self._user_memory_allowed(pending)
        deep_memory_enabled = self._deep_memory_allowed(pending)

        def operation(stores: LocalDataStores) -> PreparedPrompt:
            try:
                return finalize_prepared_prompt(
                    stores,
                    pending.seed,
                    turn_id=pending.turn.turn_id,
                    attempt=pending.turn.attempt,
                    memory_enabled=memory_enabled,
                    deep_memory_enabled=deep_memory_enabled,
                    vector_result=vector_result,
                    prompt_service=self._prompt_service,
                    follow_user_language=pending.follow_user_language,
                    message_id=pending.turn.user_message.message_id,
                )
            except Exception:
                # The user row already committed in stage one.  Keep the stable
                # assistant placeholder auditable instead of starting a model
                # request with a prompt that was not safely assembled.
                stores.conversations.finalize_assistant(
                    pending.turn.assistant_message.message_id,
                    "",
                    status=StoredMessageStatus.FAILED,
                    terminal_reason=TurnTerminalReason.LOCAL_PERSISTENCE_ERROR.value,
                    attempt=pending.turn.attempt,
                    failure_code="local_persistence",
                )
                raise

        def completed(prepared: PreparedPrompt) -> None:
            # Re-check on the Qt thread immediately before the conversation
            # coordinator can start the provider.  This closes the window where
            # memory was disabled while final revalidation was queued in SQLite.
            if not self._user_memory_allowed(pending):
                prepared = _without_user_memory(prepared)
            elif not self._deep_memory_allowed(pending):
                prepared = _without_deep_memory(prepared)
            self._working_memory_snapshot = prepared.working_memory_snapshot
            self._turn_provider_metadata[pending.turn.turn_id] = (
                (
                    self._multimodal_provider_name,
                    self._multimodal_model_name,
                )
                if prepared.provider_route is ProviderRoute.MULTIMODAL
                else (self._provider_name, self._model_name)
            )
            pending.on_success(prepared)

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.FOREGROUND,
            on_success=completed,
            on_failure=pending.on_failure,
        )
        if request_id is None:
            pending.on_failure("DataThreadStopped")

    def checkpoint_assistant(self, turn) -> None:
        if not self._writable:
            return
        request_id = self.runtime.submit(
            lambda stores: stores.conversations.checkpoint_assistant(
                turn.assistant_message.message_id,
                turn.assistant_message.content,
                attempt=turn.attempt,
            ),
            priority=DataPriority.FOREGROUND,
            on_failure=lambda category: self.operation_failed.emit("checkpoint", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("checkpoint")

    def finalize_turn(self, turn, on_success, on_failure) -> bool:
        if not self._writable:
            return False
        conversation_id = self._current_conversation_id
        if conversation_id is None:
            return False
        memory_enabled = self._memory_enabled
        provider_name, model_name = self._turn_provider_metadata.pop(
            turn.turn_id,
            (self._provider_name, self._model_name),
        )

        def operation(stores: LocalDataStores) -> bool:
            stores.conversations.finalize_assistant(
                turn.assistant_message.message_id,
                turn.assistant_message.content,
                status=turn.assistant_message.status.value,
                terminal_reason=(
                    turn.terminal_reason.value
                    if turn.terminal_reason is not None
                    else "provider_error"
                ),
                attempt=turn.attempt,
                provider_name=provider_name,
                model_name=model_name,
                failure_code=turn.provider_error_code,
            )
            enqueued = False
            progress = stores.conversations.summary_progress(conversation_id)
            if progress.last_sequence is not None and (
                progress.message_count >= SUMMARY_MESSAGE_THRESHOLD
                or progress.character_count >= SUMMARY_CHARACTER_THRESHOLD
            ):
                previous_summary = stores.conversations.latest_summary(conversation_id)
                stores.jobs.enqueue(
                    "conversation_summary",
                    f"summary:{conversation_id}:{progress.last_sequence}",
                    conversation_id=conversation_id,
                    payload={
                        "after_sequence": (
                            0
                            if previous_summary is None
                            else previous_summary.covers_through_sequence
                        )
                    },
                )
                enqueued = True
            user_message = stores.conversations.get_message(turn.user_message.message_id)
            if (
                memory_enabled
                and user_message.participates_in_memory
                and turn.terminal_reason
                in {
                    TurnTerminalReason.COMPLETED,
                    TurnTerminalReason.USER_STOPPED,
                }
            ):
                stores.jobs.enqueue(
                    "memory_extraction",
                    f"extract:{turn.user_message.message_id}",
                    profile_id=DEFAULT_PROFILE_ID,
                    conversation_id=conversation_id,
                    message_id=turn.user_message.message_id,
                    payload={
                        "source_message_ids": [turn.user_message.message_id],
                        "turn_id": turn.turn_id,
                        "terminal_reason": turn.terminal_reason.value,
                    },
                )
                enqueued = True
            return enqueued

        def completed(enqueued: bool) -> None:
            self._finalized_provider_metadata[turn.turn_id] = (
                provider_name,
                model_name,
            )
            if enqueued:
                self.jobs_enqueued.emit()
            self.refresh_history()
            on_success()

        def failed(category: str) -> None:
            self._on_finalize_failed(category)
            on_failure(category)

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.FOREGROUND,
            on_success=completed,
            on_failure=failed,
        )
        if request_id is None:
            self._on_persistence_submission_failed("finalize")
            return False
        return True

    def _on_persistence_submission_failed(self, operation: str) -> None:
        self._writable = False
        self.write_availability_changed.emit(False)
        self.operation_failed.emit(operation, "DataThreadStopped")

    def _on_finalize_failed(self, category: str) -> None:
        # A durable user message already exists, but accepting another turn
        # would be unsafe until storage is healthy again.
        self._writable = False
        self.write_availability_changed.emit(False)
        self.operation_failed.emit("finalize", category)

    def record_successful_recall(
        self,
        turn: ConversationTurn,
        prepared: PreparedPrompt,
    ) -> None:
        """Persist only the IDs that the budgeter actually injected."""

        if not self._writable or turn.terminal_reason is None:
            return
        ticket_id = prepared.retrieval_ticket_id or f"{turn.turn_id}:{turn.attempt}"
        memory_enabled = self._memory_enabled

        deep_memory_enabled = self._deep_memory_enabled

        def operation(stores: LocalDataStores) -> tuple[int, int, int, int]:
            user_message = stores.conversations.get_message(turn.user_message.message_id)
            user_count = stores.memories.record_successful_recall(
                ticket_id,
                prepared.user_memory_version_ids if memory_enabled else (),
                terminal_status=turn.terminal_reason.value,
                first_chunk_received=True,
                profile_id=DEFAULT_PROFILE_ID,
                conversation_id=user_message.conversation_id,
                assistant_message_id=turn.assistant_message.message_id,
                attempt=prepared.attempt,
            )
            persona_count = stores.personas.record_successful_recall(
                ticket_id,
                "kurisu",
                prepared.persona_knowledge_ids,
                terminal_status=turn.terminal_reason.value,
                first_chunk_received=True,
                conversation_id=user_message.conversation_id,
                assistant_message_id=turn.assistant_message.message_id,
                attempt=prepared.attempt,
            )
            reflection_count = stores.deep_memories.record_successful_recall(
                MemoryLayer.REFLECTION,
                ticket_id,
                prepared.reflection_version_ids if memory_enabled and deep_memory_enabled else (),
                terminal_status=turn.terminal_reason.value,
                first_chunk_received=True,
                profile_id=DEFAULT_PROFILE_ID,
                conversation_id=user_message.conversation_id,
                assistant_message_id=turn.assistant_message.message_id,
                attempt=prepared.attempt,
            )
            impression_count = stores.deep_memories.record_successful_recall(
                MemoryLayer.PERSONA,
                ticket_id,
                (
                    prepared.persona_impression_version_ids
                    if memory_enabled and deep_memory_enabled
                    else ()
                ),
                terminal_status=turn.terminal_reason.value,
                first_chunk_received=True,
                profile_id=DEFAULT_PROFILE_ID,
                conversation_id=user_message.conversation_id,
                assistant_message_id=turn.assistant_message.message_id,
                attempt=prepared.attempt,
            )
            return user_count, reflection_count, impression_count, persona_count

        self.runtime.submit(
            operation,
            priority=DataPriority.BACKGROUND,
            on_failure=lambda category: self.operation_failed.emit("recall_event", category),
        )

    def run_memory_maintenance(self) -> None:
        """Archive eligible ordinary events without touching disabled memory."""

        if not self._writable or not self._memory_enabled:
            return

        def completed(result: tuple[tuple[str, ...], int]) -> None:
            memory_ids, derived_count = result
            if memory_ids or derived_count:
                self.index_rebuild_requested.emit("user_memory")
                self.refresh_memories()

        self.runtime.submit(
            lambda stores: (
                stores.memories.archive_decayed_events(profile_id=DEFAULT_PROFILE_ID),
                (
                    stores.deep_memories.run_maintenance(profile_id=DEFAULT_PROFILE_ID)
                    if self._deep_memory_enabled
                    else 0
                ),
            ),
            priority=DataPriority.BACKGROUND,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit("memory_maintenance", category),
        )

    # Proactive interaction --------------------------------------------
    def load_proactive_display_count(
        self,
        local_date: date,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> bool:
        request_id = self.runtime.submit(
            lambda stores: stores.proactive.count_displayed_for_date(
                local_date,
                profile_id=profile_id,
            ),
            priority=DataPriority.FOREGROUND,
            on_success=lambda count: self.proactive_count_loaded.emit(
                local_date.isoformat(),
                int(count),
            ),
            on_failure=lambda category: self.operation_failed.emit("proactive_count", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("proactive_count")
            return False
        return True

    def load_contextual_proactive_presentation(
        self,
        *,
        include_deep: bool = True,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> bool:
        """Select at most one authorized cue without copying private text to events."""

        request_id = self.runtime.submit(
            lambda stores: stores.companion_cues.select_proactive_presentation(
                profile_id=profile_id,
                include_deep=include_deep,
            ),
            priority=DataPriority.FOREGROUND,
            on_success=self.proactive_presentation_loaded.emit,
            on_failure=lambda category: self.operation_failed.emit("proactive_context", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("proactive_context")
            return False
        return True

    def record_proactive_display(
        self,
        trigger: ProactiveTrigger | str,
        local_date: date,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        event_id: str | None = None,
        displayed_at: datetime | None = None,
        cue_id: str | None = None,
    ) -> bool:
        if not self._writable:
            self.operation_failed.emit("proactive_display", "DatabaseReadOnlyError")
            return False
        request_id = self.runtime.submit(
            lambda stores: stores.proactive.record_displayed(
                trigger,
                local_date,
                profile_id=profile_id,
                event_id=event_id,
                displayed_at=displayed_at,
                cue_id=cue_id,
            ),
            priority=DataPriority.FOREGROUND,
            on_success=self.proactive_event_displayed.emit,
            on_failure=lambda category: self.operation_failed.emit("proactive_display", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("proactive_display")
            return False
        return True

    def dismiss_proactive_event(self, event_id: str) -> bool:
        if not self._writable:
            self.operation_failed.emit("proactive_dismiss", "DatabaseReadOnlyError")
            return False
        request_id = self.runtime.submit(
            lambda stores: stores.proactive.record_dismissed(event_id),
            priority=DataPriority.FOREGROUND,
            on_success=self.proactive_event_dismissed.emit,
            on_failure=lambda category: self.operation_failed.emit("proactive_dismiss", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("proactive_dismiss")
            return False
        return True

    def persist_proactive_greeting(
        self,
        event_id: str,
        greeting: str,
        *,
        conversation_id: str | None = None,
        message_id: str | None = None,
        clicked_at: datetime | None = None,
        provider_name: str | None = None,
        model_name: str | None = None,
    ) -> bool:
        target_conversation_id = conversation_id or self._current_conversation_id
        if not self._writable:
            self.operation_failed.emit("proactive_click", "DatabaseReadOnlyError")
            return False
        if target_conversation_id is None:
            self.operation_failed.emit("proactive_click", "ConversationUnavailable")
            return False

        def operation(stores: LocalDataStores) -> tuple[object, object, ConversationSnapshot]:
            event, message = stores.proactive.persist_greeting_on_click(
                event_id,
                target_conversation_id,
                greeting,
                message_id=message_id,
                clicked_at=clicked_at,
                provider_name=provider_name,
                model_name=model_name,
            )
            conversation = stores.conversations.get_conversation(target_conversation_id)
            return event, message, _conversation_snapshot(stores, conversation)

        def completed(value: tuple[object, object, ConversationSnapshot]) -> None:
            event, message, snapshot = value
            # A greeting click can finish after the user has switched away.
            # Persist it in its original conversation without pulling the UI
            # and current-conversation cursor back to that older conversation.
            if self._current_conversation_id == target_conversation_id:
                self._on_conversation_loaded(
                    snapshot,
                    failure_operation="proactive_click",
                )
            self.proactive_greeting_persisted.emit(event, message)
            self.refresh_history()

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.FOREGROUND,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit("proactive_click", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("proactive_click")
            return False
        return True

    # History -----------------------------------------------------------
    def refresh_history(self) -> None:
        selected = self._current_conversation_id or ""
        request_id = self.runtime.submit(
            lambda stores: stores.conversations.list_conversations(),
            priority=DataPriority.INTERACTIVE,
            on_success=lambda rows: self.history_loaded.emit(rows, selected),
            on_failure=lambda category: self.operation_failed.emit("history", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("history")

    def switch_conversation(self, conversation_id: str) -> None:
        self._load_conversation(conversation_id, source_message_id=None)

    def create_conversation(self) -> None:
        if not self._writable:
            self.operation_failed.emit("create_conversation", "DatabaseReadOnlyError")
            return

        def operation(stores: LocalDataStores) -> ConversationSnapshot:
            conversation = stores.conversations.create_conversation()
            return _conversation_snapshot(stores, conversation)

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=lambda value: self._on_conversation_loaded(
                value,
                failure_operation="create_conversation",
            ),
            on_failure=lambda category: self.operation_failed.emit("create_conversation", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("create_conversation")

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        if not self._writable:
            self.operation_failed.emit("rename_conversation", "DatabaseReadOnlyError")
            return
        request_id = self.runtime.submit(
            lambda stores: (
                stores.conversations.rename_conversation(conversation_id, title),
                stores.conversations.list_conversations(),
            ),
            priority=DataPriority.INTERACTIVE,
            on_success=lambda value: self.history_loaded.emit(
                value[1], self._current_conversation_id or ""
            ),
            on_failure=lambda category: self.operation_failed.emit("rename_conversation", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("rename_conversation")

    def delete_conversation(
        self,
        conversation_id: str,
        *,
        protected_attachment_paths: tuple[str, ...] = (),
    ) -> None:
        if not self._writable:
            self.operation_failed.emit("delete_conversation", "DatabaseReadOnlyError")
            return

        current_conversation_id = self._current_conversation_id

        def operation(stores: LocalDataStores) -> ConversationSnapshot:
            stores.conversations.delete_conversation(conversation_id)
            _cleanup_attachment_orphans(
                stores,
                protected_paths=protected_attachment_paths,
            )
            if current_conversation_id is not None and current_conversation_id != conversation_id:
                conversation = stores.conversations.get_conversation(current_conversation_id)
            else:
                conversation = stores.conversations.get_or_create_active_conversation()
            return _conversation_snapshot(stores, conversation)

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=lambda value: self._on_conversation_loaded(
                value,
                failure_operation="delete_conversation",
            ),
            on_failure=lambda category: self.operation_failed.emit("delete_conversation", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("delete_conversation")

    def clear_conversations(self) -> None:
        if not self._writable:
            self.operation_failed.emit("clear_history", "DatabaseReadOnlyError")
            return

        def operation(stores: LocalDataStores) -> ConversationSnapshot:
            stores.conversations.clear_conversations()
            _cleanup_attachment_orphans(stores)
            conversation = stores.conversations.create_conversation()
            return _conversation_snapshot(stores, conversation)

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=lambda value: self._on_conversation_loaded(
                value,
                failure_operation="clear_history",
            ),
            on_failure=lambda category: self.operation_failed.emit("clear_history", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("clear_history")

    def load_older_messages(self) -> None:
        conversation_id = self._current_conversation_id
        cursor = self._next_before_sequence
        if conversation_id is None or cursor is None:
            return

        def operation(stores: LocalDataStores) -> OlderMessagesSnapshot:
            page = stores.conversations.load_message_page(
                conversation_id,
                limit=MESSAGE_PAGE_SIZE,
                before_sequence=cursor,
            )
            return OlderMessagesSnapshot(
                conversation_id,
                page.items,
                _turns_from_messages(page.items),
                page.next_before_sequence,
                _presentation_entries_from_messages(page.items),
            )

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=self._on_older_messages_loaded,
            on_failure=lambda category: self.operation_failed.emit("older_messages", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("older_messages")

    def load_source_context(self, conversation_id: str, message_id: str) -> None:
        self._load_conversation(conversation_id, source_message_id=message_id)

    def _load_conversation(
        self,
        conversation_id: str,
        *,
        source_message_id: str | None,
    ) -> None:
        def operation(stores: LocalDataStores) -> ConversationSnapshot:
            conversation = stores.conversations.get_conversation(conversation_id)
            page = (
                stores.conversations.load_message_context(
                    conversation_id,
                    source_message_id,
                    limit=MESSAGE_PAGE_SIZE,
                )
                if source_message_id is not None
                else stores.conversations.load_message_page(
                    conversation_id, limit=MESSAGE_PAGE_SIZE
                )
            )
            return _snapshot_from_page(stores, conversation, page)

        def loaded(value: object) -> None:
            if source_message_id is not None and isinstance(value, ConversationSnapshot):
                self.source_context_loaded.emit(value, source_message_id)
            else:
                self._on_conversation_loaded(value)

        operation_name = "conversation" if source_message_id is None else "source_context"
        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=loaded,
            on_failure=lambda category: self.operation_failed.emit(operation_name, category),
        )
        if request_id is None:
            self._on_persistence_submission_failed(operation_name)

    def _on_conversation_loaded(
        self,
        value: object,
        *,
        failure_operation: str = "conversation",
    ) -> None:
        if not isinstance(value, ConversationSnapshot):
            self.operation_failed.emit(failure_operation, "InvalidConversationSnapshot")
            return
        next_conversation_id = (
            None if value.conversation is None else value.conversation.conversation_id
        )
        if next_conversation_id != self._current_conversation_id:
            self._working_memory_snapshot = None
        self._current_conversation_id = next_conversation_id
        self._next_before_sequence = value.next_before_sequence
        self.conversation_loaded.emit(value)

    def _on_older_messages_loaded(self, value: object) -> None:
        if not isinstance(value, OlderMessagesSnapshot):
            self.operation_failed.emit("older_messages", "InvalidMessageSnapshot")
            return
        if value.conversation_id != self._current_conversation_id:
            return
        self._next_before_sequence = value.next_before_sequence
        self.older_messages_loaded.emit(value)

    # Local time commitments ------------------------------------------
    def create_temporal_chat_draft(self, user_text: str, spec: TemporalDraftSpec) -> bool:
        conversation_id = self._current_conversation_id
        if not self._writable or conversation_id is None:
            self.operation_failed.emit("create_temporal_draft", "DatabaseReadOnlyError")
            return False

        def operation(
            stores: LocalDataStores,
        ) -> tuple[TemporalCommitment, ConversationSnapshot]:
            commitment = stores.temporal_commitments.create_chat_draft(
                conversation_id,
                user_text,
                spec,
            )
            conversation = stores.conversations.get_conversation(conversation_id)
            return commitment, _conversation_snapshot(stores, conversation)

        def completed(value: tuple[TemporalCommitment, ConversationSnapshot]) -> None:
            commitment, snapshot = value
            if self._current_conversation_id == conversation_id:
                self._on_conversation_loaded(
                    snapshot,
                    failure_operation="create_temporal_draft",
                )
            self.temporal_chat_draft_created.emit(commitment)
            self.temporal_commitment_changed.emit(commitment)
            self.refresh_history()
            self.refresh_reminders()

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.FOREGROUND,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit(
                "create_temporal_draft", category
            ),
        )
        if request_id is None:
            self._on_persistence_submission_failed("create_temporal_draft")
            return False
        return True

    def create_manual_temporal_draft(self, spec: TemporalDraftSpec) -> bool:
        if not self._writable:
            self.operation_failed.emit("create_temporal_draft", "DatabaseReadOnlyError")
            return False
        return self._temporal_write(
            "create_temporal_draft",
            lambda stores: stores.temporal_commitments.create_manual_draft(spec),
        )

    def refresh_reminders(self, query: str = "", status: str = "") -> None:
        normalized_status = status.strip()

        def operation(stores: LocalDataStores) -> ReminderListSnapshot:
            if normalized_status == "outstanding":
                commitments = tuple(
                    commitment
                    for commitment in stores.temporal_commitments.list(query=query)
                    if commitment.status
                    in {
                        TemporalCommitmentStatus.DUE,
                        TemporalCommitmentStatus.SURFACED,
                    }
                )
            else:
                commitments = stores.temporal_commitments.list(
                    query=query,
                    status=normalized_status or None,
                )
            return ReminderListSnapshot(
                commitments,
                query=str(query),
                status=normalized_status,
                outstanding_count=stores.temporal_commitments.outstanding_count(),
            )

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=self._on_reminders_loaded,
            on_failure=lambda category: self.operation_failed.emit("reminders", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("reminders")

    def confirm_temporal_commitment(
        self,
        commitment_id: str,
        spec: TemporalDraftSpec,
    ) -> bool:
        return self._temporal_write(
            "confirm_temporal_commitment",
            lambda stores: stores.temporal_commitments.confirm(commitment_id, spec),
        )

    def revise_temporal_commitment(
        self,
        commitment_id: str,
        spec: TemporalDraftSpec,
        *,
        confirm: bool = True,
    ) -> bool:
        return self._temporal_write(
            "revise_temporal_commitment",
            lambda stores: stores.temporal_commitments.revise(
                commitment_id,
                spec,
                confirm=confirm,
            ),
        )

    def cancel_temporal_commitment(self, commitment_id: str) -> bool:
        return self._temporal_write(
            "cancel_temporal_commitment",
            lambda stores: stores.temporal_commitments.cancel(commitment_id),
        )

    def complete_temporal_commitment(self, commitment_id: str) -> bool:
        return self._temporal_write(
            "complete_temporal_commitment",
            lambda stores: stores.temporal_commitments.complete(commitment_id),
        )

    def snooze_temporal_commitment(self, commitment_id: str, minutes: int = 10) -> bool:
        return self._temporal_write(
            "snooze_temporal_commitment",
            lambda stores: stores.temporal_commitments.snooze(
                commitment_id,
                minutes=minutes,
            ),
        )

    def delete_temporal_commitment(self, commitment_id: str) -> bool:
        def operation(stores: LocalDataStores) -> None:
            stores.temporal_commitments.delete(commitment_id)
            stores.database.purge_deleted_content()

        return self._temporal_write("delete_temporal_commitment", operation)

    def scan_due_temporal_commitments(self, *, now: datetime | None = None) -> bool:
        if not self._writable:
            return False

        def completed(snapshot: TemporalDueSnapshot) -> None:
            for commitment in snapshot.became_due:
                self.temporal_commitment_changed.emit(commitment)
            if snapshot.became_due:
                self.refresh_reminders()
                if self._current_conversation_id is not None:
                    self._load_conversation(
                        self._current_conversation_id,
                        source_message_id=None,
                    )
            self.temporal_due_scanned.emit(snapshot)
            self.temporal_outstanding_count_changed.emit(snapshot.outstanding_count)

        request_id = self.runtime.submit(
            lambda stores: stores.temporal_commitments.scan_due(now=now),
            priority=DataPriority.FOREGROUND,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit("scan_reminders", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("scan_reminders")
            return False
        return True

    def mark_temporal_commitments_surfaced(
        self,
        commitment_ids: tuple[str, ...],
        *,
        reason_code: str,
    ) -> bool:
        if not commitment_ids:
            return False

        def completed(value: object) -> None:
            commitments = tuple(value) if isinstance(value, tuple) else ()
            for commitment in commitments:
                self.temporal_commitment_changed.emit(commitment)
            self.refresh_reminders()
            if commitments and self._current_conversation_id is not None:
                self._load_conversation(
                    self._current_conversation_id,
                    source_message_id=None,
                )

        request_id = self.runtime.submit(
            lambda stores: stores.temporal_commitments.mark_surfaced(
                commitment_ids,
                reason_code=reason_code,
            ),
            priority=DataPriority.FOREGROUND,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit(
                "surface_reminders", category
            ),
        )
        if request_id is None:
            self._on_persistence_submission_failed("surface_reminders")
            return False
        return True

    def load_active_temporal_count(self, conversation_id: str | None) -> None:
        key = "*" if conversation_id is None else str(conversation_id)
        request_id = self.runtime.submit(
            lambda stores: stores.temporal_commitments.active_count_for_conversation(
                conversation_id
            ),
            priority=DataPriority.INTERACTIVE,
            on_success=lambda count: self.temporal_active_count_loaded.emit(key, int(count)),
            on_failure=lambda category: self.operation_failed.emit(
                "count_active_reminders", category
            ),
        )
        if request_id is None:
            self._on_persistence_submission_failed("count_active_reminders")

    def open_temporal_followup(
        self,
        commitment_id: str,
        *,
        conversation_id: str | None = None,
    ) -> bool:
        target_conversation_id = conversation_id or self._current_conversation_id
        if not self._writable or target_conversation_id is None:
            self.operation_failed.emit("open_temporal_followup", "ConversationUnavailable")
            return False

        def operation(stores: LocalDataStores) -> ConversationSnapshot:
            stores.temporal_commitments.open_followup_in_chat(
                commitment_id,
                target_conversation_id,
            )
            conversation = stores.conversations.get_conversation(target_conversation_id)
            return _conversation_snapshot(stores, conversation)

        def completed(snapshot: ConversationSnapshot) -> None:
            if self._current_conversation_id == target_conversation_id:
                self._on_conversation_loaded(
                    snapshot,
                    failure_operation="open_temporal_followup",
                )
            self.temporal_followup_opened.emit(commitment_id)
            self.refresh_history()
            self.refresh_reminders()

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.FOREGROUND,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit(
                "open_temporal_followup", category
            ),
        )
        if request_id is None:
            self._on_persistence_submission_failed("open_temporal_followup")
            return False
        return True

    def _temporal_write(
        self,
        operation_name: str,
        operation: Callable[[LocalDataStores], object],
    ) -> bool:
        if not self._writable:
            self.operation_failed.emit(operation_name, "DatabaseReadOnlyError")
            return False

        def completed(value: object) -> None:
            if isinstance(value, TemporalCommitment):
                self.temporal_commitment_changed.emit(value)
            self.refresh_reminders()
            if self._current_conversation_id is not None:
                self._load_conversation(
                    self._current_conversation_id,
                    source_message_id=None,
                )

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit(operation_name, category),
        )
        if request_id is None:
            self._on_persistence_submission_failed(operation_name)
            return False
        return True

    def _on_reminders_loaded(self, value: object) -> None:
        if not isinstance(value, ReminderListSnapshot):
            self.operation_failed.emit("reminders", "InvalidReminderSnapshot")
            return
        self.temporal_outstanding_count_changed.emit(value.outstanding_count)
        self.reminders_loaded.emit(value)

    # Memory administration --------------------------------------------
    def refresh_memories(
        self,
        query: str = "",
        kind: str = "",
        status: str = "",
        sort: str = "updated_desc",
        *,
        pinned: bool | None = None,
    ) -> None:
        working_snapshot = self._working_memory_snapshot
        conversation_id = self._current_conversation_id

        def operation(stores: LocalDataStores) -> MemoryListSnapshot:
            if query.strip():
                records = tuple(
                    result.memory
                    for result in stores.memories.search(
                        query,
                        limit=500,
                        include_archived=status in {"", "archived"},
                    )
                )
            else:
                records = stores.memories.list_memories(
                    kind=kind or None,
                    status=status or None,
                    pinned=pinned,
                )
            if query.strip():
                records = tuple(
                    record
                    for record in records
                    if (not kind or record.kind.value == kind)
                    and (not status or record.status.value == status)
                    and (pinned is None or record.pinned is pinned)
                )
            stats = {
                item.target_id: item
                for item in stores.memories.recall_stats(
                    profile_id=DEFAULT_PROFILE_ID,
                    memory_ids=tuple(record.memory_id for record in records),
                )
            }
            if sort == "recalled_desc":
                ordered = tuple(
                    sorted(
                        records,
                        key=lambda item: (
                            -1.0
                            if stats[item.memory_id].last_recalled_at is None
                            else stats[item.memory_id].last_recalled_at.timestamp(),
                            item.updated_at.timestamp(),
                            item.memory_id,
                        ),
                        reverse=True,
                    )
                )
            else:
                ordered = _sort_memories(records, sort)
            rows = tuple(
                _memory_view_row(record, recall_stats=stats[record.memory_id]) for record in ordered
            )
            if query.strip():
                reflections = tuple(
                    record
                    for record, _rank in stores.deep_memories.search(
                        MemoryLayer.REFLECTION,
                        query,
                        include_inactive=True,
                        limit=500,
                    )
                )
                impressions = tuple(
                    record
                    for record, _rank in stores.deep_memories.search(
                        MemoryLayer.PERSONA,
                        query,
                        include_inactive=True,
                        limit=500,
                    )
                )
            else:
                reflections = stores.deep_memories.list_records(
                    MemoryLayer.REFLECTION,
                    limit=500,
                )
                impressions = stores.deep_memories.list_records(
                    MemoryLayer.PERSONA,
                    limit=500,
                )
            reflection_rows = tuple(_derived_view_row(record) for record in reflections)
            persona_rows = tuple(_derived_view_row(record) for record in impressions)
            static_documents = stores.personas.list_active_documents("kurisu", limit=500)
            static_persona_rows = tuple(
                _static_persona_view_row(document)
                for document in static_documents
                if not query.strip() or query.casefold() in document.content.casefold()
            )
            recent_rows = _recent_view_rows(stores, conversation_id)
            timeline_rows = tuple(
                _timeline_view_row(item) for item in stores.deep_memories.event_timeline(limit=500)
            )
            audit_rows = tuple(
                _audit_view_row(item) for item in stores.deep_memories.list_audit_events(limit=500)
            )
            conflict_rows = tuple(
                _conflict_view_row(item)
                for item in stores.deep_memories.list_conflicts(
                    include_resolved=True,
                    limit=500,
                )
            )
            if not stores.database.read_only:
                stores.companion_cues.expire_due(profile_id=DEFAULT_PROFILE_ID)
            companion_cues = stores.companion_cues.list(profile_id=DEFAULT_PROFILE_ID)
            cue_rows = tuple(_companion_cue_view_row(cue) for cue in companion_cues)
            memory_authorizations: dict[str, tuple[str, str]] = {}
            for cue in companion_cues:
                if cue.status not in {
                    CompanionCueStatus.PROPOSED,
                    CompanionCueStatus.ACTIVE,
                    CompanionCueStatus.SURFACED,
                }:
                    continue
                for source in cue.sources:
                    if source.source_kind is not CompanionCueSourceKind.USER_MESSAGE:
                        memory_authorizations[source.source_target_id] = (
                            cue.cue_id,
                            cue.status.value,
                        )
            rows = tuple(
                {
                    **row,
                    "companion_cue_id": memory_authorizations.get(
                        str(row["version_id"]), (None, None)
                    )[0],
                    "companion_cue_status": memory_authorizations.get(
                        str(row["version_id"]), (None, None)
                    )[1],
                }
                for row in rows
            )
            reflection_rows = tuple(
                {
                    **row,
                    "companion_cue_id": memory_authorizations.get(
                        str(row["version_id"]), (None, None)
                    )[0],
                    "companion_cue_status": memory_authorizations.get(
                        str(row["version_id"]), (None, None)
                    )[1],
                }
                for row in reflection_rows
            )
            persona_rows = tuple(
                {
                    **row,
                    "companion_cue_id": memory_authorizations.get(
                        str(row["version_id"]), (None, None)
                    )[0],
                    "companion_cue_status": memory_authorizations.get(
                        str(row["version_id"]), (None, None)
                    )[1],
                }
                for row in persona_rows
            )
            working_rows = _working_view_rows(working_snapshot)
            failed = tuple(_job_view_row(job) for job in stores.jobs.list_failed())
            return MemoryListSnapshot(
                rows,
                failed,
                working_rows=working_rows,
                recent_rows=recent_rows,
                reflection_rows=reflection_rows,
                persona_rows=persona_rows,
                static_persona_rows=static_persona_rows,
                timeline_rows=timeline_rows,
                audit_rows=audit_rows,
                conflict_rows=conflict_rows,
                cue_rows=cue_rows,
                layer_counts={
                    "working": len(working_rows),
                    "recent": len(recent_rows),
                    "fact": len(rows),
                    "reflection": len(reflection_rows),
                    "persona": len(persona_rows),
                    "timeline": len(timeline_rows),
                    "audit": len(audit_rows),
                    "cue": len(cue_rows),
                },
            )

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=self.memories_loaded.emit,
            on_failure=lambda category: self.operation_failed.emit("memories", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("memories")

    def clear_all_memories(self) -> bool:
        if not self._writable:
            self.operation_failed.emit("clear_memories", "DatabaseReadOnlyError")
            return False

        def completed(count: object) -> None:
            self.memories_cleared.emit(int(count))
            self.index_rebuild_requested.emit("user_memory")
            self.refresh_memories()

        request_id = self.runtime.submit(
            _clear_all_semantic_memory,
            priority=DataPriority.INTERACTIVE,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit("clear_memories", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("clear_memories")
            return False
        return True

    def confirm_companion_cue(
        self,
        cue_id: str,
        topic: str,
        frozen_text: str,
        keep_until_resolved: bool = False,
    ) -> None:
        self._companion_cue_write(
            "confirm_companion_cue",
            lambda stores: stores.companion_cues.confirm(
                cue_id,
                topic=topic,
                frozen_text=frozen_text,
                keep_until_resolved=keep_until_resolved,
            ),
        )

    def reject_companion_cue(self, cue_id: str) -> None:
        self._companion_cue_write(
            "reject_companion_cue",
            lambda stores: stores.companion_cues.reject(cue_id),
        )

    def resolve_companion_cue(self, cue_id: str) -> None:
        self._companion_cue_write(
            "resolve_companion_cue",
            lambda stores: stores.companion_cues.resolve(cue_id),
        )

    def set_companion_cue_keep(self, cue_id: str, enabled: bool) -> None:
        self._companion_cue_write(
            "retain_companion_cue",
            lambda stores: stores.companion_cues.set_keep_until_resolved(
                cue_id,
                enabled,
            ),
        )

    def delete_companion_cue(self, cue_id: str) -> None:
        def operation(stores: LocalDataStores) -> None:
            stores.companion_cues.delete(cue_id)
            stores.database.purge_deleted_content()

        self._companion_cue_write("delete_companion_cue", operation)

    def authorize_memory_followup(self, layer: str, version_id: str) -> None:
        source_kind = {
            MemoryLayer.FACT.value: CompanionCueSourceKind.FACT_VERSION,
            MemoryLayer.REFLECTION.value: CompanionCueSourceKind.REFLECTION_VERSION,
            MemoryLayer.PERSONA.value: CompanionCueSourceKind.PERSONA_VERSION,
        }.get(layer)
        if source_kind is None:
            self.operation_failed.emit("authorize_companion_cue", "InvalidMemoryLayer")
            return
        self._companion_cue_write(
            "authorize_companion_cue",
            lambda stores: stores.companion_cues.propose_memory_followup(
                source_kind,
                version_id,
            ),
        )

    def revoke_memory_followup(self, layer: str, version_id: str) -> None:
        source_kind = {
            MemoryLayer.FACT.value: CompanionCueSourceKind.FACT_VERSION,
            MemoryLayer.REFLECTION.value: CompanionCueSourceKind.REFLECTION_VERSION,
            MemoryLayer.PERSONA.value: CompanionCueSourceKind.PERSONA_VERSION,
        }.get(layer)
        if source_kind is None:
            self.operation_failed.emit("revoke_companion_cue", "InvalidMemoryLayer")
            return
        self._companion_cue_write(
            "revoke_companion_cue",
            lambda stores: stores.companion_cues.revoke_memory_authorization(
                source_kind,
                version_id,
            ),
        )

    def open_companion_cue(
        self,
        cue_id: str,
        *,
        conversation_id: str | None = None,
    ) -> None:
        target_conversation_id = conversation_id or self._current_conversation_id
        if not self._writable or target_conversation_id is None:
            self.operation_failed.emit("open_companion_cue", "ConversationUnavailable")
            return

        def operation(stores: LocalDataStores) -> ConversationSnapshot:
            stores.proactive.persist_companion_cue_manual_open(
                cue_id,
                target_conversation_id,
            )
            conversation = stores.conversations.get_conversation(target_conversation_id)
            return _conversation_snapshot(stores, conversation)

        def completed(snapshot: ConversationSnapshot) -> None:
            if self._current_conversation_id == target_conversation_id:
                self._on_conversation_loaded(snapshot, failure_operation="open_companion_cue")
            self.companion_cue_opened.emit(cue_id)
            self.refresh_history()
            self.refresh_memories()

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit("open_companion_cue", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("open_companion_cue")

    def _companion_cue_write(
        self,
        operation_name: str,
        operation: Callable[[LocalDataStores], object],
    ) -> None:
        if not self._writable:
            self.operation_failed.emit(operation_name, "DatabaseReadOnlyError")
            return

        def completed(value: object) -> None:
            self.companion_cue_changed.emit(value)
            self.refresh_memories()

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit(operation_name, category),
        )
        if request_id is None:
            self._on_persistence_submission_failed(operation_name)

    def load_memory_sources(self, memory_id: str) -> None:
        def operation(stores: LocalDataStores) -> tuple[dict[str, object], ...]:
            record = stores.memories.get(memory_id)
            return tuple(
                _source_view_row(
                    stores,
                    source,
                    version_number=record.current_version.version_number,
                )
                for source in stores.memories.list_sources(
                    memory_id,
                    version_id=record.current_version.version_id,
                )
            )

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=lambda rows: self.memory_sources_loaded.emit(memory_id, rows),
            on_failure=lambda category: self.operation_failed.emit("memory_sources", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("memory_sources")

    def load_layer_details(self, layer: str, memory_id: str) -> None:
        """Load immutable versions and live provenance without blocking the UI thread."""

        if layer not in {
            MemoryLayer.FACT.value,
            MemoryLayer.REFLECTION.value,
            MemoryLayer.PERSONA.value,
        }:
            self.layer_sources_loaded.emit(layer, memory_id, ())
            self.layer_versions_loaded.emit(layer, memory_id, ())
            return

        def operation(
            stores: LocalDataStores,
        ) -> tuple[tuple[dict[str, object], ...], tuple[object, ...]]:
            if layer == MemoryLayer.FACT.value:
                record = stores.memories.get(memory_id)
                sources = tuple(
                    _source_view_row(
                        stores,
                        source,
                        version_number=record.current_version.version_number,
                    )
                    for source in stores.memories.list_sources(
                        memory_id,
                        version_id=record.current_version.version_id,
                    )
                )
                versions: tuple[object, ...] = stores.memories.list_versions(memory_id)
                return sources, versions
            layer_value = MemoryLayer(layer)
            record = stores.deep_memories.get(layer_value, memory_id)
            sources = tuple(
                _derived_source_view_row(
                    stores,
                    source,
                    layer=layer_value,
                    version_number=record.current_version.version_number,
                )
                for source in stores.deep_memories.list_sources(
                    layer_value,
                    memory_id,
                    version_id=record.current_version.version_id,
                )
            )
            return sources, stores.deep_memories.list_versions(layer_value, memory_id)

        def completed(result: object) -> None:
            sources, versions = result
            self.layer_sources_loaded.emit(layer, memory_id, sources)
            self.layer_versions_loaded.emit(layer, memory_id, versions)

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit("memory_details", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("memory_details")

    def load_deletion_impact(self, layer: str, memory_id: str) -> None:
        if layer not in {
            MemoryLayer.FACT.value,
            MemoryLayer.REFLECTION.value,
            MemoryLayer.PERSONA.value,
        }:
            self.operation_failed.emit("memory_delete_impact", "InvalidMemoryLayer")
            return
        request_id = self.runtime.submit(
            lambda stores: stores.deep_memories.deletion_impact(
                MemoryLayer(layer),
                memory_id,
            ),
            priority=DataPriority.INTERACTIVE,
            on_success=lambda impact: self.deletion_impact_loaded.emit(
                layer,
                memory_id,
                impact,
            ),
            on_failure=lambda category: self.operation_failed.emit(
                "memory_delete_impact",
                category,
            ),
        )
        if request_id is None:
            self._on_persistence_submission_failed("memory_delete_impact")

    def edit_memory(self, memory_id: str, content: str) -> None:
        self._memory_write(
            "edit_memory",
            lambda stores: _edit_fact(stores, memory_id, content),
            index_changed=True,
        )

    def set_memory_pinned(self, memory_id: str, pinned: bool) -> None:
        self._memory_write(
            "pin_memory",
            lambda stores: stores.memories.set_pinned(memory_id, pinned),
        )

    def archive_memory(self, memory_id: str) -> None:
        self._memory_write(
            "archive_memory",
            lambda stores: _archive_fact(stores, memory_id),
            index_changed=True,
        )

    def restore_memory(self, memory_id: str) -> None:
        self._memory_write(
            "restore_memory",
            lambda stores: _restore_fact(stores, memory_id),
            index_changed=True,
        )

    def delete_memory(self, memory_id: str) -> None:
        self._memory_write(
            "delete_memory",
            lambda stores: stores.deep_memories.delete_cascade(
                MemoryLayer.FACT,
                memory_id,
            ),
            index_changed=True,
        )

    def edit_derived_memory(self, layer: str, memory_id: str, content: str) -> None:
        self._derived_memory_write(
            "edit_derived_memory",
            layer,
            lambda stores, layer_value: stores.deep_memories.add_version(
                layer_value,
                memory_id,
                content,
            ),
        )

    def set_derived_memory_pinned(
        self,
        layer: str,
        memory_id: str,
        pinned: bool,
    ) -> None:
        self._derived_memory_write(
            "pin_derived_memory",
            layer,
            lambda stores, layer_value: stores.deep_memories.set_pinned(
                layer_value,
                memory_id,
                pinned,
            ),
            index_changed=False,
        )

    def archive_derived_memory(self, layer: str, memory_id: str) -> None:
        self._derived_memory_write(
            "archive_derived_memory",
            layer,
            lambda stores, layer_value: stores.deep_memories.archive(layer_value, memory_id),
        )

    def restore_derived_memory(self, layer: str, memory_id: str) -> None:
        self._derived_memory_write(
            "restore_derived_memory",
            layer,
            lambda stores, layer_value: stores.deep_memories.restore(layer_value, memory_id),
        )

    def delete_derived_memory(self, layer: str, memory_id: str) -> None:
        self._derived_memory_write(
            "delete_derived_memory",
            layer,
            lambda stores, layer_value: stores.deep_memories.delete_cascade(
                layer_value,
                memory_id,
            ),
        )

    def confirm_derived_memory(self, layer: str, memory_id: str) -> None:
        self._derived_memory_write(
            "confirm_derived_memory",
            layer,
            lambda stores, layer_value: stores.deep_memories.confirm(layer_value, memory_id),
        )

    def deny_derived_memory(self, layer: str, memory_id: str) -> None:
        self._derived_memory_write(
            "deny_derived_memory",
            layer,
            lambda stores, layer_value: stores.deep_memories.deny(layer_value, memory_id),
        )

    def rollback_memory(self, layer: str, memory_id: str, version_id: str) -> None:
        if layer == MemoryLayer.FACT.value:
            self._memory_write(
                "rollback_memory",
                lambda stores: _rollback_fact(stores, memory_id, version_id),
                index_changed=True,
            )
            return
        self._derived_memory_write(
            "rollback_derived_memory",
            layer,
            lambda stores, layer_value: stores.deep_memories.rollback(
                layer_value,
                memory_id,
                version_id,
            ),
        )

    def resolve_memory_conflict(
        self,
        conflict_id: str,
        resolution: str,
        merged_content: str = "",
    ) -> None:
        self._memory_write(
            "resolve_memory_conflict",
            lambda stores: stores.deep_memories.resolve_fact_conflict(
                conflict_id,
                resolution,
                merged_content=merged_content if resolution == "merge" else None,
            ),
            index_changed=True,
        )

    def _derived_memory_write(
        self,
        name: str,
        layer: str,
        operation,
        *,
        index_changed: bool = True,
    ) -> None:
        if layer not in {MemoryLayer.REFLECTION.value, MemoryLayer.PERSONA.value}:
            self.operation_failed.emit(name, "InvalidMemoryLayer")
            return
        layer_value = MemoryLayer(layer)
        self._memory_write(
            name,
            lambda stores: operation(stores, layer_value),
            index_changed=index_changed,
        )

    def retry_failed_job(self, job_id: str) -> None:
        self._memory_write(
            "retry_job",
            lambda stores: stores.jobs.retry_failed(job_id),
            jobs_changed=True,
        )

    def _memory_write(
        self,
        name: str,
        operation,
        *,
        jobs_changed: bool = False,
        index_changed: bool = False,
    ) -> None:
        if not self._writable:
            self.operation_failed.emit(name, "DatabaseReadOnlyError")
            return

        def completed(_value: object) -> None:
            if jobs_changed:
                self.jobs_enqueued.emit()
            if index_changed:
                self.index_rebuild_requested.emit("user_memory")
            self.refresh_memories()

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit(name, category),
        )
        if request_id is None:
            self._on_persistence_submission_failed(name)

    def _user_memory_allowed(self, pending: _PendingPromptPreparation) -> bool:
        return (
            pending.memory_enabled
            and self._memory_enabled
            and pending.memory_disable_epoch == self._memory_disable_epoch
        )

    def _deep_memory_allowed(self, pending: _PendingPromptPreparation) -> bool:
        return (
            self._user_memory_allowed(pending)
            and pending.deep_memory_enabled
            and self._deep_memory_enabled
            and pending.deep_memory_disable_epoch == self._deep_memory_disable_epoch
        )

    def stop_prompt_preparations(self) -> None:
        """Reject new prompt work and cancel queued vector queries."""

        self._accept_prompt_preparations = False
        pending = tuple(self._pending_prompt_preparations.values())
        self._pending_prompt_preparations.clear()
        for preparation in pending:
            if preparation.vector_future is not None:
                preparation.vector_future.cancel()

    def shutdown(self, wait_ms: int = 5_000) -> bool:
        self.stop_prompt_preparations()
        self._working_memory_snapshot = None
        self._turn_provider_metadata.clear()
        self._finalized_provider_metadata.clear()
        return self.runtime.shutdown(wait_ms)


def _initialize_stores(stores: LocalDataStores) -> ConversationSnapshot:
    database = stores.database
    if not database.read_only:
        stores.conversations.ensure_default_profile()
        stores.conversations.recover_interrupted_messages()
        stores.jobs.recover_interrupted()
        _cleanup_attachment_orphans(stores)
        conversation = stores.conversations.get_or_create_active_conversation()
    else:
        conversations = stores.conversations.list_conversations()
        conversation = conversations[0] if conversations else None
    return _conversation_snapshot(stores, conversation)


def _edit_fact(stores: LocalDataStores, memory_id: str, content: str) -> MemoryRecord:
    record = stores.memories.edit_memory(memory_id, content)
    stores.deep_memories.suppress_fact_descendants(
        memory_id,
        reason_code="upstream_edited",
    )
    return record


def _rollback_fact(
    stores: LocalDataStores,
    memory_id: str,
    version_id: str,
) -> MemoryRecord:
    versions = stores.memories.list_versions(memory_id)
    selected = next((item for item in versions if item.version_id == version_id), None)
    if selected is None:
        raise StorageNotFoundError("memory version does not exist")
    record = stores.memories.add_version(
        memory_id,
        selected.content,
        importance=selected.importance,
        confidence=1.0,
        operation=MemoryVersionOperation.MANUAL_EDIT,
        source_message_ids=(),
        origin=MemoryVersionOrigin.MANUAL,
        event_started_at=selected.event_started_at,
        event_ended_at=selected.event_ended_at,
        time_confidence=selected.time_confidence,
    )
    stores.deep_memories.suppress_fact_descendants(
        memory_id,
        reason_code="upstream_rollback",
    )
    return record


def _archive_fact(stores: LocalDataStores, memory_id: str) -> MemoryRecord:
    record = stores.memories.archive(memory_id)
    stores.deep_memories.suppress_fact_descendants(
        memory_id,
        reason_code="upstream_archived",
    )
    return record


def _restore_fact(stores: LocalDataStores, memory_id: str) -> MemoryRecord:
    record = stores.memories.restore(memory_id)
    stores.deep_memories.reevaluate_fact_descendants(memory_id)
    return record


def _clear_all_semantic_memory(stores: LocalDataStores) -> int:
    derived = stores.deep_memories.clear_all(profile_id=DEFAULT_PROFILE_ID)
    facts = stores.memories.clear_all_memories(profile_id=DEFAULT_PROFILE_ID)
    return facts + derived


def _conversation_snapshot(
    stores: LocalDataStores,
    conversation: Conversation | None,
) -> ConversationSnapshot:
    if conversation is None:
        return ConversationSnapshot(
            None,
            stores.conversations.list_conversations(),
            (),
            (),
            None,
            stores.database.read_only,
            _migration_error_category(stores.database),
        )
    page = stores.conversations.load_message_page(
        conversation.conversation_id,
        limit=MESSAGE_PAGE_SIZE,
    )
    return _snapshot_from_page(stores, conversation, page)


def _snapshot_from_page(
    stores: LocalDataStores,
    conversation: Conversation,
    page: MessagePage,
) -> ConversationSnapshot:
    return ConversationSnapshot(
        conversation,
        stores.conversations.list_conversations(),
        page.items,
        _turns_from_messages(page.items),
        page.next_before_sequence,
        stores.database.read_only,
        _migration_error_category(stores.database),
        _presentation_entries_from_messages(page.items),
    )


def _migration_error_category(database: SQLiteDatabase) -> str | None:
    error = database.migration_error
    return None if error is None else type(error).__name__


def _build_prompt(
    stores: LocalDataStores,
    conversation_id: str,
    current_user_message: str,
    *,
    memory_enabled: bool,
    prompt_service: DefaultPromptContextService,
) -> tuple[PromptMessage, ...]:
    recent_stored = stores.conversations.load_recent_valid_messages(
        conversation_id,
        limit=PROMPT_RECENT_MESSAGE_LIMIT,
    )
    recent: list[PromptMessage] = []
    for message in recent_stored:
        if message.role is StoredMessageRole.USER:
            recent.append(PromptMessage(PromptRole.USER, message.content))
        elif (
            message.status
            in {
                StoredMessageStatus.COMPLETED,
                StoredMessageStatus.STOPPED,
            }
            and message.content
        ):
            recent.append(PromptMessage(PromptRole.ASSISTANT, message.content))

    memories: tuple[PromptMemory, ...] = ()
    if memory_enabled:
        try:
            memories = tuple(
                PromptMemory(
                    memory_id=result.memory.memory_id,
                    kind=DomainMemoryKind(result.memory.kind.value),
                    content=result.memory.current_version.content,
                    topic_key=result.memory.topic_key,
                    importance=result.memory.current_version.importance,
                    confidence=result.memory.current_version.confidence,
                    pinned=result.memory.pinned,
                    user_confirmed=(
                        result.memory.kind.value == "relationship"
                        and result.memory.current_version.origin is MemoryVersionOrigin.MANUAL
                    ),
                )
                for result in stores.memories.search(current_user_message, limit=30)
            )
        except Exception:
            # A damaged/unavailable keyword index must not turn a durable user
            # message into a network call without a prompt. Continue without
            # long-term recall; the database owner still serializes all access.
            memories = ()
    summary = stores.conversations.latest_summary(conversation_id)
    context = prompt_service.build(
        PromptContextInput(
            safety_boundary=build_capability_safety_boundary(),
            persona=build_persona_core_prompt(),
            current_date=date.today(),
            current_user_message=current_user_message,
            memories=memories,
            summary=None if summary is None else summary.content,
            recent_messages=tuple(recent),
        )
    )
    return context.messages


def _turns_from_messages(messages: tuple[StoredMessage, ...]) -> tuple[ConversationTurn, ...]:
    order: list[str] = []
    grouped: dict[str, dict[StoredMessageRole, StoredMessage]] = {}
    for message in messages:
        if message.origin is not StoredMessageOrigin.CONVERSATION:
            continue
        if message.turn_id not in grouped:
            order.append(message.turn_id)
            grouped[message.turn_id] = {}
        grouped[message.turn_id][message.role] = message

    turns: list[ConversationTurn] = []
    for turn_id in order:
        pair = grouped[turn_id]
        user = pair.get(StoredMessageRole.USER)
        if user is None:
            continue
        assistant = pair.get(StoredMessageRole.ASSISTANT)
        if assistant is None:
            assistant_message = ChatMessage(
                message_id=f"{turn_id}:missing-assistant",
                role=MessageRole.ASSISTANT,
                content="",
                status=MessageStatus.FAILED,
                error="本轮回复未完整保存，可重新发送用户消息。",
            )
            turns.append(
                ConversationTurn(
                    turn_id,
                    _chat_message(user),
                    assistant_message,
                    terminal_reason=TurnTerminalReason.LOCAL_PERSISTENCE_ERROR,
                    provider_error_code="local_persistence",
                    status_text=assistant_message.error,
                    error=assistant_message.error,
                )
            )
            continue
        reason = _terminal_reason(assistant.terminal_reason)
        error = (
            "回复失败，可重试本轮对话。" if assistant.status is StoredMessageStatus.FAILED else None
        )
        turns.append(
            ConversationTurn(
                turn_id=turn_id,
                user_message=_chat_message(user),
                assistant_message=_chat_message(assistant, error=error),
                attempt=assistant.attempt,
                terminal_reason=reason,
                provider_error_code=assistant.failure_code,
                status_text=_restored_status_text(assistant, reason),
                error=error,
            )
        )
    return tuple(turns)


def _presentation_entries_from_messages(
    messages: tuple[StoredMessage, ...],
) -> tuple[ConversationPresentationEntry, ...]:
    """Build exact-order rows without forcing proactive messages into fake turns."""

    turns = {turn.turn_id: turn for turn in _turns_from_messages(messages)}
    entries: list[ConversationPresentationEntry] = []
    seen_turns: set[str] = set()
    for stored in messages:
        if stored.origin is StoredMessageOrigin.PROACTIVE:
            entries.append(
                ConversationPresentationEntry(
                    entry_id=stored.message_id,
                    turn_id=stored.turn_id,
                    user_message=(
                        _chat_message(stored) if stored.role is StoredMessageRole.USER else None
                    ),
                    assistant_message=(
                        _chat_message(stored)
                        if stored.role is StoredMessageRole.ASSISTANT
                        else None
                    ),
                    origin=stored.origin,
                    first_sequence=stored.sequence,
                )
            )
            continue
        if stored.turn_id in seen_turns:
            continue
        seen_turns.add(stored.turn_id)
        turn = turns.get(stored.turn_id)
        if turn is not None:
            user_message = turn.user_message
            assistant_message = turn.assistant_message
        else:
            user_message = _chat_message(stored) if stored.role is StoredMessageRole.USER else None
            assistant_message = (
                _chat_message(stored) if stored.role is StoredMessageRole.ASSISTANT else None
            )
        entries.append(
            ConversationPresentationEntry(
                entry_id=stored.turn_id,
                turn_id=stored.turn_id,
                user_message=user_message,
                assistant_message=assistant_message,
                origin=StoredMessageOrigin.CONVERSATION,
                first_sequence=stored.sequence,
            )
        )
    return tuple(entries)


def _chat_message(message: StoredMessage, *, error: str | None = None) -> ChatMessage:
    return ChatMessage(
        message_id=message.message_id,
        role=MessageRole(message.role.value),
        content=message.content,
        status=MessageStatus(message.status.value),
        error=error,
        attachments=tuple(_attachment_snapshot(item) for item in message.attachments),
        input_modality=InputModality(message.input_modality.value),
        companion_cue_id=message.companion_cue_id,
        companion_source_label=message.companion_source_label,
        temporal_commitment_id=message.temporal_commitment_id,
        temporal_commitment=message.temporal_commitment,
    )


def _stored_attachment(attachment: AttachmentSnapshot) -> StoredAttachment:
    return StoredAttachment(
        attachment_id=attachment.attachment_id,
        kind=StoredAttachmentKind(attachment.kind.value),
        source=StoredAttachmentSource(attachment.source.value),
        display_name=attachment.display_name,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        sha256=attachment.sha256,
        relative_path=attachment.relative_path,
        status="ready",
        extracted_text=attachment.extracted_text,
        text_truncated=attachment.text_truncated,
        created_at=datetime.now().astimezone(),
    )


def _attachment_snapshot(attachment: StoredAttachment) -> AttachmentSnapshot:
    return AttachmentSnapshot(
        attachment_id=attachment.attachment_id,
        kind=AttachmentKind(attachment.kind.value),
        source=AttachmentSource(attachment.source.value),
        display_name=attachment.display_name,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        sha256=attachment.sha256,
        relative_path=attachment.relative_path,
        extracted_text=attachment.extracted_text,
        text_truncated=attachment.text_truncated,
    )


def _cleanup_attachment_orphans(
    stores: LocalDataStores,
    *,
    protected_paths: tuple[str, ...] = (),
) -> None:
    stores.conversations.pop_orphan_attachment_paths()
    referenced = tuple(
        dict.fromkeys((*stores.conversations.referenced_attachment_paths(), *protected_paths))
    )
    stores.attachments.cleanup_unreferenced(referenced)


def _effective_user_text(value: str) -> str:
    return value if value.strip() else "请查看我附上的资料。"


def _terminal_reason(value: str | None) -> TurnTerminalReason | None:
    if value is None:
        return None
    try:
        return TurnTerminalReason(value)
    except ValueError:
        return TurnTerminalReason.PROVIDER_ERROR


def _restored_status_text(
    message: StoredMessage,
    reason: TurnTerminalReason | None,
) -> str | None:
    if message.status is StoredMessageStatus.COMPLETED:
        return "回复完成"
    if message.status is StoredMessageStatus.STOPPED:
        return "应用退出时已停止" if reason is TurnTerminalReason.SHUTDOWN else "用户已停止"
    if message.status is StoredMessageStatus.FAILED:
        return "回复失败，可重试本轮对话。"
    return None


def _memory_view_row(
    record: MemoryRecord,
    *,
    recall_stats: object | None = None,
) -> dict[str, object]:
    version = record.current_version
    return {
        "memory_id": record.memory_id,
        "group_id": record.memory_id,
        "version_id": version.version_id,
        "layer": MemoryLayer.FACT.value,
        "kind": record.kind.value,
        "subject_scope": record.subject_scope.value,
        "status": record.status.value,
        "content": version.content,
        "topic_key": record.topic_key,
        "importance": version.importance,
        "confidence": version.confidence,
        "pinned": record.pinned,
        "version_number": version.version_number,
        "event_started_at": version.event_started_at,
        "event_ended_at": version.event_ended_at,
        "time_confidence": version.time_confidence,
        "deep_memory_eligible": version.deep_memory_eligible,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_recalled_at": getattr(recall_stats, "last_recalled_at", None),
        "successful_recall_count": getattr(
            recall_stats,
            "successful_recall_count",
            0,
        ),
    }


def _derived_view_row(record) -> dict[str, object]:
    version = record.current_version
    return {
        "memory_id": record.group_id,
        "group_id": record.group_id,
        "version_id": version.version_id,
        "layer": record.layer.value,
        "kind": record.layer.value,
        "subject_scope": record.subject_scope.value,
        "status": record.status.value,
        "content": version.content,
        "topic_key": record.topic_key,
        "importance": version.importance,
        "confidence": version.confidence,
        "evidence_score": record.evidence_score,
        "conflicted": record.conflicted,
        "pinned": record.pinned,
        "version_number": version.version_number,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_recalled_at": None,
        "successful_recall_count": 0,
    }


def _companion_cue_view_row(cue) -> dict[str, object]:
    sources = tuple(
        {
            "source_kind": source.source_kind.value,
            "source_target_id": source.source_target_id,
        }
        for source in cue.sources
    )
    return {
        "memory_id": cue.cue_id,
        "group_id": cue.cue_id,
        "version_id": cue.cue_id,
        "cue_id": cue.cue_id,
        "layer": "cue",
        "kind": cue.kind.value,
        "status": cue.status.value,
        "content": cue.frozen_text,
        "frozen_text": cue.frozen_text,
        "topic_key": cue.topic,
        "topic": cue.topic,
        "reason": cue.reason.value,
        "confidence": cue.confidence,
        "keep_until_resolved": cue.keep_until_resolved,
        "source_label": ("待续话题" if cue.kind.value == "conversation_followup" else "已授权记忆"),
        "sources": sources,
        "conversation_id": cue.conversation_id,
        "confirmed_at": cue.confirmed_at,
        "expires_at": cue.expires_at,
        "surfaced_at": cue.surfaced_at,
        "resolved_at": cue.resolved_at,
        "pinned": cue.keep_until_resolved,
        "created_at": cue.created_at,
        "updated_at": cue.updated_at,
    }


def _working_view_rows(
    snapshot: WorkingMemorySnapshot | None,
) -> tuple[dict[str, object], ...]:
    if snapshot is None:
        return ()
    rows: list[dict[str, object]] = [
        {
            "memory_id": f"working:{snapshot.message_id}",
            "group_id": f"working:{snapshot.message_id}",
            "version_id": snapshot.message_id,
            "layer": MemoryLayer.WORKING.value,
            "kind": "current_message",
            "status": "readonly",
            "content": snapshot.query,
            "topic_key": "",
            "score": None,
            "reason": "当前持久化用户消息",
            "pinned": False,
            "created_at": None,
            "updated_at": None,
        }
    ]
    rows.extend(
        {
            "memory_id": f"working:{item.layer.value}:{item.target_id}",
            "group_id": item.target_id,
            "version_id": item.version_id,
            "layer": MemoryLayer.WORKING.value,
            "source_layer": item.layer.value,
            "kind": "retrieval",
            "status": "readonly",
            "content": "",
            "topic_key": "",
            "score": item.score,
            "reason": item.reason,
            "pinned": False,
            "created_at": None,
            "updated_at": None,
        }
        for item in snapshot.selected
    )
    return tuple(rows)


def _recent_view_rows(
    stores: LocalDataStores,
    conversation_id: str | None,
) -> tuple[dict[str, object], ...]:
    if conversation_id is None:
        return ()
    rows: list[dict[str, object]] = []
    summary = stores.conversations.latest_summary(conversation_id)
    if summary is not None:
        rows.append(
            {
                "memory_id": f"summary:{conversation_id}:{summary.covers_through_sequence}",
                "group_id": f"summary:{conversation_id}",
                "version_id": str(summary.covers_through_sequence),
                "layer": MemoryLayer.RECENT.value,
                "kind": "summary",
                "status": "readonly",
                "content": summary.content,
                "topic_key": "滚动摘要",
                "pinned": False,
                "created_at": summary.created_at,
                "updated_at": summary.created_at,
            }
        )
    messages = stores.conversations.load_recent_valid_messages(conversation_id, limit=20)
    rows.extend(
        {
            "memory_id": message.message_id,
            "group_id": message.message_id,
            "version_id": message.message_id,
            "layer": MemoryLayer.RECENT.value,
            "kind": message.role.value,
            "status": "readonly",
            "content": message.content,
            "topic_key": "近期消息",
            "pinned": False,
            "created_at": message.created_at,
            "updated_at": message.updated_at,
        }
        for message in messages
    )
    return tuple(rows)


def _static_persona_view_row(document) -> dict[str, object]:
    return {
        "memory_id": document.knowledge_id,
        "group_id": document.knowledge_id,
        "version_id": document.knowledge_id,
        "layer": MemoryLayer.STATIC_PERSONA.value,
        "kind": "static_persona",
        "status": "readonly",
        "content": document.content,
        "topic_key": " ".join(document.tags),
        "pinned": False,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


def _timeline_view_row(item) -> dict[str, object]:
    return {
        "memory_id": item.memory_id,
        "group_id": item.memory_id,
        "version_id": item.version_id,
        "layer": "timeline",
        "kind": "event",
        "status": "readonly",
        "content": item.content,
        "topic_key": "明确时间" if item.occurred_at_is_explicit else "创建时间投影",
        "importance": item.importance,
        "pinned": False,
        "created_at": item.occurred_at,
        "updated_at": item.occurred_at,
    }


def _audit_view_row(item) -> dict[str, object]:
    return {
        "memory_id": item.event_id,
        "group_id": item.owner_group_id,
        "version_id": item.version_id or "",
        "layer": "audit",
        "source_layer": item.owner_layer.value,
        "kind": item.event_type,
        "status": "readonly",
        "content": "",
        "topic_key": item.reason_code,
        "reinforcement_delta": item.reinforcement_delta,
        "disputation_delta": item.disputation_delta,
        "metadata": item.metadata,
        "pinned": False,
        "created_at": item.occurred_at,
        "updated_at": item.occurred_at,
    }


def _conflict_view_row(item) -> dict[str, object]:
    return {
        "conflict_id": item.conflict_id,
        "target_layer": item.target_layer.value,
        "target_group_id": item.target_group_id,
        "incumbent_version_id": item.incumbent_version_id,
        "challenger_version_id": item.challenger_version_id,
        "status": item.status,
        "resolution": None if item.resolution is None else item.resolution.value,
        "source_message_id": item.source_message_id,
        "created_at": item.created_at,
        "resolved_at": item.resolved_at,
    }


def _source_view_row(
    stores: LocalDataStores,
    source: MemorySource,
    *,
    version_number: int,
) -> dict[str, object]:
    content = ""
    message_created_at = None
    if source.live_message_id is not None:
        message = stores.conversations.get_message(source.live_message_id)
        content = message.content
        message_created_at = message.created_at
    return {
        "conversation_id": source.live_conversation_id,
        "message_id": source.live_message_id,
        "source_message_id": source.source_message_id,
        "content": content,
        "message_created_at": message_created_at,
        "method": source.extraction_method,
        "source_deleted": source.source_deleted,
        "available": not source.is_manual and not source.source_deleted,
        "version_number": version_number,
        "current_version": True,
    }


def _derived_source_view_row(
    stores: LocalDataStores,
    source: object,
    *,
    layer: MemoryLayer,
    version_number: int,
) -> dict[str, object]:
    live_message_id = getattr(source, "live_message_id", None)
    extraction_method = str(getattr(source, "extraction_method", "automatic"))
    content = ""
    conversation_id = None
    message_created_at = None
    if live_message_id is not None:
        message = stores.conversations.get_message(str(live_message_id))
        content = message.content
        conversation_id = message.conversation_id
        message_created_at = message.created_at
    source_deleted = extraction_method == "automatic" and live_message_id is None
    parent_version_id = getattr(source, "parent_version_id", None)
    parent_group_id = None
    parent_layer = None
    if parent_version_id is not None and layer is MemoryLayer.REFLECTION:
        parent = stores.database.connection.execute(
            "SELECT memory_id FROM memory_versions WHERE id = ?",
            (parent_version_id,),
        ).fetchone()
        if parent is not None:
            parent_group_id = str(parent["memory_id"])
            parent_layer = MemoryLayer.FACT.value
    elif parent_version_id is not None and layer is MemoryLayer.PERSONA:
        parent = stores.database.connection.execute(
            "SELECT reflection_id FROM memory_reflection_versions WHERE id = ?",
            (parent_version_id,),
        ).fetchone()
        if parent is not None:
            parent_group_id = str(parent["reflection_id"])
            parent_layer = MemoryLayer.REFLECTION.value
    return {
        "conversation_id": conversation_id,
        "message_id": live_message_id,
        "source_message_id": str(getattr(source, "source_message_id", "")),
        "content": content,
        "message_created_at": message_created_at,
        "method": extraction_method,
        "source_deleted": source_deleted,
        "available": extraction_method != "manual" and not source_deleted,
        "version_number": version_number,
        "current_version": True,
        "parent_version_id": parent_version_id,
        "parent_group_id": parent_group_id,
        "parent_layer": parent_layer,
    }


def _job_view_row(job: BackgroundJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "kind": job.kind,
        "attempt_count": job.attempt_count,
        "last_error_code": job.last_error_code or "unknown",
    }


def _sort_memories(
    records: tuple[MemoryRecord, ...],
    sort: str,
) -> tuple[MemoryRecord, ...]:
    if sort == "created_desc":
        return tuple(
            sorted(records, key=lambda item: (item.created_at, item.memory_id), reverse=True)
        )
    if sort == "pinned_first":
        return tuple(
            sorted(
                records,
                key=lambda item: (item.pinned, item.updated_at, item.memory_id),
                reverse=True,
            )
        )
    if sort == "importance_desc":
        return tuple(
            sorted(
                records,
                key=lambda item: (
                    item.current_version.importance,
                    item.updated_at,
                    item.memory_id,
                ),
                reverse=True,
            )
        )
    if sort == "confidence_desc":
        return tuple(
            sorted(
                records,
                key=lambda item: (
                    item.current_version.confidence,
                    item.updated_at,
                    item.memory_id,
                ),
                reverse=True,
            )
        )
    return tuple(sorted(records, key=lambda item: (item.updated_at, item.memory_id), reverse=True))
