"""Qt-independent models for reflections, persona impressions, and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from amadeus_desktop.memory_models import (
    ConflictResolution,
    DerivedMemoryStatus,
    EvidenceSignalKind,
    MemoryLayer,
    MemorySubjectScope,
)


@dataclass(frozen=True, slots=True)
class DerivedMemoryVersion:
    version_id: str
    group_id: str
    version_number: int
    content: str
    normalized_content: str
    content_hash: str
    importance: float
    confidence: float
    origin: str
    operation: str
    supersedes_version_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DerivedMemoryRecord:
    group_id: str
    profile_id: str
    layer: MemoryLayer
    subject_scope: MemorySubjectScope
    topic_key: str
    status: DerivedMemoryStatus
    pinned: bool
    current_version: DerivedMemoryVersion
    archive_candidate_since: datetime | None
    created_at: datetime
    updated_at: datetime
    evidence_score: float = 0.0
    conflicted: bool = False


@dataclass(frozen=True, slots=True)
class DerivedMemorySource:
    source_id: str
    version_id: str
    parent_version_id: str | None
    source_message_id: str
    live_message_id: str | None
    extraction_method: str
    created_at: datetime

    @property
    def source_deleted(self) -> bool:
        return self.extraction_method == "automatic" and self.live_message_id is None


@dataclass(frozen=True, slots=True)
class EvidenceSignal:
    signal_id: str
    profile_id: str
    target_layer: MemoryLayer
    target_group_id: str
    target_version_id: str
    source_message_id: str | None
    source_fact_version_id: str | None
    kind: EvidenceSignalKind
    reinforcement_delta: float
    disputation_delta: float
    correlation_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    reinforcement: float
    disputation: float
    score: float
    indirect_support_count: int
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryConflict:
    conflict_id: str
    profile_id: str
    target_layer: MemoryLayer
    target_group_id: str
    incumbent_version_id: str
    challenger_version_id: str
    status: str
    resolution: ConflictResolution | None
    source_message_id: str | None
    created_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryAuditEvent:
    event_id: str
    profile_id: str
    owner_layer: MemoryLayer
    owner_group_id: str
    version_id: str | None
    source_message_id: str | None
    event_type: str
    reason_code: str
    reinforcement_delta: float
    disputation_delta: float
    metadata: dict[str, object]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class EventTimelineItem:
    memory_id: str
    version_id: str
    content: str
    occurred_at: datetime
    occurred_at_is_explicit: bool
    importance: float
