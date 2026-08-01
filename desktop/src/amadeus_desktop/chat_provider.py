"""Provider-neutral streaming contract and deterministic P3 mock provider."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol, runtime_checkable

from amadeus_desktop.chat_models import ChatRequest


class ChatProviderError(Exception):
    """Normalized provider failure safe to present to the chat UI."""


class CancellationRequested(ChatProviderError):
    """Raised cooperatively when the current provider request is cancelled."""


class CancellationToken:
    """Small thread-safe cancellation primitive shared with provider workers."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def wait(self, timeout_seconds: float | None = None) -> bool:
        """Wait for cancellation and return whether cancellation occurred."""

        return self._event.wait(timeout_seconds)

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise CancellationRequested("request cancelled")


@runtime_checkable
class ChatProvider(Protocol):
    """Synchronous streaming boundary executed exclusively off the UI thread."""

    def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> Iterable[str]:
        """Yield user-visible text chunks until completion or cancellation."""


class ScriptedScenario(StrEnum):
    """Deterministic scenarios used by P3 runtime simulation and tests."""

    NORMAL = "normal"
    SLOW_FIRST = "slow_first"
    NEVER = "never"
    PARTIAL_ERROR = "partial_error"
    STALL = "stall"


class ScriptedChatProvider:
    """Local provider that never performs network, database, or secret access."""

    DEFAULT_CHUNKS = ("这是一个", "本地模拟回复", "，用于验证流式对话。")

    def __init__(
        self,
        scenario: ScriptedScenario | str = ScriptedScenario.NORMAL,
        *,
        chunks: tuple[str, ...] = DEFAULT_CHUNKS,
        first_delay_ms: int = 80,
        chunk_delay_ms: int = 45,
        slow_first_delay_ms: int = 500,
    ) -> None:
        self.scenario = ScriptedScenario(scenario)
        if not chunks or any(not isinstance(chunk, str) or not chunk for chunk in chunks):
            raise ValueError("chunks must contain at least one non-empty string")
        for name, value in (
            ("first_delay_ms", first_delay_ms),
            ("chunk_delay_ms", chunk_delay_ms),
            ("slow_first_delay_ms", slow_first_delay_ms),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        self.chunks = chunks
        self.first_delay_ms = first_delay_ms
        self.chunk_delay_ms = chunk_delay_ms
        self.slow_first_delay_ms = slow_first_delay_ms
        self._requests: list[ChatRequest] = []
        self._lock = threading.Lock()

    @property
    def requests(self) -> tuple[ChatRequest, ...]:
        with self._lock:
            return tuple(self._requests)

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self._requests)

    def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> Iterable[str]:
        with self._lock:
            self._requests.append(request)

        if self.scenario is ScriptedScenario.NEVER:
            cancellation.wait()
            raise CancellationRequested("request cancelled")

        first_delay_ms = (
            self.slow_first_delay_ms
            if self.scenario is ScriptedScenario.SLOW_FIRST
            else self.first_delay_ms
        )
        self._wait_or_cancel(cancellation, first_delay_ms)

        yield self.chunks[0]
        if self.scenario is ScriptedScenario.PARTIAL_ERROR:
            self._wait_or_cancel(cancellation, self.chunk_delay_ms)
            raise ChatProviderError("本地模拟流式中断。")
        if self.scenario is ScriptedScenario.STALL:
            cancellation.wait()
            raise CancellationRequested("request cancelled")

        for chunk in self.chunks[1:]:
            self._wait_or_cancel(cancellation, self.chunk_delay_ms)
            yield chunk
        cancellation.raise_if_cancelled()

    @staticmethod
    def _wait_or_cancel(cancellation: CancellationToken, delay_ms: int) -> None:
        if cancellation.wait(delay_ms / 1000):
            raise CancellationRequested("request cancelled")
