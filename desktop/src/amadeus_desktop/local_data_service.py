"""Qt-facing P5A application service over the single serialized data thread."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import date
from inspect import Parameter, signature
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from amadeus_desktop.chat_models import (
    ChatMessage,
    ConversationTurn,
    MessageRole,
    MessageStatus,
    PreparedPrompt,
    PromptMessage,
    PromptRole,
    TurnTerminalReason,
)
from amadeus_desktop.conversation_store import BackgroundJobStore, ConversationStore
from amadeus_desktop.data_runtime import DataPriority, SerialDataThread
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.memory_models import MemoryKind as DomainMemoryKind
from amadeus_desktop.memory_models import PromptMemory
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
    Conversation,
    MemoryRecord,
    MemorySource,
    MessagePage,
    StorageNotFoundError,
    StoredMessage,
    StoredMessageRole,
    StoredMessageStatus,
)
from amadeus_desktop.vector_store import VectorStore

SUMMARY_MESSAGE_THRESHOLD = 12
SUMMARY_CHARACTER_THRESHOLD = 6_000
MESSAGE_PAGE_SIZE = 40
# The current durable user row is part of the SQL result and is removed by the
# prompt budgeter, so fetch one extra row to retain 20 historical messages.
PROMPT_RECENT_MESSAGE_LIMIT = 21
_USER_MEMORY_SECTION_PREFIX = "[用户长期记忆："


@dataclass(slots=True)
class LocalDataStores:
    database: SQLiteDatabase
    conversations: ConversationStore
    memories: MemoryService
    personas: PersonaRepository
    vectors: VectorStore
    jobs: BackgroundJobStore

    def close(self) -> None:
        self.database.close()


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    conversation: Conversation | None
    conversations: tuple[Conversation, ...]
    messages: tuple[StoredMessage, ...]
    turns: tuple[ConversationTurn, ...]
    next_before_sequence: int | None
    read_only: bool
    migration_error_category: str | None = None


@dataclass(frozen=True, slots=True)
class OlderMessagesSnapshot:
    conversation_id: str
    messages: tuple[StoredMessage, ...]
    turns: tuple[ConversationTurn, ...]
    next_before_sequence: int | None


@dataclass(frozen=True, slots=True)
class MemoryListSnapshot:
    rows: tuple[dict[str, object], ...]
    failed_jobs: tuple[dict[str, object], ...]


@dataclass(slots=True)
class _PendingPromptPreparation:
    seed: PromptRetrievalSeed
    turn: object
    memory_enabled: bool
    memory_disable_epoch: int
    on_success: Callable[[PreparedPrompt], None]
    on_failure: Callable[[str], None]
    vector_future: Future[VectorRetrievalResult] | None = None


def _supports_include_user(vector_query: Callable[..., object] | None) -> bool:
    """Detect the P5B keyword without breaking legacy injected callbacks."""

    if vector_query is None:
        return False
    try:
        parameters = signature(vector_query).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "include_user" or parameter.kind is Parameter.VAR_KEYWORD
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
                and message.content.startswith(_USER_MEMORY_SECTION_PREFIX)
            )
        ),
        user_memory_version_ids=(),
    )


def create_local_data_stores(database_path: Path, backup_directory: Path) -> LocalDataStores:
    """Open and construct all synchronous repositories on the caller's thread."""

    # Imported lazily so the pure database lifecycle remains independently testable.
    from amadeus_desktop.memory_store import MemoryStore

    database = SQLiteDatabase(database_path, backup_dir=backup_directory).open()
    return LocalDataStores(
        database=database,
        conversations=ConversationStore(database),
        memories=MemoryStore(database),
        personas=PersonaRepository(database),
        vectors=VectorStore(database),
        jobs=BackgroundJobStore(database),
    )


class LocalDataService(QObject):
    """Conversation persistence, history, keyword recall, and memory administration."""

    startup_loaded = Signal(object)
    startup_failed = Signal(str)
    conversation_loaded = Signal(object)
    older_messages_loaded = Signal(object)
    history_loaded = Signal(object, str)
    memories_loaded = Signal(object)
    memory_sources_loaded = Signal(str, object)
    source_context_loaded = Signal(object, str)
    operation_failed = Signal(str, str)
    write_availability_changed = Signal(bool)
    jobs_enqueued = Signal()
    index_rebuild_requested = Signal(str)
    _vector_query_completed = Signal(str, object)

    def __init__(
        self,
        runtime: SerialDataThread,
        *,
        memory_enabled: bool = True,
        prompt_service: DefaultPromptContextService | None = None,
        vector_query: Callable[..., Future[VectorRetrievalResult]] | None = None,
        vector_timeout_ms: int = 500,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self._memory_enabled = bool(memory_enabled)
        self._memory_disable_epoch = 0
        self._prompt_service = prompt_service or DefaultPromptContextService()
        self._vector_query = vector_query
        self._vector_query_supports_include_user = _supports_include_user(vector_query)
        self._vector_timeout_ms = max(1, int(vector_timeout_ms))
        self._pending_prompt_preparations: dict[str, _PendingPromptPreparation] = {}
        self._accept_prompt_preparations = True
        self._current_conversation_id: str | None = None
        self._next_before_sequence: int | None = None
        self._writable = False
        self._provider_name: str | None = None
        self._model_name: str | None = None
        self._started = False
        runtime.ready_changed.connect(self._on_runtime_ready)
        runtime.initialization_failed.connect(self.startup_failed.emit)
        self._vector_query_completed.connect(self._on_vector_query_completed)

    @property
    def memory_enabled(self) -> bool:
        return self._memory_enabled

    @property
    def current_conversation_id(self) -> str | None:
        return self._current_conversation_id

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

    def set_provider_metadata(self, provider_name: str | None, model_name: str | None) -> None:
        self._provider_name = provider_name
        self._model_name = model_name

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
        memory_disable_epoch = self._memory_disable_epoch

        def operation(stores: LocalDataStores) -> PromptRetrievalSeed:
            stores.conversations.save_turn(
                conversation_id,
                turn.turn_id,
                turn.user_message.message_id,
                turn.user_message.content,
                turn.assistant_message.message_id,
                attempt=turn.attempt,
                participates_in_memory=memory_enabled,
            )
            return collect_prompt_retrieval_seed(
                stores,
                conversation_id,
                turn.user_message.content,
                memory_enabled=memory_enabled,
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
        memory_disable_epoch = self._memory_disable_epoch

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
                    participates_in_memory=memory_enabled,
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
                turn.user_message.content,
                memory_enabled=memory_enabled,
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
                future = self._vector_query(
                    seed.retrieval_query,
                    include_user=include_user,
                )
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

        def operation(stores: LocalDataStores) -> PreparedPrompt:
            try:
                return finalize_prepared_prompt(
                    stores,
                    pending.seed,
                    turn_id=pending.turn.turn_id,
                    attempt=pending.turn.attempt,
                    memory_enabled=memory_enabled,
                    vector_result=vector_result,
                    prompt_service=self._prompt_service,
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
        provider_name = self._provider_name
        model_name = self._model_name

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

        def operation(stores: LocalDataStores) -> tuple[int, int]:
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
            return user_count, persona_count

        self.runtime.submit(
            operation,
            priority=DataPriority.BACKGROUND,
            on_failure=lambda category: self.operation_failed.emit("recall_event", category),
        )

    def run_memory_maintenance(self) -> None:
        """Archive eligible ordinary events without touching disabled memory."""

        if not self._writable or not self._memory_enabled:
            return

        def completed(memory_ids: tuple[str, ...]) -> None:
            if memory_ids:
                self.index_rebuild_requested.emit("user_memory")
                self.refresh_memories()

        self.runtime.submit(
            lambda stores: stores.memories.archive_decayed_events(profile_id=DEFAULT_PROFILE_ID),
            priority=DataPriority.BACKGROUND,
            on_success=completed,
            on_failure=lambda category: self.operation_failed.emit("memory_maintenance", category),
        )

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
            on_success=self._on_conversation_loaded,
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

    def delete_conversation(self, conversation_id: str) -> None:
        if not self._writable:
            self.operation_failed.emit("delete_conversation", "DatabaseReadOnlyError")
            return

        current_conversation_id = self._current_conversation_id

        def operation(stores: LocalDataStores) -> ConversationSnapshot:
            stores.conversations.delete_conversation(conversation_id)
            if current_conversation_id is not None and current_conversation_id != conversation_id:
                conversation = stores.conversations.get_conversation(current_conversation_id)
            else:
                conversation = stores.conversations.get_or_create_active_conversation()
            return _conversation_snapshot(stores, conversation)

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=self._on_conversation_loaded,
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
            conversation = stores.conversations.create_conversation()
            return _conversation_snapshot(stores, conversation)

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=self._on_conversation_loaded,
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

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=loaded,
            on_failure=lambda category: self.operation_failed.emit("conversation", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("conversation")

    def _on_conversation_loaded(self, value: object) -> None:
        if not isinstance(value, ConversationSnapshot):
            self.operation_failed.emit("conversation", "InvalidConversationSnapshot")
            return
        self._current_conversation_id = (
            None if value.conversation is None else value.conversation.conversation_id
        )
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
            failed = tuple(_job_view_row(job) for job in stores.jobs.list_failed())
            return MemoryListSnapshot(rows, failed)

        request_id = self.runtime.submit(
            operation,
            priority=DataPriority.INTERACTIVE,
            on_success=self.memories_loaded.emit,
            on_failure=lambda category: self.operation_failed.emit("memories", category),
        )
        if request_id is None:
            self._on_persistence_submission_failed("memories")

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

    def edit_memory(self, memory_id: str, content: str) -> None:
        self._memory_write(
            "edit_memory",
            lambda stores: stores.memories.edit_memory(memory_id, content),
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
            lambda stores: stores.memories.archive(memory_id),
            index_changed=True,
        )

    def restore_memory(self, memory_id: str) -> None:
        self._memory_write(
            "restore_memory",
            lambda stores: stores.memories.restore(memory_id),
            index_changed=True,
        )

    def delete_memory(self, memory_id: str) -> None:
        self._memory_write(
            "delete_memory",
            lambda stores: stores.memories.delete_memory(memory_id),
            index_changed=True,
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
        return self.runtime.shutdown(wait_ms)


def _initialize_stores(stores: LocalDataStores) -> ConversationSnapshot:
    database = stores.database
    if not database.read_only:
        stores.conversations.ensure_default_profile()
        stores.conversations.recover_interrupted_messages()
        stores.jobs.recover_interrupted()
        conversation = stores.conversations.get_or_create_active_conversation()
    else:
        conversations = stores.conversations.list_conversations()
        conversation = conversations[0] if conversations else None
    return _conversation_snapshot(stores, conversation)


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


def _chat_message(message: StoredMessage, *, error: str | None = None) -> ChatMessage:
    return ChatMessage(
        message_id=message.message_id,
        role=MessageRole(message.role.value),
        content=message.content,
        status=MessageStatus(message.status.value),
        error=error,
    )


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
        "kind": record.kind.value,
        "status": record.status.value,
        "content": version.content,
        "topic_key": record.topic_key,
        "importance": version.importance,
        "confidence": version.confidence,
        "pinned": record.pinned,
        "version_number": version.version_number,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_recalled_at": getattr(recall_stats, "last_recalled_at", None),
        "successful_recall_count": getattr(
            recall_stats,
            "successful_recall_count",
            0,
        ),
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
