"""Qt-safe conversation coordinator for the P3 simulated chat vertical slice."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from amadeus_desktop.chat_models import (
    ChatMessage,
    ChatRequest,
    ConversationState,
    ConversationTurn,
    MessageRole,
    MessageStatus,
    PromptMessage,
    TurnTerminalReason,
)
from amadeus_desktop.chat_provider import (
    CancellationRequested,
    CancellationToken,
    ChatProvider,
)

FIRST_CHUNK_TIMEOUT_TEXT = "等待回复首段超时，请重试。"
STREAM_IDLE_TIMEOUT_TEXT = "回复流式输出超时，请重试。"
EMPTY_RESPONSE_TEXT = "供应商未返回可显示的文本，请重试。"
USER_STOPPED_TEXT = "用户已停止"
COMPLETED_TEXT = "回复完成"
SHUTDOWN_TEXT = "应用退出时已停止"


class _ProviderWorker(QObject):
    chunk_ready = Signal(str, str)
    completed = Signal(str)
    cancelled = Signal(str)
    failed = Signal(str, str)
    done = Signal()

    def __init__(
        self,
        provider: ChatProvider,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._request = request
        self._cancellation = cancellation

    @Slot()
    def run(self) -> None:
        try:
            for chunk in self._provider.stream(self._request, self._cancellation):
                self._cancellation.raise_if_cancelled()
                if not isinstance(chunk, str):
                    raise TypeError("provider chunks must be strings")
                if chunk:
                    self.chunk_ready.emit(self._request.request_id, chunk)
            self._cancellation.raise_if_cancelled()
            self.completed.emit(self._request.request_id)
        except CancellationRequested:
            self.cancelled.emit(self._request.request_id)
        except Exception as exc:  # noqa: BLE001 - provider boundary normalizes all failures
            message = str(exc).strip() or type(exc).__name__
            self.failed.emit(self._request.request_id, message)
        finally:
            self.done.emit()


@dataclass(slots=True)
class _RequestContext:
    request_id: str
    turn_id: str
    cancellation: CancellationToken
    thread: QThread
    worker: _ProviderWorker
    terminal_state: ConversationState | None = None


class ConversationCoordinator(QObject):
    """Own in-memory turns and serialize cancellable provider attempts."""

    state_changed = Signal(object)
    turn_added = Signal(object)
    turn_updated = Signal(object)
    chunk_received = Signal(str, str, str)
    request_finished = Signal(str, object, object)
    error_occurred = Signal(str, str, str)

    def __init__(
        self,
        provider: ChatProvider,
        *,
        first_chunk_timeout_ms: int = 15_000,
        stream_idle_timeout_ms: int = 30_000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if first_chunk_timeout_ms <= 0:
            raise ValueError("first_chunk_timeout_ms must be positive")
        if stream_idle_timeout_ms <= 0:
            raise ValueError("stream_idle_timeout_ms must be positive")
        self._provider = provider
        self._state = ConversationState.IDLE
        self._turns: list[ConversationTurn] = []
        self._contexts: dict[str, _RequestContext] = {}
        self._active_request_id: str | None = None
        self._shutting_down = False

        self._first_chunk_timer = QTimer(self)
        self._first_chunk_timer.setSingleShot(True)
        self._first_chunk_timer.setInterval(first_chunk_timeout_ms)
        self._first_chunk_timer.timeout.connect(self._on_first_chunk_timeout)

        self._stream_idle_timer = QTimer(self)
        self._stream_idle_timer.setSingleShot(True)
        self._stream_idle_timer.setInterval(stream_idle_timeout_ms)
        self._stream_idle_timer.timeout.connect(self._on_stream_idle_timeout)

    @property
    def state(self) -> ConversationState:
        return self._state

    @property
    def turns(self) -> tuple[ConversationTurn, ...]:
        return tuple(self._turns)

    @property
    def active_request_id(self) -> str | None:
        return self._active_request_id

    @property
    def is_active(self) -> bool:
        context = self._active_context()
        return context is not None and context.terminal_state is None

    @property
    def has_running_worker(self) -> bool:
        return any(context.thread.isRunning() for context in self._contexts.values())

    @property
    def has_active_timers(self) -> bool:
        return self._first_chunk_timer.isActive() or self._stream_idle_timer.isActive()

    def send_message(self, text: str) -> ConversationTurn | None:
        """Append a new in-memory turn and start exactly one provider attempt."""

        if self._shutting_down or self._active_request_id is not None or not text.strip():
            return None
        turn = ConversationTurn(
            turn_id=uuid4().hex,
            user_message=ChatMessage(
                message_id=uuid4().hex,
                role=MessageRole.USER,
                content=text,
                status=MessageStatus.COMPLETED,
            ),
            assistant_message=ChatMessage(
                message_id=uuid4().hex,
                role=MessageRole.ASSISTANT,
                content="",
                status=MessageStatus.PENDING,
            ),
        )
        self._turns.append(turn)
        self.turn_added.emit(turn)
        self._start_attempt(turn)
        return turn

    def retry(self, turn_id: str) -> bool:
        """Retry a failed turn without adding or replacing either message ID."""

        if self._shutting_down or self._active_request_id is not None:
            return False
        index = self._turn_index(turn_id)
        if index is None:
            return False
        previous = self._turns[index]
        if previous.assistant_message.status is not MessageStatus.FAILED:
            return False
        retried = replace(
            previous,
            assistant_message=replace(
                previous.assistant_message,
                content="",
                status=MessageStatus.PENDING,
                error=None,
            ),
            attempt=previous.attempt + 1,
            terminal_reason=None,
            status_text=None,
            error=None,
        )
        self._turns[index] = retried
        self.turn_updated.emit(retried)
        self._start_attempt(retried)
        return True

    def stop(self) -> bool:
        """Stop the active attempt while preserving its current assistant text."""

        context = self._active_context()
        if context is None or context.terminal_state is not None:
            return False
        self._terminalize(
            context,
            state=ConversationState.STOPPED,
            message_status=MessageStatus.STOPPED,
            reason=TurnTerminalReason.USER_STOPPED,
            status_text=USER_STOPPED_TEXT,
        )
        context.cancellation.cancel()
        context.thread.requestInterruption()
        context.thread.quit()
        return True

    def shutdown(self, wait_ms: int = 2_000) -> bool:
        """Cancel all workers, stop timers, and wait boundedly for QThread exit."""

        if wait_ms < 0:
            raise ValueError("wait_ms must be non-negative")
        self._shutting_down = True
        self._stop_timers()
        active = self._active_context()
        if active is not None and active.terminal_state is None:
            self._terminalize(
                active,
                state=ConversationState.STOPPED,
                message_status=MessageStatus.STOPPED,
                reason=TurnTerminalReason.SHUTDOWN,
                status_text=SHUTDOWN_TEXT,
            )

        contexts = tuple(self._contexts.values())
        for context in contexts:
            context.cancellation.cancel()
            context.thread.requestInterruption()
            context.thread.quit()

        deadline = time.monotonic() + wait_ms / 1000
        clean = True
        for context in contexts:
            if not context.thread.isRunning():
                continue
            remaining_ms = max(0, round((deadline - time.monotonic()) * 1000))
            if not context.thread.wait(remaining_ms):
                clean = False

        for request_id, context in tuple(self._contexts.items()):
            if not context.thread.isRunning():
                self._contexts.pop(request_id, None)
                context.thread.deleteLater()
        self._active_request_id = None
        self._set_state(ConversationState.IDLE)
        return clean and not self.has_running_worker

    def _start_attempt(self, turn: ConversationTurn) -> None:
        self._set_state(ConversationState.SENDING)
        request_id = uuid4().hex
        request = ChatRequest(
            request_id=request_id,
            turn_id=turn.turn_id,
            attempt=turn.attempt,
            messages=self._prompt_messages(turn.turn_id),
        )
        cancellation = CancellationToken()
        thread = QThread(self)
        thread.setObjectName(f"conversation-{request_id[:8]}")
        thread.setProperty("conversation_request_id", request_id)
        worker = _ProviderWorker(self._provider, request, cancellation)
        worker.moveToThread(thread)
        context = _RequestContext(request_id, turn.turn_id, cancellation, thread, worker)
        self._contexts[request_id] = context
        self._active_request_id = request_id

        thread.started.connect(worker.run)
        worker.chunk_ready.connect(self._on_chunk)
        worker.completed.connect(self._on_completed)
        worker.cancelled.connect(self._on_cancelled)
        worker.failed.connect(self._on_failed)
        worker.done.connect(worker.deleteLater)
        worker.done.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._on_thread_finished)

        self._set_state(ConversationState.WAITING_FIRST_CHUNK)
        self._first_chunk_timer.start()
        thread.start()

    def _prompt_messages(self, current_turn_id: str) -> tuple[PromptMessage, ...]:
        messages: list[PromptMessage] = []
        for turn in self._turns:
            messages.append(PromptMessage(MessageRole.USER, turn.user_message.content))
            if (
                turn.turn_id != current_turn_id
                and turn.assistant_message.status is MessageStatus.COMPLETED
            ):
                messages.append(
                    PromptMessage(MessageRole.ASSISTANT, turn.assistant_message.content)
                )
        return tuple(messages)

    @Slot(str, str)
    def _on_chunk(self, request_id: str, chunk: str) -> None:
        context = self._matching_active_context(request_id)
        if context is None:
            return
        turn = self._turn(context.turn_id)
        if turn is None:
            return
        if self._state is ConversationState.WAITING_FIRST_CHUNK:
            self._first_chunk_timer.stop()
            self._set_state(ConversationState.STREAMING)
        assistant = replace(
            turn.assistant_message,
            content=turn.assistant_message.content + chunk,
            status=MessageStatus.STREAMING,
            error=None,
        )
        updated = replace(turn, assistant_message=assistant)
        self._replace_turn(updated)
        self.chunk_received.emit(request_id, context.turn_id, chunk)
        self.turn_updated.emit(updated)
        self._stream_idle_timer.start()

    @Slot(str)
    def _on_completed(self, request_id: str) -> None:
        context = self._matching_active_context(request_id)
        if context is None:
            return
        turn = self._turn(context.turn_id)
        if turn is None:
            return
        if not turn.assistant_message.content:
            self._terminalize_failure(
                context,
                reason=TurnTerminalReason.EMPTY_RESPONSE,
                error=EMPTY_RESPONSE_TEXT,
            )
            return
        self._terminalize(
            context,
            state=ConversationState.COMPLETED,
            message_status=MessageStatus.COMPLETED,
            reason=TurnTerminalReason.COMPLETED,
            status_text=COMPLETED_TEXT,
        )

    @Slot(str)
    def _on_cancelled(self, request_id: str) -> None:
        context = self._matching_active_context(request_id)
        if context is None:
            return
        self._terminalize(
            context,
            state=ConversationState.STOPPED,
            message_status=MessageStatus.STOPPED,
            reason=TurnTerminalReason.USER_STOPPED,
            status_text=USER_STOPPED_TEXT,
        )

    @Slot(str, str)
    def _on_failed(self, request_id: str, error: str) -> None:
        context = self._matching_active_context(request_id)
        if context is None:
            return
        self._terminalize_failure(
            context,
            reason=TurnTerminalReason.PROVIDER_ERROR,
            error=error,
        )

    @Slot()
    def _on_first_chunk_timeout(self) -> None:
        context = self._active_context()
        if (
            context is None
            or context.terminal_state is not None
            or self._state is not ConversationState.WAITING_FIRST_CHUNK
        ):
            return
        self._terminalize_failure(
            context,
            reason=TurnTerminalReason.FIRST_CHUNK_TIMEOUT,
            error=FIRST_CHUNK_TIMEOUT_TEXT,
        )
        context.cancellation.cancel()
        context.thread.requestInterruption()
        context.thread.quit()

    @Slot()
    def _on_stream_idle_timeout(self) -> None:
        context = self._active_context()
        if (
            context is None
            or context.terminal_state is not None
            or self._state is not ConversationState.STREAMING
        ):
            return
        self._terminalize_failure(
            context,
            reason=TurnTerminalReason.STREAM_IDLE_TIMEOUT,
            error=STREAM_IDLE_TIMEOUT_TEXT,
        )
        context.cancellation.cancel()
        context.thread.requestInterruption()
        context.thread.quit()

    @Slot()
    def _on_thread_finished(self) -> None:
        sender = self.sender()
        if not isinstance(sender, QThread):
            return
        request_id = sender.property("conversation_request_id")
        if not isinstance(request_id, str):
            return
        context = self._contexts.pop(request_id, None)
        if context is None:
            return
        context.thread.wait()
        context.thread.deleteLater()
        if self._active_request_id != request_id:
            return
        if context.terminal_state is None and not self._shutting_down:
            self._terminalize_failure(
                context,
                reason=TurnTerminalReason.PROVIDER_ERROR,
                error="供应商任务意外结束，请重试。",
            )
        self._active_request_id = None
        self._stop_timers()
        self._set_state(ConversationState.IDLE)

    def _terminalize_failure(
        self,
        context: _RequestContext,
        *,
        reason: TurnTerminalReason,
        error: str,
    ) -> None:
        self._terminalize(
            context,
            state=ConversationState.FAILED,
            message_status=MessageStatus.FAILED,
            reason=reason,
            status_text=error,
            error=error,
        )
        self.error_occurred.emit(context.request_id, context.turn_id, error)

    def _terminalize(
        self,
        context: _RequestContext,
        *,
        state: ConversationState,
        message_status: MessageStatus,
        reason: TurnTerminalReason,
        status_text: str,
        error: str | None = None,
    ) -> None:
        if context.terminal_state is not None:
            return
        context.terminal_state = state
        self._stop_timers()
        turn = self._turn(context.turn_id)
        if turn is None:
            return
        updated = replace(
            turn,
            assistant_message=replace(
                turn.assistant_message,
                status=message_status,
                error=error,
            ),
            terminal_reason=reason,
            status_text=status_text,
            error=error,
        )
        self._replace_turn(updated)
        self.turn_updated.emit(updated)
        self._set_state(state)
        self.request_finished.emit(context.request_id, updated, state)

    def _matching_active_context(self, request_id: str) -> _RequestContext | None:
        if self._shutting_down or self._active_request_id != request_id:
            return None
        context = self._contexts.get(request_id)
        if context is None or context.terminal_state is not None:
            return None
        return context

    def _active_context(self) -> _RequestContext | None:
        if self._active_request_id is None:
            return None
        return self._contexts.get(self._active_request_id)

    def _turn_index(self, turn_id: str) -> int | None:
        for index, turn in enumerate(self._turns):
            if turn.turn_id == turn_id:
                return index
        return None

    def _turn(self, turn_id: str) -> ConversationTurn | None:
        index = self._turn_index(turn_id)
        return self._turns[index] if index is not None else None

    def _replace_turn(self, turn: ConversationTurn) -> None:
        index = self._turn_index(turn.turn_id)
        if index is not None:
            self._turns[index] = turn

    def _stop_timers(self) -> None:
        self._first_chunk_timer.stop()
        self._stream_idle_timer.stop()

    def _set_state(self, state: ConversationState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.emit(state)
