"""Qt-independent injectable contract for auditable user-memory storage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from amadeus_desktop.memory_models import MemoryKind, MemoryOperation
from amadeus_desktop.storage_models import (
    DEFAULT_PROFILE_ID,
    MemoryRecord,
    MemorySearchResult,
    MemorySource,
    MemoryStatus,
    MemoryUpsertResult,
    MemoryVersion,
    MemoryVersionOperation,
    MemoryVersionOrigin,
)


@runtime_checkable
class MemoryService(Protocol):
    """Persistence boundary consumed by P5A application and job services."""

    def create_memory(
        self,
        kind: MemoryKind | str,
        topic_key: str,
        content: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        importance: float = 0.5,
        confidence: float = 1.0,
        source_message_ids: Sequence[str] = (),
        origin: MemoryVersionOrigin | str = MemoryVersionOrigin.AUTOMATIC,
        memory_id: str | None = None,
    ) -> MemoryRecord: ...

    def upsert_memory(
        self,
        kind: MemoryKind | str,
        topic_key: str,
        content: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        importance: float = 0.5,
        confidence: float = 1.0,
        source_message_ids: Sequence[str],
    ) -> MemoryUpsertResult: ...

    def add_version(
        self,
        memory_id: str,
        content: str,
        *,
        importance: float,
        confidence: float,
        operation: MemoryOperation | MemoryVersionOperation | str,
        source_message_ids: Sequence[str],
        origin: MemoryVersionOrigin | str = MemoryVersionOrigin.AUTOMATIC,
    ) -> MemoryRecord: ...

    def edit_memory(
        self,
        memory_id: str,
        content: str,
        *,
        importance: float | None = None,
    ) -> MemoryRecord: ...

    def get(self, memory_id: str) -> MemoryRecord: ...

    def list_memories(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        kind: MemoryKind | str | None = None,
        status: MemoryStatus | str | None = None,
        pinned: bool | None = None,
        limit: int = 500,
    ) -> tuple[MemoryRecord, ...]: ...

    def search(
        self,
        query: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        limit: int = 30,
        include_archived: bool = False,
    ) -> tuple[MemorySearchResult, ...]: ...

    def archive(self, memory_id: str) -> MemoryRecord: ...

    def restore(self, memory_id: str) -> MemoryRecord: ...

    def set_pinned(self, memory_id: str, pinned: bool) -> MemoryRecord: ...

    def attach_exact_repeat_source(
        self,
        source_message_id: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> int: ...

    def delete_memory(self, memory_id: str) -> bool: ...

    def list_versions(self, memory_id: str) -> tuple[MemoryVersion, ...]: ...

    def list_sources(
        self,
        memory_id: str,
        *,
        version_id: str | None = None,
    ) -> tuple[MemorySource, ...]: ...

    def rebuild_fts(self) -> int: ...
