"""Transactional five-layer memory storage and N.E.K.O.-style evidence math."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.deep_memory_models import (
    DerivedMemoryRecord,
    DerivedMemorySource,
    DerivedMemoryVersion,
    EventTimelineItem,
    EvidenceSnapshot,
    MemoryAuditEvent,
    MemoryConflict,
)
from amadeus_desktop.memory_models import (
    ConflictResolution,
    DerivedMemoryStatus,
    EvidenceSignalKind,
    MemoryLayer,
    MemorySubjectScope,
)
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
    StorageConflictError,
    StorageNotFoundError,
    StorageValidationError,
    decode_utc,
    encode_utc,
    utc_now,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

EVIDENCE_CONFIRMED_THRESHOLD = 1.0
EVIDENCE_PROMOTED_THRESHOLD = 2.0
EVIDENCE_ARCHIVE_THRESHOLD = -2.0
EVIDENCE_ARCHIVE_DAYS = 14
EVIDENCE_REINFORCEMENT_HALF_LIFE_DAYS = 30.0
EVIDENCE_DISPUTATION_HALF_LIFE_DAYS = 180.0
INDIRECT_SUPPORT_DELTA = 0.5
INDIRECT_REFUTE_DELTA = 1.0
DIRECT_CONFIRM_DELTA = 1.0
DIRECT_REBUT_DELTA = 1.0
INDIRECT_SUPPORT_COMBO_THRESHOLD = 2
INDIRECT_SUPPORT_COMBO_BONUS = 0.5
MAX_AUDIT_METADATA_BYTES = 2_048


@dataclass(frozen=True, slots=True)
class _LayerTables:
    groups: str
    versions: str
    sources: str
    fts: str
    group_fk: str
    parent_fk: str
    id_column: str
    active_statuses: tuple[str, ...]


_REFLECTION = _LayerTables(
    groups="memory_reflections",
    versions="memory_reflection_versions",
    sources="memory_reflection_sources",
    fts="memory_reflection_fts",
    group_fk="reflection_id",
    parent_fk="fact_version_id",
    id_column="reflection_id",
    active_statuses=(DerivedMemoryStatus.CONFIRMED.value,),
)
_PERSONA = _LayerTables(
    groups="memory_persona_impressions",
    versions="memory_persona_impression_versions",
    sources="memory_persona_impression_sources",
    fts="memory_persona_impression_fts",
    group_fk="impression_id",
    parent_fk="reflection_version_id",
    id_column="impression_id",
    active_statuses=(DerivedMemoryStatus.ACTIVE.value,),
)


class DeepMemoryStore:
    """Own derived memory projections, provenance, conflicts, and evidence events."""

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

    def create_reflection(
        self,
        content: str,
        topic_key: str,
        *,
        fact_version_ids: Sequence[str],
        subject_scope: MemorySubjectScope | str = MemorySubjectScope.USER,
        profile_id: str = DEFAULT_PROFILE_ID,
        importance: float = 0.5,
        confidence: float = 0.75,
        reflection_id: str | None = None,
    ) -> DerivedMemoryRecord:
        """Create a tentative reflection rooted in current eligible fact versions."""

        fact_ids = _unique_identifiers(fact_version_ids, "fact_version_ids")
        if not fact_ids:
            raise StorageValidationError("automatic reflections require source facts")
        scope = _subject_scope(subject_scope)
        now = encode_utc(self._clock())
        group_id = _identifier(reflection_id or self._id_factory(), "reflection_id")
        version_id = self._id_factory()
        fields = _content_fields(content)
        importance = _unit_interval(importance, "importance")
        confidence = _unit_interval(confidence, "confidence")
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT current_version_id FROM memory_reflections WHERE id = ?",
                (group_id,),
            ).fetchone()
            if existing is not None:
                row = connection.execute(
                    "SELECT content_hash FROM memory_reflection_versions WHERE id = ?",
                    (existing["current_version_id"],),
                ).fetchone()
                if row is None or row["content_hash"] != fields[2]:
                    raise StorageConflictError("reflection batch identity changed")
                return self._get_with_connection(
                    connection,
                    MemoryLayer.REFLECTION,
                    group_id,
                )
            source_rows = self._eligible_fact_sources(
                connection,
                profile_id=profile_id,
                fact_version_ids=fact_ids,
            )
            if not source_rows:
                raise StorageValidationError("reflection sources have no user-message provenance")
            self._insert_group_and_version(
                connection,
                layer=MemoryLayer.REFLECTION,
                group_id=group_id,
                version_id=version_id,
                profile_id=profile_id,
                subject_scope=scope,
                topic_key=topic_key,
                status=DerivedMemoryStatus.TENTATIVE,
                fields=fields,
                importance=importance,
                confidence=confidence,
                operation="add",
                now=now,
            )
            for row in source_rows:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO memory_reflection_sources(
                        id, version_id, fact_version_id, source_message_id,
                        live_message_id, extraction_method, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'automatic', ?)
                    """,
                    (
                        self._id_factory(),
                        version_id,
                        row["fact_version_id"],
                        row["source_message_id"],
                        row["live_message_id"],
                        now,
                    ),
                )
            initial = initial_reinforcement_from_importance(importance)
            if initial:
                self._insert_signal(
                    connection,
                    profile_id=profile_id,
                    layer=MemoryLayer.REFLECTION,
                    group_id=group_id,
                    version_id=version_id,
                    kind=EvidenceSignalKind.INITIAL,
                    reinforcement_delta=initial,
                    disputation_delta=0.0,
                    correlation_key=f"reflection-initial:{version_id}",
                    created_at=now,
                )
            self._insert_audit(
                connection,
                profile_id=profile_id,
                layer=MemoryLayer.REFLECTION,
                group_id=group_id,
                version_id=version_id,
                event_type="reflection.synthesized",
                reason_code="eligible_fact_batch",
                reinforcement_delta=initial,
                metadata={"fact_count": len(fact_ids)},
                occurred_at=now,
            )
        return self.get(MemoryLayer.REFLECTION, group_id)

    def create_reflection_batch(
        self,
        reflections: Sequence[Mapping[str, object]],
        *,
        batch_key: str,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> tuple[DerivedMemoryRecord, ...]:
        """Atomically persist one deterministic synthesis batch across crash retries."""

        key = _identifier(batch_key, "batch_key")
        prepared: list[
            tuple[
                str,
                str,
                tuple[str, ...],
                MemorySubjectScope,
                float,
                float,
                str,
                tuple[str, str, str, str],
            ]
        ] = []
        for index, item in enumerate(reflections):
            content = str(item.get("content", ""))
            topic_key = str(item.get("topic_key", ""))
            raw_fact_ids = item.get("fact_version_ids")
            if not isinstance(raw_fact_ids, Sequence) or isinstance(raw_fact_ids, str | bytes):
                raise StorageValidationError("reflection fact versions must be a sequence")
            fact_ids = _unique_identifiers(
                tuple(str(value) for value in raw_fact_ids),
                "fact_version_ids",
            )
            if not fact_ids:
                raise StorageValidationError("automatic reflections require source facts")
            scope = _subject_scope(str(item.get("subject_scope", "user")))
            importance = _unit_interval(item.get("importance", 0.5), "importance")
            confidence = _unit_interval(item.get("confidence", 0.75), "confidence")
            group_id = hashlib.sha256(f"reflection:{key}:{index}".encode()).hexdigest()
            fields = _content_fields(content)
            prepared.append(
                (
                    group_id,
                    topic_key,
                    fact_ids,
                    scope,
                    importance,
                    confidence,
                    content,
                    fields,
                )
            )

        created_ids: list[str] = []
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            for (
                group_id,
                topic_key,
                fact_ids,
                scope,
                importance,
                confidence,
                _content,
                fields,
            ) in prepared:
                existing = connection.execute(
                    "SELECT current_version_id FROM memory_reflections WHERE id = ?",
                    (group_id,),
                ).fetchone()
                if existing is not None:
                    current_version_id = str(existing["current_version_id"])
                    existing_version = connection.execute(
                        "SELECT content_hash FROM memory_reflection_versions WHERE id = ?",
                        (current_version_id,),
                    ).fetchone()
                    existing_facts = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT fact_version_id FROM memory_reflection_sources "
                            "WHERE version_id = ? AND fact_version_id IS NOT NULL",
                            (current_version_id,),
                        ).fetchall()
                    }
                    if (
                        existing_version is None
                        or existing_version["content_hash"] != fields[2]
                        or existing_facts != set(fact_ids)
                    ):
                        raise StorageConflictError("reflection batch identity changed")
                    created_ids.append(group_id)
                    continue
                source_rows = self._eligible_fact_sources(
                    connection,
                    profile_id=profile_id,
                    fact_version_ids=fact_ids,
                )
                if not source_rows:
                    raise StorageValidationError(
                        "reflection sources have no user-message provenance"
                    )
                version_id = self._id_factory()
                self._insert_group_and_version(
                    connection,
                    layer=MemoryLayer.REFLECTION,
                    group_id=group_id,
                    version_id=version_id,
                    profile_id=profile_id,
                    subject_scope=scope,
                    topic_key=topic_key,
                    status=DerivedMemoryStatus.TENTATIVE,
                    fields=fields,
                    importance=importance,
                    confidence=confidence,
                    operation="add",
                    now=now,
                )
                for source in source_rows:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO memory_reflection_sources(
                            id, version_id, fact_version_id, source_message_id,
                            live_message_id, extraction_method, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'automatic', ?)
                        """,
                        (
                            self._id_factory(),
                            version_id,
                            source["fact_version_id"],
                            source["source_message_id"],
                            source["live_message_id"],
                            now,
                        ),
                    )
                initial = initial_reinforcement_from_importance(importance)
                if initial:
                    self._insert_signal(
                        connection,
                        profile_id=profile_id,
                        layer=MemoryLayer.REFLECTION,
                        group_id=group_id,
                        version_id=version_id,
                        kind=EvidenceSignalKind.INITIAL,
                        reinforcement_delta=initial,
                        disputation_delta=0.0,
                        correlation_key=f"reflection-initial:{version_id}",
                        created_at=now,
                    )
                self._insert_audit(
                    connection,
                    profile_id=profile_id,
                    layer=MemoryLayer.REFLECTION,
                    group_id=group_id,
                    version_id=version_id,
                    event_type="reflection.synthesized",
                    reason_code="eligible_fact_batch",
                    reinforcement_delta=initial,
                    metadata={"fact_count": len(fact_ids), "batch_key": key},
                    occurred_at=now,
                )
                created_ids.append(group_id)
        return tuple(self.get(MemoryLayer.REFLECTION, group_id) for group_id in created_ids)

    def note_completed_turn(self, *, profile_id: str = DEFAULT_PROFILE_ID) -> int:
        """Advance the durable cadence counter and return its new value."""

        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory_pipeline_state(
                    profile_id, completed_turn_count, created_at, updated_at
                ) VALUES (?, 1, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    completed_turn_count = completed_turn_count + 1,
                    updated_at = excluded.updated_at
                """,
                (profile_id, now, now),
            )
            row = connection.execute(
                "SELECT completed_turn_count FROM memory_pipeline_state WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        assert row is not None
        return int(row["completed_turn_count"])

    def promote_reflection(
        self,
        reflection_id: str,
        *,
        content: str | None = None,
        target_impression_id: str | None = None,
        decision: str = "new",
    ) -> DerivedMemoryRecord:
        """Promote or merge a confirmed reflection after a fresh score/source check."""

        if decision not in {"new", "merge"}:
            raise StorageValidationError("promotion decision must be new or merge")
        reflection = self.get(MemoryLayer.REFLECTION, reflection_id)
        if reflection.status is not DerivedMemoryStatus.CONFIRMED:
            raise StorageConflictError("only confirmed reflections can be promoted")
        if reflection.conflicted or reflection.evidence_score < EVIDENCE_PROMOTED_THRESHOLD:
            raise StorageConflictError("reflection is not eligible for promotion")
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            current = self._get_with_connection(connection, MemoryLayer.REFLECTION, reflection_id)
            if (
                current.current_version.version_id != reflection.current_version.version_id
                or current.status is not DerivedMemoryStatus.CONFIRMED
                or current.conflicted
            ):
                raise StorageConflictError("reflection changed before promotion")
            score = self._evidence_snapshot_with_connection(
                connection,
                MemoryLayer.REFLECTION,
                current.current_version.version_id,
                self._clock(),
            )
            if score.score < EVIDENCE_PROMOTED_THRESHOLD:
                raise StorageConflictError("reflection evidence fell below promotion threshold")
            sources = connection.execute(
                """
                SELECT DISTINCT source_message_id, live_message_id
                FROM memory_reflection_sources
                WHERE version_id = ? AND live_message_id IS NOT NULL
                """,
                (current.current_version.version_id,),
            ).fetchall()
            if not sources:
                raise StorageConflictError("promotion requires at least one live user source")

            if decision == "new":
                impression_id = self._id_factory()
                version_id = self._id_factory()
                output_content = content or current.current_version.content
                self._insert_group_and_version(
                    connection,
                    layer=MemoryLayer.PERSONA,
                    group_id=impression_id,
                    version_id=version_id,
                    profile_id=current.profile_id,
                    subject_scope=current.subject_scope,
                    topic_key=current.topic_key,
                    status=DerivedMemoryStatus.ACTIVE,
                    fields=_content_fields(output_content),
                    importance=current.current_version.importance,
                    confidence=current.current_version.confidence,
                    operation="add",
                    now=now,
                )
            else:
                if target_impression_id is None or content is None:
                    raise StorageValidationError("merge promotion requires target and merged text")
                target = self._get_with_connection(
                    connection,
                    MemoryLayer.PERSONA,
                    target_impression_id,
                )
                if target.status is not DerivedMemoryStatus.ACTIVE or target.conflicted:
                    raise StorageConflictError("persona merge target is not active")
                impression_id = target.group_id
                version_id = self._append_derived_version(
                    connection,
                    layer=MemoryLayer.PERSONA,
                    group=target,
                    content=content,
                    importance=max(
                        target.current_version.importance,
                        current.current_version.importance,
                    ),
                    confidence=max(
                        target.current_version.confidence,
                        current.current_version.confidence,
                    ),
                    operation="merge",
                    origin="automatic",
                    now=now,
                )

            for source in sources:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO memory_persona_impression_sources(
                        id, version_id, reflection_version_id, source_message_id,
                        live_message_id, extraction_method, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'automatic', ?)
                    """,
                    (
                        self._id_factory(),
                        version_id,
                        current.current_version.version_id,
                        source["source_message_id"],
                        source["live_message_id"],
                        now,
                    ),
                )
            self._insert_signal(
                connection,
                profile_id=current.profile_id,
                layer=MemoryLayer.PERSONA,
                group_id=impression_id,
                version_id=version_id,
                kind=EvidenceSignalKind.INITIAL,
                reinforcement_delta=score.reinforcement,
                disputation_delta=score.disputation,
                correlation_key=f"persona-inherit:{current.current_version.version_id}:{version_id}",
                created_at=now,
            )
            reflection_status = "promoted" if decision == "new" else "merged"
            connection.execute(
                """
                UPDATE memory_reflections SET status = ?, updated_at = ? WHERE id = ?
                """,
                (reflection_status, now, reflection_id),
            )
            self._insert_audit(
                connection,
                profile_id=current.profile_id,
                layer=MemoryLayer.REFLECTION,
                group_id=reflection_id,
                version_id=current.current_version.version_id,
                event_type=f"reflection.{reflection_status}",
                reason_code="evidence_threshold",
                metadata={"persona_impression_id": impression_id},
                occurred_at=now,
            )
            self._insert_audit(
                connection,
                profile_id=current.profile_id,
                layer=MemoryLayer.PERSONA,
                group_id=impression_id,
                version_id=version_id,
                event_type="persona.created" if decision == "new" else "persona.merged",
                reason_code="reflection_promotion",
                metadata={"reflection_id": reflection_id},
                occurred_at=now,
            )
        return self.get(MemoryLayer.PERSONA, impression_id)

    def record_promotion_rejection(self, reflection_id: str) -> DerivedMemoryRecord:
        """Audit a model rejection without changing the confirmed reflection itself."""

        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            current = self._get_with_connection(
                connection,
                MemoryLayer.REFLECTION,
                _identifier(reflection_id, "reflection_id"),
            )
            if current.status is not DerivedMemoryStatus.CONFIRMED:
                raise StorageConflictError(
                    "only confirmed reflections can be rejected for promotion"
                )
            self._insert_audit(
                connection,
                profile_id=current.profile_id,
                layer=MemoryLayer.REFLECTION,
                group_id=current.group_id,
                version_id=current.current_version.version_id,
                event_type="reflection.promotion_rejected",
                reason_code="model_reject",
                occurred_at=now,
            )
        return self.get(MemoryLayer.REFLECTION, reflection_id)

    def get(self, layer: MemoryLayer | str, group_id: str) -> DerivedMemoryRecord:
        return self._get_with_connection(
            self._database.connection,
            _derived_layer(layer),
            _identifier(group_id, "group_id"),
        )

    def list_records(
        self,
        layer: MemoryLayer | str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        statuses: Sequence[DerivedMemoryStatus | str] | None = None,
        limit: int = 500,
    ) -> tuple[DerivedMemoryRecord, ...]:
        layer_value = _derived_layer(layer)
        _limit(limit)
        parameters: list[object] = [profile_id]
        status_clause = ""
        if statuses:
            values = tuple(_derived_status(value).value for value in statuses)
            status_clause = f" AND g.status IN ({','.join('?' for _ in values)})"
            parameters.extend(values)
        parameters.append(limit)
        rows = self._database.connection.execute(
            self._select_sql(layer_value)
            + " WHERE g.profile_id = ?"
            + status_clause
            + " ORDER BY g.pinned DESC, g.updated_at DESC, g.id LIMIT ?",
            parameters,
        ).fetchall()
        return tuple(self._record_from_row(layer_value, row) for row in rows)

    def list_active_documents(
        self,
        layer: MemoryLayer | str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        limit: int = 10_000,
    ) -> tuple[DerivedMemoryRecord, ...]:
        """Return only prompt-eligible current rows for one derived corpus."""

        layer_value = _derived_layer(layer)
        statuses = (
            (DerivedMemoryStatus.CONFIRMED,)
            if layer_value is MemoryLayer.REFLECTION
            else (DerivedMemoryStatus.ACTIVE,)
        )
        return tuple(
            record
            for record in self.list_records(
                layer_value,
                profile_id=profile_id,
                statuses=statuses,
                limit=limit,
            )
            if not record.conflicted
        )

    def get_active_by_version_ids(
        self,
        layer: MemoryLayer | str,
        version_ids: Iterable[str],
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> tuple[DerivedMemoryRecord, ...]:
        """Revalidate derived vector/FTS hits against current state and conflicts."""

        layer_value = _derived_layer(layer)
        tables = _tables(layer_value)
        ids = tuple(dict.fromkeys(_identifier(value, "version_id") for value in version_ids))
        if not ids:
            return ()
        placeholders = ",".join("?" for _ in ids)
        statuses = tables.active_statuses
        status_placeholders = ",".join("?" for _ in statuses)
        rows = self._database.connection.execute(
            self._select_sql(layer_value)
            + f"""
            WHERE g.profile_id = ? AND g.current_version_id IN ({placeholders})
              AND g.status IN ({status_placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM memory_conflicts AS c
                  WHERE c.target_layer = ? AND c.target_group_id = g.id
                    AND c.status = 'open'
              )
            """,
            (profile_id, *ids, *statuses, layer_value.value),
        ).fetchall()
        by_id = {str(row["version_id"]): self._record_from_row(layer_value, row) for row in rows}
        return tuple(by_id[value] for value in ids if value in by_id)

    def record_successful_recall(
        self,
        layer: MemoryLayer | str,
        retrieval_ticket_id: str,
        version_ids: Sequence[str],
        *,
        terminal_status: str,
        first_chunk_received: bool,
        profile_id: str = DEFAULT_PROFILE_ID,
        conversation_id: str | None = None,
        assistant_message_id: str | None = None,
        attempt: int = 1,
        recalled_at: datetime | None = None,
    ) -> int:
        """Persist prompt-selected derived IDs without creating relevance feedback."""

        if not first_chunk_received or terminal_status not in {"completed", "user_stopped"}:
            return 0
        layer_value = _derived_layer(layer)
        tables = _tables(layer_value)
        table = _recall_table(layer_value)
        ids = tuple(dict.fromkeys(_identifier(value, "version_id") for value in version_ids))
        if not ids:
            return 0
        if attempt < 1:
            raise StorageValidationError("attempt must be at least 1")
        placeholders = ",".join("?" for _ in ids)
        ticket = _identifier(retrieval_ticket_id, "retrieval_ticket_id")
        now = encode_utc(recalled_at or self._clock())
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT v.id FROM {tables.versions} AS v
                JOIN {tables.groups} AS g ON g.id = v.{tables.group_fk}
                WHERE g.profile_id = ? AND v.id IN ({placeholders})
                """,
                (profile_id, *ids),
            ).fetchall()
            if {str(row["id"]) for row in rows} != set(ids):
                raise StorageValidationError("recall contains unknown derived versions")
            before = connection.total_changes
            connection.executemany(
                f"""
                INSERT OR IGNORE INTO {table}(
                    id, retrieval_ticket_id, profile_id, version_id,
                    conversation_id, assistant_message_id, attempt,
                    terminal_status, recalled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        self._id_factory(),
                        ticket,
                        profile_id,
                        version_id,
                        conversation_id,
                        assistant_message_id,
                        attempt,
                        terminal_status,
                        now,
                    )
                    for version_id in ids
                ),
            )
            return connection.total_changes - before

    def recent_recalled_version_ids(
        self,
        layer: MemoryLayer | str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        response_limit: int = 3,
    ) -> tuple[str, ...]:
        """Return IDs used by the latest distinct successful responses."""

        layer_value = _derived_layer(layer)
        if response_limit < 1:
            return ()
        table = _recall_table(layer_value)
        rows = self._database.connection.execute(
            f"""
            WITH recent_tickets AS (
                SELECT retrieval_ticket_id, MAX(recalled_at) AS latest
                FROM {table}
                WHERE profile_id = ?
                GROUP BY retrieval_ticket_id
                ORDER BY latest DESC, retrieval_ticket_id DESC
                LIMIT ?
            )
            SELECT DISTINCT e.version_id
            FROM {table} AS e
            JOIN recent_tickets AS r USING (retrieval_ticket_id)
            ORDER BY e.version_id
            """,
            (profile_id, response_limit),
        ).fetchall()
        return tuple(str(row["version_id"]) for row in rows)

    def search(
        self,
        layer: MemoryLayer | str,
        query: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        limit: int = 30,
        include_inactive: bool = False,
    ) -> tuple[tuple[DerivedMemoryRecord, float], ...]:
        layer_value = _derived_layer(layer)
        tables = _tables(layer_value)
        _limit(limit)
        try:
            match = build_fts_match_query(query)
        except EmptySearchQuery:
            return ()
        status_clause = ""
        parameters: list[object] = [*match.parameters, profile_id]
        if not include_inactive:
            status_clause = f" AND g.status IN ({','.join('?' for _ in tables.active_statuses)})"
            parameters.extend(tables.active_statuses)
        parameters.append(limit)
        rows = self._database.connection.execute(
            f"""
            SELECT g.id AS group_id, bm25({tables.fts}) AS fts_rank
            FROM {tables.fts}
            JOIN {tables.groups} AS g ON g.id = {tables.fts}.{tables.id_column}
            WHERE {tables.fts} MATCH ? AND {tables.fts}.profile_id = ?
            {status_clause}
              AND NOT EXISTS (
                  SELECT 1 FROM memory_conflicts AS c
                  WHERE c.target_layer = ? AND c.target_group_id = g.id
                    AND c.status = 'open'
              )
            ORDER BY fts_rank, g.pinned DESC, g.updated_at DESC LIMIT ?
            """,
            (*parameters[:-1], layer_value.value, parameters[-1]),
        ).fetchall()
        return tuple(
            (
                self.get(layer_value, str(row["group_id"])),
                float(row["fts_rank"]),
            )
            for row in rows
        )

    def list_unabsorbed_fact_versions(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        subject_scope: MemorySubjectScope | str | None = None,
        limit: int = 20,
    ) -> tuple[str, ...]:
        _limit(limit)
        clauses = [
            "g.profile_id = ?",
            "g.status = 'active'",
            "v.deep_memory_eligible = 1",
            "NOT EXISTS (SELECT 1 FROM memory_reflection_sources AS rs "
            "WHERE rs.fact_version_id = v.id)",
            "NOT EXISTS (SELECT 1 FROM memory_conflicts AS c "
            "WHERE c.target_layer = 'fact' AND c.target_group_id = g.id "
            "AND c.status = 'open')",
        ]
        parameters: list[object] = [profile_id]
        if subject_scope is not None:
            scope = _subject_scope(subject_scope)
            if scope is MemorySubjectScope.COMPANION:
                return ()
            clauses.append("g.subject_scope = ?")
            parameters.append(scope.value)
        parameters.append(limit)
        rows = self._database.connection.execute(
            """
            SELECT v.id
            FROM memory_groups AS g
            JOIN memory_versions AS v ON v.id = g.current_version_id
            WHERE
            """
            + " AND ".join(clauses)
            + " ORDER BY v.created_at, v.id LIMIT ?",
            parameters,
        ).fetchall()
        return tuple(str(row["id"]) for row in rows)

    def apply_signal(
        self,
        layer: MemoryLayer | str,
        group_id: str,
        kind: EvidenceSignalKind | str,
        *,
        source_message_id: str | None = None,
        source_fact_version_id: str | None = None,
        correlation_key: str,
    ) -> EvidenceSnapshot:
        layer_value = _evidence_layer(layer)
        kind_value = _signal_kind(kind)
        now_dt = self._clock()
        now = encode_utc(now_dt)
        with self._database.transaction() as connection:
            profile_id, version_id = self._target_identity(
                connection,
                layer_value,
                group_id,
            )
            if source_message_id is not None:
                self._validate_user_source(connection, profile_id, source_message_id)
            if source_fact_version_id is not None:
                self._validate_evidence_fact(
                    connection,
                    profile_id,
                    source_fact_version_id,
                    source_message_id=source_message_id,
                )
            reinforcement, disputation = self._signal_delta(
                connection,
                layer_value,
                version_id,
                kind_value,
            )
            inserted = self._insert_signal(
                connection,
                profile_id=profile_id,
                layer=layer_value,
                group_id=group_id,
                version_id=version_id,
                kind=kind_value,
                reinforcement_delta=reinforcement,
                disputation_delta=disputation,
                correlation_key=_identifier(correlation_key, "correlation_key"),
                created_at=now,
                source_message_id=source_message_id,
                source_fact_version_id=source_fact_version_id,
            )
            snapshot = self._evidence_snapshot_with_connection(
                connection,
                layer_value,
                version_id,
                now_dt,
            )
            if inserted:
                self._apply_score_state(
                    connection,
                    layer=layer_value,
                    group_id=group_id,
                    snapshot=snapshot,
                    now=now,
                )
                if (
                    kind_value is EvidenceSignalKind.DIRECT_REBUT
                    and layer_value is not MemoryLayer.FACT
                ):
                    tables = _tables(layer_value)
                    connection.execute(
                        f"UPDATE {tables.groups} SET status = 'disputed', updated_at = ? "
                        "WHERE id = ? AND status NOT IN ('denied', 'archived')",
                        (now, group_id),
                    )
                elif (
                    kind_value is EvidenceSignalKind.DIRECT_CONFIRM
                    and layer_value is not MemoryLayer.FACT
                    and snapshot.score >= EVIDENCE_CONFIRMED_THRESHOLD
                ):
                    tables = _tables(layer_value)
                    target = (
                        DerivedMemoryStatus.CONFIRMED
                        if layer_value is MemoryLayer.REFLECTION
                        else DerivedMemoryStatus.ACTIVE
                    )
                    connection.execute(
                        f"UPDATE {tables.groups} SET status = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'disputed'",
                        (target.value, now, group_id),
                    )
                self._insert_audit(
                    connection,
                    profile_id=profile_id,
                    layer=layer_value,
                    group_id=group_id,
                    version_id=version_id,
                    source_message_id=source_message_id,
                    event_type="evidence.applied",
                    reason_code=kind_value.value,
                    reinforcement_delta=reinforcement,
                    disputation_delta=disputation,
                    occurred_at=now,
                )
            return snapshot

    def evidence_snapshot(
        self,
        layer: MemoryLayer | str,
        version_id: str,
        *,
        now: datetime | None = None,
    ) -> EvidenceSnapshot:
        return self._evidence_snapshot_with_connection(
            self._database.connection,
            _evidence_layer(layer),
            _identifier(version_id, "version_id"),
            now or self._clock(),
        )

    def confirm(self, layer: MemoryLayer | str, group_id: str) -> DerivedMemoryRecord:
        self.apply_signal(
            layer,
            group_id,
            EvidenceSignalKind.DIRECT_CONFIRM,
            correlation_key=f"manual-confirm:{group_id}:{self._id_factory()}",
        )
        return self.get(layer, group_id)

    def deny(self, layer: MemoryLayer | str, group_id: str) -> DerivedMemoryRecord:
        layer_value = _derived_layer(layer)
        self.apply_signal(
            layer_value,
            group_id,
            EvidenceSignalKind.DIRECT_REBUT,
            correlation_key=f"manual-deny:{group_id}:{self._id_factory()}",
        )
        return self.set_status(layer_value, group_id, DerivedMemoryStatus.DENIED)

    def archive(self, layer: MemoryLayer | str, group_id: str) -> DerivedMemoryRecord:
        return self.set_status(layer, group_id, DerivedMemoryStatus.ARCHIVED)

    def restore(self, layer: MemoryLayer | str, group_id: str) -> DerivedMemoryRecord:
        layer_value = _derived_layer(layer)
        current = self.get(layer_value, group_id)
        if current.status is DerivedMemoryStatus.DENIED:
            raise StorageConflictError("denied memories require an explicit correction")
        if layer_value is MemoryLayer.PERSONA:
            target = DerivedMemoryStatus.ACTIVE
        else:
            target = (
                DerivedMemoryStatus.CONFIRMED
                if current.evidence_score >= EVIDENCE_CONFIRMED_THRESHOLD
                else DerivedMemoryStatus.TENTATIVE
            )
        return self.set_status(layer_value, group_id, target)

    def set_status(
        self,
        layer: MemoryLayer | str,
        group_id: str,
        status: DerivedMemoryStatus | str,
    ) -> DerivedMemoryRecord:
        layer_value = _derived_layer(layer)
        status_value = _derived_status(status)
        if layer_value is MemoryLayer.PERSONA and status_value not in {
            DerivedMemoryStatus.ACTIVE,
            DerivedMemoryStatus.DISPUTED,
            DerivedMemoryStatus.DENIED,
            DerivedMemoryStatus.ARCHIVED,
        }:
            raise StorageValidationError("invalid persona impression status")
        now = encode_utc(self._clock())
        tables = _tables(layer_value)
        with self._database.transaction() as connection:
            current = self._get_with_connection(connection, layer_value, group_id)
            connection.execute(
                f"""
                UPDATE {tables.groups}
                SET status = ?, archive_candidate_since = NULL, updated_at = ?
                WHERE id = ?
                """,
                (status_value.value, now, group_id),
            )
            self._insert_audit(
                connection,
                profile_id=current.profile_id,
                layer=layer_value,
                group_id=group_id,
                version_id=current.current_version.version_id,
                event_type="status.changed",
                reason_code=status_value.value,
                occurred_at=now,
            )
        return self.get(layer_value, group_id)

    def set_pinned(
        self,
        layer: MemoryLayer | str,
        group_id: str,
        pinned: bool,
    ) -> DerivedMemoryRecord:
        layer_value = _derived_layer(layer)
        tables = _tables(layer_value)
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            current = self._get_with_connection(connection, layer_value, group_id)
            connection.execute(
                f"UPDATE {tables.groups} SET pinned = ?, updated_at = ? WHERE id = ?",
                (int(pinned), now, group_id),
            )
            self._insert_audit(
                connection,
                profile_id=current.profile_id,
                layer=layer_value,
                group_id=group_id,
                version_id=current.current_version.version_id,
                event_type="pin.changed",
                reason_code="pinned" if pinned else "unpinned",
                occurred_at=now,
            )
        return self.get(layer_value, group_id)

    def add_version(
        self,
        layer: MemoryLayer | str,
        group_id: str,
        content: str,
        *,
        importance: float | None = None,
        operation: str = "manual_edit",
    ) -> DerivedMemoryRecord:
        """Create a new current immutable version and suppress stale descendants."""

        layer_value = _derived_layer(layer)
        if operation not in {"correct", "manual_edit", "rollback"}:
            raise StorageValidationError("unsupported manual derived-memory operation")
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            current = self._get_with_connection(connection, layer_value, group_id)
            version_id = self._append_derived_version(
                connection,
                layer=layer_value,
                group=current,
                content=content,
                importance=(
                    current.current_version.importance if importance is None else importance
                ),
                confidence=1.0,
                operation=operation,
                origin="manual",
                now=now,
            )
            self._insert_manual_source(connection, layer_value, version_id, now)
            self._insert_audit(
                connection,
                profile_id=current.profile_id,
                layer=layer_value,
                group_id=group_id,
                version_id=version_id,
                event_type="version.created",
                reason_code=operation,
                occurred_at=now,
            )
            self._suppress_descendants(connection, layer_value, group_id, now)
        return self.get(layer_value, group_id)

    def rollback(
        self,
        layer: MemoryLayer | str,
        group_id: str,
        version_id: str,
    ) -> DerivedMemoryRecord:
        layer_value = _derived_layer(layer)
        tables = _tables(layer_value)
        row = self._database.connection.execute(
            f"SELECT content, importance FROM {tables.versions} "
            f"WHERE id = ? AND {tables.group_fk} = ?",
            (version_id, group_id),
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("derived memory version does not exist")
        return self.add_version(
            layer_value,
            group_id,
            str(row["content"]),
            importance=float(row["importance"]),
            operation="rollback",
        )

    def list_versions(
        self,
        layer: MemoryLayer | str,
        group_id: str,
    ) -> tuple[DerivedMemoryVersion, ...]:
        layer_value = _derived_layer(layer)
        tables = _tables(layer_value)
        rows = self._database.connection.execute(
            f"SELECT * FROM {tables.versions} WHERE {tables.group_fk} = ? ORDER BY version_number",
            (group_id,),
        ).fetchall()
        if not rows:
            self.get(layer_value, group_id)
        return tuple(_version_from_row(row, group_id) for row in rows)

    def list_sources(
        self,
        layer: MemoryLayer | str,
        group_id: str,
        *,
        version_id: str | None = None,
    ) -> tuple[DerivedMemorySource, ...]:
        layer_value = _derived_layer(layer)
        tables = _tables(layer_value)
        params: list[object] = [group_id]
        version_clause = ""
        if version_id is not None:
            version_clause = " AND s.version_id = ?"
            params.append(version_id)
        rows = self._database.connection.execute(
            f"""
            SELECT s.* FROM {tables.sources} AS s
            JOIN {tables.versions} AS v ON v.id = s.version_id
            WHERE v.{tables.group_fk} = ? {version_clause}
            ORDER BY v.version_number, s.created_at, s.id
            """,
            params,
        ).fetchall()
        if not rows:
            self.get(layer_value, group_id)
        return tuple(
            DerivedMemorySource(
                source_id=str(row["id"]),
                version_id=str(row["version_id"]),
                parent_version_id=row[tables.parent_fk],
                source_message_id=str(row["source_message_id"]),
                live_message_id=row["live_message_id"],
                extraction_method=str(row["extraction_method"]),
                created_at=_required_datetime(row["created_at"]),
            )
            for row in rows
        )

    def open_fact_conflict(
        self,
        memory_id: str,
        challenger_content: str,
        *,
        source_message_id: str,
        importance: float,
        confidence: float,
    ) -> MemoryConflict:
        """Persist a non-current challenger version and suppress the fact from recall."""

        fields = _content_fields(challenger_content)
        importance = _unit_interval(importance, "importance")
        confidence = _unit_interval(confidence, "confidence")
        now = encode_utc(self._clock())
        conflict_id = self._id_factory()
        challenger_id = self._id_factory()
        with self._database.transaction() as connection:
            current = connection.execute(
                """
                SELECT g.profile_id, g.current_version_id,
                       (SELECT MAX(all_v.version_number)
                        FROM memory_versions AS all_v
                        WHERE all_v.memory_id = g.id) AS max_version_number
                FROM memory_groups AS g
                JOIN memory_versions AS v ON v.id = g.current_version_id
                WHERE g.id = ?
                """,
                (memory_id,),
            ).fetchone()
            if current is None:
                raise StorageNotFoundError("memory does not exist")
            self._validate_user_source(connection, str(current["profile_id"]), source_message_id)
            if connection.execute(
                """
                SELECT 1 FROM memory_conflicts
                WHERE target_layer = 'fact' AND target_group_id = ? AND status = 'open'
                """,
                (memory_id,),
            ).fetchone():
                raise StorageConflictError("memory already has an open conflict")
            connection.execute(
                """
                INSERT INTO memory_versions(
                    id, memory_id, version_number, content, normalized_content,
                    content_hash, search_text, importance, confidence, origin,
                    operation, supersedes_version_id, created_at, deep_memory_eligible
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'automatic', 'correct', ?, ?, 1)
                """,
                (
                    challenger_id,
                    memory_id,
                    int(current["max_version_number"]) + 1,
                    *fields,
                    importance,
                    confidence,
                    current["current_version_id"],
                    now,
                ),
            )
            message = connection.execute(
                "SELECT conversation_id FROM messages WHERE id = ?",
                (source_message_id,),
            ).fetchone()
            assert message is not None
            connection.execute(
                """
                INSERT INTO memory_sources(
                    id, version_id, source_message_id, source_conversation_id,
                    live_message_id, live_conversation_id, extraction_method, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'automatic', ?)
                """,
                (
                    self._id_factory(),
                    challenger_id,
                    source_message_id,
                    message["conversation_id"],
                    source_message_id,
                    message["conversation_id"],
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO memory_conflicts(
                    id, profile_id, target_layer, target_group_id,
                    incumbent_version_id, challenger_version_id, status,
                    source_message_id, created_at
                ) VALUES (?, ?, 'fact', ?, ?, ?, 'open', ?, ?)
                """,
                (
                    conflict_id,
                    current["profile_id"],
                    memory_id,
                    current["current_version_id"],
                    challenger_id,
                    source_message_id,
                    now,
                ),
            )
            self._insert_audit(
                connection,
                profile_id=str(current["profile_id"]),
                layer=MemoryLayer.FACT,
                group_id=memory_id,
                version_id=challenger_id,
                source_message_id=source_message_id,
                event_type="conflict.opened",
                reason_code="ambiguous_correction",
                occurred_at=now,
            )
            self._suppress_descendants(connection, MemoryLayer.FACT, memory_id, now)
        return self.get_conflict(conflict_id)

    def get_conflict(self, conflict_id: str) -> MemoryConflict:
        row = self._database.connection.execute(
            "SELECT * FROM memory_conflicts WHERE id = ?",
            (_identifier(conflict_id, "conflict_id"),),
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("memory conflict does not exist")
        return _conflict_from_row(row)

    def mark_promotion_conflict(
        self,
        reflection_id: str,
        impression_id: str,
    ) -> MemoryConflict:
        """Block both sides when promotion finds an unresolved semantic conflict."""

        now = encode_utc(self._clock())
        conflict_id = self._id_factory()
        with self._database.transaction() as connection:
            reflection = self._get_with_connection(
                connection,
                MemoryLayer.REFLECTION,
                reflection_id,
            )
            impression = self._get_with_connection(
                connection,
                MemoryLayer.PERSONA,
                impression_id,
            )
            if reflection.profile_id != impression.profile_id:
                raise StorageConflictError("promotion conflict crosses profiles")
            connection.execute(
                """
                INSERT INTO memory_conflicts(
                    id, profile_id, target_layer, target_group_id,
                    incumbent_version_id, challenger_version_id, status, created_at
                ) VALUES (?, ?, 'reflection', ?, ?, ?, 'open', ?)
                """,
                (
                    conflict_id,
                    reflection.profile_id,
                    reflection_id,
                    reflection.current_version.version_id,
                    impression.current_version.version_id,
                    now,
                ),
            )
            connection.execute(
                "UPDATE memory_reflections SET status = 'disputed', updated_at = ? WHERE id = ?",
                (now, reflection_id),
            )
            connection.execute(
                """
                UPDATE memory_persona_impressions
                SET status = 'disputed', updated_at = ? WHERE id = ?
                """,
                (now, impression_id),
            )
            self._insert_audit(
                connection,
                profile_id=reflection.profile_id,
                layer=MemoryLayer.REFLECTION,
                group_id=reflection_id,
                version_id=reflection.current_version.version_id,
                event_type="conflict.opened",
                reason_code="promotion_conflict",
                metadata={"persona_impression_id": impression_id},
                occurred_at=now,
            )
        return self.get_conflict(conflict_id)

    def list_conflicts(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        include_resolved: bool = False,
        limit: int = 200,
    ) -> tuple[MemoryConflict, ...]:
        _limit(limit)
        status_clause = "" if include_resolved else " AND status = 'open'"
        rows = self._database.connection.execute(
            "SELECT * FROM memory_conflicts WHERE profile_id = ?"
            + status_clause
            + " ORDER BY created_at DESC, id LIMIT ?",
            (profile_id, limit),
        ).fetchall()
        return tuple(_conflict_from_row(row) for row in rows)

    def resolve_fact_conflict(
        self,
        conflict_id: str,
        resolution: ConflictResolution | str,
        *,
        merged_content: str | None = None,
    ) -> None:
        resolution_value = _conflict_resolution(resolution)
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            conflict = connection.execute(
                "SELECT * FROM memory_conflicts WHERE id = ? AND status = 'open'",
                (conflict_id,),
            ).fetchone()
            if conflict is None or conflict["target_layer"] != MemoryLayer.FACT.value:
                raise StorageNotFoundError("open fact conflict does not exist")
            memory_id = str(conflict["target_group_id"])
            selected_version = str(conflict["incumbent_version_id"])
            if resolution_value is ConflictResolution.ACCEPT:
                selected_version = str(conflict["challenger_version_id"])
            elif resolution_value is ConflictResolution.MERGE:
                if merged_content is None:
                    raise StorageValidationError("merge resolution requires content")
                current = connection.execute(
                    """
                    SELECT g.profile_id,
                           (SELECT MAX(all_v.version_number)
                            FROM memory_versions AS all_v
                            WHERE all_v.memory_id = g.id) AS max_version_number
                    FROM memory_groups AS g
                    JOIN memory_versions AS v ON v.id = g.current_version_id
                    WHERE g.id = ?
                    """,
                    (memory_id,),
                ).fetchone()
                assert current is not None
                selected_version = self._id_factory()
                fields = _content_fields(merged_content)
                connection.execute(
                    """
                    INSERT INTO memory_versions(
                        id, memory_id, version_number, content, normalized_content,
                        content_hash, search_text, importance, confidence, origin,
                        operation, supersedes_version_id, created_at, deep_memory_eligible
                    )
                    SELECT ?, memory_id, ?, ?, ?, ?, ?, importance, 1.0, 'manual',
                           'manual_edit', id, ?, 1
                    FROM memory_versions WHERE id = ?
                    """,
                    (
                        selected_version,
                        int(current["max_version_number"]) + 1,
                        *fields,
                        now,
                        conflict["incumbent_version_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO memory_sources(
                        id, version_id, source_message_id, extraction_method, created_at
                    ) VALUES (?, ?, ?, 'manual', ?)
                    """,
                    (
                        self._id_factory(),
                        selected_version,
                        f"manual:{selected_version}",
                        now,
                    ),
                )
            selected = connection.execute(
                "SELECT search_text FROM memory_versions WHERE id = ? AND memory_id = ?",
                (selected_version, memory_id),
            ).fetchone()
            if selected is None:
                raise StorageConflictError("conflict versions changed")
            connection.execute(
                "UPDATE memory_groups SET current_version_id = ?, updated_at = ? WHERE id = ?",
                (selected_version, now, memory_id),
            )
            connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
            connection.execute(
                """
                INSERT INTO memory_fts(version_id, memory_id, profile_id, search_text)
                SELECT ?, g.id, g.profile_id, ? FROM memory_groups AS g WHERE g.id = ?
                """,
                (selected_version, selected["search_text"], memory_id),
            )
            connection.execute(
                """
                UPDATE memory_conflicts
                SET status = 'resolved', resolution = ?, resolved_at = ? WHERE id = ?
                """,
                (resolution_value.value, now, conflict_id),
            )
            self._insert_audit(
                connection,
                profile_id=str(conflict["profile_id"]),
                layer=MemoryLayer.FACT,
                group_id=memory_id,
                version_id=selected_version,
                event_type="conflict.resolved",
                reason_code=resolution_value.value,
                occurred_at=now,
            )
            if resolution_value is ConflictResolution.KEEP:
                self._restore_fact_descendants(connection, memory_id, now)

    def suppress_fact_descendants(
        self,
        memory_id: str,
        *,
        reason_code: str = "upstream_changed",
    ) -> None:
        """Immediately stop recall of reflections/personas derived from a changed fact."""

        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            profile_id, version_id = self._target_identity(
                connection,
                MemoryLayer.FACT,
                memory_id,
            )
            self._suppress_descendants(connection, MemoryLayer.FACT, memory_id, now)
            self._insert_audit(
                connection,
                profile_id=profile_id,
                layer=MemoryLayer.FACT,
                group_id=memory_id,
                version_id=version_id,
                event_type="descendants.suppressed",
                reason_code=reason_code,
                occurred_at=now,
            )

    def reevaluate_fact_descendants(self, memory_id: str) -> None:
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            self._target_identity(connection, MemoryLayer.FACT, memory_id)
            self._restore_fact_descendants(connection, memory_id, now)

    def clear_all(self, *, profile_id: str = DEFAULT_PROFILE_ID) -> int:
        """Delete derived semantic layers and their metadata for one local profile."""

        with self._database.transaction() as connection:
            reflection_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM memory_reflections WHERE profile_id = ?",
                    (profile_id,),
                ).fetchone()[0]
            )
            persona_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM memory_persona_impressions WHERE profile_id = ?",
                    (profile_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "DELETE FROM memory_audit_events WHERE profile_id = ?",
                (profile_id,),
            )
            connection.execute(
                "DELETE FROM memory_evidence_signals WHERE profile_id = ?",
                (profile_id,),
            )
            connection.execute(
                "DELETE FROM memory_conflicts WHERE profile_id = ?",
                (profile_id,),
            )
            connection.execute(
                "DELETE FROM memory_persona_impression_fts WHERE profile_id = ?",
                (profile_id,),
            )
            connection.execute(
                "DELETE FROM memory_reflection_fts WHERE profile_id = ?",
                (profile_id,),
            )
            connection.execute(
                "DELETE FROM memory_persona_impressions WHERE profile_id = ?",
                (profile_id,),
            )
            connection.execute(
                "DELETE FROM memory_reflections WHERE profile_id = ?",
                (profile_id,),
            )
            connection.execute(
                "DELETE FROM memory_pipeline_state WHERE profile_id = ?",
                (profile_id,),
            )
            connection.execute(
                """
                DELETE FROM background_jobs
                WHERE profile_id = ? AND kind IN ('deep_memory_cycle', 'persona_promotion')
                """,
                (profile_id,),
            )
        if reflection_count or persona_count:
            self._database.purge_deleted_content()
        return reflection_count + persona_count

    def run_maintenance(self, *, profile_id: str = DEFAULT_PROFILE_ID) -> int:
        """Archive unpinned derived memories after fourteen continuous low-score days."""

        now_dt = self._clock()
        now = encode_utc(now_dt)
        archived = 0
        with self._database.transaction() as connection:
            for layer in (MemoryLayer.REFLECTION, MemoryLayer.PERSONA):
                tables = _tables(layer)
                rows = connection.execute(
                    self._select_sql(layer)
                    + " WHERE g.profile_id = ? AND g.pinned = 0 "
                    + "AND g.status NOT IN ('archived', 'denied', 'promoted', 'merged')",
                    (profile_id,),
                ).fetchall()
                for row in rows:
                    record = self._record_from_row(layer, row, connection=connection, now=now_dt)
                    if record.evidence_score > EVIDENCE_ARCHIVE_THRESHOLD:
                        if record.archive_candidate_since is not None:
                            connection.execute(
                                f"UPDATE {tables.groups} SET archive_candidate_since = NULL "
                                "WHERE id = ?",
                                (record.group_id,),
                            )
                        continue
                    candidate_since = record.archive_candidate_since
                    if candidate_since is None:
                        connection.execute(
                            f"UPDATE {tables.groups} SET archive_candidate_since = ? WHERE id = ?",
                            (now, record.group_id),
                        )
                        continue
                    if now_dt - candidate_since < timedelta(days=EVIDENCE_ARCHIVE_DAYS):
                        continue
                    connection.execute(
                        f"""
                        UPDATE {tables.groups}
                        SET status = 'archived', updated_at = ? WHERE id = ?
                        """,
                        (now, record.group_id),
                    )
                    self._insert_audit(
                        connection,
                        profile_id=profile_id,
                        layer=layer,
                        group_id=record.group_id,
                        version_id=record.current_version.version_id,
                        event_type="status.changed",
                        reason_code="evidence_decay_archive",
                        occurred_at=now,
                    )
                    archived += 1
        return archived

    def list_audit_events(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        layer: MemoryLayer | str | None = None,
        group_id: str | None = None,
        limit: int = 500,
    ) -> tuple[MemoryAuditEvent, ...]:
        _limit(limit)
        clauses = ["profile_id = ?"]
        params: list[object] = [profile_id]
        if layer is not None:
            clauses.append("owner_layer = ?")
            params.append(_evidence_layer(layer).value)
        if group_id is not None:
            clauses.append("owner_group_id = ?")
            params.append(group_id)
        params.append(limit)
        rows = self._database.connection.execute(
            "SELECT * FROM memory_audit_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY occurred_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return tuple(_audit_from_row(row) for row in rows)

    def event_timeline(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        limit: int = 500,
    ) -> tuple[EventTimelineItem, ...]:
        _limit(limit)
        rows = self._database.connection.execute(
            """
            SELECT g.id AS memory_id, v.id AS version_id, v.content,
                   v.event_started_at, v.created_at, v.importance
            FROM memory_groups AS g
            JOIN memory_versions AS v ON v.id = g.current_version_id
            WHERE g.profile_id = ? AND g.kind = 'event' AND g.status = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM memory_conflicts AS c
                  WHERE c.target_layer = 'fact' AND c.target_group_id = g.id
                    AND c.status = 'open'
              )
            ORDER BY COALESCE(v.event_started_at, v.created_at) DESC, g.id
            LIMIT ?
            """,
            (profile_id, limit),
        ).fetchall()
        return tuple(
            EventTimelineItem(
                memory_id=str(row["memory_id"]),
                version_id=str(row["version_id"]),
                content=str(row["content"]),
                occurred_at=_required_datetime(row["event_started_at"] or row["created_at"]),
                occurred_at_is_explicit=row["event_started_at"] is not None,
                importance=float(row["importance"]),
            )
            for row in rows
        )

    def delete_cascade(self, layer: MemoryLayer | str, group_id: str) -> int:
        """Delete an item and every derived descendant that may encode its content."""

        layer_value = _evidence_layer(layer)
        with self._database.transaction() as connection:
            self._target_identity(connection, layer_value, group_id)
            reflection_ids: set[str] = set()
            persona_ids: set[str] = set()
            if layer_value is MemoryLayer.FACT:
                reflection_ids.update(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT rv.reflection_id
                        FROM memory_reflection_sources AS rs
                        JOIN memory_versions AS fv ON fv.id = rs.fact_version_id
                        JOIN memory_reflection_versions AS rv ON rv.id = rs.version_id
                        WHERE fv.memory_id = ?
                        """,
                        (group_id,),
                    ).fetchall()
                )
            elif layer_value is MemoryLayer.REFLECTION:
                reflection_ids.add(group_id)
            elif layer_value is MemoryLayer.PERSONA:
                persona_ids.add(group_id)
            if reflection_ids:
                placeholders = ",".join("?" for _ in reflection_ids)
                persona_ids.update(
                    str(row[0])
                    for row in connection.execute(
                        f"""
                        SELECT DISTINCT pv.impression_id
                        FROM memory_persona_impression_sources AS ps
                        JOIN memory_reflection_versions AS rv
                          ON rv.id = ps.reflection_version_id
                        JOIN memory_persona_impression_versions AS pv
                          ON pv.id = ps.version_id
                        WHERE rv.reflection_id IN ({placeholders})
                        """,
                        tuple(reflection_ids),
                    ).fetchall()
                )
            count = len(reflection_ids) + len(persona_ids)
            if layer_value is MemoryLayer.FACT:
                count += 1
            for impression_id in persona_ids:
                self._delete_owner_metadata(connection, MemoryLayer.PERSONA, impression_id)
                connection.execute(
                    "DELETE FROM memory_persona_impression_fts WHERE impression_id = ?",
                    (impression_id,),
                )
                connection.execute(
                    "DELETE FROM memory_persona_impressions WHERE id = ?",
                    (impression_id,),
                )
            for reflection_id in reflection_ids:
                self._delete_owner_metadata(connection, MemoryLayer.REFLECTION, reflection_id)
                connection.execute(
                    "DELETE FROM memory_reflection_fts WHERE reflection_id = ?",
                    (reflection_id,),
                )
                connection.execute(
                    "DELETE FROM memory_reflections WHERE id = ?",
                    (reflection_id,),
                )
            self._delete_owner_metadata(connection, layer_value, group_id)
            if layer_value is MemoryLayer.FACT:
                connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (group_id,))
                cursor = connection.execute("DELETE FROM memory_groups WHERE id = ?", (group_id,))
            elif layer_value is MemoryLayer.REFLECTION:
                cursor = connection.execute(
                    "DELETE FROM memory_reflections WHERE id = ?",
                    (group_id,),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM memory_persona_impressions WHERE id = ?",
                    (group_id,),
                )
            del cursor
        self._database.purge_deleted_content()
        return count

    def deletion_impact(self, layer: MemoryLayer | str, group_id: str) -> dict[str, int]:
        """Return bounded descendant counts for an explicit permanent-delete warning."""

        layer_value = _evidence_layer(layer)
        connection = self._database.connection
        self._target_identity(connection, layer_value, group_id)
        reflection_ids: set[str] = set()
        persona_ids: set[str] = set()
        if layer_value is MemoryLayer.FACT:
            reflection_ids.update(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT rv.reflection_id
                    FROM memory_reflection_sources AS rs
                    JOIN memory_versions AS fv ON fv.id = rs.fact_version_id
                    JOIN memory_reflection_versions AS rv ON rv.id = rs.version_id
                    WHERE fv.memory_id = ?
                    """,
                    (group_id,),
                ).fetchall()
            )
        elif layer_value is MemoryLayer.REFLECTION:
            reflection_ids.add(group_id)
        elif layer_value is MemoryLayer.PERSONA:
            persona_ids.add(group_id)
        if reflection_ids:
            placeholders = ",".join("?" for _ in reflection_ids)
            persona_ids.update(
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT pv.impression_id
                    FROM memory_persona_impression_sources AS ps
                    JOIN memory_reflection_versions AS rv
                      ON rv.id = ps.reflection_version_id
                    JOIN memory_persona_impression_versions AS pv
                      ON pv.id = ps.version_id
                    WHERE rv.reflection_id IN ({placeholders})
                    """,
                    tuple(reflection_ids),
                ).fetchall()
            )
        return {
            "facts": int(layer_value is MemoryLayer.FACT),
            "reflections": len(reflection_ids),
            "personas": len(persona_ids),
        }

    def _eligible_fact_sources(
        self,
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        fact_version_ids: Sequence[str],
    ) -> tuple[sqlite3.Row, ...]:
        placeholders = ",".join("?" for _ in fact_version_ids)
        facts = connection.execute(
            f"""
            SELECT v.id
            FROM memory_versions AS v
            JOIN memory_groups AS g
              ON g.id = v.memory_id AND g.current_version_id = v.id
            WHERE v.id IN ({placeholders}) AND g.profile_id = ?
              AND g.status = 'active' AND v.deep_memory_eligible = 1
              AND NOT EXISTS (
                  SELECT 1 FROM memory_conflicts AS c
                  WHERE c.target_layer = 'fact' AND c.target_group_id = g.id
                    AND c.status = 'open'
              )
            """,
            (*fact_version_ids, profile_id),
        ).fetchall()
        if {str(row["id"]) for row in facts} != set(fact_version_ids):
            raise StorageValidationError("reflection facts must be current, eligible, and active")
        rows = connection.execute(
            f"""
            SELECT s.version_id AS fact_version_id, s.source_message_id,
                   s.live_message_id
            FROM memory_sources AS s
            JOIN messages AS m ON m.id = s.live_message_id
            JOIN conversations AS c ON c.id = m.conversation_id
            WHERE s.version_id IN ({placeholders}) AND s.extraction_method = 'automatic'
              AND m.role = 'user' AND m.participates_in_memory = 1
              AND c.profile_id = ?
            ORDER BY s.version_id, s.created_at, s.id
            """,
            (*fact_version_ids, profile_id),
        ).fetchall()
        by_fact = {str(row["fact_version_id"]) for row in rows}
        if by_fact != set(fact_version_ids):
            raise StorageValidationError("each reflection fact needs a live user source")
        return tuple(rows)

    def _insert_group_and_version(
        self,
        connection: sqlite3.Connection,
        *,
        layer: MemoryLayer,
        group_id: str,
        version_id: str,
        profile_id: str,
        subject_scope: MemorySubjectScope,
        topic_key: str,
        status: DerivedMemoryStatus,
        fields: tuple[str, str, str, str],
        importance: float,
        confidence: float,
        operation: str,
        now: str,
    ) -> None:
        tables = _tables(layer)
        topic = normalize_topic_key(topic_key)
        if not topic:
            raise StorageValidationError("topic_key must contain searchable text")
        connection.execute(
            f"""
            INSERT INTO {tables.groups}(
                id, profile_id, subject_scope, topic_key, status, pinned,
                current_version_id, archive_candidate_since, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)
            """,
            (group_id, profile_id, subject_scope.value, topic, status.value, now, now),
        )
        content, normalized, content_hash, search_text = fields
        connection.execute(
            f"""
            INSERT INTO {tables.versions}(
                id, {tables.group_fk}, version_number, content, normalized_content,
                content_hash, search_text, importance, confidence, origin,
                operation, supersedes_version_id, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 'automatic', ?, NULL, ?)
            """,
            (
                version_id,
                group_id,
                content,
                normalized,
                content_hash,
                search_text,
                importance,
                confidence,
                operation,
                now,
            ),
        )
        connection.execute(
            f"UPDATE {tables.groups} SET current_version_id = ? WHERE id = ?",
            (version_id, group_id),
        )
        self._replace_fts(
            connection,
            layer=layer,
            group_id=group_id,
            version_id=version_id,
            profile_id=profile_id,
            search_text=search_text,
        )

    def _append_derived_version(
        self,
        connection: sqlite3.Connection,
        *,
        layer: MemoryLayer,
        group: DerivedMemoryRecord,
        content: str,
        importance: float,
        confidence: float,
        operation: str,
        origin: str,
        now: str,
    ) -> str:
        tables = _tables(layer)
        fields = _content_fields(content)
        importance = _unit_interval(importance, "importance")
        confidence = 1.0 if origin == "manual" else _unit_interval(confidence, "confidence")
        version_id = self._id_factory()
        connection.execute(
            f"""
            INSERT INTO {tables.versions}(
                id, {tables.group_fk}, version_number, content, normalized_content,
                content_hash, search_text, importance, confidence, origin,
                operation, supersedes_version_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                group.group_id,
                group.current_version.version_number + 1,
                *fields,
                importance,
                confidence,
                origin,
                operation,
                group.current_version.version_id,
                now,
            ),
        )
        connection.execute(
            f"""
            UPDATE {tables.groups}
            SET current_version_id = ?, updated_at = ?, archive_candidate_since = NULL
            WHERE id = ?
            """,
            (version_id, now, group.group_id),
        )
        self._replace_fts(
            connection,
            layer=layer,
            group_id=group.group_id,
            version_id=version_id,
            profile_id=group.profile_id,
            search_text=fields[3],
        )
        return version_id

    def _insert_manual_source(
        self,
        connection: sqlite3.Connection,
        layer: MemoryLayer,
        version_id: str,
        now: str,
    ) -> None:
        tables = _tables(layer)
        connection.execute(
            f"""
            INSERT INTO {tables.sources}(
                id, version_id, {tables.parent_fk}, source_message_id,
                live_message_id, extraction_method, created_at
            ) VALUES (?, ?, NULL, ?, NULL, 'manual', ?)
            """,
            (self._id_factory(), version_id, f"manual:{version_id}", now),
        )

    @staticmethod
    def _replace_fts(
        connection: sqlite3.Connection,
        *,
        layer: MemoryLayer,
        group_id: str,
        version_id: str,
        profile_id: str,
        search_text: str,
    ) -> None:
        tables = _tables(layer)
        connection.execute(
            f"DELETE FROM {tables.fts} WHERE {tables.id_column} = ?",
            (group_id,),
        )
        connection.execute(
            f"""
            INSERT INTO {tables.fts}(version_id, {tables.id_column}, profile_id, search_text)
            VALUES (?, ?, ?, ?)
            """,
            (version_id, group_id, profile_id, search_text),
        )

    def _get_with_connection(
        self,
        connection: sqlite3.Connection,
        layer: MemoryLayer,
        group_id: str,
    ) -> DerivedMemoryRecord:
        row = connection.execute(
            self._select_sql(layer) + " WHERE g.id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("derived memory does not exist")
        return self._record_from_row(layer, row, connection=connection)

    @staticmethod
    def _select_sql(layer: MemoryLayer) -> str:
        tables = _tables(layer)
        return f"""
        SELECT g.id AS group_id, g.profile_id, g.subject_scope, g.topic_key,
               g.status, g.pinned, g.archive_candidate_since,
               g.created_at AS group_created_at, g.updated_at AS group_updated_at,
               v.id AS version_id, v.version_number, v.content,
               v.normalized_content, v.content_hash, v.importance, v.confidence,
               v.origin, v.operation, v.supersedes_version_id,
               v.created_at AS version_created_at
        FROM {tables.groups} AS g
        JOIN {tables.versions} AS v ON v.id = g.current_version_id
        """

    def _record_from_row(
        self,
        layer: MemoryLayer,
        row: sqlite3.Row,
        *,
        connection: sqlite3.Connection | None = None,
        now: datetime | None = None,
    ) -> DerivedMemoryRecord:
        connection = connection or self._database.connection
        version = DerivedMemoryVersion(
            version_id=str(row["version_id"]),
            group_id=str(row["group_id"]),
            version_number=int(row["version_number"]),
            content=str(row["content"]),
            normalized_content=str(row["normalized_content"]),
            content_hash=str(row["content_hash"]),
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            origin=str(row["origin"]),
            operation=str(row["operation"]),
            supersedes_version_id=row["supersedes_version_id"],
            created_at=_required_datetime(row["version_created_at"]),
        )
        snapshot = self._evidence_snapshot_with_connection(
            connection,
            layer,
            version.version_id,
            now or self._clock(),
        )
        conflicted = (
            connection.execute(
                """
            SELECT 1 FROM memory_conflicts
            WHERE target_layer = ? AND target_group_id = ? AND status = 'open'
            """,
                (layer.value, row["group_id"]),
            ).fetchone()
            is not None
        )
        return DerivedMemoryRecord(
            group_id=str(row["group_id"]),
            profile_id=str(row["profile_id"]),
            layer=layer,
            subject_scope=MemorySubjectScope(row["subject_scope"]),
            topic_key=str(row["topic_key"]),
            status=DerivedMemoryStatus(row["status"]),
            pinned=bool(row["pinned"]),
            current_version=version,
            archive_candidate_since=decode_utc(row["archive_candidate_since"]),
            created_at=_required_datetime(row["group_created_at"]),
            updated_at=_required_datetime(row["group_updated_at"]),
            evidence_score=snapshot.score,
            conflicted=conflicted,
        )

    def _target_identity(
        self,
        connection: sqlite3.Connection,
        layer: MemoryLayer,
        group_id: str,
    ) -> tuple[str, str]:
        if layer is MemoryLayer.FACT:
            row = connection.execute(
                "SELECT profile_id, current_version_id FROM memory_groups WHERE id = ?",
                (group_id,),
            ).fetchone()
        else:
            tables = _tables(layer)
            row = connection.execute(
                f"SELECT profile_id, current_version_id FROM {tables.groups} WHERE id = ?",
                (group_id,),
            ).fetchone()
        if row is None or row["current_version_id"] is None:
            raise StorageNotFoundError("memory target does not exist")
        return str(row["profile_id"]), str(row["current_version_id"])

    def _signal_delta(
        self,
        connection: sqlite3.Connection,
        layer: MemoryLayer,
        version_id: str,
        kind: EvidenceSignalKind,
    ) -> tuple[float, float]:
        if kind is EvidenceSignalKind.INDIRECT_SUPPORT:
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM memory_evidence_signals
                    WHERE target_layer = ? AND target_version_id = ?
                      AND signal_kind = 'indirect_support'
                    """,
                    (layer.value, version_id),
                ).fetchone()[0]
            )
            bonus = INDIRECT_SUPPORT_COMBO_BONUS if count >= 2 else 0.0
            return INDIRECT_SUPPORT_DELTA + bonus, 0.0
        if kind is EvidenceSignalKind.INDIRECT_REFUTE:
            return 0.0, INDIRECT_REFUTE_DELTA
        if kind is EvidenceSignalKind.DIRECT_CONFIRM:
            return DIRECT_CONFIRM_DELTA, 0.0
        if kind is EvidenceSignalKind.DIRECT_REBUT:
            return 0.0, DIRECT_REBUT_DELTA
        raise StorageValidationError("initial evidence is internal-only")

    def _insert_signal(
        self,
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        layer: MemoryLayer,
        group_id: str,
        version_id: str,
        kind: EvidenceSignalKind,
        reinforcement_delta: float,
        disputation_delta: float,
        correlation_key: str,
        created_at: str,
        source_message_id: str | None = None,
        source_fact_version_id: str | None = None,
    ) -> bool:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO memory_evidence_signals(
                id, profile_id, target_layer, target_group_id, target_version_id,
                source_message_id, source_fact_version_id, signal_kind,
                reinforcement_delta, disputation_delta, correlation_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._id_factory(),
                profile_id,
                layer.value,
                group_id,
                version_id,
                source_message_id,
                source_fact_version_id,
                kind.value,
                float(reinforcement_delta),
                float(disputation_delta),
                correlation_key,
                created_at,
            ),
        )
        return cursor.rowcount == 1

    def _evidence_snapshot_with_connection(
        self,
        connection: sqlite3.Connection,
        layer: MemoryLayer,
        version_id: str,
        now: datetime,
    ) -> EvidenceSnapshot:
        rows = connection.execute(
            """
            SELECT signal_kind, reinforcement_delta, disputation_delta, created_at
            FROM memory_evidence_signals
            WHERE target_layer = ? AND target_version_id = ?
            ORDER BY created_at, id
            """,
            (layer.value, version_id),
        ).fetchall()
        reinforcement = 0.0
        disputation = 0.0
        last_rein_at: datetime | None = None
        last_disp_at: datetime | None = None
        support_count = 0
        for row in rows:
            occurred = _required_datetime(row["created_at"])
            rein_delta = float(row["reinforcement_delta"])
            disp_delta = float(row["disputation_delta"])
            if rein_delta:
                reinforcement = _decay(
                    reinforcement,
                    last_rein_at,
                    occurred,
                    EVIDENCE_REINFORCEMENT_HALF_LIFE_DAYS,
                )
                reinforcement += rein_delta
                last_rein_at = occurred
            if disp_delta:
                disputation = _decay(
                    disputation,
                    last_disp_at,
                    occurred,
                    EVIDENCE_DISPUTATION_HALF_LIFE_DAYS,
                )
                disputation += disp_delta
                last_disp_at = occurred
            if row["signal_kind"] == EvidenceSignalKind.INDIRECT_SUPPORT.value:
                support_count += 1
        reinforcement = _decay(
            reinforcement,
            last_rein_at,
            now,
            EVIDENCE_REINFORCEMENT_HALF_LIFE_DAYS,
        )
        disputation = _decay(
            disputation,
            last_disp_at,
            now,
            EVIDENCE_DISPUTATION_HALF_LIFE_DAYS,
        )
        return EvidenceSnapshot(
            reinforcement=reinforcement,
            disputation=disputation,
            score=reinforcement - disputation,
            indirect_support_count=support_count,
            evaluated_at=now,
        )

    def _apply_score_state(
        self,
        connection: sqlite3.Connection,
        *,
        layer: MemoryLayer,
        group_id: str,
        snapshot: EvidenceSnapshot,
        now: str,
    ) -> None:
        if layer is MemoryLayer.FACT:
            return
        tables = _tables(layer)
        row = connection.execute(
            f"SELECT status, pinned, archive_candidate_since FROM {tables.groups} WHERE id = ?",
            (group_id,),
        ).fetchone()
        assert row is not None
        status = str(row["status"])
        if (
            layer is MemoryLayer.REFLECTION
            and status == "tentative"
            and snapshot.score >= EVIDENCE_CONFIRMED_THRESHOLD
        ):
            connection.execute(
                """
                UPDATE memory_reflections
                SET status = 'confirmed', updated_at = ? WHERE id = ?
                """,
                (now, group_id),
            )
        if bool(row["pinned"]):
            connection.execute(
                f"UPDATE {tables.groups} SET archive_candidate_since = NULL WHERE id = ?",
                (group_id,),
            )
        elif snapshot.score <= EVIDENCE_ARCHIVE_THRESHOLD:
            connection.execute(
                f"UPDATE {tables.groups} SET archive_candidate_since = COALESCE("
                "archive_candidate_since, ?) WHERE id = ?",
                (now, group_id),
            )
        elif row["archive_candidate_since"] is not None:
            connection.execute(
                f"UPDATE {tables.groups} SET archive_candidate_since = NULL WHERE id = ?",
                (group_id,),
            )

    def _insert_audit(
        self,
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        layer: MemoryLayer,
        group_id: str,
        event_type: str,
        reason_code: str,
        occurred_at: str,
        version_id: str | None = None,
        source_message_id: str | None = None,
        reinforcement_delta: float = 0.0,
        disputation_delta: float = 0.0,
        metadata: dict[str, object] | None = None,
    ) -> None:
        encoded = json.dumps(
            metadata or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > MAX_AUDIT_METADATA_BYTES:
            raise StorageValidationError("audit metadata is too large")
        connection.execute(
            """
            INSERT INTO memory_audit_events(
                id, profile_id, owner_layer, owner_group_id, version_id,
                source_message_id, event_type, reason_code, reinforcement_delta,
                disputation_delta, metadata_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._id_factory(),
                profile_id,
                layer.value,
                group_id,
                version_id,
                source_message_id,
                _identifier(event_type, "event_type"),
                _identifier(reason_code, "reason_code"),
                float(reinforcement_delta),
                float(disputation_delta),
                encoded,
                occurred_at,
            ),
        )

    @staticmethod
    def _validate_user_source(
        connection: sqlite3.Connection,
        profile_id: str,
        message_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT m.role, m.participates_in_memory, c.profile_id
            FROM messages AS m
            JOIN conversations AS c ON c.id = m.conversation_id
            WHERE m.id = ?
            """,
            (message_id,),
        ).fetchone()
        if (
            row is None
            or row["role"] != "user"
            or not bool(row["participates_in_memory"])
            or row["profile_id"] != profile_id
        ):
            raise StorageValidationError("evidence requires a memory-enabled user message")

    @staticmethod
    def _validate_evidence_fact(
        connection: sqlite3.Connection,
        profile_id: str,
        fact_version_id: str,
        *,
        source_message_id: str | None,
    ) -> None:
        params: list[object] = [fact_version_id, profile_id]
        message_clause = ""
        if source_message_id is not None:
            message_clause = " AND s.source_message_id = ?"
            params.append(source_message_id)
        row = connection.execute(
            """
            SELECT 1
            FROM memory_versions AS v
            JOIN memory_groups AS g ON g.id = v.memory_id AND g.current_version_id = v.id
            JOIN memory_sources AS s ON s.version_id = v.id
            JOIN messages AS m ON m.id = s.live_message_id
            WHERE v.id = ? AND g.profile_id = ? AND g.status = 'active'
              AND v.deep_memory_eligible = 1 AND m.role = 'user'
              AND m.participates_in_memory = 1
              AND NOT EXISTS (
                  SELECT 1 FROM memory_conflicts AS c
                  WHERE c.target_layer = 'fact' AND c.target_group_id = g.id
                    AND c.status = 'open'
              )
            """
            + message_clause
            + " LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            raise StorageValidationError(
                "evidence fact must be current and trace to its cited user message"
            )

    def _suppress_descendants(
        self,
        connection: sqlite3.Connection,
        layer: MemoryLayer,
        group_id: str,
        now: str,
    ) -> None:
        reflection_ids: tuple[str, ...] = ()
        if layer is MemoryLayer.FACT:
            reflection_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT rv.reflection_id
                    FROM memory_reflection_sources AS rs
                    JOIN memory_versions AS fv ON fv.id = rs.fact_version_id
                    JOIN memory_reflection_versions AS rv ON rv.id = rs.version_id
                    WHERE fv.memory_id = ?
                    """,
                    (group_id,),
                ).fetchall()
            )
        elif layer is MemoryLayer.REFLECTION:
            reflection_ids = (group_id,)
        if reflection_ids:
            placeholders = ",".join("?" for _ in reflection_ids)
            connection.execute(
                f"""
                UPDATE memory_reflections SET status = 'disputed', updated_at = ?
                WHERE id IN ({placeholders})
                  AND status NOT IN ('archived', 'denied', 'promoted', 'merged')
                """,
                (now, *reflection_ids),
            )
            connection.execute(
                f"""
                UPDATE memory_persona_impressions SET status = 'disputed', updated_at = ?
                WHERE id IN (
                    SELECT DISTINCT pv.impression_id
                    FROM memory_persona_impression_sources AS ps
                    JOIN memory_reflection_versions AS rv
                      ON rv.id = ps.reflection_version_id
                    JOIN memory_persona_impression_versions AS pv ON pv.id = ps.version_id
                    WHERE rv.reflection_id IN ({placeholders})
                ) AND status NOT IN ('archived', 'denied')
                """,
                (now, *reflection_ids),
            )

    def _restore_fact_descendants(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
        now: str,
    ) -> None:
        reflection_ids = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT rv.reflection_id
                FROM memory_reflection_sources AS rs
                JOIN memory_versions AS fv ON fv.id = rs.fact_version_id
                JOIN memory_reflection_versions AS rv ON rv.id = rs.version_id
                WHERE fv.memory_id = ?
                """,
                (memory_id,),
            ).fetchall()
        )
        for reflection_id in reflection_ids:
            record = self._get_with_connection(
                connection,
                MemoryLayer.REFLECTION,
                reflection_id,
            )
            if record.status is not DerivedMemoryStatus.DISPUTED:
                continue
            invalid_fact_source = connection.execute(
                """
                SELECT 1
                FROM memory_reflection_sources AS rs
                JOIN memory_versions AS fv ON fv.id = rs.fact_version_id
                JOIN memory_groups AS fg ON fg.id = fv.memory_id
                JOIN memory_reflection_versions AS rv ON rv.id = rs.version_id
                WHERE rv.reflection_id = ?
                  AND (
                    fg.status != 'active'
                    OR fg.current_version_id != fv.id
                    OR EXISTS (
                        SELECT 1 FROM memory_conflicts AS c
                        WHERE c.target_layer = 'fact'
                          AND c.target_group_id = fg.id AND c.status = 'open'
                    )
                  )
                LIMIT 1
                """,
                (reflection_id,),
            ).fetchone()
            if invalid_fact_source is not None:
                continue
            target = (
                DerivedMemoryStatus.CONFIRMED
                if record.evidence_score >= EVIDENCE_CONFIRMED_THRESHOLD
                else DerivedMemoryStatus.TENTATIVE
            )
            connection.execute(
                "UPDATE memory_reflections SET status = ?, updated_at = ? WHERE id = ?",
                (target.value, now, reflection_id),
            )

        if not reflection_ids:
            return
        placeholders = ",".join("?" for _ in reflection_ids)
        persona_ids = tuple(
            str(row[0])
            for row in connection.execute(
                f"""
                SELECT DISTINCT pv.impression_id
                FROM memory_persona_impression_sources AS ps
                JOIN memory_reflection_versions AS rv ON rv.id = ps.reflection_version_id
                JOIN memory_persona_impression_versions AS pv ON pv.id = ps.version_id
                WHERE rv.reflection_id IN ({placeholders})
                """,
                reflection_ids,
            ).fetchall()
        )
        for impression_id in persona_ids:
            impression = self._get_with_connection(
                connection,
                MemoryLayer.PERSONA,
                impression_id,
            )
            if impression.status is not DerivedMemoryStatus.DISPUTED:
                continue
            invalid_reflection_source = connection.execute(
                """
                SELECT 1
                FROM memory_persona_impression_sources AS ps
                JOIN memory_reflection_versions AS rv ON rv.id = ps.reflection_version_id
                JOIN memory_reflections AS rg ON rg.id = rv.reflection_id
                JOIN memory_persona_impression_versions AS pv ON pv.id = ps.version_id
                WHERE pv.impression_id = ?
                  AND (
                    rg.current_version_id != rv.id
                    OR rg.status NOT IN ('confirmed', 'promoted')
                     OR EXISTS (
                         SELECT 1 FROM memory_conflicts AS c
                         WHERE c.target_layer = 'reflection'
                           AND c.target_group_id = rg.id AND c.status = 'open'
                     )
                     OR EXISTS (
                         SELECT 1
                         FROM memory_reflection_sources AS rs
                         JOIN memory_versions AS fv ON fv.id = rs.fact_version_id
                         JOIN memory_groups AS fg ON fg.id = fv.memory_id
                         WHERE rs.version_id = rv.id
                           AND (
                               fg.status != 'active'
                               OR fg.current_version_id != fv.id
                               OR EXISTS (
                                   SELECT 1 FROM memory_conflicts AS fc
                                   WHERE fc.target_layer = 'fact'
                                     AND fc.target_group_id = fg.id AND fc.status = 'open'
                               )
                           )
                     )
                   )
                LIMIT 1
                """,
                (impression_id,),
            ).fetchone()
            if invalid_reflection_source is None:
                connection.execute(
                    "UPDATE memory_persona_impressions "
                    "SET status = 'active', updated_at = ? WHERE id = ?",
                    (now, impression_id),
                )

    @staticmethod
    def _delete_owner_metadata(
        connection: sqlite3.Connection,
        layer: MemoryLayer,
        group_id: str,
    ) -> None:
        connection.execute(
            "DELETE FROM memory_evidence_signals WHERE target_layer = ? AND target_group_id = ?",
            (layer.value, group_id),
        )
        connection.execute(
            "DELETE FROM memory_conflicts WHERE target_layer = ? AND target_group_id = ?",
            (layer.value, group_id),
        )
        connection.execute(
            "DELETE FROM memory_audit_events WHERE owner_layer = ? AND owner_group_id = ?",
            (layer.value, group_id),
        )


def _recall_table(layer: MemoryLayer) -> str:
    if layer is MemoryLayer.REFLECTION:
        return "memory_reflection_recall_events"
    if layer is MemoryLayer.PERSONA:
        return "memory_persona_impression_recall_events"
    raise StorageValidationError("derived recall requires reflection or persona")


def initial_reinforcement_from_importance(importance: float) -> float:
    """Adapt N.E.K.O.'s 7/8/9/10 importance ladder to Amadeus' 0..1 scale."""

    value = _unit_interval(importance, "importance")
    if value >= 1.0:
        return 0.8
    if value >= 0.9:
        return 0.6
    if value >= 0.8:
        return 0.4
    if value >= 0.7:
        return 0.2
    return 0.0


def _decay(
    value: float,
    last_signal_at: datetime | None,
    now: datetime,
    half_life_days: float,
) -> float:
    if not value or last_signal_at is None:
        return value
    age_days = max(0.0, (now.astimezone(UTC) - last_signal_at.astimezone(UTC)).total_seconds())
    age_days /= 86_400.0
    return value * math.pow(0.5, age_days / half_life_days)


def _tables(layer: MemoryLayer) -> _LayerTables:
    if layer is MemoryLayer.REFLECTION:
        return _REFLECTION
    if layer is MemoryLayer.PERSONA:
        return _PERSONA
    raise StorageValidationError("layer has no derived-memory tables")


def _derived_layer(value: MemoryLayer | str) -> MemoryLayer:
    try:
        layer = value if isinstance(value, MemoryLayer) else MemoryLayer(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported memory layer") from exc
    if layer not in {MemoryLayer.REFLECTION, MemoryLayer.PERSONA}:
        raise StorageValidationError("operation requires a derived memory layer")
    return layer


def _evidence_layer(value: MemoryLayer | str) -> MemoryLayer:
    try:
        layer = value if isinstance(value, MemoryLayer) else MemoryLayer(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported evidence layer") from exc
    if layer not in {MemoryLayer.FACT, MemoryLayer.REFLECTION, MemoryLayer.PERSONA}:
        raise StorageValidationError("unsupported evidence layer")
    return layer


def _subject_scope(value: MemorySubjectScope | str) -> MemorySubjectScope:
    try:
        return value if isinstance(value, MemorySubjectScope) else MemorySubjectScope(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported memory subject scope") from exc


def _derived_status(value: DerivedMemoryStatus | str) -> DerivedMemoryStatus:
    try:
        return value if isinstance(value, DerivedMemoryStatus) else DerivedMemoryStatus(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported derived memory status") from exc


def _signal_kind(value: EvidenceSignalKind | str) -> EvidenceSignalKind:
    try:
        return value if isinstance(value, EvidenceSignalKind) else EvidenceSignalKind(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported evidence signal") from exc


def _conflict_resolution(value: ConflictResolution | str) -> ConflictResolution:
    try:
        return value if isinstance(value, ConflictResolution) else ConflictResolution(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported conflict resolution") from exc


def _content_fields(content: str) -> tuple[str, str, str, str]:
    if not isinstance(content, str) or not content.strip():
        raise StorageValidationError("memory content must not be blank")
    normalized = normalize_memory_content(content)
    search_text = build_search_text(content)
    if not normalized or not search_text:
        raise StorageValidationError("memory content has no searchable text")
    return content.strip(), normalized, exact_memory_hash(content), search_text


def _unit_interval(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageValidationError(f"{field} must be numeric")
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise StorageValidationError(f"{field} must be between 0 and 1")
    return numeric


def _identifier(value: str, field: str) -> str:
    result = str(value).strip()
    if not result or len(result) > 512 or "\x00" in result:
        raise StorageValidationError(f"{field} must be a safe non-empty identifier")
    return result


def _unique_identifiers(values: Iterable[str], field: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_identifier(str(value), field) for value in values))


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
        raise StorageValidationError("limit must be between 1 and 10000")


def _required_datetime(value: str) -> datetime:
    decoded = decode_utc(value)
    assert decoded is not None
    return decoded


def _version_from_row(row: sqlite3.Row, group_id: str) -> DerivedMemoryVersion:
    return DerivedMemoryVersion(
        version_id=str(row["id"]),
        group_id=group_id,
        version_number=int(row["version_number"]),
        content=str(row["content"]),
        normalized_content=str(row["normalized_content"]),
        content_hash=str(row["content_hash"]),
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        origin=str(row["origin"]),
        operation=str(row["operation"]),
        supersedes_version_id=row["supersedes_version_id"],
        created_at=_required_datetime(row["created_at"]),
    )


def _conflict_from_row(row: sqlite3.Row) -> MemoryConflict:
    resolution = row["resolution"]
    return MemoryConflict(
        conflict_id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        target_layer=MemoryLayer(row["target_layer"]),
        target_group_id=str(row["target_group_id"]),
        incumbent_version_id=str(row["incumbent_version_id"]),
        challenger_version_id=str(row["challenger_version_id"]),
        status=str(row["status"]),
        resolution=None if resolution is None else ConflictResolution(resolution),
        source_message_id=row["source_message_id"],
        created_at=_required_datetime(row["created_at"]),
        resolved_at=decode_utc(row["resolved_at"]),
    )


def _audit_from_row(row: sqlite3.Row) -> MemoryAuditEvent:
    try:
        metadata = json.loads(str(row["metadata_json"]))
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return MemoryAuditEvent(
        event_id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        owner_layer=MemoryLayer(row["owner_layer"]),
        owner_group_id=str(row["owner_group_id"]),
        version_id=row["version_id"],
        source_message_id=row["source_message_id"],
        event_type=str(row["event_type"]),
        reason_code=str(row["reason_code"]),
        reinforcement_delta=float(row["reinforcement_delta"]),
        disputation_delta=float(row["disputation_delta"]),
        metadata=metadata,
        occurred_at=_required_datetime(row["occurred_at"]),
    )
