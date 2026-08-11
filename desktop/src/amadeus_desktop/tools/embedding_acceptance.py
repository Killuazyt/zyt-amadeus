"""Offline model smoke and 10k hybrid-retrieval performance acceptance."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import socket
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication

from amadeus_desktop.chat_models import (
    ChatMessage,
    ConversationTurn,
    MessageRole,
    MessageStatus,
    PreparedPrompt,
)
from amadeus_desktop.conversation_store import ConversationStore
from amadeus_desktop.data_runtime import SerialDataThread
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.deep_memory_store import DeepMemoryStore
from amadeus_desktop.embedding_backend import (
    CPU_PROVIDER,
    EmbeddingBackend,
    FastEmbedEmbeddingBackend,
)
from amadeus_desktop.embedding_calibration import CALIBRATION_CASES, calibrate_backend
from amadeus_desktop.embedding_model import (
    MODEL_API_NAME,
    MODEL_DIMENSION,
    MODEL_ONNX_SHA256,
    MODEL_REVISION,
    ModelVerification,
    inspect_model,
    resolve_runtime_model_directory,
    verify_model,
)
from amadeus_desktop.local_data_service import LocalDataService, create_local_data_stores
from amadeus_desktop.memory_search import (
    build_search_text,
    exact_memory_hash,
    normalize_memory_content,
)
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.storage_models import DEFAULT_PROFILE_ID, encode_utc
from amadeus_desktop.vector_index import VectorIndexCoordinator, VectorIndexRepositories
from amadeus_desktop.vector_runtime import (
    PriorityVectorRuntime,
    VectorCacheSet,
    VectorCacheSnapshot,
    VectorCorpus,
    VectorRecord,
)
from amadeus_desktop.vector_store import VectorStore

MEMORY_BENCHMARK_ITEMS = 10_000
PERSONA_BENCHMARK_ITEMS = 40
REFLECTION_BENCHMARK_ITEMS = 20
PERSONA_IMPRESSION_BENCHMARK_ITEMS = 10
BENCHMARK_WARMUPS = 5
BENCHMARK_QUERY_COUNT = 100
BENCHMARK_P95_LIMIT_MS = 300.0
EXPECTED_MEMORY_MATRIX_BYTES = MEMORY_BENCHMARK_ITEMS * MODEL_DIMENSION * 4

BENCHMARK_QUERIES = tuple(
    f"请检索公开合成偏好记录编号{index:05d}" for index in range(BENCHMARK_QUERY_COUNT)
)
BENCHMARK_WARMUP_QUERIES = tuple(
    f"离线检索预热请求{index + 1}" for index in range(BENCHMARK_WARMUPS)
)

BackendFactory = Callable[[Path], EmbeddingBackend]
Verifier = Callable[..., ModelVerification]


class AcceptanceFailure(RuntimeError):
    def __init__(self, error_category: str) -> None:
        super().__init__(error_category)
        self.error_category = error_category


class OfflineNetworkAttempt(AcceptanceFailure):
    def __init__(self) -> None:
        super().__init__("offline_network_attempted")


def run_offline_smoke(
    prepared_directory: Path,
    *,
    backend_factory: BackendFactory | None = None,
    verifier: Verifier | None = None,
) -> dict[str, object]:
    """Verify CPU/512-D inference, calibration, and paired rewrite recall."""

    factory = backend_factory or FastEmbedEmbeddingBackend
    with _deny_network():
        verification = (verifier or verify_model)(
            prepared_directory,
            backend_factory=factory,
        )
        if not verification.ready:
            raise AcceptanceFailure(verification.error_category or "model_verification_failed")

        load_started = time.perf_counter()
        backend = factory(prepared_directory)
        load_ms = _elapsed_ms(load_started)
        try:
            _require_backend_identity(backend)
            calibration = calibrate_backend(backend)
            documents = tuple(
                document
                for case in CALIBRATION_CASES
                for document in (case.positive_document, case.negative_document)
            )
            document_vectors = backend.embed_documents(documents)
            successes = 0
            for index, case in enumerate(CALIBRATION_CASES):
                snapshot = VectorCacheSnapshot.build(
                    f"rewrite-{index}",
                    (
                        VectorRecord(f"positive-{index}", document_vectors[index * 2]),
                        VectorRecord(f"negative-{index}", document_vectors[index * 2 + 1]),
                    ),
                )
                hits = snapshot.search(
                    backend.embed_query(case.query),
                    limit=1,
                    minimum_score=calibration.calibration.threshold,
                )
                successes += int(bool(hits) and hits[0].target_id == f"positive-{index}")
        finally:
            backend.close()

    if successes != len(CALIBRATION_CASES):
        raise AcceptanceFailure("rewrite_recall_failed")
    result = calibration.calibration
    return {
        "command": "offline-smoke",
        "status": "passed",
        "model": MODEL_API_NAME,
        "revision": MODEL_REVISION,
        "provider": CPU_PROVIDER,
        "dimension": MODEL_DIMENSION,
        "model_load_ms": _rounded(load_ms),
        "positive_samples": result.positive_count,
        "negative_samples": result.negative_count,
        "worst_positive": _rounded(result.worst_positive, 6),
        "best_negative": _rounded(result.best_negative, 6),
        "calibration_gap": _rounded(result.gap, 6),
        "calibration_threshold": _rounded(result.threshold, 6),
        "rewrite_recall_total": len(CALIBRATION_CASES),
        "rewrite_recall_successes": successes,
    }


def run_benchmark(
    prepared_directory: Path,
    *,
    backend_factory: BackendFactory | None = None,
    memory_items: int = MEMORY_BENCHMARK_ITEMS,
    persona_items: int = PERSONA_BENCHMARK_ITEMS,
    warmup_queries: Sequence[str] = BENCHMARK_WARMUP_QUERIES,
    measured_queries: Sequence[str] = BENCHMARK_QUERIES,
    p95_limit_ms: float = BENCHMARK_P95_LIMIT_MS,
) -> dict[str, object]:
    """Measure query embedding plus independent user/persona cache scans."""

    if memory_items <= 0 or persona_items <= 0:
        raise ValueError("benchmark cache sizes must be positive")
    if len(set(measured_queries)) != len(measured_queries) or not measured_queries:
        raise ValueError("benchmark queries must be non-empty and distinct")
    factory = backend_factory or FastEmbedEmbeddingBackend
    with _deny_network():
        structural = inspect_model(prepared_directory)
        if backend_factory is None and not structural.ready:
            raise AcceptanceFailure(structural.error_category or "model_verification_failed")

        load_started = time.perf_counter()
        backend = factory(prepared_directory)
        load_ms = _elapsed_ms(load_started)
        try:
            _require_backend_identity(backend)
            cache_started = time.perf_counter()
            caches = _build_benchmark_caches(memory_items, persona_items)
            cache_build_ms = _elapsed_ms(cache_started)
            memory_snapshot = caches.snapshot(VectorCorpus.USER_MEMORY)
            persona_snapshot = caches.snapshot(VectorCorpus.PERSONA_KNOWLEDGE)
            assert memory_snapshot is not None
            assert persona_snapshot is not None

            warmup_started = time.perf_counter()
            for query in warmup_queries:
                _complete_retrieval(backend, caches, query)
            warmup_ms = _elapsed_ms(warmup_started)

            latencies_ms: list[float] = []
            for query in measured_queries:
                started = time.perf_counter()
                _complete_retrieval(backend, caches, query)
                latencies_ms.append(_elapsed_ms(started))
        finally:
            backend.close()

    p50 = _percentile(latencies_ms, 0.50)
    p95 = _percentile(latencies_ms, 0.95)
    maximum = max(latencies_ms)
    status = "passed" if p95 <= p95_limit_ms else "failed"
    result: dict[str, object] = {
        "command": "benchmark",
        "status": status,
        "error_category": None if status == "passed" else "benchmark_p95_exceeded",
        "model": MODEL_API_NAME,
        "revision": MODEL_REVISION,
        "provider": CPU_PROVIDER,
        "dimension": MODEL_DIMENSION,
        "memory_items": memory_items,
        "persona_items": persona_items,
        "memory_matrix_bytes": memory_snapshot.byte_size,
        "persona_matrix_bytes": persona_snapshot.byte_size,
        "total_matrix_bytes": caches.byte_size,
        "model_load_ms": _rounded(load_ms),
        "cache_build_ms": _rounded(cache_build_ms),
        "warmup_count": len(warmup_queries),
        "warmup_ms": _rounded(warmup_ms),
        "query_count": len(latencies_ms),
        "p50_ms": _rounded(p50),
        "p95_ms": _rounded(p95),
        "max_ms": _rounded(maximum),
        "p95_limit_ms": _rounded(p95_limit_ms),
        "machine": _machine_metrics(),
    }
    if (
        memory_items == MEMORY_BENCHMARK_ITEMS
        and memory_snapshot.byte_size != EXPECTED_MEMORY_MATRIX_BYTES
    ):
        raise AcceptanceFailure("benchmark_matrix_size_mismatch")
    return result


def run_production_benchmark(
    prepared_directory: Path,
    *,
    backend_factory: BackendFactory | None = None,
    memory_items: int = MEMORY_BENCHMARK_ITEMS,
    persona_items: int = PERSONA_BENCHMARK_ITEMS,
    reflection_items: int = REFLECTION_BENCHMARK_ITEMS,
    persona_impression_items: int = PERSONA_IMPRESSION_BENCHMARK_ITEMS,
    warmup_queries: Sequence[str] = BENCHMARK_WARMUP_QUERIES,
    measured_queries: Sequence[str] = BENCHMARK_QUERIES,
    p95_limit_ms: float = BENCHMARK_P95_LIMIT_MS,
) -> dict[str, object]:
    """Measure the complete production SQLite/vector/fusion/prompt pipeline."""

    if memory_items <= 0 or memory_items > MEMORY_BENCHMARK_ITEMS:
        raise ValueError("production benchmark memory size must be between 1 and 10,000")
    if persona_items <= 0 or persona_items > MEMORY_BENCHMARK_ITEMS:
        raise ValueError("production benchmark persona size must be between 1 and 10,000")
    if reflection_items <= 0 or reflection_items > MEMORY_BENCHMARK_ITEMS:
        raise ValueError("production benchmark reflection size must be between 1 and 10,000")
    if persona_impression_items <= 0 or persona_impression_items > MEMORY_BENCHMARK_ITEMS:
        raise ValueError("production benchmark impression size must be between 1 and 10,000")
    if not measured_queries or len(set(measured_queries)) != len(measured_queries):
        raise ValueError("benchmark queries must be non-empty and distinct")
    factory = backend_factory or FastEmbedEmbeddingBackend
    structural = inspect_model(prepared_directory)
    if backend_factory is None and not structural.ready:
        raise AcceptanceFailure(structural.error_category or "model_verification_failed")

    with _deny_network(), tempfile.TemporaryDirectory(prefix="amadeus-p5b-benchmark-") as root:
        benchmark_root = Path(root)
        model_load_started = time.perf_counter()
        calibration_backend = factory(prepared_directory)
        model_load_ms = _elapsed_ms(model_load_started)
        try:
            _require_backend_identity(calibration_backend)
            calibration = calibrate_backend(calibration_backend).calibration
        finally:
            calibration_backend.close()

        seed_started = time.perf_counter()
        database_path = benchmark_root / "amadeus.sqlite3"
        backup_path = benchmark_root / "backups"
        _seed_production_database(
            database_path,
            backup_path,
            memory_items=memory_items,
            persona_items=persona_items,
            reflection_items=reflection_items,
            persona_impression_items=persona_impression_items,
            threshold=calibration.threshold,
        )
        seed_ms = _elapsed_ms(seed_started)

        application = QCoreApplication.instance() or QCoreApplication(
            ["amadeus-production-benchmark"]
        )
        data_runtime = SerialDataThread(
            lambda: create_local_data_stores(database_path, backup_path),
            resource_close=lambda stores: stores.close(),
        )
        vector_runtime = PriorityVectorRuntime(thread_name="amadeus-acceptance-vector")
        coordinator = VectorIndexCoordinator(
            data_runtime,
            vector_runtime,
            lambda: factory(prepared_directory),
            lambda resource: VectorIndexRepositories(
                resource.memories,
                resource.personas,
                resource.vectors,
                resource.deep_memories,
            ),
            backend_calibrator=lambda backend: calibrate_backend(backend).calibration.threshold,
        )
        service = LocalDataService(data_runtime, vector_query=coordinator.query)
        startup_errors: list[str] = []
        cleanup_errors: list[str] = []
        service.startup_failed.connect(startup_errors.append)
        service.start()
        try:
            if (
                not _pump_until(
                    application,
                    lambda: service.is_writable or bool(startup_errors),
                    timeout_seconds=30.0,
                )
                or startup_errors
            ):
                raise AcceptanceFailure("production_data_startup_failed")

            cache_started = time.perf_counter()
            if not coordinator.start() or not _pump_until(
                application,
                lambda: coordinator.status.category not in {"idle", "loading"},
                timeout_seconds=120.0,
            ):
                raise AcceptanceFailure("production_cache_startup_failed")
            cache_prewarm_ms = _elapsed_ms(cache_started)
            if coordinator.status.category != "ready":
                raise AcceptanceFailure("production_cache_unavailable")
            if coordinator.status.user_count != memory_items:
                raise AcceptanceFailure("production_memory_cache_count_mismatch")
            if coordinator.status.persona_count != persona_items:
                raise AcceptanceFailure("production_persona_cache_count_mismatch")
            if coordinator.status.reflection_count != reflection_items:
                raise AcceptanceFailure("production_reflection_cache_count_mismatch")
            if coordinator.status.persona_impression_count != persona_impression_items:
                raise AcceptanceFailure("production_impression_cache_count_mismatch")

            warmup_started = time.perf_counter()
            for index, query in enumerate(warmup_queries):
                _prepare_production_prompt(application, service, query, sequence=index)
            warmup_ms = _elapsed_ms(warmup_started)

            latencies_ms: list[float] = []
            correct_recall_count = 0
            cross_library_mis_hit_count = 0
            offset = len(warmup_queries)
            for index, query in enumerate(measured_queries):
                started = time.perf_counter()
                prompt = _prepare_production_prompt(
                    application,
                    service,
                    query,
                    sequence=offset + index,
                )
                latencies_ms.append(_elapsed_ms(started))
                expected_version_id = f"benchmark-version-{index:05d}"
                correct_recall_count += int(expected_version_id in prompt.user_memory_version_ids)
                cross_library_mis_hit_count += _user_query_cross_library_mis_hits(prompt)
            cache_bytes = coordinator.cache_byte_size
            active_machine = _machine_metrics()
        finally:
            try:
                coordinator.close().result(timeout=30.0)
            except Exception:
                cleanup_errors.append("coordinator")
            try:
                vector_runtime.close(timeout=30.0)
            except Exception:
                cleanup_errors.append("vector_runtime")
            if vector_runtime.alive:
                cleanup_errors.append("vector_thread")
            try:
                data_clean = service.shutdown(30_000)
            except Exception:
                cleanup_errors.append("data_runtime")
            else:
                if not data_clean or data_runtime.is_running:
                    cleanup_errors.append("data_thread")
            try:
                _verify_database_released(database_path, backup_path)
            except Exception:
                cleanup_errors.append("database_handle")
            if cleanup_errors:
                raise AcceptanceFailure("production_cleanup_failed")

    p50 = _percentile(latencies_ms, 0.50)
    p95 = _percentile(latencies_ms, 0.95)
    maximum = max(latencies_ms)
    expected_cache_bytes = (
        (memory_items + persona_items + reflection_items + persona_impression_items)
        * MODEL_DIMENSION
        * 4
    )
    if cache_bytes != expected_cache_bytes:
        raise AcceptanceFailure("production_cache_size_mismatch")
    if correct_recall_count != len(latencies_ms):
        status = "failed"
        error_category = "production_recall_mismatch"
    elif cross_library_mis_hit_count:
        status = "failed"
        error_category = "production_corpus_isolation_failed"
    elif p95 > p95_limit_ms:
        status = "failed"
        error_category = "benchmark_p95_exceeded"
    else:
        status = "passed"
        error_category = None
    return {
        "command": "production-benchmark",
        "status": status,
        "error_category": error_category,
        "model": MODEL_API_NAME,
        "revision": MODEL_REVISION,
        "provider": CPU_PROVIDER,
        "dimension": MODEL_DIMENSION,
        "memory_items": memory_items,
        "persona_items": persona_items,
        "reflection_items": reflection_items,
        "persona_impression_items": persona_impression_items,
        "memory_matrix_bytes": memory_items * MODEL_DIMENSION * 4,
        "total_cache_bytes": cache_bytes,
        "model_load_ms": _rounded(model_load_ms),
        "database_seed_ms": _rounded(seed_ms),
        "cache_prewarm_ms": _rounded(cache_prewarm_ms),
        "warmup_count": len(warmup_queries),
        "warmup_ms": _rounded(warmup_ms),
        "query_count": len(latencies_ms),
        "correct_recall_count": correct_recall_count,
        "cross_library_mis_hit_count": cross_library_mis_hit_count,
        "p50_ms": _rounded(p50),
        "p95_ms": _rounded(p95),
        "max_ms": _rounded(maximum),
        "p95_limit_ms": _rounded(p95_limit_ms),
        "machine": active_machine,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amadeus-embedding-acceptance",
        description="Run aggregate-only P5B offline embedding acceptance.",
    )
    parser.add_argument(
        "command",
        choices=("offline-smoke", "benchmark", "production-benchmark"),
    )
    parser.add_argument("--model-directory", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    local = AppPaths.for_current_user().embedding_model_directory
    prepared = args.model_directory or resolve_runtime_model_directory(local)
    try:
        if args.command == "offline-smoke":
            result = run_offline_smoke(prepared)
        elif args.command == "production-benchmark":
            result = run_production_benchmark(prepared)
        else:
            result = run_benchmark(prepared)
    except AcceptanceFailure as error:
        result = _failure_result(args.command, error.error_category)
    except MemoryError:
        result = _failure_result(args.command, "benchmark_resource_exhausted")
    except Exception:
        result = _failure_result(args.command, "embedding_acceptance_failed")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


def _build_benchmark_caches(memory_items: int, persona_items: int) -> VectorCacheSet:
    basis = tuple(_basis_vector(index) for index in range(MODEL_DIMENSION))
    memory = VectorCacheSnapshot.build(
        "benchmark-memory",
        (
            VectorRecord(f"memory-{index}", basis[index % MODEL_DIMENSION])
            for index in range(memory_items)
        ),
    )
    persona = VectorCacheSnapshot.build(
        "benchmark-persona",
        (
            VectorRecord(f"persona-{index}", basis[index % MODEL_DIMENSION])
            for index in range(persona_items)
        ),
    )
    caches = VectorCacheSet()
    caches.swap(VectorCorpus.USER_MEMORY, memory)
    caches.swap(VectorCorpus.PERSONA_KNOWLEDGE, persona)
    return caches


def _seed_production_database(
    database_path: Path,
    backup_path: Path,
    *,
    memory_items: int,
    persona_items: int,
    reflection_items: int,
    persona_impression_items: int,
    threshold: float,
) -> None:
    database = SQLiteDatabase(database_path, backup_dir=backup_path).open()
    try:
        conversations = ConversationStore(database)
        conversation = conversations.create_conversation(
            "公开合成性能验收",
            conversation_id="benchmark-conversation",
        )
        source_fact_count = min(
            memory_items,
            reflection_items + persona_impression_items,
        )
        source_messages = tuple(
            conversations.save_user_message(
                conversation.conversation_id,
                f"benchmark-source-turn-{index:05d}",
                f"benchmark-source-message-{index:05d}",
                f"公开合成深层证据编号{index:05d}",
            )
            for index in range(source_fact_count)
        )
        now = encode_utc(datetime.now(UTC))
        memory_rows: list[tuple[object, ...]] = []
        version_rows: list[tuple[object, ...]] = []
        fts_rows: list[tuple[str, str, str, str]] = []
        for index in range(memory_items):
            memory_id = f"benchmark-memory-{index:05d}"
            version_id = f"benchmark-version-{index:05d}"
            content = f"用户的公开合成偏好记录编号{index:05d}用于离线性能验收"
            normalized = normalize_memory_content(content)
            search_text = build_search_text(content)
            memory_rows.append(
                (
                    memory_id,
                    DEFAULT_PROFILE_ID,
                    "preference",
                    f"benchmark:{index:05d}",
                    version_id,
                    now,
                    now,
                )
            )
            version_rows.append(
                (
                    version_id,
                    memory_id,
                    content,
                    normalized,
                    exact_memory_hash(content),
                    search_text,
                    now,
                    int(index < source_fact_count),
                )
            )
            fts_rows.append((version_id, memory_id, DEFAULT_PROFILE_ID, search_text))
        with database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO memory_groups(
                    id, profile_id, kind, topic_key, status, pinned,
                    current_version_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', 0, ?, ?, ?)
                """,
                memory_rows,
            )
            connection.executemany(
                """
                INSERT INTO memory_versions(
                    id, memory_id, version_number, content, normalized_content,
                    content_hash, search_text, importance, confidence, origin,
                    operation, supersedes_version_id, created_at, deep_memory_eligible
                ) VALUES (?, ?, 1, ?, ?, ?, ?, 0.75, 1.0, 'manual',
                          'manual_edit', NULL, ?, ?)
                """,
                version_rows,
            )
            connection.executemany(
                """
                INSERT INTO memory_sources(
                    id, version_id, source_message_id, live_message_id,
                    extraction_method, created_at
                ) VALUES (?, ?, ?, ?, 'automatic', ?)
                """,
                (
                    (
                        f"benchmark-memory-source-{index:05d}",
                        f"benchmark-version-{index:05d}",
                        source_messages[index].message_id,
                        source_messages[index].message_id,
                        now,
                    )
                    for index in range(source_fact_count)
                ),
            )
            connection.executemany(
                """
                INSERT INTO memory_fts(version_id, memory_id, profile_id, search_text)
                VALUES (?, ?, ?, ?)
                """,
                fts_rows,
            )

            persona_rows: list[tuple[object, ...]] = []
            persona_fts_rows: list[tuple[str, str, str]] = []
            for index in range(persona_items):
                knowledge_id = f"benchmark-persona-{index:05d}"
                # Deliberately disjoint from the Chinese user-memory queries.
                # Any injected persona ID is therefore a genuine corpus leak,
                # rather than a legitimate dual-corpus relevance match.
                content = f"zephyrquartzpersona{index:05d}"
                search_text = build_search_text(content)
                persona_rows.append(
                    (
                        knowledge_id,
                        content,
                        search_text,
                        hashlib.sha256(f"source-{index}".encode()).hexdigest(),
                        exact_memory_hash(content),
                        now,
                        now,
                    )
                )
                persona_fts_rows.append((knowledge_id, "kurisu", search_text))
            connection.executemany(
                """
                INSERT INTO persona_knowledge(
                    id, persona_id, content, search_text, tags_json, source_ref,
                    source_hash, content_hash, active, created_at, updated_at
                ) VALUES (?, 'kurisu', ?, ?, '["synthetic"]', 'synthetic:benchmark',
                          ?, ?, 1, ?, ?)
                """,
                persona_rows,
            )
            connection.executemany(
                """
                INSERT INTO persona_fts(knowledge_id, persona_id, search_text)
                VALUES (?, ?, ?)
                """,
                persona_fts_rows,
            )

        deep_memories = DeepMemoryStore(database)
        active_reflections = []
        persona_impressions = []
        for index in range(reflection_items + persona_impression_items):
            fact_index = index % source_fact_count
            reflection = deep_memories.create_reflection(
                f"zephyrreflection{index:05d}",
                f"benchmark-reflection:{index:05d}",
                fact_version_ids=(f"benchmark-version-{fact_index:05d}",),
                importance=1.0,
                confidence=1.0,
            )
            reflection = deep_memories.confirm("reflection", reflection.group_id)
            if index < persona_impression_items:
                reflection = deep_memories.confirm("reflection", reflection.group_id)
                persona_impressions.append(
                    deep_memories.promote_reflection(
                        reflection.group_id,
                        content=f"zephyrimpression{index:05d}",
                    )
                )
            else:
                active_reflections.append(reflection)

        vectors = VectorStore(database)
        user_generation = vectors.begin_memory_generation(
            generation_id="benchmark-user-generation",
            model_name=MODEL_API_NAME,
            model_commit=MODEL_REVISION,
            model_sha256=MODEL_ONNX_SHA256,
            calibration_threshold=threshold,
        )
        persona_generation = vectors.begin_persona_generation(
            generation_id="benchmark-persona-generation",
            persona_id="kurisu",
            model_name=MODEL_API_NAME,
            model_commit=MODEL_REVISION,
            model_sha256=MODEL_ONNX_SHA256,
            calibration_threshold=threshold,
        )
        reflection_generation = vectors.begin_reflection_generation(
            generation_id="benchmark-reflection-generation",
            model_name=MODEL_API_NAME,
            model_commit=MODEL_REVISION,
            model_sha256=MODEL_ONNX_SHA256,
            calibration_threshold=threshold,
        )
        impression_generation = vectors.begin_persona_impression_generation(
            generation_id="benchmark-impression-generation",
            model_name=MODEL_API_NAME,
            model_commit=MODEL_REVISION,
            model_sha256=MODEL_ONNX_SHA256,
            calibration_threshold=threshold,
        )
        basis = tuple(_basis_vector(index) for index in range(MODEL_DIMENSION))
        vectors.activate_memory_generation(
            user_generation.generation_id,
            {
                f"benchmark-version-{index:05d}": basis[index % MODEL_DIMENSION]
                for index in range(memory_items)
            },
        )
        vectors.activate_persona_generation(
            persona_generation.generation_id,
            {
                f"benchmark-persona-{index:05d}": basis[(index + 257) % MODEL_DIMENSION]
                for index in range(persona_items)
            },
        )
        vectors.activate_reflection_generation(
            reflection_generation.generation_id,
            {
                record.current_version.version_id: basis[(index + 129) % MODEL_DIMENSION]
                for index, record in enumerate(active_reflections)
            },
        )
        vectors.activate_persona_impression_generation(
            impression_generation.generation_id,
            {
                record.current_version.version_id: basis[(index + 385) % MODEL_DIMENSION]
                for index, record in enumerate(persona_impressions)
            },
        )
    finally:
        database.close()


def _prepare_production_prompt(
    application: QCoreApplication,
    service: LocalDataService,
    query: str,
    *,
    sequence: int,
) -> PreparedPrompt:
    completed: list[object] = []
    failures: list[str] = []
    turn = ConversationTurn(
        turn_id=f"benchmark-turn-{sequence:05d}",
        user_message=ChatMessage(
            f"benchmark-user-message-{sequence:05d}",
            MessageRole.USER,
            query,
            MessageStatus.COMPLETED,
        ),
        assistant_message=ChatMessage(
            f"benchmark-assistant-message-{sequence:05d}",
            MessageRole.ASSISTANT,
            "",
            MessageStatus.PENDING,
        ),
    )
    if not service.prepare_new_turn(turn, completed.append, failures.append):
        raise AcceptanceFailure("production_prompt_submission_failed")
    if not _pump_until(
        application,
        lambda: bool(completed) or bool(failures),
        timeout_seconds=5.0,
    ):
        raise AcceptanceFailure("production_prompt_timeout")
    if failures or not completed:
        raise AcceptanceFailure("production_prompt_failed")
    prompt = completed[0]
    if (
        not isinstance(prompt, PreparedPrompt)
        or not prompt.messages
        or prompt.messages[-1].content != query
        or len(prompt.user_memory_version_ids) > 8
        or len(prompt.persona_knowledge_ids) > 4
    ):
        raise AcceptanceFailure("production_prompt_contract_failed")
    return prompt


def _user_query_cross_library_mis_hits(prompt: PreparedPrompt) -> int:
    """Count role-corpus injections into a controlled user-only query."""

    return (
        len(prompt.reflection_version_ids)
        + len(prompt.persona_impression_version_ids)
        + len(prompt.persona_knowledge_ids)
        + sum(
            not value.startswith("benchmark-version-") for value in prompt.user_memory_version_ids
        )
    )


def _verify_database_released(database_path: Path, backup_path: Path) -> None:
    """Reopen and rename the temporary DB to prove every owner released it."""

    database = SQLiteDatabase(database_path, backup_dir=backup_path).open()
    try:
        row = database.connection.execute("PRAGMA quick_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            raise AcceptanceFailure("production_database_check_failed")
    finally:
        database.close()
    renamed = database_path.with_name("released.sqlite3")
    database_path.replace(renamed)
    renamed.replace(database_path)


def _pump_until(
    application: QCoreApplication,
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return True
        time.sleep(0.001)
    application.processEvents()
    return predicate()


def _complete_retrieval(
    backend: EmbeddingBackend,
    caches: VectorCacheSet,
    query: str,
) -> None:
    query_vector = backend.embed_query(query)
    caches.search(VectorCorpus.USER_MEMORY, query_vector, limit=30, minimum_score=-1.0)
    caches.search(VectorCorpus.PERSONA_KNOWLEDGE, query_vector, limit=30, minimum_score=-1.0)


def _require_backend_identity(backend: EmbeddingBackend) -> None:
    if backend.provider != CPU_PROVIDER:
        raise AcceptanceFailure("model_provider_mismatch")
    if backend.dimension != MODEL_DIMENSION:
        raise AcceptanceFailure("model_dimension_mismatch")


def _basis_vector(index: int) -> tuple[float, ...]:
    values = [0.0] * MODEL_DIMENSION
    values[index] = 1.0
    return tuple(values)


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a percentile of no values")
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _rounded(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _failure_result(command: str, category: str) -> dict[str, object]:
    return {
        "command": command,
        "status": "failed",
        "error_category": category,
        "model": MODEL_API_NAME,
        "revision": MODEL_REVISION,
    }


@contextmanager
def _deny_network() -> Iterator[None]:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_sendto = socket.socket.sendto

    def blocked(*_args: object, **_kwargs: object) -> Any:
        raise OfflineNetworkAttempt()

    socket.socket.connect = blocked  # type: ignore[method-assign]
    socket.socket.connect_ex = blocked  # type: ignore[method-assign]
    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    socket.socket.sendto = blocked  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo
        socket.socket.sendto = original_sendto  # type: ignore[method-assign]


def _machine_metrics() -> dict[str, object]:
    cpu = platform.processor().strip() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown")
    return {
        "os": platform.platform(),
        "cpu": cpu,
        "physical_memory_bytes": _physical_memory_bytes(),
        "process_rss_bytes": _process_rss_bytes(),
    }


def _physical_memory_bytes() -> int | None:
    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.total_physical)
        return None
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return None


def _process_rss_bytes() -> int | None:
    if os.name == "nt":

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("page_fault_count", ctypes.c_ulong),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(ProcessMemoryCounters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        )
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        process = kernel32.GetCurrentProcess()
        succeeded = psapi.GetProcessMemoryInfo(
            process,
            ctypes.byref(counters),
            counters.cb,
        )
        return int(counters.working_set_size) if succeeded else None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        resident_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
        return int(page_size * resident_pages)
    except (AttributeError, IndexError, OSError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
