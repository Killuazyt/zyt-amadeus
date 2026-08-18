"""Immutable domain models and injectable boundaries for P5 memory work."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class MemoryLayer(StrEnum):
    """The five product layers plus the isolated static persona corpus."""

    WORKING = "working"
    RECENT = "recent"
    FACT = "fact"
    REFLECTION = "reflection"
    PERSONA = "persona"
    STATIC_PERSONA = "static_persona"


class MemorySubjectScope(StrEnum):
    USER = "user"
    COMPANION = "companion"
    RELATIONSHIP = "relationship"


class DerivedMemoryStatus(StrEnum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    PROMOTED = "promoted"
    MERGED = "merged"
    ACTIVE = "active"
    DISPUTED = "disputed"
    DENIED = "denied"
    ARCHIVED = "archived"


class EvidenceSignalKind(StrEnum):
    INITIAL = "initial"
    INDIRECT_SUPPORT = "indirect_support"
    INDIRECT_REFUTE = "indirect_refute"
    DIRECT_CONFIRM = "direct_confirm"
    DIRECT_REBUT = "direct_rebut"


class ConflictResolution(StrEnum):
    KEEP = "keep"
    ACCEPT = "accept"
    MERGE = "merge"


class MemoryKind(StrEnum):
    """Long-term user-memory categories supported by the MVP."""

    FACT = "fact"
    PREFERENCE = "preference"
    EVENT = "event"
    RELATIONSHIP = "relationship"


class MemoryOperation(StrEnum):
    """How an extracted candidate relates to an existing topic."""

    ADD = "add"
    SUPPLEMENT = "supplement"
    CORRECT = "correct"


class SourceRole(StrEnum):
    """Roles accepted at the extraction provenance boundary."""

    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ExtractionSource:
    """A local message that an extraction candidate may cite."""

    message_id: str
    role: SourceRole
    content: str


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A parsed, persistence-independent memory candidate."""

    kind: MemoryKind
    operation: MemoryOperation
    content: str
    topic_key: str
    importance: float
    confidence: float
    source_message_ids: tuple[str, ...]
    subject_scope: MemorySubjectScope = MemorySubjectScope.USER
    event_started_at: str | None = None
    event_ended_at: str | None = None
    time_confidence: float | None = None
    correction_explicit: bool = False


@dataclass(frozen=True, slots=True)
class PromptMemory:
    """Minimal current memory snapshot that may be injected into a prompt."""

    memory_id: str
    kind: MemoryKind
    content: str
    topic_key: str = ""
    importance: float = 0.5
    confidence: float = 1.0
    pinned: bool = False
    memory_version_id: str = ""
    user_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class PromptPersonaKnowledge:
    """Minimal local persona fragment that may be injected into a prompt."""

    knowledge_id: str
    persona_id: str
    content: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class PromptDerivedMemory:
    """A confirmed reflection or active persona impression for prompt injection."""

    group_id: str
    version_id: str
    layer: MemoryLayer
    subject_scope: MemorySubjectScope
    content: str
    topic_key: str
    importance: float
    confidence: float
    evidence_score: float
    pinned: bool = False


@dataclass(frozen=True, slots=True)
class WorkingMemoryItem:
    """Content-free identity and ranking explanation for one selected memory."""

    layer: MemoryLayer
    target_id: str
    version_id: str
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class WorkingMemorySnapshot:
    """Ephemeral prompt-assembly diagnostics; never persisted to SQLite."""

    conversation_id: str
    message_id: str
    query: str
    selected: tuple[WorkingMemoryItem, ...]


@runtime_checkable
class MemoryExtractor(Protocol):
    """Injectable boundary for a background structured extraction backend."""

    async def extract(
        self,
        sources: Sequence[ExtractionSource],
        existing_memories: Sequence[PromptMemory] = (),
    ) -> tuple[MemoryCandidate, ...]:
        """Return validated candidates without persisting them."""


@runtime_checkable
class MemoryRetriever(Protocol):
    """Injectable boundary for keyword memory retrieval."""

    def retrieve(self, query: str, *, limit: int = 8) -> tuple[PromptMemory, ...]:
        """Return current, active memories ordered by descending relevance."""
