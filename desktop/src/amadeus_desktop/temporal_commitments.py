"""Versioned local persistence for one-shot reminders and scheduled follow-ups."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.storage_models import (
    DEFAULT_PROFILE_ID,
    StorageConflictError,
    StorageNotFoundError,
    StorageValidationError,
    TemporalCommitment,
    TemporalCommitmentAuditEvent,
    TemporalCommitmentKind,
    TemporalCommitmentStatus,
    TemporalCommitmentVersion,
    TemporalSourceKind,
    TemporalVersionOrigin,
    decode_utc,
    encode_utc,
    utc_now,
)

MAX_TEMPORAL_CONTENT_CHARS = 500
MAX_TEMPORAL_FUTURE = timedelta(days=5 * 366)
ACTIVE_TEMPORAL_STATUSES = (
    TemporalCommitmentStatus.DRAFT,
    TemporalCommitmentStatus.SCHEDULED,
    TemporalCommitmentStatus.DUE,
    TemporalCommitmentStatus.SURFACED,
)
OUTSTANDING_TEMPORAL_STATUSES = (
    TemporalCommitmentStatus.DUE,
    TemporalCommitmentStatus.SURFACED,
)


@dataclass(frozen=True, slots=True)
class TemporalDraftSpec:
    kind: TemporalCommitmentKind
    content: str
    due_at_utc: datetime | None = None
    original_local_time: str | None = None
    timezone_name: str | None = None
    utc_offset_minutes: int | None = None
    show_content: bool = False


@dataclass(frozen=True, slots=True)
class TemporalDueSnapshot:
    due: tuple[TemporalCommitment, ...]
    next_due_at_utc: datetime | None
    outstanding_count: int


class TemporalCommitmentStore:
    """Keep reminder text versioned while status/audit updates remain content-free."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        clock=utc_now,
    ) -> None:
        self._database = database
        self._clock = clock

    def create_chat_draft(
        self,
        conversation_id: str,
        user_text: str,
        spec: TemporalDraftSpec,
        *,
        commitment_id: str | None = None,
        version_id: str | None = None,
        turn_id: str | None = None,
        user_message_id: str | None = None,
        assistant_message_id: str | None = None,
        assistant_text: str = "我先把时间和内容列出来；只有你确认后，我才会安排。",
    ) -> TemporalCommitment:
        normalized_user = str(user_text).strip()
        if not normalized_user:
            raise StorageValidationError("local reminder message must not be blank")
        normalized = _validate_spec(spec, allow_unresolved=True)
        now = _aware_utc(self._clock())
        encoded_now = encode_utc(now)
        commitment_id = _identifier(commitment_id or uuid4().hex, "commitment_id")
        version_id = _identifier(version_id or uuid4().hex, "version_id")
        turn_id = _identifier(turn_id or uuid4().hex, "turn_id")
        user_message_id = _identifier(user_message_id or uuid4().hex, "user_message_id")
        assistant_message_id = _identifier(
            assistant_message_id or uuid4().hex,
            "assistant_message_id",
        )
        try:
            with self._database.transaction() as connection:
                if connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ? AND profile_id = ?",
                    (conversation_id, DEFAULT_PROFILE_ID),
                ).fetchone() is None:
                    raise StorageNotFoundError("conversation does not exist")
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, conversation_id, turn_id, role, origin, content, status, attempt,
                        participates_in_memory, created_at, updated_at, completed_at,
                        input_modality
                    ) VALUES (?, ?, ?, 'user', 'conversation', ?, 'completed', 1,
                              0, ?, ?, ?, 'text')
                    """,
                    (
                        user_message_id,
                        conversation_id,
                        turn_id,
                        normalized_user,
                        encoded_now,
                        encoded_now,
                        encoded_now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO temporal_commitments(
                        id, profile_id, source_kind, source_message_id,
                        source_conversation_id, live_source_message_id,
                        live_source_conversation_id, status, current_version_id,
                        created_at, updated_at
                    ) VALUES (?, ?, 'chat', ?, ?, ?, ?, 'draft', ?, ?, ?)
                    """,
                    (
                        commitment_id,
                        DEFAULT_PROFILE_ID,
                        user_message_id,
                        conversation_id,
                        user_message_id,
                        conversation_id,
                        version_id,
                        encoded_now,
                        encoded_now,
                    ),
                )
                self._insert_version(
                    connection,
                    commitment_id,
                    version_id,
                    1,
                    normalized,
                    TemporalVersionOrigin.CHAT,
                    None,
                    encoded_now,
                )
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, conversation_id, turn_id, role, origin, content, status, attempt,
                        participates_in_memory, created_at, updated_at, completed_at,
                        input_modality, temporal_commitment_id
                    ) VALUES (?, ?, ?, 'assistant', 'conversation', ?, 'completed', 1,
                              0, ?, ?, ?, 'text', ?)
                    """,
                    (
                        assistant_message_id,
                        conversation_id,
                        turn_id,
                        str(assistant_text).strip(),
                        encoded_now,
                        encoded_now,
                        encoded_now,
                        commitment_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE conversations
                    SET last_activity_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (encoded_now, encoded_now, conversation_id),
                )
                self._insert_audit(
                    connection,
                    commitment_id,
                    "created",
                    "chat_intent_confirm_required",
                    None,
                    TemporalCommitmentStatus.DRAFT,
                    version_id,
                    encoded_now,
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("temporal draft identifiers conflict") from exc
        return self.get(commitment_id)

    def create_manual_draft(
        self,
        spec: TemporalDraftSpec,
        *,
        commitment_id: str | None = None,
        version_id: str | None = None,
    ) -> TemporalCommitment:
        normalized = _validate_spec(spec, allow_unresolved=True)
        commitment_id = _identifier(commitment_id or uuid4().hex, "commitment_id")
        version_id = _identifier(version_id or uuid4().hex, "version_id")
        now = _aware_utc(self._clock())
        encoded_now = encode_utc(now)
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO temporal_commitments(
                        id, profile_id, source_kind, status, current_version_id,
                        created_at, updated_at
                    ) VALUES (?, ?, 'manual', 'draft', ?, ?, ?)
                    """,
                    (commitment_id, DEFAULT_PROFILE_ID, version_id, encoded_now, encoded_now),
                )
                self._insert_version(
                    connection,
                    commitment_id,
                    version_id,
                    1,
                    normalized,
                    TemporalVersionOrigin.MANUAL,
                    None,
                    encoded_now,
                )
                self._insert_audit(
                    connection,
                    commitment_id,
                    "created",
                    "manual_confirm_required",
                    None,
                    TemporalCommitmentStatus.DRAFT,
                    version_id,
                    encoded_now,
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("temporal draft identifiers conflict") from exc
        return self.get(commitment_id)

    def get(self, commitment_id: str) -> TemporalCommitment:
        row = self._joined_row(commitment_id)
        if row is None:
            raise StorageNotFoundError("temporal commitment does not exist")
        return temporal_commitment_from_row(row)

    def list(
        self,
        *,
        query: str = "",
        status: TemporalCommitmentStatus | str | None = None,
        limit: int = 500,
    ) -> tuple[TemporalCommitment, ...]:
        if not 1 <= limit <= 1_000:
            raise StorageValidationError("temporal list limit is invalid")
        filters = ["c.profile_id = ?"]
        params: list[object] = [DEFAULT_PROFILE_ID]
        if status is not None and str(status):
            status_value = TemporalCommitmentStatus(status).value
            filters.append("c.status = ?")
            params.append(status_value)
        normalized_query = str(query).strip()
        if normalized_query:
            filters.append("instr(lower(v.content), lower(?)) > 0")
            params.append(normalized_query)
        params.append(limit)
        rows = self._database.connection.execute(
            _JOINED_SELECT
            + " WHERE "
            + " AND ".join(filters)
            + " ORDER BY c.updated_at DESC, c.id DESC LIMIT ?",
            params,
        ).fetchall()
        return tuple(temporal_commitment_from_row(row) for row in rows)

    def versions(self, commitment_id: str) -> tuple[TemporalCommitmentVersion, ...]:
        self.get(commitment_id)
        rows = self._database.connection.execute(
            """
            SELECT * FROM temporal_commitment_versions
            WHERE commitment_id = ? ORDER BY version_number DESC
            """,
            (commitment_id,),
        ).fetchall()
        return tuple(temporal_version_from_row(row) for row in rows)

    def audits(self, commitment_id: str) -> tuple[TemporalCommitmentAuditEvent, ...]:
        rows = self._database.connection.execute(
            """
            SELECT * FROM temporal_commitment_audit_events
            WHERE commitment_id = ? ORDER BY occurred_at, id
            """,
            (commitment_id,),
        ).fetchall()
        return tuple(temporal_audit_from_row(row) for row in rows)

    def confirm(self, commitment_id: str, spec: TemporalDraftSpec) -> TemporalCommitment:
        return self.revise(
            commitment_id,
            spec,
            origin=TemporalVersionOrigin.EDIT,
            confirm=True,
            reason_code="user_confirmed",
        )

    def revise(
        self,
        commitment_id: str,
        spec: TemporalDraftSpec,
        *,
        origin: TemporalVersionOrigin = TemporalVersionOrigin.EDIT,
        confirm: bool = True,
        reason_code: str = "user_edited",
    ) -> TemporalCommitment:
        normalized = _validate_spec(spec, allow_unresolved=not confirm)
        now = _aware_utc(self._clock())
        if confirm:
            _validate_future_due(normalized.due_at_utc, now)
        encoded_now = encode_utc(now)
        with self._database.transaction() as connection:
            row = connection.execute(
                _JOINED_SELECT + " WHERE c.id = ?",
                (commitment_id,),
            ).fetchone()
            if row is None:
                raise StorageNotFoundError("temporal commitment does not exist")
            previous = temporal_commitment_from_row(row)
            if previous.status in {
                TemporalCommitmentStatus.COMPLETED,
                TemporalCommitmentStatus.CANCELLED,
            }:
                raise StorageConflictError("terminal temporal commitment cannot be revised")
            next_status = (
                TemporalCommitmentStatus.SCHEDULED if confirm else TemporalCommitmentStatus.DRAFT
            )
            previous_version = previous.current_version
            if _spec_matches_version(normalized, previous_version):
                next_version_id = previous_version.version_id
            else:
                next_version_id = uuid4().hex
                self._insert_version(
                    connection,
                    commitment_id,
                    next_version_id,
                    previous_version.version_number + 1,
                    normalized,
                    TemporalVersionOrigin(origin),
                    previous_version.version_id,
                    encoded_now,
                )
            cursor = connection.execute(
                """
                UPDATE temporal_commitments
                SET status = ?, current_version_id = ?, updated_at = ?,
                    confirmed_at = CASE WHEN ? = 'scheduled' THEN COALESCE(confirmed_at, ?) ELSE NULL END,
                    due_detected_at = NULL, surfaced_at = NULL,
                    completed_at = NULL, cancelled_at = NULL
                WHERE id = ? AND current_version_id = ? AND status = ?
                """,
                (
                    next_status.value,
                    next_version_id,
                    encoded_now,
                    next_status.value,
                    encoded_now,
                    commitment_id,
                    previous_version.version_id,
                    previous.status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StorageConflictError("temporal commitment changed concurrently")
            self._insert_audit(
                connection,
                commitment_id,
                "confirmed" if confirm else "revised",
                reason_code,
                previous.status,
                next_status,
                next_version_id,
                encoded_now,
            )
        return self.get(commitment_id)

    def cancel(self, commitment_id: str) -> TemporalCommitment:
        return self._terminal_transition(
            commitment_id,
            TemporalCommitmentStatus.CANCELLED,
            reason_code="user_cancelled",
        )

    def complete(self, commitment_id: str) -> TemporalCommitment:
        return self._terminal_transition(
            commitment_id,
            TemporalCommitmentStatus.COMPLETED,
            reason_code="user_completed",
        )

    def snooze(
        self,
        commitment_id: str,
        *,
        minutes: int = 10,
        local_now: datetime | None = None,
    ) -> TemporalCommitment:
        if not 1 <= int(minutes) <= 30 * 24 * 60:
            raise StorageValidationError("snooze duration is invalid")
        previous = self.get(commitment_id)
        if previous.status not in {
            TemporalCommitmentStatus.DUE,
            TemporalCommitmentStatus.SURFACED,
            TemporalCommitmentStatus.SCHEDULED,
        }:
            raise StorageConflictError("temporal commitment cannot be snoozed")
        local = (local_now or datetime.now().astimezone()).astimezone()
        due_local = local + timedelta(minutes=int(minutes))
        offset = due_local.utcoffset()
        spec = TemporalDraftSpec(
            previous.current_version.kind,
            previous.current_version.content,
            due_local.astimezone(UTC),
            due_local.isoformat(timespec="minutes"),
            due_local.tzname() or "local",
            None if offset is None else round(offset.total_seconds() / 60),
            previous.current_version.show_content,
        )
        return self.revise(
            commitment_id,
            spec,
            origin=TemporalVersionOrigin.SNOOZE,
            confirm=True,
            reason_code="user_snoozed",
        )

    def scan_due(self, *, now: datetime | None = None) -> TemporalDueSnapshot:
        current = _aware_utc(now or self._clock())
        encoded_now = encode_utc(current)
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.status, c.current_version_id
                FROM temporal_commitments c
                JOIN temporal_commitment_versions v ON v.id = c.current_version_id
                WHERE c.profile_id = ? AND c.status = 'scheduled'
                  AND v.due_at_utc IS NOT NULL AND v.due_at_utc <= ?
                ORDER BY v.due_at_utc, c.id
                """,
                (DEFAULT_PROFILE_ID, encoded_now),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE temporal_commitments
                    SET status = 'due', due_detected_at = COALESCE(due_detected_at, ?),
                        updated_at = ?
                    WHERE id = ? AND status = 'scheduled' AND current_version_id = ?
                    """,
                    (encoded_now, encoded_now, row["id"], row["current_version_id"]),
                )
                self._insert_audit(
                    connection,
                    str(row["id"]),
                    "became_due",
                    "clock_reached_due_time",
                    TemporalCommitmentStatus.SCHEDULED,
                    TemporalCommitmentStatus.DUE,
                    str(row["current_version_id"]),
                    encoded_now,
                )
        due = self.list(status=TemporalCommitmentStatus.DUE)
        next_row = self._database.connection.execute(
            """
            SELECT MIN(v.due_at_utc) AS due_at
            FROM temporal_commitments c
            JOIN temporal_commitment_versions v ON v.id = c.current_version_id
            WHERE c.profile_id = ? AND c.status = 'scheduled' AND v.due_at_utc IS NOT NULL
            """,
            (DEFAULT_PROFILE_ID,),
        ).fetchone()
        return TemporalDueSnapshot(
            due,
            decode_utc(None if next_row is None else next_row["due_at"]),
            self.outstanding_count(),
        )

    def mark_surfaced(
        self,
        commitment_ids: Sequence[str],
        *,
        reason_code: str,
        surfaced_at: datetime | None = None,
    ) -> tuple[TemporalCommitment, ...]:
        identifiers = tuple(dict.fromkeys(_identifier(value, "commitment_id") for value in commitment_ids))
        if not identifiers:
            return ()
        now = encode_utc(_aware_utc(surfaced_at or self._clock()))
        changed: list[str] = []
        with self._database.transaction() as connection:
            for commitment_id in identifiers:
                row = connection.execute(
                    "SELECT status, current_version_id FROM temporal_commitments WHERE id = ?",
                    (commitment_id,),
                ).fetchone()
                if row is None:
                    continue
                if str(row["status"]) != TemporalCommitmentStatus.DUE.value:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE temporal_commitments
                    SET status = 'surfaced', surfaced_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'due'
                    """,
                    (now, now, commitment_id),
                )
                if cursor.rowcount != 1:
                    continue
                changed.append(commitment_id)
                self._insert_audit(
                    connection,
                    commitment_id,
                    "surfaced",
                    reason_code,
                    TemporalCommitmentStatus.DUE,
                    TemporalCommitmentStatus.SURFACED,
                    str(row["current_version_id"]),
                    now,
                )
        return tuple(self.get(identifier) for identifier in changed)

    def outstanding_count(self) -> int:
        row = self._database.connection.execute(
            """
            SELECT COUNT(*) AS count FROM temporal_commitments
            WHERE profile_id = ? AND status IN ('due', 'surfaced')
            """,
            (DEFAULT_PROFILE_ID,),
        ).fetchone()
        return 0 if row is None else int(row["count"])

    def open_followup_in_chat(
        self,
        commitment_id: str,
        conversation_id: str,
        *,
        message_id: str | None = None,
    ):
        """Persist one frozen follow-up locally and complete it without a provider call."""

        commitment_id = _identifier(commitment_id, "commitment_id")
        conversation_id = _identifier(conversation_id, "conversation_id")
        message_id = _identifier(message_id or uuid4().hex, "message_id")
        now = encode_utc(_aware_utc(self._clock()))
        with self._database.transaction() as connection:
            row = connection.execute(
                _JOINED_SELECT + " WHERE c.id = ?",
                (commitment_id,),
            ).fetchone()
            if row is None:
                raise StorageNotFoundError("temporal commitment does not exist")
            commitment = temporal_commitment_from_row(row)
            if commitment.current_version.kind is not TemporalCommitmentKind.SCHEDULED_FOLLOWUP:
                raise StorageConflictError("only a scheduled follow-up can open in chat")
            if commitment.status not in {
                TemporalCommitmentStatus.DUE,
                TemporalCommitmentStatus.SURFACED,
            }:
                raise StorageConflictError("scheduled follow-up is not available")
            conversation = connection.execute(
                "SELECT profile_id, status FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise StorageNotFoundError("conversation does not exist")
            if str(conversation["profile_id"]) != commitment.profile_id:
                raise StorageConflictError("follow-up and conversation profiles do not match")
            if str(conversation["status"]) != "normal":
                raise StorageConflictError("follow-up requires an active conversation")
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, turn_id, role, origin, content, status,
                    attempt, terminal_reason, participates_in_memory, created_at,
                    updated_at, completed_at, input_modality, temporal_commitment_id
                ) VALUES (?, ?, ?, 'assistant', 'proactive', ?, 'completed', 1,
                          'completed', 0, ?, ?, ?, 'text', ?)
                """,
                (
                    message_id,
                    conversation_id,
                    f"temporal-followup:{commitment_id}:{message_id}",
                    commitment.current_version.content,
                    now,
                    now,
                    now,
                    commitment_id,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE temporal_commitments
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('due', 'surfaced')
                """,
                (now, now, commitment_id),
            )
            if cursor.rowcount != 1:
                raise StorageConflictError("scheduled follow-up state changed")
            connection.execute(
                """
                UPDATE conversations SET last_activity_at = ?, updated_at = ? WHERE id = ?
                """,
                (now, now, conversation_id),
            )
            self._insert_audit(
                connection,
                commitment_id,
                "completed",
                "user_opened_followup",
                commitment.status,
                TemporalCommitmentStatus.COMPLETED,
                commitment.current_version.version_id,
                now,
            )
        return message_id

    def active_count_for_conversation(self, conversation_id: str | None = None) -> int:
        if conversation_id is None:
            row = self._database.connection.execute(
                """
                SELECT COUNT(*) AS count FROM temporal_commitments
                WHERE profile_id = ? AND status IN ('draft', 'scheduled', 'due', 'surfaced')
                  AND source_kind = 'chat'
                """,
                (DEFAULT_PROFILE_ID,),
            ).fetchone()
        else:
            row = self._database.connection.execute(
                """
                SELECT COUNT(*) AS count FROM temporal_commitments
                WHERE profile_id = ? AND source_conversation_id = ?
                  AND status IN ('draft', 'scheduled', 'due', 'surfaced')
                """,
                (DEFAULT_PROFILE_ID, conversation_id),
            ).fetchone()
        return 0 if row is None else int(row["count"])

    def delete(self, commitment_id: str) -> None:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM temporal_commitments WHERE id = ?",
                (commitment_id,),
            )
            if cursor.rowcount != 1:
                raise StorageNotFoundError("temporal commitment does not exist")

    def _terminal_transition(
        self,
        commitment_id: str,
        resulting: TemporalCommitmentStatus,
        *,
        reason_code: str,
    ) -> TemporalCommitment:
        now = encode_utc(_aware_utc(self._clock()))
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT status, current_version_id FROM temporal_commitments WHERE id = ?",
                (commitment_id,),
            ).fetchone()
            if row is None:
                raise StorageNotFoundError("temporal commitment does not exist")
            previous = TemporalCommitmentStatus(row["status"])
            if previous in {TemporalCommitmentStatus.COMPLETED, TemporalCommitmentStatus.CANCELLED}:
                raise StorageConflictError("temporal commitment is already terminal")
            timestamp_column = (
                "completed_at"
                if resulting is TemporalCommitmentStatus.COMPLETED
                else "cancelled_at"
            )
            connection.execute(
                f"""
                UPDATE temporal_commitments
                SET status = ?, {timestamp_column} = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (resulting.value, now, now, commitment_id, previous.value),
            )
            self._insert_audit(
                connection,
                commitment_id,
                resulting.value,
                reason_code,
                previous,
                resulting,
                str(row["current_version_id"]),
                now,
            )
        return self.get(commitment_id)

    def _joined_row(self, commitment_id: str):
        return self._database.connection.execute(
            _JOINED_SELECT + " WHERE c.id = ?",
            (commitment_id,),
        ).fetchone()

    @staticmethod
    def _insert_version(
        connection,
        commitment_id: str,
        version_id: str,
        version_number: int,
        spec: TemporalDraftSpec,
        origin: TemporalVersionOrigin,
        supersedes_version_id: str | None,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO temporal_commitment_versions(
                id, commitment_id, version_number, kind, content, due_at_utc,
                original_local_time, timezone_name, utc_offset_minutes, show_content,
                origin, supersedes_version_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                commitment_id,
                version_number,
                spec.kind.value,
                spec.content,
                None if spec.due_at_utc is None else encode_utc(spec.due_at_utc),
                spec.original_local_time,
                spec.timezone_name,
                spec.utc_offset_minutes,
                int(spec.show_content),
                origin.value,
                supersedes_version_id,
                created_at,
            ),
        )

    @staticmethod
    def _insert_audit(
        connection,
        commitment_id: str,
        event_type: str,
        reason_code: str,
        previous_status: TemporalCommitmentStatus | None,
        resulting_status: TemporalCommitmentStatus | None,
        version_id: str | None,
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO temporal_commitment_audit_events(
                id, profile_id, commitment_id, event_type, reason_code,
                previous_status, resulting_status, version_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                DEFAULT_PROFILE_ID,
                commitment_id,
                str(event_type),
                str(reason_code),
                None if previous_status is None else previous_status.value,
                None if resulting_status is None else resulting_status.value,
                version_id,
                occurred_at,
            ),
        )


_JOINED_SELECT = """
SELECT c.*,
       v.id AS version_id,
       v.version_number,
       v.kind AS version_kind,
       v.content AS version_content,
       v.due_at_utc AS version_due_at_utc,
       v.original_local_time AS version_original_local_time,
       v.timezone_name AS version_timezone_name,
       v.utc_offset_minutes AS version_utc_offset_minutes,
       v.show_content AS version_show_content,
       v.origin AS version_origin,
       v.supersedes_version_id AS version_supersedes_version_id,
       v.created_at AS version_created_at
FROM temporal_commitments c
JOIN temporal_commitment_versions v ON v.id = c.current_version_id
"""


def temporal_commitment_from_row(row) -> TemporalCommitment:
    version = TemporalCommitmentVersion(
        version_id=str(row["version_id"]),
        commitment_id=str(row["id"]),
        version_number=int(row["version_number"]),
        kind=TemporalCommitmentKind(row["version_kind"]),
        content=str(row["version_content"]),
        due_at_utc=decode_utc(row["version_due_at_utc"]),
        original_local_time=row["version_original_local_time"],
        timezone_name=row["version_timezone_name"],
        utc_offset_minutes=(
            None
            if row["version_utc_offset_minutes"] is None
            else int(row["version_utc_offset_minutes"])
        ),
        show_content=bool(row["version_show_content"]),
        origin=TemporalVersionOrigin(row["version_origin"]),
        supersedes_version_id=row["version_supersedes_version_id"],
        created_at=_required_datetime(row["version_created_at"]),
    )
    return TemporalCommitment(
        commitment_id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        source_kind=TemporalSourceKind(row["source_kind"]),
        source_message_id=row["source_message_id"],
        source_conversation_id=row["source_conversation_id"],
        live_source_message_id=row["live_source_message_id"],
        live_source_conversation_id=row["live_source_conversation_id"],
        status=TemporalCommitmentStatus(row["status"]),
        current_version=version,
        created_at=_required_datetime(row["created_at"]),
        updated_at=_required_datetime(row["updated_at"]),
        confirmed_at=decode_utc(row["confirmed_at"]),
        due_detected_at=decode_utc(row["due_detected_at"]),
        surfaced_at=decode_utc(row["surfaced_at"]),
        completed_at=decode_utc(row["completed_at"]),
        cancelled_at=decode_utc(row["cancelled_at"]),
    )


def temporal_version_from_row(row) -> TemporalCommitmentVersion:
    return TemporalCommitmentVersion(
        version_id=str(row["id"]),
        commitment_id=str(row["commitment_id"]),
        version_number=int(row["version_number"]),
        kind=TemporalCommitmentKind(row["kind"]),
        content=str(row["content"]),
        due_at_utc=decode_utc(row["due_at_utc"]),
        original_local_time=row["original_local_time"],
        timezone_name=row["timezone_name"],
        utc_offset_minutes=(
            None if row["utc_offset_minutes"] is None else int(row["utc_offset_minutes"])
        ),
        show_content=bool(row["show_content"]),
        origin=TemporalVersionOrigin(row["origin"]),
        supersedes_version_id=row["supersedes_version_id"],
        created_at=_required_datetime(row["created_at"]),
    )


def temporal_audit_from_row(row) -> TemporalCommitmentAuditEvent:
    return TemporalCommitmentAuditEvent(
        event_id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        commitment_id=str(row["commitment_id"]),
        event_type=str(row["event_type"]),
        reason_code=str(row["reason_code"]),
        previous_status=(
            None
            if row["previous_status"] is None
            else TemporalCommitmentStatus(row["previous_status"])
        ),
        resulting_status=(
            None
            if row["resulting_status"] is None
            else TemporalCommitmentStatus(row["resulting_status"])
        ),
        version_id=row["version_id"],
        occurred_at=_required_datetime(row["occurred_at"]),
    )


def _validate_spec(spec: TemporalDraftSpec, *, allow_unresolved: bool) -> TemporalDraftSpec:
    kind = TemporalCommitmentKind(spec.kind)
    content = " ".join(str(spec.content).strip().split())
    if not content or len(content) > MAX_TEMPORAL_CONTENT_CHARS:
        raise StorageValidationError("temporal content length is invalid")
    due = None if spec.due_at_utc is None else _aware_utc(spec.due_at_utc)
    if due is None:
        if not allow_unresolved:
            raise StorageValidationError("confirmed temporal commitment requires a due time")
        return TemporalDraftSpec(kind, content, show_content=bool(spec.show_content))
    if not spec.original_local_time or not spec.timezone_name or spec.utc_offset_minutes is None:
        raise StorageValidationError("resolved temporal commitment requires local time metadata")
    offset = int(spec.utc_offset_minutes)
    if not -840 <= offset <= 840:
        raise StorageValidationError("temporal UTC offset is invalid")
    return TemporalDraftSpec(
        kind,
        content,
        due,
        str(spec.original_local_time),
        str(spec.timezone_name),
        offset,
        bool(spec.show_content),
    )


def _validate_future_due(value: datetime | None, now: datetime) -> None:
    if value is None:
        raise StorageValidationError("confirmed temporal commitment requires a due time")
    due = _aware_utc(value)
    if due <= now:
        raise StorageValidationError("temporal due time must be in the future")
    if due - now > MAX_TEMPORAL_FUTURE:
        raise StorageValidationError("temporal due time exceeds the supported horizon")


def _spec_matches_version(spec: TemporalDraftSpec, version: TemporalCommitmentVersion) -> bool:
    return (
        spec.kind is version.kind
        and spec.content == version.content
        and spec.due_at_utc == version.due_at_utc
        and spec.original_local_time == version.original_local_time
        and spec.timezone_name == version.timezone_name
        and spec.utc_offset_minutes == version.utc_offset_minutes
        and spec.show_content is version.show_content
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise StorageValidationError("temporal timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _required_datetime(value: object) -> datetime:
    decoded = decode_utc(str(value))
    if decoded is None:
        raise StorageValidationError("required temporal timestamp is missing")
    return decoded


def _identifier(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128 or "\x00" in normalized:
        raise StorageValidationError(f"{field} is invalid")
    return normalized
