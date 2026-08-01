"""In-memory chat models shared by the P4 UI and provider boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum


class ConversationState(StrEnum):
    """Observable lifecycle states for one conversation request."""

    IDLE = "idle"
    SENDING = "sending"
    WAITING_FIRST_CHUNK = "waiting_first_chunk"
    STREAMING = "streaming"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class MessageRole(StrEnum):
    """User-visible roles used by the in-memory chat presentation model."""

    USER = "user"
    ASSISTANT = "assistant"


class PromptRole(StrEnum):
    """Roles accepted by an OpenAI-compatible prompt."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class GenerationPurpose(StrEnum):
    """Why a provider request is being made.

    The value is deliberately provider-neutral so background generation can
    share the P4 provider boundary without being mistaken for visible chat.
    """

    MAIN_CONVERSATION = "main_conversation"
    CONVERSATION_SUMMARY = "conversation_summary"
    MEMORY_EXTRACTION = "memory_extraction"
    STRUCTURE_REPAIR = "structure_repair"


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    """Optional per-request generation limits for foreground/background work."""

    purpose: GenerationPurpose = GenerationPurpose.MAIN_CONVERSATION
    temperature: float | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(self.temperature)
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("temperature must be a finite number between 0 and 2")
        if self.max_output_tokens is not None and (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")


class MessageStatus(StrEnum):
    """Persistence-independent status of one in-memory message."""

    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class TurnTerminalReason(StrEnum):
    """Stable terminal reasons suitable for UI mapping and later persistence."""

    COMPLETED = "completed"
    USER_STOPPED = "user_stopped"
    FIRST_CHUNK_TIMEOUT = "first_chunk_timeout"
    STREAM_IDLE_TIMEOUT = "stream_idle_timeout"
    PROVIDER_ERROR = "provider_error"
    EMPTY_RESPONSE = "empty_response"
    LOCAL_PERSISTENCE_ERROR = "local_persistence_error"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Immutable message snapshot emitted across the presentation boundary."""

    message_id: str
    role: MessageRole
    content: str
    status: MessageStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One user message and its stable, reusable assistant placeholder."""

    turn_id: str
    user_message: ChatMessage
    assistant_message: ChatMessage
    attempt: int = 1
    terminal_reason: TurnTerminalReason | None = None
    provider_error_code: str | None = None
    status_text: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PromptMessage:
    """Minimal immutable message passed to a background provider worker."""

    role: PromptRole
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Provider-neutral snapshot for one unique streaming attempt."""

    request_id: str
    turn_id: str
    attempt: int
    messages: tuple[PromptMessage, ...]
    options: GenerationOptions = field(default_factory=GenerationOptions)
