"""Local-only persona knowledge persistence, FTS, and auditable recall events."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from uuid import uuid4

from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.memory_search import (
    EmptySearchQuery,
    build_fts_match_query,
    build_search_text,
    exact_memory_hash,
)
from amadeus_desktop.storage_models import (
    PersonaKnowledge,
    PersonaKnowledgeDraft,
    PersonaSearchResult,
    RecallStats,
    RecallTerminalStatus,
    StorageNotFoundError,
    StorageValidationError,
    decode_utc,
    encode_utc,
    utc_now,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
MAX_INDEX_DOCUMENTS = 10_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PersonaRepository:
    """Store role knowledge independently from user memory."""

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

    def upsert_knowledge(
        self,
        persona_id: str,
        content: str,
        *,
        tags: Sequence[str],
        source_ref: str,
        source_hash: str,
        knowledge_id: str | None = None,
    ) -> PersonaKnowledge:
        persona_id = _identifier(persona_id, "persona_id")
        prepared = _prepare_draft(
            PersonaKnowledgeDraft(
                content=content,
                tags=tuple(tags),
                source_ref=source_ref,
                source_hash=source_hash,
                knowledge_id=knowledge_id,
            )
        )
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            stored_id = self._upsert_with_connection(connection, persona_id, prepared, now)
        return self.get(stored_id)

    def replace_persona(
        self,
        persona_id: str,
        drafts: Sequence[PersonaKnowledgeDraft],
    ) -> tuple[PersonaKnowledge, ...]:
        """Atomically activate the supplied corpus and deactivate omitted entries."""

        persona_id = _identifier(persona_id, "persona_id")
        prepared = tuple(_prepare_draft(draft) for draft in drafts)
        hashes = [item.content_hash for item in prepared]
        if len(hashes) != len(set(hashes)):
            raise StorageValidationError("persona corpus contains duplicate content")
        explicit_ids = [item.knowledge_id for item in prepared if item.knowledge_id is not None]
        if len(explicit_ids) != len(set(explicit_ids)):
            raise StorageValidationError("persona corpus contains duplicate knowledge_id")
        now = encode_utc(self._clock())
        stored_ids: list[str] = []
        with self._database.transaction() as connection:
            for item in prepared:
                stored_ids.append(self._upsert_with_connection(connection, persona_id, item, now))
            if hashes:
                placeholders = ",".join("?" for _value in hashes)
                deactivated = connection.execute(
                    f"""
                    SELECT id FROM persona_knowledge
                    WHERE persona_id = ? AND content_hash NOT IN ({placeholders}) AND active = 1
                    """,
                    (persona_id, *hashes),
                ).fetchall()
            else:
                deactivated = connection.execute(
                    "SELECT id FROM persona_knowledge WHERE persona_id = ? AND active = 1",
                    (persona_id,),
                ).fetchall()
            deactivated_ids = tuple(str(row["id"]) for row in deactivated)
            if deactivated_ids:
                placeholders = ",".join("?" for _value in deactivated_ids)
                connection.execute(
                    f"UPDATE persona_knowledge SET active = 0, updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, *deactivated_ids),
                )
                connection.executemany(
                    "DELETE FROM persona_fts WHERE knowledge_id = ?",
                    ((knowledge_id,) for knowledge_id in deactivated_ids),
                )
        return self.get_active_by_ids(persona_id, stored_ids)

    def get(self, knowledge_id: str) -> PersonaKnowledge:
        row = self._database.connection.execute(
            "SELECT * FROM persona_knowledge WHERE id = ?",
            (_identifier(knowledge_id, "knowledge_id"),),
        ).fetchone()
        if row is None:
            raise StorageNotFoundError("persona knowledge does not exist")
        return _knowledge_from_row(row)

    def list_active_documents(
        self,
        persona_id: str,
        *,
        limit: int = MAX_INDEX_DOCUMENTS,
    ) -> tuple[PersonaKnowledge, ...]:
        _validate_limit(limit, MAX_INDEX_DOCUMENTS)
        rows = self._database.connection.execute(
            """
            SELECT * FROM persona_knowledge
            WHERE persona_id = ? AND active = 1
            ORDER BY updated_at DESC, id
            LIMIT ?
            """,
            (_identifier(persona_id, "persona_id"), limit),
        ).fetchall()
        return tuple(_knowledge_from_row(row) for row in rows)

    def get_active_by_ids(
        self,
        persona_id: str,
        knowledge_ids: Iterable[str],
    ) -> tuple[PersonaKnowledge, ...]:
        ids = tuple(dict.fromkeys(_identifier(value, "knowledge_id") for value in knowledge_ids))
        if not ids:
            return ()
        placeholders = ",".join("?" for _value in ids)
        rows = self._database.connection.execute(
            f"""
            SELECT * FROM persona_knowledge
            WHERE persona_id = ? AND active = 1 AND id IN ({placeholders})
            """,
            (_identifier(persona_id, "persona_id"), *ids),
        ).fetchall()
        by_id = {str(row["id"]): _knowledge_from_row(row) for row in rows}
        return tuple(by_id[value] for value in ids if value in by_id)

    def search(
        self,
        persona_id: str,
        query: str,
        *,
        limit: int = 30,
    ) -> tuple[PersonaSearchResult, ...]:
        _validate_limit(limit, 2_000)
        try:
            match = build_fts_match_query(query)
        except EmptySearchQuery:
            return ()
        rows = self._database.connection.execute(
            """
            SELECT p.*, bm25(persona_fts) AS fts_rank
            FROM persona_fts
            JOIN persona_knowledge AS p ON p.id = persona_fts.knowledge_id
            WHERE persona_fts MATCH ? AND persona_fts.persona_id = ? AND p.active = 1
            ORDER BY fts_rank, p.updated_at DESC, p.id
            LIMIT ?
            """,
            (*match.parameters, _identifier(persona_id, "persona_id"), limit),
        ).fetchall()
        return tuple(
            PersonaSearchResult(knowledge=_knowledge_from_row(row), rank=float(row["fts_rank"]))
            for row in rows
        )

    def set_active(self, knowledge_id: str, active: bool) -> PersonaKnowledge:
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM persona_knowledge WHERE id = ?",
                (_identifier(knowledge_id, "knowledge_id"),),
            ).fetchone()
            if row is None:
                raise StorageNotFoundError("persona knowledge does not exist")
            connection.execute(
                "UPDATE persona_knowledge SET active = ?, updated_at = ? WHERE id = ?",
                (int(active), now, knowledge_id),
            )
            connection.execute("DELETE FROM persona_fts WHERE knowledge_id = ?", (knowledge_id,))
            if active:
                connection.execute(
                    """
                    INSERT INTO persona_fts(knowledge_id, persona_id, search_text)
                    VALUES (?, ?, ?)
                    """,
                    (knowledge_id, row["persona_id"], row["search_text"]),
                )
        return self.get(knowledge_id)

    def delete(self, knowledge_id: str) -> bool:
        knowledge_id = _identifier(knowledge_id, "knowledge_id")
        with self._database.transaction() as connection:
            connection.execute("DELETE FROM persona_fts WHERE knowledge_id = ?", (knowledge_id,))
            cursor = connection.execute(
                "DELETE FROM persona_knowledge WHERE id = ?", (knowledge_id,)
            )
        if cursor.rowcount == 1:
            self._database.purge_deleted_content()
        return cursor.rowcount == 1

    def record_successful_recall(
        self,
        retrieval_ticket_id: str,
        persona_id: str,
        knowledge_ids: Sequence[str],
        *,
        terminal_status: RecallTerminalStatus | str,
        first_chunk_received: bool,
        conversation_id: str | None = None,
        assistant_message_id: str | None = None,
        attempt: int = 1,
        recalled_at: datetime | None = None,
    ) -> int:
        status = _successful_terminal(terminal_status, first_chunk_received)
        if status is None:
            return 0
        ticket_id = _identifier(retrieval_ticket_id, "retrieval_ticket_id")
        persona_id = _identifier(persona_id, "persona_id")
        ids = tuple(dict.fromkeys(_identifier(value, "knowledge_id") for value in knowledge_ids))
        if not ids:
            return 0
        _validate_attempt(attempt)
        now = encode_utc(recalled_at or self._clock())
        with self._database.transaction() as connection:
            valid = _existing_ids(
                connection,
                "SELECT id FROM persona_knowledge WHERE persona_id = ? AND id IN ({})",
                persona_id,
                ids,
            )
            if valid != set(ids):
                raise StorageValidationError("recall contains unknown persona knowledge")
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO persona_recall_events(
                    id, retrieval_ticket_id, persona_id, knowledge_id,
                    conversation_id, assistant_message_id, attempt,
                    terminal_status, recalled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        self._id_factory(),
                        ticket_id,
                        persona_id,
                        knowledge_id,
                        conversation_id,
                        assistant_message_id,
                        attempt,
                        status.value,
                        now,
                    )
                    for knowledge_id in ids
                ),
            )
            return connection.total_changes - before

    def recall_stats(
        self, persona_id: str, knowledge_ids: Sequence[str] | None = None
    ) -> tuple[RecallStats, ...]:
        parameters: list[object] = [_identifier(persona_id, "persona_id")]
        id_clause = ""
        if knowledge_ids is not None:
            ids = tuple(
                dict.fromkeys(_identifier(value, "knowledge_id") for value in knowledge_ids)
            )
            if not ids:
                return ()
            placeholders = ",".join("?" for _value in ids)
            id_clause = f"AND p.id IN ({placeholders})"
            parameters.extend(ids)
        rows = self._database.connection.execute(
            """
            SELECT p.id AS target_id, COUNT(e.id) AS recall_count,
                   MAX(e.recalled_at) AS last_recalled_at
            FROM persona_knowledge AS p
            LEFT JOIN persona_recall_events AS e ON e.knowledge_id = p.id
            WHERE p.persona_id = ?
            """
            + id_clause
            + " GROUP BY p.id ORDER BY p.id",
            parameters,
        ).fetchall()
        return tuple(_recall_stats_from_row(row) for row in rows)

    def _upsert_with_connection(
        self,
        connection: sqlite3.Connection,
        persona_id: str,
        draft: _PreparedDraft,
        now: str,
    ) -> str:
        existing = connection.execute(
            """
            SELECT id FROM persona_knowledge
            WHERE persona_id = ? AND content_hash = ?
            """,
            (persona_id, draft.content_hash),
        ).fetchone()
        if existing is None and draft.knowledge_id is not None:
            existing = connection.execute(
                "SELECT id, persona_id FROM persona_knowledge WHERE id = ?",
                (draft.knowledge_id,),
            ).fetchone()
            if existing is not None and existing["persona_id"] != persona_id:
                raise StorageValidationError("knowledge_id belongs to another persona")
        if existing is not None:
            knowledge_id = str(existing["id"])
            if draft.knowledge_id is not None and draft.knowledge_id != knowledge_id:
                raise StorageValidationError("knowledge_id conflicts with existing persona content")
            connection.execute(
                """
                UPDATE persona_knowledge
                SET content = ?, search_text = ?, tags_json = ?, source_ref = ?,
                    source_hash = ?, content_hash = ?, active = 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    draft.content,
                    draft.search_text,
                    draft.tags_json,
                    draft.source_ref,
                    draft.source_hash,
                    draft.content_hash,
                    now,
                    knowledge_id,
                ),
            )
        else:
            knowledge_id = _identifier(
                draft.knowledge_id or self._id_factory(),
                "knowledge_id",
            )
            connection.execute(
                """
                INSERT INTO persona_knowledge(
                    id, persona_id, content, search_text, tags_json, source_ref,
                    source_hash, content_hash, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    knowledge_id,
                    persona_id,
                    draft.content,
                    draft.search_text,
                    draft.tags_json,
                    draft.source_ref,
                    draft.source_hash,
                    draft.content_hash,
                    now,
                    now,
                ),
            )
        connection.execute("DELETE FROM persona_fts WHERE knowledge_id = ?", (knowledge_id,))
        connection.execute(
            """
            INSERT INTO persona_fts(knowledge_id, persona_id, search_text)
            VALUES (?, ?, ?)
            """,
            (knowledge_id, persona_id, draft.search_text),
        )
        return knowledge_id


class _PreparedDraft:
    __slots__ = (
        "content",
        "content_hash",
        "knowledge_id",
        "search_text",
        "source_hash",
        "source_ref",
        "tags_json",
    )

    def __init__(
        self,
        *,
        content: str,
        content_hash: str,
        knowledge_id: str | None,
        search_text: str,
        source_hash: str,
        source_ref: str,
        tags_json: str,
    ) -> None:
        self.content = content
        self.content_hash = content_hash
        self.knowledge_id = knowledge_id
        self.search_text = search_text
        self.source_hash = source_hash
        self.source_ref = source_ref
        self.tags_json = tags_json


def _prepare_draft(draft: PersonaKnowledgeDraft) -> _PreparedDraft:
    if not isinstance(draft, PersonaKnowledgeDraft):
        raise StorageValidationError("persona corpus entries must be PersonaKnowledgeDraft values")
    if not isinstance(draft.content, str) or not draft.content.strip():
        raise StorageValidationError("persona content must not be blank")
    content = draft.content.strip()
    search_text = build_search_text(content)
    if not search_text:
        raise StorageValidationError("persona content has no searchable text")
    try:
        content_hash = exact_memory_hash(content)
    except ValueError as exc:
        raise StorageValidationError("persona content has no searchable text") from exc
    tags: list[str] = []
    for tag in draft.tags:
        normalized = str(tag).strip()
        if not normalized or len(normalized) > 100:
            raise StorageValidationError("persona tags must be non-empty short text")
        if normalized not in tags:
            tags.append(normalized)
    source_ref = _text(draft.source_ref, "source_ref", maximum=2_000)
    source_hash = str(draft.source_hash).strip().lower()
    if not _SHA256_RE.fullmatch(source_hash):
        raise StorageValidationError("source_hash must be a lowercase SHA-256 digest")
    return _PreparedDraft(
        content=content,
        content_hash=content_hash,
        knowledge_id=(
            None if draft.knowledge_id is None else _identifier(draft.knowledge_id, "knowledge_id")
        ),
        search_text=search_text,
        source_hash=source_hash,
        source_ref=source_ref,
        tags_json=json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
    )


def _knowledge_from_row(row: sqlite3.Row) -> PersonaKnowledge:
    tags = json.loads(str(row["tags_json"]))
    if not isinstance(tags, list) or not all(isinstance(value, str) for value in tags):
        raise StorageValidationError("stored persona tags are invalid")
    return PersonaKnowledge(
        knowledge_id=str(row["id"]),
        persona_id=str(row["persona_id"]),
        content=str(row["content"]),
        search_text=str(row["search_text"]),
        tags=tuple(tags),
        source_ref=str(row["source_ref"]),
        source_hash=str(row["source_hash"]),
        content_hash=str(row["content_hash"]),
        active=bool(row["active"]),
        created_at=_required_datetime(row["created_at"]),
        updated_at=_required_datetime(row["updated_at"]),
    )


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


def _existing_ids(
    connection: sqlite3.Connection,
    statement: str,
    scope_id: str,
    ids: Sequence[str],
) -> set[str]:
    placeholders = ",".join("?" for _value in ids)
    rows = connection.execute(statement.format(placeholders), (scope_id, *ids)).fetchall()
    return {str(row[0]) for row in rows}


def _recall_stats_from_row(row: sqlite3.Row) -> RecallStats:
    return RecallStats(
        target_id=str(row["target_id"]),
        successful_recall_count=int(row["recall_count"]),
        last_recalled_at=decode_utc(row["last_recalled_at"]),
    )


def _identifier(value: str, field: str) -> str:
    value = str(value).strip()
    if not value or len(value) > 200:
        raise StorageValidationError(f"{field} must be a non-empty identifier")
    return value


def _text(value: str, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StorageValidationError(f"{field} must be non-empty text")
    return value.strip()


def _validate_limit(limit: int, maximum: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise StorageValidationError(f"limit must be between 1 and {maximum}")


def _validate_attempt(attempt: int) -> None:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise StorageValidationError("attempt must be a positive integer")


def _required_datetime(value: str) -> datetime:
    decoded = decode_utc(value)
    assert decoded is not None
    return decoded
