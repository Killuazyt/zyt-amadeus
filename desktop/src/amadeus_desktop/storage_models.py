"""Qt-independent persistence models for conversations and long-term memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from amadeus_desktop.memory_models import MemoryKind, MemorySubjectScope

DEFAULT_PROFILE_ID = "default"


class StorageError(RuntimeError):
    """Base class for safe, local persistence failures."""


class StorageNotFoundError(StorageError):
    """Raised when a requested persistent entity does not exist."""


class StorageConflictError(StorageError):
    """Raised when stable IDs or optimistic expectations conflict."""


class StorageValidationError(StorageError, ValueError):
    """Raised before invalid data reaches SQLite."""


class ManualVersionProtectedError(StorageConflictError):
    """Raised when an automatic supplement would replace a manual version."""


class StaleMemorySourceError(StorageConflictError):
    """Raised when delayed extraction would replace a newer automatic version."""


class ConversationStatus(StrEnum):
    NORMAL = "normal"
    ARCHIVED = "archived"


class StoredMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class StoredMessageStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class StoredMessageOrigin(StrEnum):
    CONVERSATION = "conversation"
    PROACTIVE = "proactive"


class StoredInputModality(StrEnum):
    TEXT = "text"
    VOICE = "voice"


class StoredAttachmentKind(StrEnum):
    IMAGE = "image"
    DOCUMENT = "document"


class StoredAttachmentSource(StrEnum):
    FILE_PICKER = "file_picker"
    DROP = "drop"
    CLIPBOARD = "clipboard"
    SCREENSHOT = "screenshot"
    SCREEN = "screen"
    WINDOW = "window"
    CAMERA = "camera"


class ProactiveTrigger(StrEnum):
    STARTUP = "startup"
    IDLE = "idle"


class ProactiveDisposition(StrEnum):
    DISPLAYED = "displayed"
    CLICKED = "clicked"
    DISMISSED = "dismissed"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MemoryVersionOrigin(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class MemoryVersionOperation(StrEnum):
    ADD = "add"
    SUPPLEMENT = "supplement"
    CORRECT = "correct"
    MANUAL_EDIT = "manual_edit"


class EmbeddingCorpus(StrEnum):
    MEMORY = "memory"
    REFLECTION = "reflection"
    PERSONA_IMPRESSION = "persona_impression"
    PERSONA = "persona"


class EmbeddingGenerationStatus(StrEnum):
    BUILDING = "building"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


class RecallTerminalStatus(StrEnum):
    COMPLETED = "completed"
    USER_STOPPED = "user_stopped"


class BackgroundJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    FAILED = "failed"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class Profile:
    profile_id: str
    display_name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: str
    profile_id: str
    title: str
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    attachment_id: str
    kind: StoredAttachmentKind
    source: StoredAttachmentSource
    display_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    relative_path: str
    status: str
    extracted_text: str
    text_truncated: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredMessage:
    sequence: int
    message_id: str
    conversation_id: str
    turn_id: str
    role: StoredMessageRole
    origin: StoredMessageOrigin
    content: str
    status: StoredMessageStatus
    attempt: int
    terminal_reason: str | None
    provider_name: str | None
    model_name: str | None
    failure_code: str | None
    participates_in_memory: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    input_modality: StoredInputModality = StoredInputModality.TEXT
    attachments: tuple[StoredAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class ProactiveInteractionEvent:
    event_id: str
    profile_id: str
    local_date: date
    trigger: ProactiveTrigger
    displayed_at: datetime
    disposition: ProactiveDisposition
    message_id: str | None


@dataclass(frozen=True, slots=True)
class MessagePage:
    """A chronological page plus the exclusive cursor for the next older page."""

    items: tuple[StoredMessage, ...]
    next_before_sequence: int | None


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    summary_id: str
    conversation_id: str
    content: str
    covers_through_sequence: int
    message_count: int
    character_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SummaryProgress:
    message_count: int
    character_count: int
    last_sequence: int | None


@dataclass(frozen=True, slots=True)
class MemoryVersion:
    version_id: str
    memory_id: str
    version_number: int
    content: str
    normalized_content: str
    content_hash: str
    importance: float
    confidence: float
    origin: MemoryVersionOrigin
    operation: MemoryVersionOperation
    supersedes_version_id: str | None
    created_at: datetime
    event_started_at: datetime | None = None
    event_ended_at: datetime | None = None
    time_confidence: float | None = None
    deep_memory_eligible: bool = False


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    profile_id: str
    kind: MemoryKind
    topic_key: str
    status: MemoryStatus
    pinned: bool
    current_version: MemoryVersion
    created_at: datetime
    updated_at: datetime
    subject_scope: MemorySubjectScope = MemorySubjectScope.USER


@dataclass(frozen=True, slots=True)
class MemorySource:
    source_id: str
    version_id: str
    source_message_id: str
    source_conversation_id: str | None
    live_message_id: str | None
    live_conversation_id: str | None
    extraction_method: str
    created_at: datetime

    @property
    def is_manual(self) -> bool:
        return self.extraction_method == MemoryVersionOrigin.MANUAL.value

    @property
    def source_deleted(self) -> bool:
        return not self.is_manual and self.live_message_id is None


@dataclass(frozen=True, slots=True)
class MemoryUpsertResult:
    memory: MemoryRecord
    created_group: bool
    created_version: bool
    sources_added: int


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    memory: MemoryRecord
    rank: float


@dataclass(frozen=True, slots=True)
class EmbeddingGeneration:
    generation_id: str
    corpus: EmbeddingCorpus
    scope_id: str
    model_name: str
    model_commit: str
    dimension: int
    model_sha256: str
    calibration_threshold: float
    status: EmbeddingGenerationStatus
    item_count: int
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredVector:
    generation_id: str
    target_id: str
    vector: tuple[float, ...]
    vector_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecallStats:
    target_id: str
    successful_recall_count: int
    last_recalled_at: datetime | None


@dataclass(frozen=True, slots=True)
class PersonaKnowledge:
    knowledge_id: str
    persona_id: str
    content: str
    search_text: str
    tags: tuple[str, ...]
    source_ref: str
    source_hash: str
    content_hash: str
    active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PersonaKnowledgeDraft:
    content: str
    tags: tuple[str, ...]
    source_ref: str
    source_hash: str
    knowledge_id: str | None = None


@dataclass(frozen=True, slots=True)
class PersonaSearchResult:
    knowledge: PersonaKnowledge
    rank: float


@dataclass(frozen=True, slots=True)
class BackgroundJob:
    job_id: str
    kind: str
    dedupe_key: str
    status: BackgroundJobStatus
    payload: dict[str, object]
    profile_id: str | None
    conversation_id: str | None
    message_id: str | None
    attempt_count: int
    run_after: datetime
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def encode_utc(value: datetime) -> str:
    """Encode an aware datetime into a stable, sortable UTC representation."""

    if value.tzinfo is None:
        raise StorageValidationError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def decode_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
