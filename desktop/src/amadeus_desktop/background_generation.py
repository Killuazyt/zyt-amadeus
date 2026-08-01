"""Low-priority, cancellable provider runner for summaries and memory jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from amadeus_desktop.chat_models import ChatRequest
from amadeus_desktop.chat_provider import (
    CancellationRequested,
    CancellationToken,
    ChatProvider,
    ChatProviderError,
    ProviderErrorCode,
)

MAX_BACKGROUND_OUTPUT_CHARS = 65_536


class _GenerationWorker(QObject):
    completed = Signal(str, str)
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
            content = asyncio.run(self._collect())
            self.completed.emit(self._request.request_id, content)
        except CancellationRequested:
            self.cancelled.emit(self._request.request_id)
        except ChatProviderError as exc:
            self.failed.emit(self._request.request_id, exc.code.value)
        except Exception:  # noqa: BLE001 - no remote text crosses this boundary
            self.failed.emit(self._request.request_id, ProviderErrorCode.PROTOCOL.value)
        finally:
            self.done.emit()

    async def _collect(self) -> str:
        self._cancellation.bind_current_task()
        chunks: list[str] = []
        characters = 0
        try:
            async for chunk in self._provider.stream(self._request, self._cancellation):
                self._cancellation.raise_if_cancelled()
                if not isinstance(chunk, str):
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                characters += len(chunk)
                if characters > MAX_BACKGROUND_OUTPUT_CHARS:
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                chunks.append(chunk)
            self._cancellation.raise_if_cancelled()
        except asyncio.CancelledError as exc:
            raise CancellationRequested from exc
        finally:
            self._cancellation.unbind_current_task()
        content = "".join(chunks)
        if not content.strip():
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        return content


@dataclass(slots=True)
class _GenerationContext:
    request: ChatRequest
    cancellation: CancellationToken
    thread: QThread
    worker: _GenerationWorker
    on_success: Callable[[str], None]
    on_failure: Callable[[str], None]
    terminal: bool = False


class BackgroundGenerationRunner(QObject):
    """Run at most one background model request and support pause-before-switch."""

    idle = Signal()

    def __init__(self, provider: ChatProvider, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._context: _GenerationContext | None = None
        self._paused = False
        self._shutting_down = False

    @property
    def is_running(self) -> bool:
        context = self._context
        return context is not None and context.thread.isRunning()

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(
        self,
        request: ChatRequest,
        *,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
    ) -> bool:
        if self._paused or self._shutting_down or self._context is not None:
            return False
        cancellation = CancellationToken()
        thread = QThread(self)
        thread.setObjectName(f"background-generation-{request.request_id[:8]}")
        worker = _GenerationWorker(self._provider, request, cancellation)
        worker.moveToThread(thread)
        context = _GenerationContext(
            request,
            cancellation,
            thread,
            worker,
            on_success,
            on_failure,
        )
        self._context = context
        thread.started.connect(worker.run)
        worker.completed.connect(self._on_completed)
        worker.cancelled.connect(self._on_cancelled)
        worker.failed.connect(self._on_failed)
        worker.done.connect(worker.deleteLater)
        worker.done.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._on_thread_finished)
        thread.start()
        return True

    def pause(self, wait_ms: int = 2_000) -> bool:
        self._paused = True
        return self._cancel_and_wait(wait_ms)

    def resume(self) -> None:
        if not self._shutting_down:
            self._paused = False

    def set_provider(self, provider: ChatProvider) -> bool:
        if self._context is not None:
            return False
        self._provider = provider
        return True

    def shutdown(self, wait_ms: int = 2_000) -> bool:
        self._shutting_down = True
        self._paused = True
        return self._cancel_and_wait(wait_ms)

    def _cancel_and_wait(self, wait_ms: int) -> bool:
        if wait_ms < 0:
            raise ValueError("wait_ms must be non-negative")
        context = self._context
        if context is None:
            return True
        context.cancellation.cancel()
        context.thread.requestInterruption()
        context.thread.quit()
        if context.thread.isRunning() and not context.thread.wait(wait_ms):
            return False
        # ``wait`` blocks the UI event loop, so the queued ``finished`` slot
        # cannot clear the context before a credential switch. Finalize it here
        # once the native thread has actually stopped; queued late signals then
        # become harmless because they no longer match an active context.
        if self._context is context:
            try:
                if not context.terminal:
                    context.terminal = True
                    context.on_failure("cancelled")
            finally:
                context.thread.deleteLater()
                self._context = None
                self.idle.emit()
        return True

    @Slot(str, str)
    def _on_completed(self, request_id: str, content: str) -> None:
        context = self._matching(request_id)
        if context is None:
            return
        context.terminal = True
        context.on_success(content)

    @Slot(str)
    def _on_cancelled(self, request_id: str) -> None:
        context = self._matching(request_id)
        if context is None:
            return
        context.terminal = True
        context.on_failure("cancelled")

    @Slot(str, str)
    def _on_failed(self, request_id: str, category: str) -> None:
        context = self._matching(request_id)
        if context is None:
            return
        context.terminal = True
        context.on_failure(category)

    @Slot()
    def _on_thread_finished(self) -> None:
        context = self._context
        if context is None or self.sender() is not context.thread:
            return
        context.thread.wait()
        if not context.terminal:
            context.on_failure("worker_ended")
        context.thread.deleteLater()
        self._context = None
        self.idle.emit()

    def _matching(self, request_id: str) -> _GenerationContext | None:
        context = self._context
        if context is None or context.request.request_id != request_id or context.terminal:
            return None
        return context
