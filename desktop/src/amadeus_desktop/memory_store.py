"""Auditable immutable memory versions with transactional FTS5 maintenance."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from uuid import uuid4

from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.memory_models import MemoryKind, MemoryOperation
from amadeus_desktop.memory_search import (
    EmptySearchQuery,
    build_fts_match_query,
    build_search_text,
    exact_memory_hash,
    normalize_memory_content,
    normalize_topic_key,
)
from amadeus_desktop.storage_models import (
    DEFAULT_PROFILE_ID,
    ManualVersionProtectedError,
    MemoryRecord,
    MemorySearchResult,
    MemorySource,
    MemoryStatus,
    MemoryUpsertResult,
    MemoryVersion,
    MemoryVersionOperation,
    MemoryVersionOrigin,
    RecallStats,
    RecallTerminalStatus,
    StaleMemorySourceError,
    StorageNotFoundError,
    StorageValidationError,
    decode_utc,
    encode_utc,
    utc_now,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
MAX_INDEX_DOCUMENTS = 10_000

_MEMORY_SELECT = """
SELECT g.id AS memory_id, g.profile_id, g.kind, g.topic_key, g.status, g.pinned,
       g.created_at AS group_created_at, g.updated_at AS group_updated_at,
       v.id AS version_id, v.version_number, v.content, v.normalized_content,
       v.content_hash, v.importance, v.confidence, v.origin, v.operation,
       v.supersedes_version_id, v.created_at AS version_created_at
FROM memory_groups AS g
JOIN memory_versions AS v ON v.id = g.current_version_id
"""


class MemoryStore:
    """Store current memory pointers while retaining immutable prior versions."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        clock: Clock = utc_now,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid4().hex)

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
    ) -> MemoryRecord:
        """Create a distinct logical memory; use ``upsert_memory`` for extraction."""

        kind_value = _memory_kind(kind)
        topic = _topic_key(topic_key)
        content, normalized, content_hash, search_text = _content_fields(content)
        importance = _unit_interval(importance, "importance")
        origin_value = _origin(origin)
        confidence = (
            1.0
            if origin_value is MemoryVersionOrigin.MANUAL
            else _unit_interval(confidence, "confidence")
        )
        memory_id = _identifier(memory_id or self._id_factory(), "memory_id")
        version_id = self._id_factory()
        now = encode_utc(self._clock())

        with self._database.transaction() as connection:
            self._ensure_profile(connection, profile_id, now)
            sources = self._validated_sources(
                connection,
                profile_id,
                source_message_ids,
                origin=origin_value,
            )
            connection.execute(
                """
                INSERT INTO memory_groups(
                    id, profile_id, kind, topic_key, status, pinned,
                    current_version_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', 0, NULL, ?, ?)
                """,
                (memory_id, profile_id, kind_value.value, topic, now, now),
            )
            operation = (
                MemoryVersionOperation.MANUAL_EDIT
                if origin_value is MemoryVersionOrigin.MANUAL
                else MemoryVersionOperation.ADD
            )
            self._insert_version(
                connection,
                version_id=version_id,
                memory_id=memory_id,
                version_number=1,
                content=content,
                normalized_content=normalized,
                content_hash=content_hash,
                search_text=search_text,
                importance=importance,
                confidence=confidence,
                origin=origin_value,
                operation=operation,
                supersedes_version_id=None,
                created_at=now,
            )
            connection.execute(
                "UPDATE memory_groups SET current_version_id = ? WHERE id = ?",
                (version_id, memory_id),
            )
            self._insert_sources(
                connection,
                version_id=version_id,
                origin=origin_value,
                sources=sources,
                created_at=now,
            )
            self._replace_fts(
                connection,
                memory_id=memory_id,
                version_id=version_id,
                profile_id=profile_id,
                search_text=search_text,
            )
        return self.get(memory_id)

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
    ) -> MemoryUpsertResult:
        """Deduplicate exact normalized content and only add new provenance."""

        kind_value = _memory_kind(kind)
        topic = _topic_key(topic_key)
        stored_content, normalized, content_hash, _search_text = _content_fields(content)
        importance = _unit_interval(importance, "importance")
        confidence = _unit_interval(confidence, "confidence")
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            self._ensure_profile(connection, profile_id, now)
            sources = self._validated_sources(
                connection,
                profile_id,
                source_message_ids,
                origin=MemoryVersionOrigin.AUTOMATIC,
            )
            row = connection.execute(
                """
                SELECT g.id AS memory_id, v.id AS version_id
                FROM memory_groups AS g
                JOIN memory_versions AS v ON v.id = g.current_version_id
                WHERE g.profile_id = ? AND v.content_hash = ?
                  AND v.normalized_content = ?
                ORDER BY g.updated_at DESC, g.id
                LIMIT 1
                """,
                (profile_id, content_hash, normalized),
            ).fetchone()
            if row is None:
                created_id = None
            else:
                created_id = str(row["memory_id"])
                added = self._insert_sources(
                    connection,
                    version_id=str(row["version_id"]),
                    origin=MemoryVersionOrigin.AUTOMATIC,
                    sources=sources,
                    created_at=now,
                )
                connection.execute(
                    "UPDATE memory_groups SET updated_at = ? WHERE id = ?",
                    (now, created_id),
                )
        if created_id is not None:
            return MemoryUpsertResult(
                memory=self.get(created_id),
                created_group=False,
                created_version=False,
                sources_added=added,
            )
        memory = self.create_memory(
            kind_value,
            topic,
            stored_content,
            profile_id=profile_id,
            importance=importance,
            confidence=confidence,
            source_message_ids=source_message_ids,
        )
        return MemoryUpsertResult(
            memory=memory,
            created_group=True,
            created_version=True,
            sources_added=len(tuple(dict.fromkeys(source_message_ids))),
        )

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
    ) -> MemoryRecord:
        """Supplement/correct by atomically switching to a new immutable version."""

        content, normalized, content_hash, search_text = _content_fields(content)
        importance = _unit_interval(importance, "importance")
        origin_value = _origin(origin)
        operation_value = _operation(operation, origin_value)
        confidence = (
            1.0
            if origin_value is MemoryVersionOrigin.MANUAL
            else _unit_interval(confidence, "confidence")
        )
        now = encode_utc(self._clock())
        new_version_id = self._id_factory()
        with self._database.transaction() as connection:
            current = connection.execute(
                """
                SELECT g.profile_id, g.current_version_id, v.version_number,
                       v.normalized_content, v.origin,
                       v.created_at AS version_created_at
                FROM memory_groups AS g
                JOIN memory_versions AS v ON v.id = g.current_version_id
                WHERE g.id = ?
                """,
                (memory_id,),
            ).fetchone()
            if current is None:
                raise StorageNotFoundError("memory does not exist")
            if (
                current["origin"] == MemoryVersionOrigin.MANUAL.value
                and origin_value is MemoryVersionOrigin.AUTOMATIC
                and operation_value is not MemoryVersionOperation.CORRECT
            ):
                raise ManualVersionProtectedError(
                    "automatic supplements cannot replace a manual memory version"
                )
            sources = self._validated_sources(
                connection,
                str(current["profile_id"]),
                source_message_ids,
                origin=origin_value,
            )
            if (
                current["origin"] == MemoryVersionOrigin.AUTOMATIC.value
                and origin_value is MemoryVersionOrigin.AUTOMATIC
                and current["normalized_content"] != normalized
            ):
                latest_source = connection.execute(
                    """
                    SELECT MAX(m.sequence) AS sequence
                    FROM memory_sources AS s
                    JOIN messages AS m ON m.id = s.live_message_id
                    WHERE s.version_id = ?
                    """,
                    (current["current_version_id"],),
                ).fetchone()
                latest_sequence = None if latest_source is None else latest_source["sequence"]
                has_newer_source = (
                    any(int(source["sequence"]) > int(latest_sequence) for source in sources)
                    if latest_sequence is not None
                    else any(
                        str(source["created_at"]) > str(current["version_created_at"])
                        for source in sources
                    )
                )
                if not has_newer_source:
                    raise StaleMemorySourceError(
                        "automatic versions need a user source newer than the current version"
                    )
            if (
                current["origin"] == MemoryVersionOrigin.MANUAL.value
                and origin_value is MemoryVersionOrigin.AUTOMATIC
                and operation_value is MemoryVersionOperation.CORRECT
                and not any(
                    str(source["created_at"]) > str(current["version_created_at"])
                    for source in sources
                )
            ):
                raise ManualVersionProtectedError(
                    "automatic corrections need a user source newer than the manual version"
                )
            if (
                current["normalized_content"] == normalized
                and origin_value is MemoryVersionOrigin.AUTOMATIC
            ):
                self._insert_sources(
                    connection,
                    version_id=str(current["current_version_id"]),
                    origin=origin_value,
                    sources=sources,
                    created_at=now,
                )
                return self._get_with_connection(connection, memory_id)
            self._insert_version(
                connection,
                version_id=new_version_id,
                memory_id=memory_id,
                version_number=int(current["version_number"]) + 1,
                content=content,
                normalized_content=normalized,
                content_hash=content_hash,
                search_text=search_text,
                importance=importance,
                confidence=confidence,
                origin=origin_value,
                operation=operation_value,
                supersedes_version_id=str(current["current_version_id"]),
                created_at=now,
            )
            self._insert_sources(
                connection,
                version_id=new_version_id,
                origin=origin_value,
                sources=sources,
                created_at=now,
            )
            connection.execute(
                """
                UPDATE memory_groups
                SET current_version_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_version_id, now, memory_id),
            )
            self._replace_fts(
                connection,
                memory_id=memory_id,
                version_id=new_version_id,
                profile_id=str(current["profile_id"]),
                search_text=search_text,
            )
        return self.get(memory_id)

    def edit_memory(
        self,
        memory_id: str,
        content: str,
        *,
        importance: float | None = None,
    ) -> MemoryRecord:
        current = self.get(memory_id)
        return self.add_version(
            memory_id,
            content,
            importance=(current.current_version.importance if importance is None else importance),
            confidence=1.0,
            operation=MemoryVersionOperation.MANUAL_EDIT,
            source_message_ids=(),
            origin=MemoryVersionOrigin.MANUAL,
        )

    def get(self, memory_id: str) -> MemoryRecord:
        return self._get_with_connection(self._database.connection, memory_id)

    def list_memories(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        kind: MemoryKind | str | None = None,
        status: MemoryStatus | str | None = None,
        pinned: bool | None = None,
        limit: int = 500,
    ) -> tuple[MemoryRecord, ...]:
        _validate_limit(limit, 2_000)
        clauses = ["g.profile_id = ?"]
        parameters: list[object] = [profile_id]
        if kind is not None:
            clauses.append("g.kind = ?")
            parameters.append(_memory_kind(kind).value)
        if status is not None:
            clauses.append("g.status = ?")
            parameters.append(_status(status).value)
        if pinned is not None:
            clauses.append("g.pinned = ?")
            parameters.append(int(pinned))
        parameters.append(limit)
        rows = self._database.connection.execute(
            _MEMORY_SELECT
            + " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY g.pinned DESC, g.updated_at DESC, g.id LIMIT ?",
            parameters,
        ).fetchall()
        return tuple(_memory_from_row(row) for row in rows)

    def search(
        self,
        query: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        limit: int = 30,
        include_archived: bool = False,
    ) -> tuple[MemorySearchResult, ...]:
        _validate_limit(limit, 2_000)
        try:
            match = build_fts_match_query(query)
        except EmptySearchQuery:
            return ()
        status_clause = "" if include_archived else "AND g.status = 'active'"
        rows = self._database.connection.execute(
            """
            SELECT g.id AS memory_id, bm25(memory_fts) AS fts_rank
            FROM memory_fts
            JOIN memory_groups AS g ON g.id = memory_fts.memory_id
            WHERE memory_fts MATCH ? AND memory_fts.profile_id = ?
            """
            + status_clause
            + " ORDER BY fts_rank, g.pinned DESC, g.updated_at DESC LIMIT ?",
            (*match.parameters, profile_id, limit),
        ).fetchall()
        return tuple(
            MemorySearchResult(
                memory=self.get(str(row["memory_id"])),
                rank=float(row["fts_rank"]),
            )
            for row in rows
        )

    def list_active_documents(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        limit: int = MAX_INDEX_DOCUMENTS,
    ) -> tuple[MemoryRecord, ...]:
        """Return current active versions for a generation rebuild."""

        _validate_limit(limit, MAX_INDEX_DOCUMENTS)
        rows = self._database.connection.execute(
            _MEMORY_SELECT
            + """
            WHERE g.profile_id = ? AND g.status = 'active'
            ORDER BY g.updated_at DESC, g.id
            LIMIT ?
            """,
            (_identifier(profile_id, "profile_id"), limit),
        ).fetchall()
        return tuple(_memory_from_row(row) for row in rows)

    def get_active_by_version_ids(
        self,
        version_ids: Iterable[str],
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> tuple[MemoryRecord, ...]:
        """Revalidate vector hits against current active immutable versions."""

        ids = tuple(dict.fromkeys(_identifier(value, "version_id") for value in version_ids))
        if not ids:
            return ()
        placeholders = ",".join("?" for _value in ids)
        rows = self._database.connection.execute(
            _MEMORY_SELECT
            + f"""
            WHERE g.profile_id = ? AND g.status = 'active'
              AND g.current_version_id IN ({placeholders})
            """,
            (_identifier(profile_id, "profile_id"), *ids),
        ).fetchall()
        by_id = {str(row["version_id"]): _memory_from_row(row) for row in rows}
        return tuple(by_id[value] for value in ids if value in by_id)

    def record_successful_recall(
        self,
        retrieval_ticket_id: str,
        version_ids: Sequence[str],
        *,
        terminal_status: RecallTerminalStatus | str,
        first_chunk_received: bool,
        profile_id: str = DEFAULT_PROFILE_ID,
        conversation_id: str | None = None,
        assistant_message_id: str | None = None,
        attempt: int = 1,
        recalled_at: datetime | None = None,
    ) -> int:
        """Record only prompt entries that produced a visible successful response."""

        status = _successful_terminal(terminal_status, first_chunk_received)
        if status is None:
            return 0
        ticket_id = _identifier(retrieval_ticket_id, "retrieval_ticket_id")
        profile_id = _identifier(profile_id, "profile_id")
        ids = tuple(dict.fromkeys(_identifier(value, "version_id") for value in version_ids))
        if not ids:
            return 0
        _validate_attempt(attempt)
        now = encode_utc(recalled_at or self._clock())
        placeholders = ",".join("?" for _value in ids)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT v.id
                FROM memory_versions AS v
                JOIN memory_groups AS g ON g.id = v.memory_id
                WHERE g.profile_id = ? AND v.id IN ({placeholders})
                """,
                (profile_id, *ids),
            ).fetchall()
            if {str(row["id"]) for row in rows} != set(ids):
                raise StorageValidationError("recall contains unknown memory versions")
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO memory_recall_events(
                    id, retrieval_ticket_id, profile_id, version_id,
                    conversation_id, assistant_message_id, attempt,
                    terminal_status, recalled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        self._id_factory(),
                        ticket_id,
                        profile_id,
                        version_id,
                        conversation_id,
                        assistant_message_id,
                        attempt,
                        status.value,
                        now,
                    )
                    for version_id in ids
                ),
            )
            return connection.total_changes - before

    def recall_stats(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        memory_ids: Sequence[str] | None = None,
    ) -> tuple[RecallStats, ...]:
        """Aggregate success history across every immutable version in a group."""

        parameters: list[object] = [_identifier(profile_id, "profile_id")]
        id_clause = ""
        if memory_ids is not None:
            ids = tuple(dict.fromkeys(_identifier(value, "memory_id") for value in memory_ids))
            if not ids:
                return ()
            placeholders = ",".join("?" for _value in ids)
            id_clause = f"AND g.id IN ({placeholders})"
            parameters.extend(ids)
        rows = self._database.connection.execute(
            """
            SELECT g.id AS target_id, COUNT(e.id) AS recall_count,
                   MAX(e.recalled_at) AS last_recalled_at
            FROM memory_groups AS g
            LEFT JOIN memory_versions AS v ON v.memory_id = g.id
            LEFT JOIN memory_recall_events AS e ON e.version_id = v.id
            WHERE g.profile_id = ?
            """
            + id_clause
            + " GROUP BY g.id ORDER BY g.id",
            parameters,
        ).fetchall()
        return tuple(
            RecallStats(
                target_id=str(row["target_id"]),
                successful_recall_count=int(row["recall_count"]),
                last_recalled_at=decode_utc(row["last_recalled_at"]),
            )
            for row in rows
        )

    def archive_decayed_events(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Archive inactive low-value event memories; never delete user data."""

        now_value = now or self._clock()
        now_text = encode_utc(now_value)
        profile_id = _identifier(profile_id, "profile_id")
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT g.id, v.created_at AS current_version_created_at, v.importance,
                       MAX(e.recalled_at) AS last_recalled_at
                FROM memory_groups AS g
                JOIN memory_versions AS v ON v.id = g.current_version_id
                LEFT JOIN memory_versions AS all_versions ON all_versions.memory_id = g.id
                LEFT JOIN memory_recall_events AS e ON e.version_id = all_versions.id
                WHERE g.profile_id = ? AND g.status = 'active'
                  AND g.kind = 'event' AND g.pinned = 0 AND v.importance < 0.85
                GROUP BY g.id, v.created_at, v.importance
                """,
                (profile_id,),
            ).fetchall()
            archived: list[str] = []
            for row in rows:
                created_at = _required_datetime(row["current_version_created_at"])
                recalled_at = decode_utc(row["last_recalled_at"])
                anchor = recalled_at or created_at
                inactive_days = max(0.0, (now_value - anchor).total_seconds() / 86_400.0)
                effective_score = float(row["importance"]) * 0.5 ** (inactive_days / 30.0)
                if inactive_days >= 90.0 and effective_score < 0.15:
                    archived.append(str(row["id"]))
            if archived:
                placeholders = ",".join("?" for _value in archived)
                connection.execute(
                    f"""
                    UPDATE memory_groups SET status = 'archived', updated_at = ?
                    WHERE profile_id = ? AND status = 'active'
                      AND id IN ({placeholders})
                    """,
                    (now_text, profile_id, *archived),
                )
        return tuple(archived)

    def archive(self, memory_id: str) -> MemoryRecord:
        return self._set_status(memory_id, MemoryStatus.ARCHIVED)

    def restore(self, memory_id: str) -> MemoryRecord:
        return self._set_status(memory_id, MemoryStatus.ACTIVE)

    def set_pinned(self, memory_id: str, pinned: bool) -> MemoryRecord:
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE memory_groups SET pinned = ?, updated_at = ? WHERE id = ?",
                (int(pinned), now, memory_id),
            )
            if cursor.rowcount != 1:
                raise StorageNotFoundError("memory does not exist")
        return self.get(memory_id)

    def attach_exact_repeat_source(
        self,
        source_message_id: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> int:
        """Attach a repeated user message to current memories it exactly repeats.

        This deterministic path preserves provenance even when the extractor
        correctly decides that an exact repeat contains no new candidate.
        Superseded versions are deliberately excluded, so an old statement
        cannot regain authority after a correction or manual edit.
        """

        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            sources = self._validated_sources(
                connection,
                profile_id,
                (source_message_id,),
                origin=MemoryVersionOrigin.AUTOMATIC,
            )
            source = sources[0]
            versions = connection.execute(
                """
                SELECT DISTINCT g.current_version_id AS version_id
                FROM messages AS prior
                JOIN conversations AS c ON c.id = prior.conversation_id
                JOIN memory_sources AS s ON s.live_message_id = prior.id
                JOIN memory_versions AS v ON v.id = s.version_id
                JOIN memory_groups AS g
                  ON g.id = v.memory_id AND g.current_version_id = v.id
                WHERE c.profile_id = ?
                  AND prior.role = 'user'
                  AND prior.participates_in_memory = 1
                  AND prior.sequence < ?
                  AND prior.content = ?
                """,
                (profile_id, source["sequence"], source["content"]),
            ).fetchall()
            added = 0
            for version in versions:
                added += self._insert_sources(
                    connection,
                    version_id=str(version["version_id"]),
                    origin=MemoryVersionOrigin.AUTOMATIC,
                    sources=sources,
                    created_at=now,
                )
        return added

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a whole logical group, versions, provenance, and its FTS row."""

        with self._database.transaction() as connection:
            connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
            cursor = connection.execute("DELETE FROM memory_groups WHERE id = ?", (memory_id,))
        if cursor.rowcount == 1:
            self._database.purge_deleted_content()
        return cursor.rowcount == 1

    def delete(self, memory_id: str) -> bool:
        return self.delete_memory(memory_id)

    def list_versions(self, memory_id: str) -> tuple[MemoryVersion, ...]:
        rows = self._database.connection.execute(
            """
            SELECT * FROM memory_versions
            WHERE memory_id = ? ORDER BY version_number
            """,
            (memory_id,),
        ).fetchall()
        if not rows:
            self.get(memory_id)
        return tuple(_version_from_row(row) for row in rows)

    def list_sources(
        self, memory_id: str, *, version_id: str | None = None
    ) -> tuple[MemorySource, ...]:
        parameters: list[object] = [memory_id]
        version_clause = ""
        if version_id is not None:
            version_clause = "AND s.version_id = ?"
            parameters.append(version_id)
        rows = self._database.connection.execute(
            """
            SELECT s.* FROM memory_sources AS s
            JOIN memory_versions AS v ON v.id = s.version_id
            WHERE v.memory_id = ?
            """
            + version_clause
            + " ORDER BY v.version_number, s.created_at, s.id",
            parameters,
        ).fetchall()
        if not rows:
            self.get(memory_id)
        return tuple(_source_from_row(row) for row in rows)

    def rebuild_fts(self) -> int:
        with self._database.transaction() as connection:
            connection.execute("DELETE FROM memory_fts")
            cursor = connection.execute(
                """
                INSERT INTO memory_fts(version_id, memory_id, profile_id, search_text)
                SELECT v.id, g.id, g.profile_id, v.search_text
                FROM memory_groups AS g
                JOIN memory_versions AS v ON v.id = g.current_version_id
                """
            )
        return max(0, cursor.rowcount)

    def _set_status(self, memory_id: str, status: MemoryStatus) -> MemoryRecord:
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE memory_groups SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, memory_id),
            )
            if cursor.rowcount != 1:
                raise StorageNotFoundError("memory does not exist")
        return self.get(memory_id)

    def _get_with_connection(self, connection: sqlite3.Connection, memory_id: str) -> MemoryRecord:
        row = connection.execute(_MEMORY_SELECT + " WHERE g.id = ?", (memory_id,)).fetchone()
        if row is None:
            raise StorageNotFoundError("memory does not exist")
        return _memory_from_row(row)

    @staticmethod
    def _ensure_profile(connection: sqlite3.Connection, profile_id: str, now: str) -> None:
        if profile_id == DEFAULT_PROFILE_ID:
            connection.execute(
                """
                INSERT OR IGNORE INTO profiles(id, display_name, created_at, updated_at)
                VALUES (?, '用户', ?, ?)
                """,
                (DEFAULT_PROFILE_ID, now, now),
            )
        row = connection.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise StorageNotFoundError("profile does not exist")

    @staticmethod
    def _validated_sources(
        connection: sqlite3.Connection,
        profile_id: str,
        source_message_ids: Sequence[str],
        *,
        origin: MemoryVersionOrigin,
    ) -> tuple[sqlite3.Row, ...]:
        source_ids = tuple(dict.fromkeys(str(value) for value in source_message_ids))
        if origin is MemoryVersionOrigin.MANUAL:
            if source_ids:
                raise StorageValidationError("manual versions do not cite automatic sources")
            return ()
        if not source_ids:
            raise StorageValidationError("automatic memories require user-message provenance")
        placeholders = ",".join("?" for _source_id in source_ids)
        rows = connection.execute(
            f"""
            SELECT m.id, m.sequence, m.conversation_id, m.role, m.content,
                   m.participates_in_memory, m.created_at,
                   c.profile_id
            FROM messages AS m
            JOIN conversations AS c ON c.id = m.conversation_id
            WHERE m.id IN ({placeholders})
            """,
            source_ids,
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        if set(by_id) != set(source_ids):
            raise StorageValidationError("memory provenance contains a missing message")
        ordered = tuple(by_id[source_id] for source_id in source_ids)
        if any(row["role"] != "user" for row in ordered):
            raise StorageValidationError("only user messages can source user memory")
        if any(not bool(row["participates_in_memory"]) for row in ordered):
            raise StorageValidationError("memory-disabled messages cannot source memory")
        if any(row["profile_id"] != profile_id for row in ordered):
            raise StorageValidationError("memory provenance belongs to another profile")
        return ordered

    def _insert_sources(
        self,
        connection: sqlite3.Connection,
        *,
        version_id: str,
        origin: MemoryVersionOrigin,
        sources: Sequence[sqlite3.Row],
        created_at: str,
    ) -> int:
        before = connection.total_changes
        if origin is MemoryVersionOrigin.MANUAL:
            connection.execute(
                """
                INSERT OR IGNORE INTO memory_sources(
                    id, version_id, source_message_id, source_conversation_id,
                    live_message_id, live_conversation_id, extraction_method, created_at
                ) VALUES (?, ?, ?, NULL, NULL, NULL, 'manual', ?)
                """,
                (self._id_factory(), version_id, f"manual:{version_id}", created_at),
            )
        else:
            for source in sources:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO memory_sources(
                        id, version_id, source_message_id, source_conversation_id,
                        live_message_id, live_conversation_id, extraction_method, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'automatic', ?)
                    """,
                    (
                        self._id_factory(),
                        version_id,
                        source["id"],
                        source["conversation_id"],
                        source["id"],
                        source["conversation_id"],
                        created_at,
                    ),
                )
        return connection.total_changes - before

    @staticmethod
    def _insert_version(
        connection: sqlite3.Connection,
        *,
        version_id: str,
        memory_id: str,
        version_number: int,
        content: str,
        normalized_content: str,
        content_hash: str,
        search_text: str,
        importance: float,
        confidence: float,
        origin: MemoryVersionOrigin,
        operation: MemoryVersionOperation,
        supersedes_version_id: str | None,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_versions(
                id, memory_id, version_number, content, normalized_content,
                content_hash, search_text, importance, confidence, origin,
                operation, supersedes_version_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                memory_id,
                version_number,
                content,
                normalized_content,
                content_hash,
                search_text,
                importance,
                confidence,
                origin.value,
                operation.value,
                supersedes_version_id,
                created_at,
            ),
        )

    @staticmethod
    def _replace_fts(
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        version_id: str,
        profile_id: str,
        search_text: str,
    ) -> None:
        connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        connection.execute(
            """
            INSERT INTO memory_fts(version_id, memory_id, profile_id, search_text)
            VALUES (?, ?, ?, ?)
            """,
            (version_id, memory_id, profile_id, search_text),
        )


def _memory_kind(value: MemoryKind | str) -> MemoryKind:
    try:
        return value if isinstance(value, MemoryKind) else MemoryKind(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported memory kind") from exc


def _origin(value: MemoryVersionOrigin | str) -> MemoryVersionOrigin:
    try:
        return value if isinstance(value, MemoryVersionOrigin) else MemoryVersionOrigin(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported memory version origin") from exc


def _operation(
    value: MemoryOperation | MemoryVersionOperation | str,
    origin: MemoryVersionOrigin,
) -> MemoryVersionOperation:
    if origin is MemoryVersionOrigin.MANUAL:
        return MemoryVersionOperation.MANUAL_EDIT
    raw = str(value)
    if raw == MemoryOperation.CORRECT.value:
        return MemoryVersionOperation.CORRECT
    try:
        operation = MemoryVersionOperation(raw)
    except ValueError as exc:
        raise StorageValidationError("unsupported memory version operation") from exc
    if operation is MemoryVersionOperation.MANUAL_EDIT:
        raise StorageValidationError("automatic versions cannot be manual edits")
    return operation


def _status(value: MemoryStatus | str) -> MemoryStatus:
    try:
        return value if isinstance(value, MemoryStatus) else MemoryStatus(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported memory status") from exc


def _content_fields(content: str) -> tuple[str, str, str, str]:
    if not isinstance(content, str) or not content.strip():
        raise StorageValidationError("memory content must not be blank")
    normalized = normalize_memory_content(content)
    try:
        content_hash = exact_memory_hash(content)
    except ValueError as exc:
        raise StorageValidationError("memory content has no searchable text") from exc
    search_text = build_search_text(content)
    if not search_text:
        raise StorageValidationError("memory content has no searchable text")
    return content.strip(), normalized, content_hash, search_text


def _topic_key(value: str) -> str:
    if not isinstance(value, str):
        raise StorageValidationError("topic_key must be text")
    normalized = normalize_topic_key(value)
    if not normalized:
        raise StorageValidationError("topic_key must contain searchable text")
    return normalized


def _identifier(value: str, field: str) -> str:
    value = str(value).strip()
    if not value or len(value) > 200:
        raise StorageValidationError(f"{field} must be a non-empty identifier")
    return value


def _unit_interval(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageValidationError(f"{field} must be numeric")
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise StorageValidationError(f"{field} must be between 0 and 1")
    return numeric


def _validate_limit(limit: int, maximum: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise StorageValidationError(f"limit must be between 1 and {maximum}")


def _validate_attempt(attempt: int) -> None:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise StorageValidationError("attempt must be a positive integer")


def _successful_terminal(
    value: RecallTerminalStatus | str, first_chunk_received: bool
) -> RecallTerminalStatus | None:
    if not first_chunk_received:
        return None
    try:
        return (
            value if isinstance(value, RecallTerminalStatus) else RecallTerminalStatus(str(value))
        )
    except ValueError:
        return None


def _required_datetime(value: str) -> datetime:
    decoded = decode_utc(value)
    assert decoded is not None
    return decoded


def _version_from_row(row: sqlite3.Row) -> MemoryVersion:
    return MemoryVersion(
        version_id=str(row["id"]),
        memory_id=str(row["memory_id"]),
        version_number=int(row["version_number"]),
        content=str(row["content"]),
        normalized_content=str(row["normalized_content"]),
        content_hash=str(row["content_hash"]),
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        origin=MemoryVersionOrigin(row["origin"]),
        operation=MemoryVersionOperation(row["operation"]),
        supersedes_version_id=row["supersedes_version_id"],
        created_at=_required_datetime(row["created_at"]),
    )


def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
    version = MemoryVersion(
        version_id=str(row["version_id"]),
        memory_id=str(row["memory_id"]),
        version_number=int(row["version_number"]),
        content=str(row["content"]),
        normalized_content=str(row["normalized_content"]),
        content_hash=str(row["content_hash"]),
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        origin=MemoryVersionOrigin(row["origin"]),
        operation=MemoryVersionOperation(row["operation"]),
        supersedes_version_id=row["supersedes_version_id"],
        created_at=_required_datetime(row["version_created_at"]),
    )
    return MemoryRecord(
        memory_id=str(row["memory_id"]),
        profile_id=str(row["profile_id"]),
        kind=MemoryKind(row["kind"]),
        topic_key=str(row["topic_key"]),
        status=MemoryStatus(row["status"]),
        pinned=bool(row["pinned"]),
        current_version=version,
        created_at=_required_datetime(row["group_created_at"]),
        updated_at=_required_datetime(row["group_updated_at"]),
    )


def _source_from_row(row: sqlite3.Row) -> MemorySource:
    return MemorySource(
        source_id=str(row["id"]),
        version_id=str(row["version_id"]),
        source_message_id=str(row["source_message_id"]),
        source_conversation_id=row["source_conversation_id"],
        live_message_id=row["live_message_id"],
        live_conversation_id=row["live_conversation_id"],
        extraction_method=str(row["extraction_method"]),
        created_at=_required_datetime(row["created_at"]),
    )
