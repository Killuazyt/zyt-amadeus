"""Validated float32 vector persistence with atomic generation switching."""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import struct
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from uuid import uuid4

from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.storage_models import (
    DEFAULT_PROFILE_ID,
    EmbeddingCorpus,
    EmbeddingGeneration,
    EmbeddingGenerationStatus,
    StorageConflictError,
    StorageNotFoundError,
    StorageValidationError,
    StoredVector,
    decode_utc,
    encode_utc,
    utc_now,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

VECTOR_DIMENSION = 512
VECTOR_BLOB_BYTES = VECTOR_DIMENSION * 4
MAX_INDEX_DOCUMENTS = 10_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def encode_vector(
    values: Sequence[float], *, dimension: int = VECTOR_DIMENSION
) -> tuple[bytes, str]:
    """Return a normalized little-endian float32 BLOB and its SHA-256 digest."""

    if dimension != VECTOR_DIMENSION:
        raise StorageValidationError("the configured embedding dimension must be 512")
    if len(values) != dimension:
        raise StorageValidationError(f"vector must contain exactly {dimension} values")
    floats: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise StorageValidationError("vector values must be finite numbers")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise StorageValidationError("vector values must be finite numbers") from exc
        if not math.isfinite(numeric):
            raise StorageValidationError("vector values must be finite numbers")
        floats.append(numeric)
    norm = math.sqrt(math.fsum(value * value for value in floats))
    if not math.isfinite(norm) or norm <= 0.0:
        raise StorageValidationError("vector norm must be positive and finite")
    normalized = tuple(value / norm for value in floats)
    blob = struct.pack(f"<{dimension}f", *normalized)
    return blob, hashlib.sha256(blob).hexdigest()


def decode_vector(
    blob: bytes | bytearray | memoryview,
    vector_hash: str,
    *,
    dimension: int = VECTOR_DIMENSION,
) -> tuple[float, ...]:
    """Validate persisted bytes before exposing them to an inference cache."""

    if dimension != VECTOR_DIMENSION:
        raise StorageValidationError("the stored embedding dimension must be 512")
    raw = bytes(blob)
    if len(raw) != dimension * 4:
        raise StorageValidationError("stored vector BLOB has an invalid length")
    if not _SHA256_RE.fullmatch(str(vector_hash)):
        raise StorageValidationError("stored vector hash is invalid")
    if hashlib.sha256(raw).hexdigest() != vector_hash:
        raise StorageValidationError("stored vector BLOB checksum does not match")
    values = struct.unpack(f"<{dimension}f", raw)
    if not all(math.isfinite(value) for value in values):
        raise StorageValidationError("stored vector contains a non-finite value")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
        raise StorageValidationError("stored vector is not L2 normalized")
    return tuple(float(value) for value in values)


class VectorStore:
    """Keep user-memory and persona vector generations physically separate."""

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

    def recover_interrupted_generations(self) -> tuple[int, int, int, int]:
        """Fail stale building generations while preserving every active one."""

        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            memory = connection.execute(
                """
                UPDATE memory_embedding_generations
                SET status = 'failed', failure_code = 'interrupted', updated_at = ?
                WHERE status = 'building'
                """,
                (now,),
            )
            persona = connection.execute(
                """
                UPDATE persona_embedding_generations
                SET status = 'failed', failure_code = 'interrupted', updated_at = ?
                WHERE status = 'building'
                """,
                (now,),
            )
            reflection = connection.execute(
                """
                UPDATE memory_reflection_embedding_generations
                SET status = 'failed', failure_code = 'interrupted', updated_at = ?
                WHERE status = 'building'
                """,
                (now,),
            )
            impression = connection.execute(
                """
                UPDATE memory_persona_impression_embedding_generations
                SET status = 'failed', failure_code = 'interrupted', updated_at = ?
                WHERE status = 'building'
                """,
                (now,),
            )
        return (
            max(0, memory.rowcount),
            max(0, reflection.rowcount),
            max(0, impression.rowcount),
            max(0, persona.rowcount),
        )

    def begin_memory_generation(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        model_name: str,
        model_commit: str,
        model_sha256: str,
        calibration_threshold: float,
        dimension: int = VECTOR_DIMENSION,
        generation_id: str | None = None,
    ) -> EmbeddingGeneration:
        profile_id = _identifier(profile_id, "profile_id")
        now = encode_utc(self._clock())
        values = _generation_values(
            model_name,
            model_commit,
            model_sha256,
            calibration_threshold,
            dimension,
        )
        generation_id = _identifier(generation_id or self._id_factory(), "generation_id")
        with self._database.transaction() as connection:
            _ensure_profile(connection, profile_id, now)
            connection.execute(
                """
                INSERT INTO memory_embedding_generations(
                    id, profile_id, model_name, model_commit, dimension, model_sha256,
                    calibration_threshold, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'building', ?, ?)
                """,
                (generation_id, profile_id, *values, now, now),
            )
        return self.get_memory_generation(generation_id)

    def begin_persona_generation(
        self,
        *,
        persona_id: str,
        model_name: str,
        model_commit: str,
        model_sha256: str,
        calibration_threshold: float,
        dimension: int = VECTOR_DIMENSION,
        generation_id: str | None = None,
    ) -> EmbeddingGeneration:
        persona_id = _identifier(persona_id, "persona_id")
        values = _generation_values(
            model_name,
            model_commit,
            model_sha256,
            calibration_threshold,
            dimension,
        )
        generation_id = _identifier(generation_id or self._id_factory(), "generation_id")
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO persona_embedding_generations(
                    id, persona_id, model_name, model_commit, dimension, model_sha256,
                    calibration_threshold, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'building', ?, ?)
                """,
                (generation_id, persona_id, *values, now, now),
            )
        return self.get_persona_generation(generation_id)

    def begin_reflection_generation(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        model_name: str,
        model_commit: str,
        model_sha256: str,
        calibration_threshold: float,
        dimension: int = VECTOR_DIMENSION,
        generation_id: str | None = None,
    ) -> EmbeddingGeneration:
        return self._begin_derived_generation(
            "memory_reflection_embedding_generations",
            EmbeddingCorpus.REFLECTION,
            profile_id=profile_id,
            model_name=model_name,
            model_commit=model_commit,
            model_sha256=model_sha256,
            calibration_threshold=calibration_threshold,
            dimension=dimension,
            generation_id=generation_id,
        )

    def begin_persona_impression_generation(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        model_name: str,
        model_commit: str,
        model_sha256: str,
        calibration_threshold: float,
        dimension: int = VECTOR_DIMENSION,
        generation_id: str | None = None,
    ) -> EmbeddingGeneration:
        return self._begin_derived_generation(
            "memory_persona_impression_embedding_generations",
            EmbeddingCorpus.PERSONA_IMPRESSION,
            profile_id=profile_id,
            model_name=model_name,
            model_commit=model_commit,
            model_sha256=model_sha256,
            calibration_threshold=calibration_threshold,
            dimension=dimension,
            generation_id=generation_id,
        )

    def _begin_derived_generation(
        self,
        table: str,
        corpus: EmbeddingCorpus,
        *,
        profile_id: str,
        model_name: str,
        model_commit: str,
        model_sha256: str,
        calibration_threshold: float,
        dimension: int,
        generation_id: str | None,
    ) -> EmbeddingGeneration:
        profile_id = _identifier(profile_id, "profile_id")
        generation_id = _identifier(generation_id or self._id_factory(), "generation_id")
        values = _generation_values(
            model_name,
            model_commit,
            model_sha256,
            calibration_threshold,
            dimension,
        )
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            _ensure_profile(connection, profile_id, now)
            connection.execute(
                f"""
                INSERT INTO {table}(
                    id, profile_id, model_name, model_commit, dimension, model_sha256,
                    calibration_threshold, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'building', ?, ?)
                """,
                (generation_id, profile_id, *values, now, now),
            )
        return self._get_derived_generation(table, corpus, generation_id)

    def activate_memory_generation(
        self,
        generation_id: str,
        vectors: Mapping[str, Sequence[float]],
    ) -> EmbeddingGeneration:
        generation = self.get_memory_generation(generation_id)
        encoded = _encode_vectors(vectors, generation.dimension)
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            current = _generation_row(
                connection,
                "memory_embedding_generations",
                generation_id,
            )
            _require_building(current)
            _activate_memory_with_connection(
                connection,
                current=current,
                generation_id=generation_id,
                encoded=encoded,
                now=now,
            )
        return self.get_memory_generation(generation_id)

    def activate_persona_generation(
        self,
        generation_id: str,
        vectors: Mapping[str, Sequence[float]],
    ) -> EmbeddingGeneration:
        generation = self.get_persona_generation(generation_id)
        encoded = _encode_vectors(vectors, generation.dimension)
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            current = _generation_row(
                connection,
                "persona_embedding_generations",
                generation_id,
            )
            _require_building(current)
            _activate_persona_with_connection(
                connection,
                current=current,
                generation_id=generation_id,
                encoded=encoded,
                now=now,
            )
        return self.get_persona_generation(generation_id)

    def activate_reflection_generation(
        self,
        generation_id: str,
        vectors: Mapping[str, Sequence[float]],
    ) -> EmbeddingGeneration:
        return self._activate_derived_generation(
            EmbeddingCorpus.REFLECTION,
            generation_id,
            vectors,
        )

    def activate_persona_impression_generation(
        self,
        generation_id: str,
        vectors: Mapping[str, Sequence[float]],
    ) -> EmbeddingGeneration:
        return self._activate_derived_generation(
            EmbeddingCorpus.PERSONA_IMPRESSION,
            generation_id,
            vectors,
        )

    def _activate_derived_generation(
        self,
        corpus: EmbeddingCorpus,
        generation_id: str,
        vectors: Mapping[str, Sequence[float]],
    ) -> EmbeddingGeneration:
        table, _groups, _versions, _vectors, _group_fk, _statuses = _derived_vector_spec(corpus)
        generation = self._get_derived_generation(table, corpus, generation_id)
        encoded = _encode_vectors(vectors, generation.dimension)
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            current = _generation_row(connection, table, generation_id)
            _require_building(current)
            _activate_derived_with_connection(
                connection,
                corpus=corpus,
                current=current,
                generation_id=generation_id,
                encoded=encoded,
                now=now,
            )
        return self._get_derived_generation(table, corpus, generation_id)

    def activate_generations_atomically(
        self,
        memory_generation_id: str,
        memory_vectors: Mapping[str, Sequence[float]],
        persona_generation_id: str,
        persona_vectors: Mapping[str, Sequence[float]],
    ) -> tuple[EmbeddingGeneration, EmbeddingGeneration]:
        """Switch both independent corpora in one SQLite transaction."""

        memory_generation = self.get_memory_generation(memory_generation_id)
        persona_generation = self.get_persona_generation(persona_generation_id)
        encoded_memory = _encode_vectors(memory_vectors, memory_generation.dimension)
        encoded_persona = _encode_vectors(persona_vectors, persona_generation.dimension)
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            current_memory = _generation_row(
                connection,
                "memory_embedding_generations",
                memory_generation_id,
            )
            current_persona = _generation_row(
                connection,
                "persona_embedding_generations",
                persona_generation_id,
            )
            _require_building(current_memory)
            _require_building(current_persona)
            _activate_memory_with_connection(
                connection,
                current=current_memory,
                generation_id=memory_generation_id,
                encoded=encoded_memory,
                now=now,
            )
            _activate_persona_with_connection(
                connection,
                current=current_persona,
                generation_id=persona_generation_id,
                encoded=encoded_persona,
                now=now,
            )
        return (
            self.get_memory_generation(memory_generation_id),
            self.get_persona_generation(persona_generation_id),
        )

    def fail_memory_generation(self, generation_id: str, failure_code: str) -> EmbeddingGeneration:
        self._fail_generation("memory_embedding_generations", generation_id, failure_code)
        return self.get_memory_generation(generation_id)

    def fail_persona_generation(self, generation_id: str, failure_code: str) -> EmbeddingGeneration:
        self._fail_generation("persona_embedding_generations", generation_id, failure_code)
        return self.get_persona_generation(generation_id)

    def fail_reflection_generation(
        self, generation_id: str, failure_code: str
    ) -> EmbeddingGeneration:
        table = "memory_reflection_embedding_generations"
        self._fail_generation(table, generation_id, failure_code)
        return self._get_derived_generation(table, EmbeddingCorpus.REFLECTION, generation_id)

    def fail_persona_impression_generation(
        self, generation_id: str, failure_code: str
    ) -> EmbeddingGeneration:
        table = "memory_persona_impression_embedding_generations"
        self._fail_generation(table, generation_id, failure_code)
        return self._get_derived_generation(
            table,
            EmbeddingCorpus.PERSONA_IMPRESSION,
            generation_id,
        )

    def get_memory_generation(self, generation_id: str) -> EmbeddingGeneration:
        row = _generation_row(
            self._database.connection,
            "memory_embedding_generations",
            _identifier(generation_id, "generation_id"),
        )
        return _generation_from_row(row, EmbeddingCorpus.MEMORY)

    def get_persona_generation(self, generation_id: str) -> EmbeddingGeneration:
        row = _generation_row(
            self._database.connection,
            "persona_embedding_generations",
            _identifier(generation_id, "generation_id"),
        )
        return _generation_from_row(row, EmbeddingCorpus.PERSONA)

    def get_reflection_generation(self, generation_id: str) -> EmbeddingGeneration:
        return self._get_derived_generation(
            "memory_reflection_embedding_generations",
            EmbeddingCorpus.REFLECTION,
            generation_id,
        )

    def get_persona_impression_generation(self, generation_id: str) -> EmbeddingGeneration:
        return self._get_derived_generation(
            "memory_persona_impression_embedding_generations",
            EmbeddingCorpus.PERSONA_IMPRESSION,
            generation_id,
        )

    def _get_derived_generation(
        self,
        table: str,
        corpus: EmbeddingCorpus,
        generation_id: str,
    ) -> EmbeddingGeneration:
        row = _generation_row(
            self._database.connection,
            table,
            _identifier(generation_id, "generation_id"),
        )
        return _generation_from_row(row, corpus)

    def get_active_memory_generation(
        self, profile_id: str = DEFAULT_PROFILE_ID
    ) -> EmbeddingGeneration | None:
        row = self._database.connection.execute(
            """
            SELECT *, profile_id AS scope_id
            FROM memory_embedding_generations
            WHERE profile_id = ? AND status = 'active'
            """,
            (_identifier(profile_id, "profile_id"),),
        ).fetchone()
        return None if row is None else _generation_from_row(row, EmbeddingCorpus.MEMORY)

    def get_active_persona_generation(self, persona_id: str) -> EmbeddingGeneration | None:
        row = self._database.connection.execute(
            """
            SELECT *, persona_id AS scope_id
            FROM persona_embedding_generations
            WHERE persona_id = ? AND status = 'active'
            """,
            (_identifier(persona_id, "persona_id"),),
        ).fetchone()
        return None if row is None else _generation_from_row(row, EmbeddingCorpus.PERSONA)

    def get_active_reflection_generation(
        self, profile_id: str = DEFAULT_PROFILE_ID
    ) -> EmbeddingGeneration | None:
        return self._get_active_derived_generation(
            "memory_reflection_embedding_generations",
            EmbeddingCorpus.REFLECTION,
            profile_id,
        )

    def get_active_persona_impression_generation(
        self, profile_id: str = DEFAULT_PROFILE_ID
    ) -> EmbeddingGeneration | None:
        return self._get_active_derived_generation(
            "memory_persona_impression_embedding_generations",
            EmbeddingCorpus.PERSONA_IMPRESSION,
            profile_id,
        )

    def _get_active_derived_generation(
        self,
        table: str,
        corpus: EmbeddingCorpus,
        profile_id: str,
    ) -> EmbeddingGeneration | None:
        row = self._database.connection.execute(
            f"""
            SELECT *, profile_id AS scope_id FROM {table}
            WHERE profile_id = ? AND status = 'active'
            """,
            (_identifier(profile_id, "profile_id"),),
        ).fetchone()
        return None if row is None else _generation_from_row(row, corpus)

    def upsert_memory_vector(
        self,
        generation_id: str,
        version_id: str,
        vector: Sequence[float],
    ) -> StoredVector:
        generation = self.get_memory_generation(generation_id)
        blob, vector_hash = encode_vector(vector, dimension=generation.dimension)
        now_value = self._clock()
        now = encode_utc(now_value)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT g.id AS memory_id
                FROM memory_embedding_generations AS eg
                JOIN memory_groups AS g ON g.profile_id = eg.profile_id
                JOIN memory_versions AS v
                  ON v.id = g.current_version_id AND v.id = ?
                WHERE eg.id = ? AND eg.status IN ('building', 'active')
                  AND g.status = 'active'
                """,
                (version_id, generation_id),
            ).fetchone()
            if row is None:
                raise StorageConflictError("vector target is not a current active memory version")
            connection.execute(
                """
                DELETE FROM memory_vectors
                WHERE generation_id = ? AND version_id IN (
                    SELECT id FROM memory_versions WHERE memory_id = ? AND id <> ?
                )
                """,
                (generation_id, row["memory_id"], version_id),
            )
            connection.execute(
                """
                INSERT INTO memory_vectors(
                    generation_id, version_id, vector_blob, vector_hash, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(generation_id, version_id) DO UPDATE SET
                    vector_blob = excluded.vector_blob,
                    vector_hash = excluded.vector_hash,
                    created_at = excluded.created_at
                """,
                (generation_id, version_id, blob, vector_hash, now),
            )
            self._refresh_count(connection, "memory", generation_id, now)
        return StoredVector(
            generation_id=generation_id,
            target_id=version_id,
            vector=decode_vector(blob, vector_hash, dimension=generation.dimension),
            vector_hash=vector_hash,
            created_at=now_value,
        )

    def upsert_persona_vector(
        self,
        generation_id: str,
        knowledge_id: str,
        vector: Sequence[float],
    ) -> StoredVector:
        generation = self.get_persona_generation(generation_id)
        blob, vector_hash = encode_vector(vector, dimension=generation.dimension)
        now_value = self._clock()
        now = encode_utc(now_value)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM persona_embedding_generations AS eg
                JOIN persona_knowledge AS p
                  ON p.persona_id = eg.persona_id AND p.id = ?
                WHERE eg.id = ? AND eg.status IN ('building', 'active') AND p.active = 1
                """,
                (knowledge_id, generation_id),
            ).fetchone()
            if row is None:
                raise StorageConflictError("vector target is not active persona knowledge")
            connection.execute(
                """
                INSERT INTO persona_vectors(
                    generation_id, knowledge_id, vector_blob, vector_hash, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(generation_id, knowledge_id) DO UPDATE SET
                    vector_blob = excluded.vector_blob,
                    vector_hash = excluded.vector_hash,
                    created_at = excluded.created_at
                """,
                (generation_id, knowledge_id, blob, vector_hash, now),
            )
            self._refresh_count(connection, "persona", generation_id, now)
        return StoredVector(
            generation_id=generation_id,
            target_id=knowledge_id,
            vector=decode_vector(blob, vector_hash, dimension=generation.dimension),
            vector_hash=vector_hash,
            created_at=now_value,
        )

    def upsert_reflection_vector(
        self,
        generation_id: str,
        version_id: str,
        vector: Sequence[float],
    ) -> StoredVector:
        return self._upsert_derived_vector(
            EmbeddingCorpus.REFLECTION,
            generation_id,
            version_id,
            vector,
        )

    def upsert_persona_impression_vector(
        self,
        generation_id: str,
        version_id: str,
        vector: Sequence[float],
    ) -> StoredVector:
        return self._upsert_derived_vector(
            EmbeddingCorpus.PERSONA_IMPRESSION,
            generation_id,
            version_id,
            vector,
        )

    def _upsert_derived_vector(
        self,
        corpus: EmbeddingCorpus,
        generation_id: str,
        version_id: str,
        vector: Sequence[float],
    ) -> StoredVector:
        table, groups, versions, vectors, group_fk, statuses = _derived_vector_spec(corpus)
        generation = self._get_derived_generation(table, corpus, generation_id)
        blob, vector_hash = encode_vector(vector, dimension=generation.dimension)
        now_value = self._clock()
        now = encode_utc(now_value)
        status_placeholders = ",".join("?" for _ in statuses)
        with self._database.transaction() as connection:
            row = connection.execute(
                f"""
                SELECT g.id AS group_id
                FROM {table} AS eg
                JOIN {groups} AS g ON g.profile_id = eg.profile_id
                JOIN {versions} AS v ON v.id = g.current_version_id AND v.id = ?
                WHERE eg.id = ? AND eg.status IN ('building', 'active')
                  AND g.status IN ({status_placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_conflicts AS c
                      WHERE c.target_layer = ? AND c.target_group_id = g.id
                        AND c.status = 'open'
                  )
                """,
                (version_id, generation_id, *statuses, _derived_layer_value(corpus)),
            ).fetchone()
            if row is None:
                raise StorageConflictError("vector target is not current derived memory")
            connection.execute(
                f"""
                DELETE FROM {vectors}
                WHERE generation_id = ? AND version_id IN (
                    SELECT id FROM {versions} WHERE {group_fk} = ? AND id <> ?
                )
                """,
                (generation_id, row["group_id"], version_id),
            )
            connection.execute(
                f"""
                INSERT INTO {vectors}(
                    generation_id, version_id, vector_blob, vector_hash, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(generation_id, version_id) DO UPDATE SET
                    vector_blob = excluded.vector_blob,
                    vector_hash = excluded.vector_hash,
                    created_at = excluded.created_at
                """,
                (generation_id, version_id, blob, vector_hash, now),
            )
            self._refresh_count(connection, corpus.value, generation_id, now)
        return StoredVector(
            generation_id=generation_id,
            target_id=version_id,
            vector=decode_vector(blob, vector_hash, dimension=generation.dimension),
            vector_hash=vector_hash,
            created_at=now_value,
        )

    def load_memory_vectors(self, profile_id: str = DEFAULT_PROFILE_ID) -> tuple[StoredVector, ...]:
        rows = self._database.connection.execute(
            """
            SELECT mv.generation_id, mv.version_id AS target_id, mv.vector_blob,
                   mv.vector_hash, mv.created_at, eg.dimension
            FROM memory_embedding_generations AS eg
            JOIN memory_vectors AS mv ON mv.generation_id = eg.id
            JOIN memory_groups AS g
              ON g.current_version_id = mv.version_id AND g.profile_id = eg.profile_id
            WHERE eg.profile_id = ? AND eg.status = 'active' AND g.status = 'active'
            ORDER BY mv.version_id
            """,
            (_identifier(profile_id, "profile_id"),),
        ).fetchall()
        return tuple(_stored_vector_from_row(row) for row in rows)

    def load_persona_vectors(self, persona_id: str) -> tuple[StoredVector, ...]:
        rows = self._database.connection.execute(
            """
            SELECT pv.generation_id, pv.knowledge_id AS target_id, pv.vector_blob,
                   pv.vector_hash, pv.created_at, eg.dimension
            FROM persona_embedding_generations AS eg
            JOIN persona_vectors AS pv ON pv.generation_id = eg.id
            JOIN persona_knowledge AS p
              ON p.id = pv.knowledge_id AND p.persona_id = eg.persona_id
            WHERE eg.persona_id = ? AND eg.status = 'active' AND p.active = 1
            ORDER BY pv.knowledge_id
            """,
            (_identifier(persona_id, "persona_id"),),
        ).fetchall()
        return tuple(_stored_vector_from_row(row) for row in rows)

    def load_reflection_vectors(
        self, profile_id: str = DEFAULT_PROFILE_ID
    ) -> tuple[StoredVector, ...]:
        return self._load_derived_vectors(EmbeddingCorpus.REFLECTION, profile_id)

    def load_persona_impression_vectors(
        self, profile_id: str = DEFAULT_PROFILE_ID
    ) -> tuple[StoredVector, ...]:
        return self._load_derived_vectors(EmbeddingCorpus.PERSONA_IMPRESSION, profile_id)

    def _load_derived_vectors(
        self,
        corpus: EmbeddingCorpus,
        profile_id: str,
    ) -> tuple[StoredVector, ...]:
        table, groups, _versions, vectors, _group_fk, statuses = _derived_vector_spec(corpus)
        status_placeholders = ",".join("?" for _ in statuses)
        rows = self._database.connection.execute(
            f"""
            SELECT dv.generation_id, dv.version_id AS target_id, dv.vector_blob,
                   dv.vector_hash, dv.created_at, eg.dimension
            FROM {table} AS eg
            JOIN {vectors} AS dv ON dv.generation_id = eg.id
            JOIN {groups} AS g
              ON g.current_version_id = dv.version_id AND g.profile_id = eg.profile_id
            WHERE eg.profile_id = ? AND eg.status = 'active'
              AND g.status IN ({status_placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM memory_conflicts AS c
                  WHERE c.target_layer = ? AND c.target_group_id = g.id
                    AND c.status = 'open'
              )
            ORDER BY dv.version_id
            """,
            (
                _identifier(profile_id, "profile_id"),
                *statuses,
                _derived_layer_value(corpus),
            ),
        ).fetchall()
        return tuple(_stored_vector_from_row(row) for row in rows)

    def _fail_generation(self, table: str, generation_id: str, failure_code: str) -> None:
        generation_id = _identifier(generation_id, "generation_id")
        safe_code = _safe_code(failure_code)
        now = encode_utc(self._clock())
        with self._database.transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE {table}
                SET status = 'failed', failure_code = ?, updated_at = ?
                WHERE id = ? AND status = 'building'
                """,
                (safe_code, now, generation_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (generation_id,))
                if row.fetchone() is None:
                    raise StorageNotFoundError("embedding generation does not exist")
                raise StorageConflictError("only a building generation can fail")

    @staticmethod
    def _refresh_count(
        connection: sqlite3.Connection, corpus: str, generation_id: str, now: str
    ) -> None:
        mapping = {
            "memory": ("memory_embedding_generations", "memory_vectors"),
            "reflection": (
                "memory_reflection_embedding_generations",
                "memory_reflection_vectors",
            ),
            "persona_impression": (
                "memory_persona_impression_embedding_generations",
                "memory_persona_impression_vectors",
            ),
            "persona": ("persona_embedding_generations", "persona_vectors"),
        }
        try:
            table, vectors = mapping[corpus]
        except KeyError as exc:
            raise StorageValidationError("unknown vector corpus") from exc
        connection.execute(
            f"""
            UPDATE {table}
            SET item_count = (SELECT COUNT(*) FROM {vectors} WHERE generation_id = ?),
                updated_at = ?
            WHERE id = ?
            """,
            (generation_id, now, generation_id),
        )


def _generation_values(
    model_name: str,
    model_commit: str,
    model_sha256: str,
    calibration_threshold: float,
    dimension: int,
) -> tuple[object, ...]:
    model_name = _identifier(model_name, "model_name")
    model_commit = _identifier(model_commit, "model_commit")
    sha256 = str(model_sha256).strip().lower()
    if not _SHA256_RE.fullmatch(sha256):
        raise StorageValidationError("model_sha256 must be a lowercase SHA-256 digest")
    if dimension != VECTOR_DIMENSION:
        raise StorageValidationError("dimension must be 512")
    if isinstance(calibration_threshold, bool):
        raise StorageValidationError("calibration_threshold must be numeric")
    try:
        threshold = float(calibration_threshold)
    except (TypeError, ValueError) as exc:
        raise StorageValidationError("calibration_threshold must be numeric") from exc
    if not math.isfinite(threshold) or not -1.0 <= threshold <= 1.0:
        raise StorageValidationError("calibration_threshold must be between -1 and 1")
    return model_name, model_commit, dimension, sha256, threshold


def _encode_vectors(
    vectors: Mapping[str, Sequence[float]], dimension: int
) -> dict[str, tuple[bytes, str]]:
    return {
        _identifier(target_id, "vector target id"): encode_vector(vector, dimension=dimension)
        for target_id, vector in vectors.items()
    }


def _activate_memory_with_connection(
    connection: sqlite3.Connection,
    *,
    current: sqlite3.Row,
    generation_id: str,
    encoded: Mapping[str, tuple[bytes, str]],
    now: str,
) -> None:
    """Activate one user-memory generation inside the caller's transaction."""

    valid_targets = {
        str(row["id"])
        for row in connection.execute(
            """
            SELECT v.id
            FROM memory_groups AS g
            JOIN memory_versions AS v ON v.id = g.current_version_id
            WHERE g.profile_id = ? AND g.status = 'active'
            ORDER BY g.updated_at DESC, g.id
            LIMIT ?
            """,
            (current["scope_id"], MAX_INDEX_DOCUMENTS),
        ).fetchall()
    }
    _require_exact_targets(encoded, valid_targets)
    connection.execute("DELETE FROM memory_vectors WHERE generation_id = ?", (generation_id,))
    connection.executemany(
        """
        INSERT INTO memory_vectors(
            generation_id, version_id, vector_blob, vector_hash, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (generation_id, target_id, blob, vector_hash, now)
            for target_id, (blob, vector_hash) in encoded.items()
        ),
    )
    connection.execute(
        """
        UPDATE memory_embedding_generations
        SET status = 'retired', updated_at = ?
        WHERE profile_id = ? AND status = 'active' AND id <> ?
        """,
        (now, current["scope_id"], generation_id),
    )
    connection.execute(
        """
        UPDATE memory_embedding_generations
        SET status = 'active', item_count = ?, failure_code = NULL,
            updated_at = ?, activated_at = ?
        WHERE id = ?
        """,
        (len(encoded), now, now, generation_id),
    )


def _activate_persona_with_connection(
    connection: sqlite3.Connection,
    *,
    current: sqlite3.Row,
    generation_id: str,
    encoded: Mapping[str, tuple[bytes, str]],
    now: str,
) -> None:
    """Activate one persona generation inside the caller's transaction."""

    valid_targets = {
        str(row["id"])
        for row in connection.execute(
            """
            SELECT id
            FROM persona_knowledge
            WHERE persona_id = ? AND active = 1
            ORDER BY updated_at DESC, id
            LIMIT ?
            """,
            (current["scope_id"], MAX_INDEX_DOCUMENTS),
        ).fetchall()
    }
    _require_exact_targets(encoded, valid_targets)
    connection.execute("DELETE FROM persona_vectors WHERE generation_id = ?", (generation_id,))
    connection.executemany(
        """
        INSERT INTO persona_vectors(
            generation_id, knowledge_id, vector_blob, vector_hash, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (generation_id, target_id, blob, vector_hash, now)
            for target_id, (blob, vector_hash) in encoded.items()
        ),
    )
    connection.execute(
        """
        UPDATE persona_embedding_generations
        SET status = 'retired', updated_at = ?
        WHERE persona_id = ? AND status = 'active' AND id <> ?
        """,
        (now, current["scope_id"], generation_id),
    )
    connection.execute(
        """
        UPDATE persona_embedding_generations
        SET status = 'active', item_count = ?, failure_code = NULL,
            updated_at = ?, activated_at = ?
        WHERE id = ?
        """,
        (len(encoded), now, now, generation_id),
    )


def _activate_derived_with_connection(
    connection: sqlite3.Connection,
    *,
    corpus: EmbeddingCorpus,
    current: sqlite3.Row,
    generation_id: str,
    encoded: Mapping[str, tuple[bytes, str]],
    now: str,
) -> None:
    table, groups, versions, vectors, group_fk, statuses = _derived_vector_spec(corpus)
    status_placeholders = ",".join("?" for _ in statuses)
    layer = _derived_layer_value(corpus)
    valid_targets = {
        str(row["id"])
        for row in connection.execute(
            f"""
            SELECT v.id
            FROM {groups} AS g
            JOIN {versions} AS v ON v.id = g.current_version_id
            WHERE g.profile_id = ? AND g.status IN ({status_placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM memory_conflicts AS c
                  WHERE c.target_layer = ? AND c.target_group_id = g.id
                    AND c.status = 'open'
              )
            ORDER BY g.updated_at DESC, g.id
            LIMIT ?
            """,
            (current["scope_id"], *statuses, layer, MAX_INDEX_DOCUMENTS),
        ).fetchall()
    }
    _require_exact_targets(encoded, valid_targets)
    connection.execute(f"DELETE FROM {vectors} WHERE generation_id = ?", (generation_id,))
    connection.executemany(
        f"""
        INSERT INTO {vectors}(
            generation_id, version_id, vector_blob, vector_hash, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (generation_id, target_id, blob, vector_hash, now)
            for target_id, (blob, vector_hash) in encoded.items()
        ),
    )
    connection.execute(
        f"""
        UPDATE {table}
        SET status = 'retired', updated_at = ?
        WHERE profile_id = ? AND status = 'active' AND id <> ?
        """,
        (now, current["scope_id"], generation_id),
    )
    connection.execute(
        f"""
        UPDATE {table}
        SET status = 'active', item_count = ?, failure_code = NULL,
            updated_at = ?, activated_at = ?
        WHERE id = ?
        """,
        (len(encoded), now, now, generation_id),
    )


def _require_exact_targets(
    encoded: Mapping[str, tuple[bytes, str]], valid_targets: set[str]
) -> None:
    targets = set(encoded)
    if targets != valid_targets:
        raise StorageValidationError("generation vectors must exactly match active current records")


def _generation_row(connection: sqlite3.Connection, table: str, generation_id: str) -> sqlite3.Row:
    scope_column = "persona_id" if table == "persona_embedding_generations" else "profile_id"
    row = connection.execute(
        f"SELECT *, {scope_column} AS scope_id FROM {table} WHERE id = ?",
        (generation_id,),
    ).fetchone()
    if row is None:
        raise StorageNotFoundError("embedding generation does not exist")
    return row


def _derived_vector_spec(
    corpus: EmbeddingCorpus,
) -> tuple[str, str, str, str, str, tuple[str, ...]]:
    if corpus is EmbeddingCorpus.REFLECTION:
        return (
            "memory_reflection_embedding_generations",
            "memory_reflections",
            "memory_reflection_versions",
            "memory_reflection_vectors",
            "reflection_id",
            ("confirmed",),
        )
    if corpus is EmbeddingCorpus.PERSONA_IMPRESSION:
        return (
            "memory_persona_impression_embedding_generations",
            "memory_persona_impressions",
            "memory_persona_impression_versions",
            "memory_persona_impression_vectors",
            "impression_id",
            ("active",),
        )
    raise StorageValidationError("corpus is not a derived memory corpus")


def _derived_layer_value(corpus: EmbeddingCorpus) -> str:
    if corpus is EmbeddingCorpus.REFLECTION:
        return "reflection"
    if corpus is EmbeddingCorpus.PERSONA_IMPRESSION:
        return "persona"
    raise StorageValidationError("corpus is not a derived memory corpus")


def _require_building(row: sqlite3.Row) -> None:
    if row["status"] != EmbeddingGenerationStatus.BUILDING.value:
        raise StorageConflictError("only a building generation can be activated")


def _generation_from_row(row: sqlite3.Row, corpus: EmbeddingCorpus) -> EmbeddingGeneration:
    return EmbeddingGeneration(
        generation_id=str(row["id"]),
        corpus=corpus,
        scope_id=str(row["scope_id"]),
        model_name=str(row["model_name"]),
        model_commit=str(row["model_commit"]),
        dimension=int(row["dimension"]),
        model_sha256=str(row["model_sha256"]),
        calibration_threshold=float(row["calibration_threshold"]),
        status=EmbeddingGenerationStatus(row["status"]),
        item_count=int(row["item_count"]),
        failure_code=row["failure_code"],
        created_at=_required_datetime(row["created_at"]),
        updated_at=_required_datetime(row["updated_at"]),
        activated_at=decode_utc(row["activated_at"]),
    )


def _stored_vector_from_row(row: sqlite3.Row) -> StoredVector:
    vector_hash = str(row["vector_hash"])
    return StoredVector(
        generation_id=str(row["generation_id"]),
        target_id=str(row["target_id"]),
        vector=decode_vector(row["vector_blob"], vector_hash, dimension=int(row["dimension"])),
        vector_hash=vector_hash,
        created_at=_required_datetime(row["created_at"]),
    )


def _ensure_profile(connection: sqlite3.Connection, profile_id: str, now: str) -> None:
    if profile_id == DEFAULT_PROFILE_ID:
        connection.execute(
            """
            INSERT OR IGNORE INTO profiles(id, display_name, created_at, updated_at)
            VALUES (?, '用户', ?, ?)
            """,
            (DEFAULT_PROFILE_ID, now, now),
        )
    if connection.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone() is None:
        raise StorageNotFoundError("profile does not exist")


def _identifier(value: str, field: str) -> str:
    value = str(value).strip()
    if not value or len(value) > 200:
        raise StorageValidationError(f"{field} must be a non-empty identifier")
    return value


def _safe_code(value: str) -> str:
    safe = str(value).replace("\r", " ").replace("\n", " ")[:128]
    if not safe:
        raise StorageValidationError("failure_code must not be blank")
    return safe


def _required_datetime(value: str) -> datetime:
    decoded = decode_utc(value)
    assert decoded is not None
    return decoded
