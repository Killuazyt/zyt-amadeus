"""Auditable, version-bound companion follow-up authorizations."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from datetime import timedelta
from uuid import uuid4

from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.storage_models import (
    DEFAULT_PROFILE_ID,
    CompanionCue,
    CompanionCueAuditEvent,
    CompanionCueKind,
    CompanionCueReason,
    CompanionCueSource,
    CompanionCueSourceKind,
    CompanionCueStatus,
    ProactivePresentation,
    StorageConflictError,
    StorageNotFoundError,
    StorageValidationError,
    decode_utc,
    encode_utc,
    utc_now,
)

CONTEXTUAL_PREVIEW_TEXT = "有件你之前提过的事，我还记着。想继续聊聊吗？"
DEFAULT_EXPIRY_DAYS = 30
MAX_CUE_TOPIC_CHARS = 120
MAX_CUE_TEXT_CHARS = 240


class CompanionCueUnavailableError(StorageConflictError):
    """Raised when a selected cue is no longer authorized at commit time."""


class CompanionCueStore:
    """Synchronous cue repository for the serialized data thread."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        clock=utc_now,
        id_factory=None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid4().hex)

    def propose_conversation_followup(
        self,
        *,
        conversation_id: str,
        topic: str,
        frozen_text: str,
        reason: CompanionCueReason,
        confidence: float,
        source_message_ids: Iterable[str],
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> CompanionCue:
        """Persist an automatic suggestion; it cannot be surfaced before confirmation."""

        normalized_reason = CompanionCueReason(reason)
        if normalized_reason is CompanionCueReason.MEMORY_AUTHORIZED:
            raise StorageValidationError("conversation cue reason is invalid")
        numeric_confidence = _confidence(confidence)
        if numeric_confidence < 0.80:
            raise StorageValidationError("conversation cue confidence is below threshold")
        source_ids = tuple(
            dict.fromkeys(_identifier(value, "source_message_id") for value in source_message_ids)
        )
        if not 1 <= len(source_ids) <= 3:
            raise StorageValidationError("conversation cue requires one to three sources")
        topic = _text(topic, "topic", MAX_CUE_TOPIC_CHARS)
        frozen_text = _text(frozen_text, "frozen_text", MAX_CUE_TEXT_CHARS)
        with self._database.transaction() as connection:
            placeholders = ",".join("?" for _ in source_ids)
            rows = connection.execute(
                f"""
                SELECT id, conversation_id
                FROM messages
                WHERE id IN ({placeholders})
                  AND role = 'user'
                  AND status = 'completed'
                  AND participates_in_memory = 1
                """,
                source_ids,
            ).fetchall()
            if len(rows) != len(source_ids) or any(
                str(row["conversation_id"]) != conversation_id for row in rows
            ):
                raise StorageValidationError("conversation cue sources are not live user messages")
            conversation = connection.execute(
                "SELECT profile_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None or str(conversation["profile_id"]) != profile_id:
                raise StorageNotFoundError("conversation does not exist")
            dedupe_key = _dedupe_key(
                CompanionCueKind.CONVERSATION_FOLLOWUP,
                normalized_reason.value,
                *sorted(source_ids),
            )
            existing = self._live_by_dedupe(connection, profile_id, dedupe_key)
            if existing is not None:
                return self._cue_from_row(connection, existing)
            cue_id = self._insert_cue(
                connection,
                profile_id=profile_id,
                conversation_id=conversation_id,
                kind=CompanionCueKind.CONVERSATION_FOLLOWUP,
                topic=topic,
                frozen_text=frozen_text,
                reason=normalized_reason,
                confidence=numeric_confidence,
                dedupe_key=dedupe_key,
            )
            now = encode_utc(self._clock())
            for source_id in source_ids:
                connection.execute(
                    """
                    INSERT INTO companion_cue_sources(
                        id, cue_id, source_kind, source_message_id, created_at
                    ) VALUES (?, ?, 'user_message', ?, ?)
                    """,
                    (self._id_factory(), cue_id, source_id, now),
                )
            return self._cue_from_row(
                connection,
                connection.execute(
                    "SELECT * FROM companion_cues WHERE id = ?", (cue_id,)
                ).fetchone(),
            )

    def propose_memory_followup(
        self,
        source_kind: CompanionCueSourceKind,
        version_id: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> CompanionCue:
        """Create an editable local draft for one exact live memory version."""

        source_kind = CompanionCueSourceKind(source_kind)
        if source_kind is CompanionCueSourceKind.USER_MESSAGE:
            raise StorageValidationError("memory authorization requires a memory version")
        version_id = _identifier(version_id, "version_id")
        with self._database.transaction() as connection:
            source = self._validate_memory_source(connection, source_kind, version_id, profile_id)
            dedupe_key = _dedupe_key(
                CompanionCueKind.MEMORY_FOLLOWUP,
                source_kind.value,
                version_id,
            )
            existing = self._live_by_dedupe(connection, profile_id, dedupe_key)
            if existing is not None:
                return self._cue_from_row(connection, existing)
            topic = _text(str(source["topic_key"]), "topic", MAX_CUE_TOPIC_CHARS)
            frozen_text = _memory_draft(str(source["content"]))
            cue_id = self._insert_cue(
                connection,
                profile_id=profile_id,
                conversation_id=None,
                kind=CompanionCueKind.MEMORY_FOLLOWUP,
                topic=topic,
                frozen_text=frozen_text,
                reason=CompanionCueReason.MEMORY_AUTHORIZED,
                confidence=float(source["confidence"]),
                dedupe_key=dedupe_key,
            )
            column = _source_column(source_kind)
            connection.execute(
                f"""
                INSERT INTO companion_cue_sources(
                    id, cue_id, source_kind, {column}, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._id_factory(),
                    cue_id,
                    source_kind.value,
                    version_id,
                    encode_utc(self._clock()),
                ),
            )
            return self.get(cue_id, connection=connection)

    def confirm(
        self,
        cue_id: str,
        *,
        topic: str,
        frozen_text: str,
        keep_until_resolved: bool = False,
    ) -> CompanionCue:
        cue_id = _identifier(cue_id, "cue_id")
        topic = _text(topic, "topic", MAX_CUE_TOPIC_CHARS)
        frozen_text = _text(frozen_text, "frozen_text", MAX_CUE_TEXT_CHARS)
        keep = bool(keep_until_resolved)
        now_value = self._clock()
        now = encode_utc(now_value)
        expires_at = None if keep else encode_utc(now_value + timedelta(days=DEFAULT_EXPIRY_DAYS))
        with self._database.transaction() as connection:
            cue = self.get(cue_id, connection=connection)
            if cue.status is not CompanionCueStatus.PROPOSED:
                raise StorageConflictError("only proposed companion cues can be confirmed")
            self._validate_sources(connection, cue)
            updated = connection.execute(
                """
                UPDATE companion_cues
                SET topic = ?, frozen_text = ?, status = 'active',
                    keep_until_resolved = ?, confirmed_at = ?, expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'proposed'
                """,
                (topic, frozen_text, int(keep), now, expires_at, now, cue_id),
            )
            if updated.rowcount != 1:
                raise StorageConflictError("companion cue changed before confirmation")
            return self.get(cue_id, connection=connection)

    def reject(self, cue_id: str) -> CompanionCue:
        return self._transition(
            cue_id,
            allowed=(CompanionCueStatus.PROPOSED, CompanionCueStatus.ACTIVE),
            target=CompanionCueStatus.REJECTED,
        )

    def resolve(self, cue_id: str) -> CompanionCue:
        return self._transition(
            cue_id,
            allowed=(CompanionCueStatus.ACTIVE, CompanionCueStatus.SURFACED),
            target=CompanionCueStatus.RESOLVED,
            resolved=True,
        )

    def set_keep_until_resolved(self, cue_id: str, enabled: bool) -> CompanionCue:
        cue_id = _identifier(cue_id, "cue_id")
        now_value = self._clock()
        with self._database.transaction() as connection:
            cue = self.get(cue_id, connection=connection)
            if cue.status not in {CompanionCueStatus.ACTIVE, CompanionCueStatus.SURFACED}:
                raise StorageConflictError("only confirmed companion cues can change retention")
            expires = (
                None if enabled else encode_utc(now_value + timedelta(days=DEFAULT_EXPIRY_DAYS))
            )
            connection.execute(
                """
                UPDATE companion_cues
                SET keep_until_resolved = ?, expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(bool(enabled)), expires, encode_utc(now_value), cue_id),
            )
            return self.get(cue_id, connection=connection)

    def revoke_memory_authorization(
        self,
        source_kind: CompanionCueSourceKind,
        version_id: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> int:
        source_kind = CompanionCueSourceKind(source_kind)
        column = _source_column(source_kind)
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            result = connection.execute(
                f"""
                UPDATE companion_cues
                SET status = 'rejected', updated_at = ?
                WHERE profile_id = ? AND status IN ('proposed', 'active', 'surfaced')
                  AND id IN (
                      SELECT cue_id FROM companion_cue_sources
                      WHERE source_kind = ? AND {column} = ?
                  )
                """,
                (now, profile_id, source_kind.value, version_id),
            )
            return int(result.rowcount)

    def delete(self, cue_id: str) -> None:
        cue_id = _identifier(cue_id, "cue_id")
        with self._database.transaction() as connection:
            result = connection.execute("DELETE FROM companion_cues WHERE id = ?", (cue_id,))
            if result.rowcount != 1:
                raise StorageNotFoundError("companion cue does not exist")

    def get(
        self,
        cue_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> CompanionCue:
        cue_id = _identifier(cue_id, "cue_id")
        active_connection = connection or self._database.connection
        row = active_connection.execute(
            "SELECT * FROM companion_cues WHERE id = ?",
            (cue_id,),
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("companion cue does not exist")
        return self._cue_from_row(active_connection, row)

    def list(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        statuses: Iterable[CompanionCueStatus] | None = None,
    ) -> tuple[CompanionCue, ...]:
        parameters: list[object] = [profile_id]
        where = "profile_id = ?"
        if statuses is not None:
            normalized = tuple(dict.fromkeys(CompanionCueStatus(value).value for value in statuses))
            if not normalized:
                return ()
            where += f" AND status IN ({','.join('?' for _ in normalized)})"
            parameters.extend(normalized)
        rows = self._database.connection.execute(
            f"""
            SELECT * FROM companion_cues
            WHERE {where}
            ORDER BY
                CASE status
                    WHEN 'proposed' THEN 0 WHEN 'active' THEN 1 WHEN 'surfaced' THEN 2
                    WHEN 'resolved' THEN 3 WHEN 'expired' THEN 4 ELSE 5 END,
                created_at, id
            """,
            parameters,
        ).fetchall()
        return tuple(self._cue_from_row(self._database.connection, row) for row in rows)

    def list_audit_events(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> tuple[CompanionCueAuditEvent, ...]:
        rows = self._database.connection.execute(
            """
            SELECT * FROM companion_cue_audit_events
            WHERE profile_id = ? ORDER BY occurred_at, id
            """,
            (profile_id,),
        ).fetchall()
        return tuple(_audit_from_row(row) for row in rows)

    def expire_due(self, *, profile_id: str = DEFAULT_PROFILE_ID) -> int:
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE companion_cues
                SET status = 'expired', updated_at = ?
                WHERE profile_id = ?
                  AND status IN ('active', 'surfaced')
                  AND keep_until_resolved = 0
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now, profile_id, now),
            )
            return int(result.rowcount)

    def select_proactive_presentation(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        include_deep: bool = True,
    ) -> ProactivePresentation | None:
        self.expire_due(profile_id=profile_id)
        parameters: list[object] = [profile_id]
        deep_clause = (
            ""
            if include_deep
            else """AND (
                kind = 'conversation_followup'
                OR EXISTS (
                    SELECT 1 FROM companion_cue_sources source
                    WHERE source.cue_id = companion_cues.id
                      AND source.source_kind = 'fact_version'
                )
            )"""
        )
        rows = self._database.connection.execute(
            f"""
            SELECT * FROM companion_cues
            WHERE profile_id = ? AND status = 'active' {deep_clause}
            ORDER BY CASE kind WHEN 'conversation_followup' THEN 0 ELSE 1 END,
                     CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END,
                     expires_at, created_at, id
            """,
            parameters,
        ).fetchall()
        for row in rows:
            cue = self._cue_from_row(self._database.connection, row)
            try:
                self._validate_sources(self._database.connection, cue)
            except CompanionCueUnavailableError:
                self._expire_invalid(cue.cue_id)
                continue
            return ProactivePresentation(
                preview_text=CONTEXTUAL_PREVIEW_TEXT,
                expanded_text=cue.frozen_text,
                cue_id=cue.cue_id,
                source_label=(
                    "待续话题"
                    if cue.kind is CompanionCueKind.CONVERSATION_FOLLOWUP
                    else "已授权记忆"
                ),
            )
        return None

    def mark_surfaced(
        self,
        cue_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> CompanionCue:
        """Atomically consume one cue; a second attempt can never display it."""

        if connection is None:
            with self._database.transaction() as active_connection:
                return self.mark_surfaced(cue_id, connection=active_connection)
        cue = self.get(cue_id, connection=connection)
        if cue.status is not CompanionCueStatus.ACTIVE:
            raise CompanionCueUnavailableError("companion cue is no longer active")
        self._validate_sources(connection, cue)
        now = encode_utc(self._clock())
        result = connection.execute(
            """
            UPDATE companion_cues
            SET status = 'surfaced', surfaced_at = ?, updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (now, now, cue_id),
        )
        if result.rowcount != 1:
            raise CompanionCueUnavailableError("companion cue was consumed concurrently")
        return self.get(cue_id, connection=connection)

    def authorized_for_manual_open(
        self,
        cue_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> CompanionCue:
        if connection is None:
            self.expire_due()
        active_connection = connection or self._database.connection
        cue = self.get(cue_id, connection=active_connection)
        if cue.status not in {CompanionCueStatus.ACTIVE, CompanionCueStatus.SURFACED}:
            raise CompanionCueUnavailableError("companion cue cannot be opened")
        if (
            not cue.keep_until_resolved
            and cue.expires_at is not None
            and cue.expires_at <= self._clock()
        ):
            raise CompanionCueUnavailableError("companion cue has expired")
        self._validate_sources(active_connection, cue)
        return cue

    def authorized_for_click(
        self,
        cue_id: str,
        *,
        connection: sqlite3.Connection,
    ) -> CompanionCue:
        """Revalidate a surfaced cue inside the click-persistence transaction."""

        cue = self.get(cue_id, connection=connection)
        if cue.status is not CompanionCueStatus.SURFACED:
            raise CompanionCueUnavailableError("companion cue is not surfaced")
        self._validate_sources(connection, cue)
        return cue

    def _insert_cue(
        self,
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        conversation_id: str | None,
        kind: CompanionCueKind,
        topic: str,
        frozen_text: str,
        reason: CompanionCueReason,
        confidence: float,
        dedupe_key: str,
    ) -> str:
        cue_id = self._id_factory()
        now = encode_utc(self._clock())
        try:
            connection.execute(
                """
                INSERT INTO companion_cues(
                    id, profile_id, conversation_id, kind, topic, frozen_text,
                    status, reason, confidence, keep_until_resolved, dedupe_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?, 0, ?, ?, ?)
                """,
                (
                    cue_id,
                    profile_id,
                    conversation_id,
                    kind.value,
                    topic,
                    frozen_text,
                    reason.value,
                    confidence,
                    dedupe_key,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO companion_cue_audit_events(
                    id, profile_id, cue_id, cue_kind, event_type, reason_code,
                    previous_status, resulting_status, occurred_at
                ) VALUES (?, ?, ?, ?, 'proposed', 'model_or_local_draft', NULL, 'proposed', ?)
                """,
                (self._id_factory(), profile_id, cue_id, kind.value, now),
            )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("companion cue conflicts with existing state") from exc
        return cue_id

    def _transition(
        self,
        cue_id: str,
        *,
        allowed: tuple[CompanionCueStatus, ...],
        target: CompanionCueStatus,
        resolved: bool = False,
    ) -> CompanionCue:
        cue_id = _identifier(cue_id, "cue_id")
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            cue = self.get(cue_id, connection=connection)
            if cue.status not in set(allowed):
                raise StorageConflictError("companion cue transition is invalid")
            placeholders = ",".join("?" for _ in allowed)
            values: list[object] = [target.value, now]
            resolved_sql = ", resolved_at = ?" if resolved else ""
            if resolved:
                values.append(now)
            values.extend([cue_id, *(status.value for status in allowed)])
            result = connection.execute(
                f"""
                UPDATE companion_cues
                SET status = ?, updated_at = ?{resolved_sql}
                WHERE id = ? AND status IN ({placeholders})
                """,
                values,
            )
            if result.rowcount != 1:
                raise StorageConflictError("companion cue changed concurrently")
            return self.get(cue_id, connection=connection)

    def _validate_sources(self, connection: sqlite3.Connection, cue: CompanionCue) -> None:
        if not cue.sources:
            raise CompanionCueUnavailableError("companion cue has no source")
        if cue.kind is CompanionCueKind.CONVERSATION_FOLLOWUP:
            if any(
                source.source_kind is not CompanionCueSourceKind.USER_MESSAGE
                for source in cue.sources
            ):
                raise CompanionCueUnavailableError("conversation cue source is invalid")
            for source in cue.sources:
                row = connection.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE id = ? AND role = 'user' AND status = 'completed'
                      AND participates_in_memory = 1
                    """,
                    (source.source_target_id,),
                ).fetchone()
                if row is None:
                    raise CompanionCueUnavailableError("conversation cue source is unavailable")
            return
        if len(cue.sources) != 1:
            raise CompanionCueUnavailableError("memory cue source is invalid")
        source = cue.sources[0]
        if source.source_kind is CompanionCueSourceKind.USER_MESSAGE:
            raise CompanionCueUnavailableError("memory cue source is invalid")
        try:
            self._validate_memory_source(
                connection,
                source.source_kind,
                source.source_target_id,
                cue.profile_id,
            )
        except (StorageConflictError, StorageNotFoundError, StorageValidationError) as exc:
            raise CompanionCueUnavailableError("memory cue source is unavailable") from exc

    def _validate_memory_source(
        self,
        connection: sqlite3.Connection,
        source_kind: CompanionCueSourceKind,
        version_id: str,
        profile_id: str,
    ) -> sqlite3.Row:
        spec = {
            CompanionCueSourceKind.FACT_VERSION: (
                "memory_versions",
                "memory_groups",
                "memory_id",
                "active",
                "memory_sources",
            ),
            CompanionCueSourceKind.REFLECTION_VERSION: (
                "memory_reflection_versions",
                "memory_reflections",
                "reflection_id",
                ("confirmed", "promoted"),
                "memory_reflection_sources",
            ),
            CompanionCueSourceKind.PERSONA_VERSION: (
                "memory_persona_impression_versions",
                "memory_persona_impressions",
                "impression_id",
                "active",
                "memory_persona_impression_sources",
            ),
        }.get(source_kind)
        if spec is None:
            raise StorageValidationError("memory cue source kind is invalid")
        versions, groups, group_fk, allowed_statuses, sources = spec
        allowed = (
            (allowed_statuses,) if isinstance(allowed_statuses, str) else tuple(allowed_statuses)
        )
        placeholders = ",".join("?" for _ in allowed)
        row = connection.execute(
            f"""
            SELECT v.content, v.confidence, g.topic_key, g.status, g.current_version_id,
                   g.profile_id, g.id AS group_id
            FROM {versions} v
            JOIN {groups} g ON g.id = v.{group_fk}
            WHERE v.id = ? AND g.profile_id = ? AND g.current_version_id = v.id
              AND g.status IN ({placeholders})
              AND (
                  v.origin = 'manual'
                  OR EXISTS (
                      SELECT 1 FROM {sources} s
                      JOIN messages m ON m.id = s.live_message_id
                      WHERE s.version_id = v.id AND m.role = 'user'
                        AND m.status = 'completed' AND m.participates_in_memory = 1
                  )
              )
            """,
            (version_id, profile_id, *allowed),
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("memory version is not current and user-supported")
        conflict = connection.execute(
            """
            SELECT 1 FROM memory_conflicts
            WHERE profile_id = ? AND target_layer = ? AND target_group_id = ?
              AND status = 'open'
            """,
            (
                profile_id,
                {
                    CompanionCueSourceKind.FACT_VERSION: "fact",
                    CompanionCueSourceKind.REFLECTION_VERSION: "reflection",
                    CompanionCueSourceKind.PERSONA_VERSION: "persona",
                }[source_kind],
                str(row["group_id"]),
            ),
        ).fetchone()
        if conflict is not None:
            raise StorageConflictError("memory version has an open conflict")
        return row

    def _expire_invalid(self, cue_id: str) -> None:
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE companion_cues SET status = 'expired', updated_at = ?,
                    expires_at = COALESCE(expires_at, ?)
                WHERE id = ? AND status IN ('proposed', 'active', 'surfaced')
                """,
                (now, now, cue_id),
            )

    @staticmethod
    def _live_by_dedupe(
        connection: sqlite3.Connection,
        profile_id: str,
        dedupe_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM companion_cues
            WHERE profile_id = ? AND dedupe_key = ?
              AND status IN ('proposed', 'active', 'surfaced')
            """,
            (profile_id, dedupe_key),
        ).fetchone()

    @staticmethod
    def _cue_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> CompanionCue:
        sources = tuple(
            _source_from_row(source)
            for source in connection.execute(
                "SELECT * FROM companion_cue_sources WHERE cue_id = ? ORDER BY created_at, id",
                (str(row["id"]),),
            ).fetchall()
        )
        return CompanionCue(
            cue_id=str(row["id"]),
            profile_id=str(row["profile_id"]),
            conversation_id=(
                None if row["conversation_id"] is None else str(row["conversation_id"])
            ),
            kind=CompanionCueKind(str(row["kind"])),
            topic=str(row["topic"]),
            frozen_text=str(row["frozen_text"]),
            status=CompanionCueStatus(str(row["status"])),
            reason=CompanionCueReason(str(row["reason"])),
            confidence=float(row["confidence"]),
            keep_until_resolved=bool(row["keep_until_resolved"]),
            dedupe_key=str(row["dedupe_key"]),
            created_at=decode_utc(str(row["created_at"])),
            updated_at=decode_utc(str(row["updated_at"])),
            confirmed_at=decode_utc(row["confirmed_at"]),
            expires_at=decode_utc(row["expires_at"]),
            surfaced_at=decode_utc(row["surfaced_at"]),
            resolved_at=decode_utc(row["resolved_at"]),
            sources=sources,
        )


def _source_from_row(row: sqlite3.Row) -> CompanionCueSource:
    source_kind = CompanionCueSourceKind(str(row["source_kind"]))
    target = row[_source_column(source_kind)]
    return CompanionCueSource(
        source_id=str(row["id"]),
        cue_id=str(row["cue_id"]),
        source_kind=source_kind,
        source_target_id=str(target),
        created_at=decode_utc(str(row["created_at"])),
    )


def _audit_from_row(row: sqlite3.Row) -> CompanionCueAuditEvent:
    previous = row["previous_status"]
    resulting = row["resulting_status"]
    return CompanionCueAuditEvent(
        event_id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        cue_id=str(row["cue_id"]),
        cue_kind=CompanionCueKind(str(row["cue_kind"])),
        event_type=str(row["event_type"]),
        reason_code=str(row["reason_code"]),
        previous_status=None if previous is None else CompanionCueStatus(str(previous)),
        resulting_status=None if resulting is None else CompanionCueStatus(str(resulting)),
        occurred_at=decode_utc(str(row["occurred_at"])),
    )


def _source_column(source_kind: CompanionCueSourceKind) -> str:
    try:
        return {
            CompanionCueSourceKind.USER_MESSAGE: "source_message_id",
            CompanionCueSourceKind.FACT_VERSION: "fact_version_id",
            CompanionCueSourceKind.REFLECTION_VERSION: "reflection_version_id",
            CompanionCueSourceKind.PERSONA_VERSION: "persona_version_id",
        }[source_kind]
    except KeyError as exc:
        raise StorageValidationError("companion cue source kind is invalid") from exc


def _memory_draft(content: str) -> str:
    normalized = " ".join(content.split())
    prefix = "你之前明确允许我记住这件事："
    suffix = "。如果你愿意，我们可以继续聊聊。"
    available = MAX_CUE_TEXT_CHARS - len(prefix) - len(suffix)
    body = normalized[:available].rstrip("。；; ")
    return _text(f"{prefix}{body}{suffix}", "frozen_text", MAX_CUE_TEXT_CHARS)


def _dedupe_key(kind: CompanionCueKind, *parts: str) -> str:
    payload = "\x1f".join((kind.value, *parts)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise StorageValidationError(f"{field} is invalid")
    return value.strip()


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise StorageValidationError(f"{field} must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        raise StorageValidationError(f"{field} is invalid")
    return normalized


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageValidationError("confidence must be numeric")
    normalized = float(value)
    if not 0.0 <= normalized <= 1.0:
        raise StorageValidationError("confidence is invalid")
    return normalized
