"""Transactional conversation, summary, and resumable-job repositories."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from uuid import uuid4

from amadeus_desktop.companion_cues import (
    CompanionCueStore,
    CompanionCueUnavailableError,
)
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.storage_models import (
    DEFAULT_PROFILE_ID,
    BackgroundJob,
    BackgroundJobStatus,
    Conversation,
    ConversationStatus,
    ConversationSummary,
    MessagePage,
    ProactiveDisposition,
    ProactiveInteractionEvent,
    ProactiveTrigger,
    Profile,
    StorageConflictError,
    StorageNotFoundError,
    StorageValidationError,
    StoredAttachment,
    StoredAttachmentKind,
    StoredAttachmentSource,
    StoredInputModality,
    StoredMessage,
    StoredMessageOrigin,
    StoredMessageRole,
    StoredMessageStatus,
    SummaryProgress,
    decode_utc,
    encode_utc,
    utc_now,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]


class ConversationStore:
    """Synchronous repository to be called from the application's data thread."""

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

    def ensure_default_profile(self, display_name: str = "用户") -> Profile:
        return self.create_profile(
            display_name=display_name,
            profile_id=DEFAULT_PROFILE_ID,
            if_missing=True,
        )

    def create_profile(
        self,
        display_name: str,
        *,
        profile_id: str | None = None,
        if_missing: bool = False,
    ) -> Profile:
        profile_id = _required_identifier(profile_id or self._id_factory(), "profile_id")
        display_name = _required_text(display_name, "display_name")
        now = encode_utc(self._clock())
        try:
            with self._database.transaction() as connection:
                if if_missing:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO profiles(id, display_name, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (profile_id, display_name, now, now),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO profiles(id, display_name, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (profile_id, display_name, now, now),
                    )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("profile ID already exists") from exc
        return self.get_profile(profile_id)

    def get_profile(self, profile_id: str = DEFAULT_PROFILE_ID) -> Profile:
        row = self._database.connection.execute(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("profile does not exist")
        return _profile_from_row(row)

    def create_conversation(
        self,
        title: str = "新对话",
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        conversation_id: str | None = None,
    ) -> Conversation:
        if profile_id == DEFAULT_PROFILE_ID:
            self.ensure_default_profile()
        else:
            self.get_profile(profile_id)
        conversation_id = _required_identifier(
            conversation_id or self._id_factory(), "conversation_id"
        )
        title = _required_text(title, "title")
        now = encode_utc(self._clock())
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO conversations(
                        id, profile_id, title, status, created_at, updated_at, last_activity_at
                    ) VALUES (?, ?, ?, 'normal', ?, ?, ?)
                    """,
                    (conversation_id, profile_id, title, now, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("conversation ID already exists") from exc
        return self.get_conversation(conversation_id)

    def get_or_create_active_conversation(
        self, profile_id: str = DEFAULT_PROFILE_ID
    ) -> Conversation:
        if profile_id == DEFAULT_PROFILE_ID:
            self.ensure_default_profile()
        row = self._database.connection.execute(
            """
            SELECT * FROM conversations
            WHERE profile_id = ? AND status = 'normal'
            ORDER BY last_activity_at DESC, id DESC
            LIMIT 1
            """,
            (profile_id,),
        ).fetchone()
        if row is None:
            return self.create_conversation(profile_id=profile_id)
        return _conversation_from_row(row)

    def get_conversation(self, conversation_id: str) -> Conversation:
        row = self._database.connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("conversation does not exist")
        return _conversation_from_row(row)

    def list_conversations(
        self,
        profile_id: str = DEFAULT_PROFILE_ID,
        *,
        include_archived: bool = False,
        limit: int = 200,
    ) -> tuple[Conversation, ...]:
        _validate_limit(limit, maximum=1_000)
        status_clause = "" if include_archived else "AND status = 'normal'"
        rows = self._database.connection.execute(
            f"""
            SELECT * FROM conversations
            WHERE profile_id = ? {status_clause}
            ORDER BY last_activity_at DESC, id DESC
            LIMIT ?
            """,
            (profile_id, limit),
        ).fetchall()
        return tuple(_conversation_from_row(row) for row in rows)

    def rename_conversation(self, conversation_id: str, title: str) -> Conversation:
        title = _required_text(title, "title")
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conversation_id),
            )
            if cursor.rowcount != 1:
                raise StorageNotFoundError("conversation does not exist")
        return self.get_conversation(conversation_id)

    def set_conversation_archived(self, conversation_id: str, archived: bool) -> Conversation:
        now = encode_utc(self._clock())
        status = ConversationStatus.ARCHIVED if archived else ConversationStatus.NORMAL
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, conversation_id),
            )
            if cursor.rowcount != 1:
                raise StorageNotFoundError("conversation does not exist")
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete message bodies; memory source IDs survive as tombstones."""

        with self._database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
        if cursor.rowcount == 1:
            self._database.purge_deleted_content()
        return cursor.rowcount == 1

    def clear_conversations(self, profile_id: str = DEFAULT_PROFILE_ID) -> int:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE profile_id = ?", (profile_id,)
            )
        deleted = max(0, cursor.rowcount)
        if deleted:
            self._database.purge_deleted_content()
        return deleted

    def pop_orphan_attachment_paths(self) -> tuple[str, ...]:
        """Delete unlinked metadata and return byte paths no linked row still owns."""

        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, relative_path FROM attachments
                WHERE NOT EXISTS (
                    SELECT 1 FROM message_attachments
                    WHERE message_attachments.attachment_id = attachments.id
                )
                """
            ).fetchall()
            if not rows:
                return ()
            orphan_ids = tuple(str(row["id"]) for row in rows)
            placeholders = ",".join("?" for _ in orphan_ids)
            connection.execute(
                f"DELETE FROM attachments WHERE id IN ({placeholders})",
                orphan_ids,
            )
            candidates = tuple(dict.fromkeys(str(row["relative_path"]) for row in rows))
            deletable: list[str] = []
            for relative_path in candidates:
                remaining = connection.execute(
                    "SELECT 1 FROM attachments WHERE relative_path = ? LIMIT 1",
                    (relative_path,),
                ).fetchone()
                if remaining is None:
                    deletable.append(relative_path)
        return tuple(deletable)

    def referenced_attachment_paths(self) -> tuple[str, ...]:
        rows = self._database.connection.execute(
            """
            SELECT DISTINCT a.relative_path
            FROM attachments AS a
            JOIN message_attachments AS ma ON ma.attachment_id = a.id
            ORDER BY a.relative_path
            """
        ).fetchall()
        return tuple(str(row["relative_path"]) for row in rows)

    def save_user_message(
        self,
        conversation_id: str,
        turn_id: str,
        message_id: str,
        content: str,
        *,
        created_at: datetime | None = None,
        participates_in_memory: bool = True,
        input_modality: StoredInputModality = StoredInputModality.TEXT,
        attachments: Sequence[StoredAttachment] = (),
    ) -> StoredMessage:
        content = _user_content(content, attachments)
        return self._insert_message(
            conversation_id=conversation_id,
            turn_id=turn_id,
            message_id=message_id,
            role=StoredMessageRole.USER,
            content=content,
            status=StoredMessageStatus.COMPLETED,
            attempt=1,
            created_at=created_at,
            completed=True,
            participates_in_memory=participates_in_memory,
            input_modality=input_modality,
            attachments=attachments,
        )

    def save_turn(
        self,
        conversation_id: str,
        turn_id: str,
        user_message_id: str,
        user_content: str,
        assistant_message_id: str,
        *,
        attempt: int = 1,
        participates_in_memory: bool = True,
        created_at: datetime | None = None,
        input_modality: StoredInputModality = StoredInputModality.TEXT,
        attachments: Sequence[StoredAttachment] = (),
    ) -> tuple[StoredMessage, StoredMessage]:
        """Atomically commit a user message and its stable assistant placeholder."""

        _required_identifier(turn_id, "turn_id")
        _required_identifier(user_message_id, "user_message_id")
        _required_identifier(assistant_message_id, "assistant_message_id")
        user_content = _user_content(user_content, attachments)
        input_modality = StoredInputModality(input_modality)
        _validate_attachments(attachments)
        if attempt < 1:
            raise StorageValidationError("attempt must be positive")
        now = encode_utc(created_at or self._clock())
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, conversation_id, turn_id, role, origin, content, status, attempt,
                        participates_in_memory, created_at, updated_at, completed_at,
                        input_modality
                    ) VALUES (?, ?, ?, 'user', 'conversation', ?, 'completed', 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_message_id,
                        conversation_id,
                        turn_id,
                        user_content,
                        int(participates_in_memory),
                        now,
                        now,
                        now,
                        input_modality.value,
                    ),
                )
                self._persist_attachments(
                    connection,
                    user_message_id,
                    attachments,
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, conversation_id, turn_id, role, origin, content, status, attempt,
                        participates_in_memory, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, 'assistant', 'conversation', '', 'pending', ?, 0, ?, ?, NULL)
                    """,
                    (assistant_message_id, conversation_id, turn_id, attempt, now, now),
                )
                connection.execute(
                    """
                    UPDATE conversations
                    SET last_activity_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, conversation_id),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("message or turn role already exists") from exc
        return self.get_message(user_message_id), self.get_message(assistant_message_id)

    def create_assistant_placeholder(
        self,
        conversation_id: str,
        turn_id: str,
        message_id: str,
        *,
        attempt: int = 1,
        created_at: datetime | None = None,
    ) -> StoredMessage:
        if attempt < 1:
            raise StorageValidationError("attempt must be positive")
        return self._insert_message(
            conversation_id=conversation_id,
            turn_id=turn_id,
            message_id=message_id,
            role=StoredMessageRole.ASSISTANT,
            content="",
            status=StoredMessageStatus.PENDING,
            attempt=attempt,
            created_at=created_at,
            completed=False,
            participates_in_memory=False,
        )

    def begin_assistant_attempt(self, message_id: str, attempt: int) -> StoredMessage:
        if attempt < 2:
            raise StorageValidationError("a retry attempt must be at least 2")
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT role, origin, attempt FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise StorageNotFoundError("message does not exist")
            if row["role"] != StoredMessageRole.ASSISTANT.value:
                raise StorageConflictError("only assistant messages can be retried")
            if row["origin"] != StoredMessageOrigin.CONVERSATION.value:
                raise StorageConflictError("proactive messages cannot be retried")
            if int(row["attempt"]) >= attempt:
                raise StorageConflictError("attempt must increase monotonically")
            connection.execute(
                """
                UPDATE messages
                SET content = '', status = 'pending', attempt = ?, terminal_reason = NULL,
                    failure_code = NULL, completed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (attempt, now, message_id),
            )
        return self.get_message(message_id)

    def checkpoint_assistant(self, message_id: str, content: str, *, attempt: int) -> StoredMessage:
        """Persist the complete coalesced stream snapshot for the current attempt."""

        if attempt < 1:
            raise StorageValidationError("attempt must be positive")
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT role, origin, status, attempt FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise StorageNotFoundError("message does not exist")
            if row["role"] != StoredMessageRole.ASSISTANT.value:
                raise StorageConflictError("only assistant messages accept checkpoints")
            if row["origin"] != StoredMessageOrigin.CONVERSATION.value:
                raise StorageConflictError("proactive messages cannot accept checkpoints")
            if int(row["attempt"]) != attempt:
                raise StorageConflictError("checkpoint belongs to a stale attempt")
            if row["status"] not in {
                StoredMessageStatus.PENDING.value,
                StoredMessageStatus.STREAMING.value,
            }:
                raise StorageConflictError("terminal messages cannot accept checkpoints")
            connection.execute(
                """
                UPDATE messages
                SET content = ?, status = 'streaming', updated_at = ?
                WHERE id = ?
                """,
                (content, now, message_id),
            )
            self._touch_conversation_for_message(connection, message_id, now)
        return self.get_message(message_id)

    def finalize_assistant(
        self,
        message_id: str,
        content: str,
        *,
        status: StoredMessageStatus | str,
        terminal_reason: str,
        attempt: int,
        provider_name: str | None = None,
        model_name: str | None = None,
        failure_code: str | None = None,
    ) -> StoredMessage:
        status_value = str(status)
        allowed = {
            StoredMessageStatus.COMPLETED.value,
            StoredMessageStatus.STOPPED.value,
            StoredMessageStatus.FAILED.value,
        }
        if status_value not in allowed:
            raise StorageValidationError("assistant terminal status is invalid")
        if attempt < 1:
            raise StorageValidationError("attempt must be positive")
        terminal_reason = _required_text(terminal_reason, "terminal_reason")
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT role, origin, attempt FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise StorageNotFoundError("message does not exist")
            if row["role"] != StoredMessageRole.ASSISTANT.value:
                raise StorageConflictError("only assistant messages can be finalized")
            if row["origin"] != StoredMessageOrigin.CONVERSATION.value:
                raise StorageConflictError("proactive messages cannot be finalized")
            if int(row["attempt"]) != attempt:
                raise StorageConflictError("terminal update belongs to a stale attempt")
            connection.execute(
                """
                UPDATE messages
                SET content = ?, status = ?, terminal_reason = ?, provider_name = ?,
                    model_name = ?, failure_code = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    content,
                    status_value,
                    terminal_reason,
                    provider_name,
                    model_name,
                    failure_code,
                    now,
                    now,
                    message_id,
                ),
            )
            self._touch_conversation_for_message(connection, message_id, now)
        return self.get_message(message_id)

    def set_message_memory_eligibility(self, message_id: str, participates: bool) -> StoredMessage:
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT origin FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise StorageNotFoundError("message does not exist")
            if participates and row["origin"] == StoredMessageOrigin.PROACTIVE.value:
                raise StorageConflictError("proactive messages cannot participate in memory")
            cursor = connection.execute(
                """
                UPDATE messages SET participates_in_memory = ?, updated_at = ? WHERE id = ?
                """,
                (int(participates), now, message_id),
            )
            assert cursor.rowcount == 1
        return self.get_message(message_id)

    def recover_interrupted_messages(self) -> int:
        """Terminalize persisted streams left active by an unclean process exit."""

        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE messages
                SET status = 'stopped', terminal_reason = 'shutdown',
                    updated_at = ?, completed_at = ?
                WHERE role = 'assistant' AND origin = 'conversation'
                  AND status IN ('pending', 'streaming')
                """,
                (now, now),
            )
        return max(0, cursor.rowcount)

    def get_message(self, message_id: str) -> StoredMessage:
        row = self._database.connection.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("message does not exist")
        return self._messages_from_rows((row,))[0]

    def load_message_page(
        self,
        conversation_id: str,
        *,
        limit: int = 40,
        before_sequence: int | None = None,
    ) -> MessagePage:
        _validate_limit(limit, maximum=500)
        if before_sequence is not None and before_sequence <= 0:
            raise StorageValidationError("before_sequence must be positive")
        cursor_clause = "" if before_sequence is None else "AND sequence < ?"
        params: list[object] = [conversation_id]
        if before_sequence is not None:
            params.append(before_sequence)
        # Fetch two look-ahead rows: one may be needed to complete a regular
        # user/assistant turn at the page boundary, while the second preserves
        # an exact has-older decision after that expansion. Standalone proactive
        # messages remain one independently ordered row.
        params.append(limit + 2)
        rows = self._database.connection.execute(
            f"""
            SELECT * FROM messages
            WHERE conversation_id = ? {cursor_clause}
            ORDER BY sequence DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        selected = rows[:limit]
        if selected and len(rows) > limit:
            oldest_selected = selected[-1]
            next_older = rows[limit]
            if (
                oldest_selected["origin"] == StoredMessageOrigin.CONVERSATION.value
                and next_older["origin"] == StoredMessageOrigin.CONVERSATION.value
                and oldest_selected["turn_id"] == next_older["turn_id"]
            ):
                selected.append(next_older)
        has_older = len(rows) > len(selected)
        items = self._messages_from_rows(tuple(reversed(selected)))
        next_cursor = items[0].sequence if has_older and items else None
        return MessagePage(items=items, next_before_sequence=next_cursor)

    def load_recent_messages(
        self, conversation_id: str, *, limit: int = 20
    ) -> tuple[StoredMessage, ...]:
        return self.load_message_page(conversation_id, limit=limit).items

    def load_recent_valid_messages(
        self, conversation_id: str, *, limit: int = 20
    ) -> tuple[StoredMessage, ...]:
        """Return exactly the recent messages eligible for prompt/summary context."""

        _validate_limit(limit, maximum=500)
        rows = self._database.connection.execute(
            """
            SELECT * FROM messages
            WHERE conversation_id = ?
              AND origin = 'conversation'
              AND ((role = 'user' AND status = 'completed')
                   OR (role = 'assistant' AND status IN ('completed', 'stopped')
                       AND LENGTH(content) > 0))
            ORDER BY sequence DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
        return self._messages_from_rows(tuple(reversed(rows)))

    def load_message_context(
        self,
        conversation_id: str,
        message_id: str,
        *,
        limit: int = 40,
    ) -> MessagePage:
        """Return a chronological page ending at a provenance target message."""

        _validate_limit(limit, maximum=500)
        row = self._database.connection.execute(
            "SELECT sequence, conversation_id FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None or row["conversation_id"] != conversation_id:
            raise StorageNotFoundError("message does not belong to the conversation")
        return self.load_message_page(
            conversation_id,
            limit=limit,
            before_sequence=int(row["sequence"]) + 1,
        )

    def load_messages_after(
        self,
        conversation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> tuple[StoredMessage, ...]:
        """Load valid incremental-summary messages in chronological order."""

        if after_sequence < 0:
            raise StorageValidationError("after_sequence must be non-negative")
        _validate_limit(limit, maximum=2_000)
        rows = self._database.connection.execute(
            """
            SELECT * FROM messages
            WHERE conversation_id = ? AND sequence > ?
              AND origin = 'conversation'
              AND ((role = 'user' AND status = 'completed')
                   OR (role = 'assistant' AND status IN ('completed', 'stopped')
                       AND LENGTH(content) > 0))
            ORDER BY sequence
            LIMIT ?
            """,
            (conversation_id, after_sequence, limit),
        ).fetchall()
        return self._messages_from_rows(tuple(rows))

    def save_summary(
        self,
        conversation_id: str,
        content: str,
        covers_through_sequence: int,
        *,
        message_count: int,
        character_count: int,
    ) -> ConversationSummary:
        content = _required_text(content, "content")
        if covers_through_sequence < 0 or message_count < 0 or character_count < 0:
            raise StorageValidationError("summary counters must be non-negative")
        summary_id = self._id_factory()
        now = encode_utc(self._clock())
        try:
            with self._database.transaction() as connection:
                if covers_through_sequence:
                    covered = connection.execute(
                        """
                        SELECT 1 FROM messages
                        WHERE conversation_id = ? AND sequence = ?
                        """,
                        (conversation_id, covers_through_sequence),
                    ).fetchone()
                    if covered is None:
                        raise StorageValidationError(
                            "summary cursor does not belong to the conversation"
                        )
                connection.execute(
                    """
                    INSERT INTO conversation_summaries(
                        id, conversation_id, content, covers_through_sequence,
                        message_count, character_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary_id,
                        conversation_id,
                        content,
                        covers_through_sequence,
                        message_count,
                        character_count,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("summary cursor already exists") from exc
        row = self._database.connection.execute(
            "SELECT * FROM conversation_summaries WHERE id = ?", (summary_id,)
        ).fetchone()
        assert row is not None
        return _summary_from_row(row)

    def latest_summary(self, conversation_id: str) -> ConversationSummary | None:
        row = self._database.connection.execute(
            """
            SELECT * FROM conversation_summaries
            WHERE conversation_id = ?
            ORDER BY covers_through_sequence DESC, created_at DESC
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        return None if row is None else _summary_from_row(row)

    def summary_progress(self, conversation_id: str) -> SummaryProgress:
        summary = self.latest_summary(conversation_id)
        covered = summary.covers_through_sequence if summary is not None else 0
        row = self._database.connection.execute(
            """
            SELECT COUNT(*) AS message_count,
                   COALESCE(SUM(LENGTH(content)), 0) AS character_count,
                   MAX(sequence) AS last_sequence
            FROM messages
            WHERE conversation_id = ? AND sequence > ?
              AND origin = 'conversation'
              AND ((role = 'user' AND status = 'completed')
                   OR (role = 'assistant' AND status IN ('completed', 'stopped')
                       AND LENGTH(content) > 0))
            """,
            (conversation_id, covered),
        ).fetchone()
        assert row is not None
        return SummaryProgress(
            message_count=int(row["message_count"]),
            character_count=int(row["character_count"]),
            last_sequence=None if row["last_sequence"] is None else int(row["last_sequence"]),
        )

    def _insert_message(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        message_id: str,
        role: StoredMessageRole,
        content: str,
        status: StoredMessageStatus,
        attempt: int,
        created_at: datetime | None,
        completed: bool,
        participates_in_memory: bool,
        origin: StoredMessageOrigin = StoredMessageOrigin.CONVERSATION,
        input_modality: StoredInputModality = StoredInputModality.TEXT,
        attachments: Sequence[StoredAttachment] = (),
    ) -> StoredMessage:
        _required_identifier(turn_id, "turn_id")
        _required_identifier(message_id, "message_id")
        input_modality = StoredInputModality(input_modality)
        _validate_attachments(attachments)
        now = encode_utc(created_at or self._clock())
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, conversation_id, turn_id, role, origin, content, status, attempt,
                        participates_in_memory, created_at, updated_at, completed_at,
                        input_modality
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        conversation_id,
                        turn_id,
                        role.value,
                        origin.value,
                        content,
                        status.value,
                        attempt,
                        int(participates_in_memory),
                        now,
                        now,
                        now if completed else None,
                        input_modality.value,
                    ),
                )
                self._persist_attachments(
                    connection,
                    message_id,
                    attachments,
                    created_at=now,
                )
                connection.execute(
                    """
                    UPDATE conversations
                    SET last_activity_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, conversation_id),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("message or turn role already exists") from exc
        return self.get_message(message_id)

    def _messages_from_rows(self, rows: Sequence[sqlite3.Row]) -> tuple[StoredMessage, ...]:
        messages = tuple(_message_from_row(row) for row in rows)
        if not messages:
            return ()
        identifiers = tuple(message.message_id for message in messages)
        placeholders = ",".join("?" for _ in identifiers)
        attachment_rows = self._database.connection.execute(
            f"""
            SELECT ma.message_id, ma.ordinal, a.*
            FROM message_attachments AS ma
            JOIN attachments AS a ON a.id = ma.attachment_id
            WHERE ma.message_id IN ({placeholders})
            ORDER BY ma.message_id, ma.ordinal
            """,
            identifiers,
        ).fetchall()
        grouped: dict[str, list[StoredAttachment]] = {identifier: [] for identifier in identifiers}
        for row in attachment_rows:
            grouped[str(row["message_id"])].append(_attachment_from_row(row))
        messages = tuple(
            replace(message, attachments=tuple(grouped[message.message_id])) for message in messages
        )
        cue_ids = tuple(
            dict.fromkeys(
                message.companion_cue_id
                for message in messages
                if message.companion_cue_id is not None
            )
        )
        labels: dict[str, str] = {}
        if cue_ids:
            cue_placeholders = ",".join("?" for _ in cue_ids)
            for row in self._database.connection.execute(
                f"SELECT id, kind FROM companion_cues WHERE id IN ({cue_placeholders})",
                cue_ids,
            ).fetchall():
                labels[str(row["id"])] = (
                    "待续话题" if str(row["kind"]) == "conversation_followup" else "已授权记忆"
                )
        return tuple(
            replace(
                message,
                companion_source_label=labels.get(message.companion_cue_id or ""),
            )
            for message in messages
        )

    @staticmethod
    def _persist_attachments(
        connection: sqlite3.Connection,
        message_id: str,
        attachments: Sequence[StoredAttachment],
        *,
        created_at: str,
    ) -> None:
        for ordinal, attachment in enumerate(attachments):
            connection.execute(
                """
                INSERT OR IGNORE INTO attachments(
                    id, kind, source, display_name, mime_type, size_bytes, sha256,
                    relative_path, status, extracted_text, text_truncated, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment.attachment_id,
                    attachment.kind.value,
                    attachment.source.value,
                    attachment.display_name,
                    attachment.mime_type,
                    attachment.size_bytes,
                    attachment.sha256,
                    attachment.relative_path,
                    attachment.status,
                    attachment.extracted_text,
                    int(attachment.text_truncated),
                    created_at,
                ),
            )
            existing = connection.execute(
                """
                SELECT kind, source, display_name, mime_type, size_bytes, sha256,
                       relative_path, status, extracted_text, text_truncated
                FROM attachments WHERE id = ?
                """,
                (attachment.attachment_id,),
            ).fetchone()
            expected = (
                attachment.kind.value,
                attachment.source.value,
                attachment.display_name,
                attachment.mime_type,
                attachment.size_bytes,
                attachment.sha256,
                attachment.relative_path,
                attachment.status,
                attachment.extracted_text,
                int(attachment.text_truncated),
            )
            if existing is None or tuple(existing) != expected:
                raise StorageConflictError("attachment ID already has different metadata")
            connection.execute(
                """
                INSERT OR IGNORE INTO message_attachments(message_id, attachment_id, ordinal)
                VALUES (?, ?, ?)
                """,
                (message_id, attachment.attachment_id, ordinal),
            )

    @staticmethod
    def _touch_conversation_for_message(
        connection: sqlite3.Connection, message_id: str, now: str
    ) -> None:
        connection.execute(
            """
            UPDATE conversations
            SET last_activity_at = ?, updated_at = ?
            WHERE id = (SELECT conversation_id FROM messages WHERE id = ?)
            """,
            (now, now, message_id),
        )


class ProactiveInteractionStore:
    """Content-free display ledger plus atomic click-to-history persistence."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        clock: Clock = utc_now,
        id_factory: IdFactory | None = None,
        companion_cues: CompanionCueStore | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._companion_cues = companion_cues or CompanionCueStore(
            database,
            clock=clock,
            id_factory=self._id_factory,
        )

    def record_displayed(
        self,
        trigger: ProactiveTrigger | str,
        local_date: date,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        event_id: str | None = None,
        displayed_at: datetime | None = None,
        cue_id: str | None = None,
    ) -> ProactiveInteractionEvent:
        """Count one greeting only after its bubble was actually displayed."""

        trigger_value = _proactive_trigger(trigger)
        date_value = _local_date(local_date)
        profile_id = _required_identifier(profile_id, "profile_id")
        event_id = _required_identifier(event_id or self._id_factory(), "event_id")
        shown_at = encode_utc(displayed_at or self._clock())
        normalized_cue_id = None if cue_id is None else _required_identifier(cue_id, "cue_id")
        try:
            with self._database.transaction() as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
                    ).fetchone()
                    is None
                ):
                    raise StorageNotFoundError("profile does not exist")
                if normalized_cue_id is not None:
                    cue = self._companion_cues.mark_surfaced(
                        normalized_cue_id,
                        connection=connection,
                    )
                    if cue.profile_id != profile_id:
                        raise StorageConflictError(
                            "proactive event and companion cue profiles do not match"
                        )
                connection.execute(
                    """
                    INSERT INTO proactive_events(
                        id, profile_id, local_date, trigger_kind, displayed_at,
                        disposition, message_id, cue_id
                    ) VALUES (?, ?, ?, ?, ?, 'displayed', NULL, ?)
                    """,
                    (
                        event_id,
                        profile_id,
                        date_value,
                        trigger_value.value,
                        shown_at,
                        normalized_cue_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("proactive event ID already exists") from exc
        return self.get(event_id)

    def count_displayed_for_date(
        self,
        local_date: date,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> int:
        """Return persisted displays regardless of their later disposition."""

        row = self._database.connection.execute(
            """
            SELECT COUNT(*) AS event_count
            FROM proactive_events
            WHERE profile_id = ? AND local_date = ?
            """,
            (
                _required_identifier(profile_id, "profile_id"),
                _local_date(local_date),
            ),
        ).fetchone()
        assert row is not None
        return int(row["event_count"])

    def count_for_date(
        self,
        local_date: date,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> int:
        """Short alias used by the proactive policy boundary."""

        return self.count_displayed_for_date(local_date, profile_id=profile_id)

    def record_dismissed(self, event_id: str) -> ProactiveInteractionEvent:
        event_id = _required_identifier(event_id, "event_id")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT disposition FROM proactive_events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise StorageNotFoundError("proactive event does not exist")
            disposition = ProactiveDisposition(row["disposition"])
            if disposition is ProactiveDisposition.CLICKED:
                raise StorageConflictError("clicked proactive events cannot be dismissed")
            if disposition is ProactiveDisposition.DISPLAYED:
                connection.execute(
                    """
                    UPDATE proactive_events SET disposition = 'dismissed'
                    WHERE id = ? AND disposition = 'displayed'
                    """,
                    (event_id,),
                )
        return self.get(event_id)

    def record_clicked(
        self,
        event_id: str,
        conversation_id: str,
        greeting: str,
        *,
        message_id: str | None = None,
        clicked_at: datetime | None = None,
        provider_name: str | None = None,
        model_name: str | None = None,
    ) -> tuple[ProactiveInteractionEvent, StoredMessage]:
        """Persist a clicked greeting; this is intentionally the only click path."""

        return self.persist_greeting_on_click(
            event_id,
            conversation_id,
            greeting,
            message_id=message_id,
            clicked_at=clicked_at,
            provider_name=provider_name,
            model_name=model_name,
        )

    def persist_greeting_on_click(
        self,
        event_id: str,
        conversation_id: str,
        greeting: str,
        *,
        message_id: str | None = None,
        clicked_at: datetime | None = None,
        provider_name: str | None = None,
        model_name: str | None = None,
    ) -> tuple[ProactiveInteractionEvent, StoredMessage]:
        """Atomically link one completed standalone assistant message to a click."""

        event_id = _required_identifier(event_id, "event_id")
        conversation_id = _required_identifier(conversation_id, "conversation_id")
        greeting = _required_text(greeting, "greeting", strip=False)
        candidate_message_id = _required_identifier(message_id or self._id_factory(), "message_id")
        now = encode_utc(clicked_at or self._clock())
        existing_message_id: str | None = None
        try:
            with self._database.transaction() as connection:
                event = connection.execute(
                    "SELECT * FROM proactive_events WHERE id = ?", (event_id,)
                ).fetchone()
                if event is None:
                    raise StorageNotFoundError("proactive event does not exist")
                disposition = ProactiveDisposition(event["disposition"])
                if disposition is ProactiveDisposition.DISMISSED:
                    raise StorageConflictError("dismissed proactive events cannot be clicked")
                if disposition is ProactiveDisposition.CLICKED:
                    existing_message_id = event["message_id"]
                    if existing_message_id is None:
                        raise StorageConflictError(
                            "clicked proactive message is no longer available"
                        )
                else:
                    conversation = connection.execute(
                        "SELECT profile_id, status FROM conversations WHERE id = ?",
                        (conversation_id,),
                    ).fetchone()
                    if conversation is None:
                        raise StorageNotFoundError("conversation does not exist")
                    if conversation["profile_id"] != event["profile_id"]:
                        raise StorageConflictError(
                            "proactive event and conversation profiles do not match"
                        )
                    if conversation["status"] != ConversationStatus.NORMAL.value:
                        raise StorageConflictError(
                            "proactive messages require an active conversation"
                        )
                    cue_id = event["cue_id"]
                    if cue_id is not None:
                        cue = self._companion_cues.authorized_for_click(
                            str(cue_id),
                            connection=connection,
                        )
                        if cue.profile_id != str(event["profile_id"]):
                            raise CompanionCueUnavailableError("companion cue profile changed")
                        if greeting != cue.frozen_text:
                            raise CompanionCueUnavailableError(
                                "companion cue text does not match authorization"
                            )
                    connection.execute(
                        """
                        INSERT INTO messages(
                            id, conversation_id, turn_id, role, origin, content,
                            status, attempt, terminal_reason, provider_name, model_name,
                            failure_code, participates_in_memory, created_at, updated_at,
                            completed_at, companion_cue_id
                        ) VALUES (
                            ?, ?, ?, 'assistant', 'proactive', ?, 'completed', 1,
                            'completed', ?, ?, NULL, 0, ?, ?, ?, ?
                        )
                        """,
                        (
                            candidate_message_id,
                            conversation_id,
                            event_id,
                            greeting,
                            provider_name,
                            model_name,
                            now,
                            now,
                            now,
                            cue_id,
                        ),
                    )
                    cursor = connection.execute(
                        """
                        UPDATE proactive_events
                        SET disposition = 'clicked', message_id = ?
                        WHERE id = ? AND disposition = 'displayed'
                        """,
                        (candidate_message_id, event_id),
                    )
                    if cursor.rowcount != 1:
                        raise StorageConflictError("proactive event state changed")
                    ConversationStore._touch_conversation_for_message(
                        connection, candidate_message_id, now
                    )
                    existing_message_id = candidate_message_id
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("proactive message could not be persisted") from exc

        assert existing_message_id is not None
        message = _message_from_row(
            self._database.connection.execute(
                "SELECT * FROM messages WHERE id = ?", (existing_message_id,)
            ).fetchone()
        )
        return self.get(event_id), message

    def persist_companion_cue_manual_open(
        self,
        cue_id: str,
        conversation_id: str,
        *,
        message_id: str | None = None,
        opened_at: datetime | None = None,
    ) -> StoredMessage:
        """Open authorized frozen text in chat without any provider request."""

        cue_id = _required_identifier(cue_id, "cue_id")
        conversation_id = _required_identifier(conversation_id, "conversation_id")
        candidate_message_id = _required_identifier(message_id or self._id_factory(), "message_id")
        now = encode_utc(opened_at or self._clock())
        try:
            with self._database.transaction() as connection:
                cue = self._companion_cues.authorized_for_manual_open(
                    cue_id,
                    connection=connection,
                )
                conversation = connection.execute(
                    "SELECT profile_id, status FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if conversation is None:
                    raise StorageNotFoundError("conversation does not exist")
                if str(conversation["profile_id"]) != cue.profile_id:
                    raise StorageConflictError(
                        "companion cue and conversation profiles do not match"
                    )
                if str(conversation["status"]) != ConversationStatus.NORMAL.value:
                    raise StorageConflictError("companion cue requires an active conversation")
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, conversation_id, turn_id, role, origin, content,
                        status, attempt, terminal_reason, failure_code,
                        participates_in_memory, created_at, updated_at, completed_at,
                        companion_cue_id
                    ) VALUES (
                        ?, ?, ?, 'assistant', 'proactive', ?, 'completed', 1,
                        'completed', NULL, 0, ?, ?, ?, ?
                    )
                    """,
                    (
                        candidate_message_id,
                        conversation_id,
                        f"companion-cue:{cue_id}:{candidate_message_id}",
                        cue.frozen_text,
                        now,
                        now,
                        now,
                        cue_id,
                    ),
                )
                ConversationStore._touch_conversation_for_message(
                    connection,
                    candidate_message_id,
                    now,
                )
        except sqlite3.IntegrityError as exc:
            raise StorageConflictError("companion cue message could not be persisted") from exc
        return _message_from_row(
            self._database.connection.execute(
                "SELECT * FROM messages WHERE id = ?",
                (candidate_message_id,),
            ).fetchone()
        )

    def get(self, event_id: str) -> ProactiveInteractionEvent:
        row = self._database.connection.execute(
            "SELECT * FROM proactive_events WHERE id = ?",
            (_required_identifier(event_id, "event_id"),),
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("proactive event does not exist")
        return _proactive_event_from_row(row)

    def list_for_date(
        self,
        local_date: date,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> tuple[ProactiveInteractionEvent, ...]:
        rows = self._database.connection.execute(
            """
            SELECT * FROM proactive_events
            WHERE profile_id = ? AND local_date = ?
            ORDER BY displayed_at, id
            """,
            (
                _required_identifier(profile_id, "profile_id"),
                _local_date(local_date),
            ),
        ).fetchall()
        return tuple(_proactive_event_from_row(row) for row in rows)


class BackgroundJobStore:
    """Durable, idempotent job states for summary/extraction retry workers."""

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

    def enqueue(
        self,
        kind: str,
        dedupe_key: str,
        *,
        payload: Mapping[str, object] | None = None,
        profile_id: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        run_after: datetime | None = None,
    ) -> BackgroundJob:
        kind = _required_text(kind, "kind")
        dedupe_key = _required_text(dedupe_key, "dedupe_key")
        job_id = self._id_factory()
        now = encode_utc(self._clock())
        ready = encode_utc(run_after or self._clock())
        payload_json = json.dumps(
            dict(payload or {}), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO background_jobs(
                    id, kind, dedupe_key, status, payload_json, profile_id,
                    conversation_id, message_id, attempt_count, run_after,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    dedupe_key,
                    payload_json,
                    profile_id,
                    conversation_id,
                    message_id,
                    ready,
                    now,
                    now,
                ),
            )
        row = self._database.connection.execute(
            "SELECT * FROM background_jobs WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        assert row is not None
        return _job_from_row(row)

    def schedule_deep_memory_cycle(
        self,
        *,
        completed_turn_count: int,
        source_message_ids: Sequence[str],
        profile_id: str,
        conversation_id: str | None,
        message_id: str | None,
        run_after: datetime,
        trigger: str,
    ) -> BackgroundJob:
        """Coalesce a pending deep cycle and move its idle deadline after each turn."""

        if completed_turn_count < 1:
            raise StorageValidationError("completed_turn_count must be positive")
        profile_id = _required_identifier(profile_id, "profile_id")
        if trigger not in {"idle", "turn"}:
            raise StorageValidationError("deep-memory trigger must be idle or turn")
        source_ids = tuple(
            dict.fromkeys(
                _required_identifier(value, "source_message_id") for value in source_message_ids
            )
        )
        now = encode_utc(self._clock())
        ready = encode_utc(run_after)
        with self._database.transaction() as connection:
            pending_rows = connection.execute(
                """
                SELECT * FROM background_jobs
                WHERE kind = 'deep_memory_cycle' AND profile_id = ? AND status = 'pending'
                ORDER BY created_at, id
                """,
                (profile_id,),
            ).fetchall()
            pending = None
            previous_payload = None
            for row in pending_rows:
                candidate_payload = json.loads(str(row["payload_json"]))
                if not isinstance(candidate_payload, dict):
                    raise StorageValidationError("deep-memory job payload is invalid")
                if candidate_payload.get("trigger") in {"idle", "turn"}:
                    pending = row
                    previous_payload = candidate_payload
                    break
            if pending is not None:
                assert previous_payload is not None
                previous_sources = previous_payload.get("source_message_ids", [])
                if not isinstance(previous_sources, list):
                    raise StorageValidationError("deep-memory job source list is invalid")
                merged_sources = tuple(
                    dict.fromkeys(
                        [
                            *(
                                _required_identifier(value, "source_message_id")
                                for value in previous_sources
                            ),
                            *source_ids,
                        ]
                    )
                )
                previous_trigger = previous_payload.get("trigger")
                effective_trigger = (
                    "turn" if trigger == "turn" or previous_trigger == "turn" else "idle"
                )
                effective_ready = str(pending["run_after"]) if previous_trigger == "turn" else ready
                payload_json = json.dumps(
                    {
                        "source_message_ids": list(merged_sources),
                        "trigger": effective_trigger,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                connection.execute(
                    """
                    UPDATE background_jobs
                    SET payload_json = ?, conversation_id = ?, message_id = ?,
                        run_after = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (
                        payload_json,
                        conversation_id,
                        message_id,
                        effective_ready,
                        now,
                        pending["id"],
                    ),
                )
                job_id = str(pending["id"])
            else:
                job_id = self._id_factory()
                payload_json = json.dumps(
                    {"source_message_ids": list(source_ids), "trigger": trigger},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                connection.execute(
                    """
                    INSERT INTO background_jobs(
                        id, kind, dedupe_key, status, payload_json, profile_id,
                        conversation_id, message_id, attempt_count, run_after,
                        created_at, updated_at
                    ) VALUES (?, 'deep_memory_cycle', ?, 'pending', ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        job_id,
                        f"deep-memory:{profile_id}:{completed_turn_count}",
                        payload_json,
                        profile_id,
                        conversation_id,
                        message_id,
                        ready,
                        now,
                        now,
                    ),
                )
        return self.get(job_id)

    def recover_interrupted(self) -> int:
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE background_jobs
                SET status = 'retry', run_after = ?, updated_at = ?
                WHERE status = 'running'
                """,
                (now, now),
            )
        return max(0, cursor.rowcount)

    def claim_ready(
        self,
        *,
        limit: int = 1,
        kinds: Sequence[str] | None = None,
    ) -> tuple[BackgroundJob, ...]:
        _validate_limit(limit, maximum=100)
        if kinds is not None and not kinds:
            return ()
        normalized_kinds = (
            tuple(_required_text(kind, "kind") for kind in kinds) if kinds is not None else ()
        )
        kind_clause = ""
        if normalized_kinds:
            placeholders = ",".join("?" for _kind in normalized_kinds)
            kind_clause = f"AND kind IN ({placeholders})"
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            parameters: list[object] = [now]
            parameters.extend(normalized_kinds)
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT id FROM background_jobs
                WHERE status IN ('pending', 'retry') AND run_after <= ?
                    {kind_clause}
                ORDER BY run_after, created_at, id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            job_ids = tuple(str(row["id"]) for row in rows)
            for job_id in job_ids:
                connection.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'running', attempt_count = attempt_count + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
            claimed = [
                connection.execute(
                    "SELECT * FROM background_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                for job_id in job_ids
            ]
        return tuple(_job_from_row(row) for row in claimed if row is not None)

    def mark_completed(self, job_id: str) -> BackgroundJob:
        return self._set_terminal(job_id, BackgroundJobStatus.COMPLETED)

    def mark_failed(self, job_id: str, *, error_code: str | None = None) -> BackgroundJob:
        return self._set_terminal(job_id, BackgroundJobStatus.FAILED, error_code)

    def mark_retry(
        self,
        job_id: str,
        run_after: datetime,
        *,
        error_code: str | None = None,
    ) -> BackgroundJob:
        now = encode_utc(self._clock())
        safe_code = _safe_error_code(error_code)
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE background_jobs
                SET status = 'retry', run_after = ?, last_error_code = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (encode_utc(run_after), safe_code, now, job_id),
            )
            if cursor.rowcount != 1:
                raise StorageConflictError("only running jobs can be retried")
        return self.get(job_id)

    def retry_failed(
        self,
        job_id: str,
        *,
        run_after: datetime | None = None,
        reset_attempts: bool = True,
    ) -> BackgroundJob:
        """Start a user-requested retry cycle for one terminal failed job."""

        now_value = self._clock()
        now = encode_utc(now_value)
        attempt_expression = "0" if reset_attempts else "attempt_count"
        with self._database.transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE background_jobs
                SET status = 'retry', run_after = ?, last_error_code = NULL,
                    attempt_count = {attempt_expression}, updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (encode_utc(run_after or now_value), now, job_id),
            )
            if cursor.rowcount != 1:
                raise StorageConflictError("only failed jobs can be retried manually")
        return self.get(job_id)

    def get(self, job_id: str) -> BackgroundJob:
        row = self._database.connection.execute(
            "SELECT * FROM background_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("background job does not exist")
        return _job_from_row(row)

    def list_failed(self, *, limit: int = 100) -> tuple[BackgroundJob, ...]:
        _validate_limit(limit, maximum=1_000)
        rows = self._database.connection.execute(
            """
            SELECT * FROM background_jobs
            WHERE status = 'failed'
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(_job_from_row(row) for row in rows)

    def _set_terminal(
        self,
        job_id: str,
        status: BackgroundJobStatus,
        error_code: str | None = None,
    ) -> BackgroundJob:
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE background_jobs
                SET status = ?, last_error_code = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (status.value, _safe_error_code(error_code), now, job_id),
            )
            if cursor.rowcount != 1:
                raise StorageConflictError("only running jobs can be terminalized")
        return self.get(job_id)


def _required_identifier(value: str, field: str) -> str:
    value = str(value).strip()
    if not value or len(value) > 200:
        raise StorageValidationError(f"{field} must be a non-empty identifier")
    return value


def _required_text(value: str, field: str, *, strip: bool = True) -> str:
    if not isinstance(value, str):
        raise StorageValidationError(f"{field} must be text")
    result = value.strip() if strip else value
    if not result or not value.strip():
        raise StorageValidationError(f"{field} must not be blank")
    return result


def _user_content(value: str, attachments: Sequence[StoredAttachment]) -> str:
    if not isinstance(value, str):
        raise StorageValidationError("user content must be text")
    if not value.strip() and not attachments:
        raise StorageValidationError("user content or attachments are required")
    return value


def _validate_attachments(attachments: Sequence[StoredAttachment]) -> None:
    if len(attachments) > 5:
        raise StorageValidationError("a message cannot contain more than five attachments")
    total = 0
    identifiers: set[str] = set()
    for attachment in attachments:
        if not isinstance(attachment, StoredAttachment):
            raise StorageValidationError("attachment metadata is invalid")
        _required_identifier(attachment.attachment_id, "attachment_id")
        if attachment.attachment_id in identifiers:
            raise StorageValidationError("attachment IDs must be unique per message")
        identifiers.add(attachment.attachment_id)
        if (
            isinstance(attachment.size_bytes, bool)
            or not isinstance(attachment.size_bytes, int)
            or not 0 < attachment.size_bytes <= 25 * 1024 * 1024
        ):
            raise StorageValidationError("attachment size is invalid")
        total += attachment.size_bytes
        if total > 50 * 1024 * 1024:
            raise StorageValidationError("attachment total size is invalid")
        if len(attachment.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in attachment.sha256
        ):
            raise StorageValidationError("attachment checksum is invalid")
        if (
            not attachment.display_name
            or len(attachment.display_name) > 255
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in attachment.display_name
            )
        ):
            raise StorageValidationError("attachment display name is invalid")
        parts = attachment.relative_path.split("/")
        if (
            not parts
            or attachment.relative_path.startswith("/")
            or "\\" in attachment.relative_path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise StorageValidationError("attachment relative path is invalid")
        if attachment.status != "ready":
            raise StorageValidationError("only ready attachments can be linked")
        if len(attachment.extracted_text) > 64_000:
            raise StorageValidationError("attachment extracted text is too long")


def _proactive_trigger(value: ProactiveTrigger | str) -> ProactiveTrigger:
    try:
        return ProactiveTrigger(str(value))
    except ValueError as exc:
        raise StorageValidationError("unsupported proactive trigger") from exc


def _local_date(value: date) -> str:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise StorageValidationError("local_date must be a date")
    return value.isoformat()


def _validate_limit(limit: int, *, maximum: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise StorageValidationError(f"limit must be between 1 and {maximum}")


def _safe_error_code(error_code: str | None) -> str | None:
    if error_code is None:
        return None
    return str(error_code).replace("\r", " ").replace("\n", " ")[:128]


def _required_datetime(value: str) -> datetime:
    decoded = decode_utc(value)
    assert decoded is not None
    return decoded


def _profile_from_row(row: sqlite3.Row) -> Profile:
    return Profile(
        profile_id=str(row["id"]),
        display_name=str(row["display_name"]),
        created_at=_required_datetime(row["created_at"]),
        updated_at=_required_datetime(row["updated_at"]),
    )


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        conversation_id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        title=str(row["title"]),
        status=ConversationStatus(row["status"]),
        created_at=_required_datetime(row["created_at"]),
        updated_at=_required_datetime(row["updated_at"]),
        last_activity_at=_required_datetime(row["last_activity_at"]),
    )


def _message_from_row(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        sequence=int(row["sequence"]),
        message_id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        turn_id=str(row["turn_id"]),
        role=StoredMessageRole(row["role"]),
        origin=StoredMessageOrigin(row["origin"]),
        content=str(row["content"]),
        status=StoredMessageStatus(row["status"]),
        attempt=int(row["attempt"]),
        terminal_reason=row["terminal_reason"],
        provider_name=row["provider_name"],
        model_name=row["model_name"],
        failure_code=row["failure_code"],
        participates_in_memory=bool(row["participates_in_memory"]),
        created_at=_required_datetime(row["created_at"]),
        updated_at=_required_datetime(row["updated_at"]),
        completed_at=decode_utc(row["completed_at"]),
        input_modality=(
            StoredInputModality(row["input_modality"])
            if "input_modality" in tuple(row.keys())
            else StoredInputModality.TEXT
        ),
        companion_cue_id=(
            str(row["companion_cue_id"])
            if "companion_cue_id" in tuple(row.keys()) and row["companion_cue_id"] is not None
            else None
        ),
    )


def _attachment_from_row(row: sqlite3.Row) -> StoredAttachment:
    return StoredAttachment(
        attachment_id=str(row["id"]),
        kind=StoredAttachmentKind(row["kind"]),
        source=StoredAttachmentSource(row["source"]),
        display_name=str(row["display_name"]),
        mime_type=str(row["mime_type"]),
        size_bytes=int(row["size_bytes"]),
        sha256=str(row["sha256"]),
        relative_path=str(row["relative_path"]),
        status=str(row["status"]),
        extracted_text=str(row["extracted_text"]),
        text_truncated=bool(row["text_truncated"]),
        created_at=_required_datetime(row["created_at"]),
    )


def _proactive_event_from_row(row: sqlite3.Row) -> ProactiveInteractionEvent:
    try:
        local_date = date.fromisoformat(str(row["local_date"]))
    except ValueError as exc:
        raise StorageValidationError("proactive event local date is invalid") from exc
    return ProactiveInteractionEvent(
        event_id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        local_date=local_date,
        trigger=ProactiveTrigger(row["trigger_kind"]),
        displayed_at=_required_datetime(row["displayed_at"]),
        disposition=ProactiveDisposition(row["disposition"]),
        message_id=row["message_id"],
        cue_id=(
            str(row["cue_id"])
            if "cue_id" in tuple(row.keys()) and row["cue_id"] is not None
            else None
        ),
    )


def _summary_from_row(row: sqlite3.Row) -> ConversationSummary:
    return ConversationSummary(
        summary_id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        content=str(row["content"]),
        covers_through_sequence=int(row["covers_through_sequence"]),
        message_count=int(row["message_count"]),
        character_count=int(row["character_count"]),
        created_at=_required_datetime(row["created_at"]),
    )


def _job_from_row(row: sqlite3.Row) -> BackgroundJob:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise StorageValidationError("background job payload is not an object")
    return BackgroundJob(
        job_id=str(row["id"]),
        kind=str(row["kind"]),
        dedupe_key=str(row["dedupe_key"]),
        status=BackgroundJobStatus(row["status"]),
        payload=payload,
        profile_id=row["profile_id"],
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        attempt_count=int(row["attempt_count"]),
        run_after=_required_datetime(row["run_after"]),
        last_error_code=row["last_error_code"],
        created_at=_required_datetime(row["created_at"]),
        updated_at=_required_datetime(row["updated_at"]),
    )
