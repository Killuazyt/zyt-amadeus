"""Aggregate-only local index and persona acceptance for P5B."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from contextlib import suppress

from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.embedding_backend import FastEmbedEmbeddingBackend
from amadeus_desktop.embedding_calibration import calibrate_backend
from amadeus_desktop.embedding_model import PINNED_MODEL, resolve_runtime_model_directory
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.persona_repository import PersonaRepository
from amadeus_desktop.tools.embedding_acceptance import _deny_network
from amadeus_desktop.vector_runtime import VectorCacheSnapshot, VectorRecord
from amadeus_desktop.vector_store import VectorStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amadeus-index-acceptance",
        description="Rebuild and verify local P5B indexes without exposing content.",
    )
    parser.add_argument("command", choices=("rebuild", "persona-smoke"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    command = build_parser().parse_args(argv).command
    paths = AppPaths.for_current_user()
    try:
        with _deny_network():
            result = (
                rebuild_local_indexes(paths)
                if command == "rebuild"
                else run_local_persona_smoke(paths)
            )
    except Exception:
        result = {
            "command": command,
            "status": "failed",
            "error_category": "local_index_acceptance_failed",
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


def rebuild_local_indexes(paths: AppPaths) -> dict[str, object]:
    """Build both generations from current rows while emitting only aggregate metadata."""

    database = SQLiteDatabase(
        paths.database_file,
        backup_dir=paths.migration_backup_directory,
    ).open()
    backend = None
    user_generation = None
    persona_generation = None
    try:
        memories = MemoryStore(database)
        personas = PersonaRepository(database)
        vectors = VectorStore(database)
        vectors.recover_interrupted_generations()
        user_documents = memories.list_active_documents(limit=10_000)
        persona_documents = personas.list_active_documents("kurisu", limit=10_000)
        prepared = resolve_runtime_model_directory(paths.embedding_model_directory)
        backend = FastEmbedEmbeddingBackend(prepared)
        calibration = calibrate_backend(backend).calibration
        user_generation = vectors.begin_memory_generation(
            model_name=PINNED_MODEL.api_name,
            model_commit=PINNED_MODEL.revision,
            model_sha256=PINNED_MODEL.onnx_sha256,
            calibration_threshold=calibration.threshold,
            dimension=PINNED_MODEL.dimension,
        )
        persona_generation = vectors.begin_persona_generation(
            persona_id="kurisu",
            model_name=PINNED_MODEL.api_name,
            model_commit=PINNED_MODEL.revision,
            model_sha256=PINNED_MODEL.onnx_sha256,
            calibration_threshold=calibration.threshold,
            dimension=PINNED_MODEL.dimension,
        )
        user_embeddings = backend.embed_documents(
            tuple(record.current_version.content for record in user_documents)
        )
        persona_embeddings = backend.embed_documents(
            tuple(record.content for record in persona_documents)
        )
        active_user, active_persona = vectors.activate_generations_atomically(
            user_generation.generation_id,
            {
                record.current_version.version_id: embedding
                for record, embedding in zip(
                    user_documents,
                    user_embeddings,
                    strict=True,
                )
            },
            persona_generation.generation_id,
            {
                record.knowledge_id: embedding
                for record, embedding in zip(
                    persona_documents,
                    persona_embeddings,
                    strict=True,
                )
            },
        )
        return {
            "command": "rebuild",
            "status": "passed",
            "error_category": None,
            "user_index_count": active_user.item_count,
            "persona_index_count": active_persona.item_count,
            "calibration_gap": round(calibration.gap, 6),
            "calibration_threshold": round(calibration.threshold, 6),
        }
    except Exception:
        vectors = VectorStore(database)
        if user_generation is not None:
            with suppress(Exception):
                vectors.fail_memory_generation(
                    user_generation.generation_id,
                    "acceptance_failed",
                )
        if persona_generation is not None:
            with suppress(Exception):
                vectors.fail_persona_generation(
                    persona_generation.generation_id,
                    "acceptance_failed",
                )
        raise
    finally:
        if backend is not None:
            backend.close()
        database.close()


def run_local_persona_smoke(paths: AppPaths) -> dict[str, object]:
    """Check every local persona row against its own active vector cache."""

    database = SQLiteDatabase(
        paths.database_file,
        backup_dir=paths.migration_backup_directory,
    ).open()
    backend = None
    try:
        personas = PersonaRepository(database)
        vectors = VectorStore(database)
        persona_documents = personas.list_active_documents("kurisu", limit=10_000)
        persona_generation = vectors.get_active_persona_generation("kurisu")
        user_generation = vectors.get_active_memory_generation()
        if not persona_documents or persona_generation is None or user_generation is None:
            raise ValueError("active_generation_missing")
        persona_snapshot = VectorCacheSnapshot.build(
            persona_generation.generation_id,
            (
                VectorRecord(item.target_id, item.vector)
                for item in vectors.load_persona_vectors("kurisu")
            ),
        )
        user_snapshot = VectorCacheSnapshot.build(
            user_generation.generation_id,
            (VectorRecord(item.target_id, item.vector) for item in vectors.load_memory_vectors()),
        )
        prepared = resolve_runtime_model_directory(paths.embedding_model_directory)
        backend = FastEmbedEmbeddingBackend(prepared)
        correct = 0
        cross_library_misses = 0
        for record in persona_documents:
            query = backend.embed_query(record.content)
            persona_hits = persona_snapshot.search(
                query,
                limit=1,
                minimum_score=persona_generation.calibration_threshold,
            )
            user_hits = user_snapshot.search(
                query,
                limit=30,
                minimum_score=user_generation.calibration_threshold,
            )
            correct += int(bool(persona_hits) and persona_hits[0].target_id == record.knowledge_id)
            # Each query is the exact content of a persona row. Any user-memory
            # vector above the calibrated threshold is a cross-corpus mis-hit.
            cross_library_misses += len(user_hits)
        status = (
            "passed"
            if correct == len(persona_documents) and cross_library_misses == 0
            else "failed"
        )
        return {
            "command": "persona-smoke",
            "status": status,
            "error_category": None if status == "passed" else "persona_recall_failed",
            "fragment_count": len(persona_documents),
            "query_count": len(persona_documents),
            "correct_count": correct,
            "cross_library_miss_count": cross_library_misses,
        }
    finally:
        if backend is not None:
            backend.close()
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
