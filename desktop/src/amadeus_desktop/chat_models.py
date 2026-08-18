"""In-memory chat models shared by the P4 UI and provider boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from amadeus_desktop.memory_models import WorkingMemorySnapshot


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


class InputModality(StrEnum):
    """How the user supplied the text stored for one turn."""

    TEXT = "text"
    VOICE = "voice"


class AttachmentKind(StrEnum):
    """Attachment families with different provider and retention behavior."""

    IMAGE = "image"
    DOCUMENT = "document"


class AttachmentSource(StrEnum):
    """User-visible provenance for one managed attachment."""

    FILE_PICKER = "file_picker"
    DROP = "drop"
    CLIPBOARD = "clipboard"
    SCREENSHOT = "screenshot"
    SCREEN = "screen"
    WINDOW = "window"
    CAMERA = "camera"


class CompanionRequestKind(StrEnum):
    """The user-authorized request boundary represented by one snapshot."""

    CONVERSATION = "conversation"
    PROACTIVE = "proactive"


@dataclass(frozen=True, slots=True)
class CompanionContextSnapshot:
    """Immutable evidence of what this exact request actually contains.

    The snapshot deliberately records supplied data instead of device
    availability.  A camera becoming available later therefore cannot widen a
    retry that originally contained only text.
    """

    request_kind: CompanionRequestKind = CompanionRequestKind.CONVERSATION
    input_modality: InputModality = InputModality.TEXT
    visual_sources: tuple[AttachmentSource, ...] = ()
    has_document_attachment: bool = False

    def __post_init__(self) -> None:
        normalized = tuple(dict.fromkeys(AttachmentSource(value) for value in self.visual_sources))
        object.__setattr__(self, "visual_sources", normalized)

    @property
    def has_visual_evidence(self) -> bool:
        return bool(self.visual_sources)

    @property
    def is_voice_transcript(self) -> bool:
        return self.input_modality is InputModality.VOICE


class ProviderCapability(StrEnum):
    """Provider feature sets used by the local router."""

    TEXT = "text"
    MULTIMODAL = "multimodal"


class ProviderRoute(StrEnum):
    """Immutable routing decision captured before a worker starts."""

    TEXT = "text"
    MULTIMODAL = "multimodal"


class GenerationPurpose(StrEnum):
    """Why a provider request is being made.

    The value is deliberately provider-neutral so background generation can
    share the P4 provider boundary without being mistaken for visible chat.
    """

    MAIN_CONVERSATION = "main_conversation"
    CONVERSATION_SUMMARY = "conversation_summary"
    MEMORY_EXTRACTION = "memory_extraction"
    STRUCTURE_REPAIR = "structure_repair"
    MEMORY_EVIDENCE = "memory_evidence"
    REFLECTION_SYNTHESIS = "reflection_synthesis"
    PERSONA_PROMOTION = "persona_promotion"
    PROACTIVE_GREETING = "proactive_greeting"


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
    attachments: tuple[AttachmentSnapshot, ...] = ()
    input_modality: InputModality = InputModality.TEXT
    companion_cue_id: str | None = None
    companion_source_label: str | None = None


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
    companion_context: CompanionContextSnapshot | None = None


@dataclass(frozen=True, slots=True)
class AttachmentSnapshot:
    """Immutable metadata for a byte-identical managed attachment."""

    attachment_id: str
    kind: AttachmentKind
    source: AttachmentSource
    display_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    relative_path: str
    extracted_text: str = ""
    text_truncated: bool = False


@dataclass(frozen=True, slots=True)
class TextPart:
    """One OpenAI-compatible text content part."""

    text: str


@dataclass(frozen=True, slots=True)
class ImagePart:
    """One sanitized image data URL associated with a managed attachment."""

    attachment_id: str
    data_url: str
    detail: str = "auto"


PromptContentPart: TypeAlias = TextPart | ImagePart
PromptContent: TypeAlias = str | tuple[PromptContentPart, ...]


@dataclass(frozen=True, slots=True)
class PromptMessage:
    """Minimal immutable message passed to a background provider worker."""

    role: PromptRole
    content: PromptContent


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Provider-neutral snapshot for one unique streaming attempt."""

    request_id: str
    turn_id: str
    attempt: int
    messages: tuple[PromptMessage, ...]
    options: GenerationOptions = field(default_factory=GenerationOptions)
    attachments: tuple[AttachmentSnapshot, ...] = ()
    provider_route: ProviderRoute = ProviderRoute.TEXT
    provider_role: str | None = None
    companion_context: CompanionContextSnapshot = field(default_factory=CompanionContextSnapshot)


@dataclass(frozen=True, slots=True)
class PreparedPrompt:
    """Provider messages plus the local retrieval evidence for one attempt."""

    messages: tuple[PromptMessage, ...]
    user_memory_version_ids: tuple[str, ...] = ()
    reflection_version_ids: tuple[str, ...] = ()
    persona_impression_version_ids: tuple[str, ...] = ()
    persona_knowledge_ids: tuple[str, ...] = ()
    retrieval_ticket_id: str = ""
    attempt: int = 1
    attachments: tuple[AttachmentSnapshot, ...] = ()
    provider_route: ProviderRoute = ProviderRoute.TEXT
    working_memory_snapshot: WorkingMemorySnapshot | None = None
    companion_context: CompanionContextSnapshot = field(default_factory=CompanionContextSnapshot)
