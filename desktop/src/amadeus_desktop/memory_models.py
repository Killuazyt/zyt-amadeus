"""Immutable domain models and injectable boundaries for P5 memory work."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


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


@dataclass(frozen=True, slots=True)
class PromptPersonaKnowledge:
    """Minimal local persona fragment that may be injected into a prompt."""

    knowledge_id: str
    persona_id: str
    content: str
    active: bool = True


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
