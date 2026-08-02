"""Qt-safe orchestration for offline vector caches and generation rebuilds."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from PySide6.QtCore import QObject, Signal

from amadeus_desktop.data_runtime import DataPriority, SerialDataThread
from amadeus_desktop.embedding_backend import (
    CPU_PROVIDER,
    EmbeddingBackend,
    EmbeddingUnavailableError,
)
from amadeus_desktop.embedding_model import PINNED_MODEL
from amadeus_desktop.hybrid_retrieval import RankedRetrievalHit
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.persona_repository import PersonaRepository
from amadeus_desktop.retrieval_pipeline import VectorRetrievalResult
from amadeus_desktop.storage_models import EmbeddingGeneration, StoredVector
from amadeus_desktop.vector_runtime import (
    PriorityVectorRuntime,
    VectorCacheSet,
    VectorCacheSnapshot,
    VectorCorpus,
    VectorRecord,
    VectorRuntimeClosedError,
    VectorTaskPriority,
)
from amadeus_desktop.vector_store import MAX_INDEX_DOCUMENTS, VectorStore

PERSONA_ID = "kurisu"
DEFAULT_BATCH_SIZE = 64
DEFAULT_CALIBRATION_THRESHOLD = 0.6


class MemoryIndexRepository(Protocol):
    def list_active_documents(self, *, limit: int = MAX_INDEX_DOCUMENTS) -> tuple[object, ...]: ...


class PersonaIndexRepository(Protocol):
    def list_active_documents(
        self, persona_id: str, *, limit: int = MAX_INDEX_DOCUMENTS
    ) -> tuple[object, ...]: ...


@dataclass(frozen=True, slots=True)
class VectorIndexRepositories:
    memories: MemoryStore
    personas: PersonaRepository
    vectors: VectorStore


RepositoryResolver = Callable[[object], VectorIndexRepositories]
BackendFactory = Callable[[], EmbeddingBackend]
BackendCalibrator = Callable[[EmbeddingBackend], float]


@dataclass(frozen=True, slots=True)
class VectorIndexStatus:
    """Content-free state safe for UI signals and diagnostic logs."""

    category: str
    available: bool
    user_generation_id: str | None
    user_count: int
    persona_generation_id: str | None
    persona_count: int

    @property
    def model_status(self) -> str:
        if self.available:
            return "loading" if self.category == "loading" else "ready"
        return {
            "loading": "loading",
            "model_missing": "missing",
            "model_corrupt": "corrupt",
            "model_version_mismatch": "version_mismatch",
        }.get(self.category, "unavailable")

    @property
    def safe_error_category(self) -> str:
        return (
            self.category
            if self.category
            in {
                "model_missing",
                "model_corrupt",
                "model_version_mismatch",
                "model_runtime_unavailable",
                "model_inference_failed",
                "generation_model_mismatch",
                "invalid_vector",
                "storage_error",
            }
            else ""
        )

    @property
    def user_generation_status(self) -> str:
        return "active" if self.user_generation_id else "missing"

    @property
    def persona_generation_status(self) -> str:
        return "active" if self.persona_generation_id else "missing"

    @property
    def user_index_count(self) -> int:
        return self.user_count

    @property
    def persona_index_count(self) -> int:
        return self.persona_count

    @property
    def last_rebuild_status(self) -> str:
        if self.category == "rebuilding":
            return "building"
        if self.category in {"ready", "loading", "idle", "incremental"}:
            return "active" if self.user_generation_id or self.persona_generation_id else ""
        return "failed"


VectorQueryResult = VectorRetrievalResult


@dataclass(frozen=True, slots=True)
class _LoadedCorpus:
    generation: EmbeddingGeneration
    vectors: tuple[StoredVector, ...]


@dataclass(frozen=True, slots=True)
class _LoadedState:
    user: _LoadedCorpus | None
    persona: _LoadedCorpus | None


@dataclass(frozen=True, slots=True)
class _Document:
    corpus: VectorCorpus
    target_id: str
    content: str


@dataclass(frozen=True, slots=True)
class _RebuildSeed:
    user_generation_id: str
    persona_generation_id: str
    documents: tuple[_Document, ...]


@dataclass(slots=True)
class _RebuildContext:
    seed: _RebuildSeed
    offset: int = 0
    user_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    persona_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    user_snapshot: VectorCacheSnapshot | None = None
    persona_snapshot: VectorCacheSnapshot | None = None


@dataclass(frozen=True, slots=True)
class _IncrementalSeed:
    requires_rebuild: bool
    user_generation_id: str | None = None
    persona_generation_id: str | None = None
    documents: tuple[_Document, ...] = ()


@dataclass(slots=True)
class _IncrementalContext:
    seed: _IncrementalSeed
    offset: int = 0
    user_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    persona_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)


class VectorIndexCoordinator(QObject):
    """Coordinate SQLite, inference, and immutable caches without blocking Qt."""

    status_changed = Signal(object)

    def __init__(
        self,
        data_thread: SerialDataThread,
        vector_runtime: PriorityVectorRuntime,
        backend_factory: BackendFactory,
        repository_resolver: RepositoryResolver,
        *,
        persona_id: str = PERSONA_ID,
        batch_size: int = DEFAULT_BATCH_SIZE,
        calibration_threshold: float = DEFAULT_CALIBRATION_THRESHOLD,
        backend_calibrator: BackendCalibrator | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if persona_id != PERSONA_ID:
            raise ValueError("P5B supports only the fixed kurisu persona")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if not -1.0 <= calibration_threshold <= 1.0:
            raise ValueError("calibration_threshold must be between -1 and 1")
        self._data_thread = data_thread
        self._vector_runtime = vector_runtime
        self._backend_factory = backend_factory
        self._repository_resolver = repository_resolver
        self._persona_id = persona_id
        self._batch_size = batch_size
        self._calibration_threshold = float(calibration_threshold)
        self._backend_calibrator = backend_calibrator
        self._caches = VectorCacheSet()
        self._backend: EmbeddingBackend | None = None
        self._backend_unavailable = False
        self._closed = False
        self._rebuild_in_progress = False
        self._incremental_in_progress = False
        self._incremental_pending = False
        self._thresholds = {
            VectorCorpus.USER_MEMORY: self._calibration_threshold,
            VectorCorpus.PERSONA_KNOWLEDGE: self._calibration_threshold,
        }
        self._state_lock = threading.Lock()
        self._status = VectorIndexStatus("idle", False, None, 0, None, 0)

    @property
    def status(self) -> VectorIndexStatus:
        with self._state_lock:
            return self._status

    @property
    def cache_byte_size(self) -> int:
        return self._caches.byte_size

    def start(self) -> bool:
        """Load persisted active generations without touching SQLite on Qt."""

        with self._state_lock:
            if self._closed:
                return False
        request_id = self._data_thread.submit(
            self._load_persisted,
            priority=DataPriority.BACKGROUND,
            on_success=self._on_persisted_loaded,
            on_failure=lambda _category: self._publish_status("storage_error"),
        )
        if request_id is None:
            self._publish_status("storage_unavailable")
            return False
        self._publish_status("loading")
        return True

    def rebuild(self) -> bool:
        """Start a resumable two-corpus rebuild and return without waiting."""

        with self._state_lock:
            if (
                self._closed
                or self._rebuild_in_progress
                or self._incremental_in_progress
                or self._backend_unavailable
            ):
                return False
            self._rebuild_in_progress = True
        request_id = self._data_thread.submit(
            self._begin_rebuild,
            priority=DataPriority.BACKGROUND,
            on_success=self._on_rebuild_seeded,
            on_failure=lambda _category: self._finish_rebuild_failure("storage_error"),
        )
        if request_id is None:
            self._finish_rebuild_failure("storage_unavailable")
            return False
        self._publish_status("rebuilding")
        return True

    def refresh_incremental(self) -> bool:
        """Embed only new/current rows, falling back to a generation rebuild on deletions."""

        with self._state_lock:
            if self._closed or self._backend_unavailable:
                return False
            if self._rebuild_in_progress or self._incremental_in_progress:
                self._incremental_pending = True
                return False
            self._incremental_in_progress = True
        request_id = self._data_thread.submit(
            self._begin_incremental,
            priority=DataPriority.BACKGROUND,
            on_success=self._on_incremental_seeded,
            on_failure=lambda _category: self._finish_incremental_failure("storage_error"),
        )
        if request_id is None:
            self._finish_incremental_failure("storage_unavailable")
            return False
        self._publish_status("incremental")
        return True

    def query(
        self,
        query_text: str,
        *,
        user_limit: int = 30,
        persona_limit: int = 30,
        include_user: bool = True,
    ) -> Future[VectorQueryResult]:
        """Embed and scan both immutable caches at foreground priority."""

        cleaned = query_text.strip()
        if not cleaned:
            return _failed_future(ValueError("query_text must not be blank"))
        if user_limit < 1 or persona_limit < 1:
            return _failed_future(ValueError("query limits must be positive"))
        with self._state_lock:
            if self._closed or self._backend_unavailable:
                return _failed_future(EmbeddingUnavailableError("embedding_unavailable"))

        def operation() -> VectorQueryResult:
            try:
                backend = self._require_backend()
                vector = backend.embed_query(cleaned)
                with self._state_lock:
                    user_threshold = self._thresholds[VectorCorpus.USER_MEMORY]
                    persona_threshold = self._thresholds[VectorCorpus.PERSONA_KNOWLEDGE]
                user_hits = (
                    self._caches.search(
                        VectorCorpus.USER_MEMORY,
                        vector,
                        limit=user_limit,
                        minimum_score=user_threshold,
                    )
                    if include_user
                    else ()
                )
                persona_hits = self._caches.search(
                    VectorCorpus.PERSONA_KNOWLEDGE,
                    vector,
                    limit=persona_limit,
                    minimum_score=persona_threshold,
                )
                return VectorRetrievalResult(
                    user_hits=tuple(
                        RankedRetrievalHit(hit.target_id, hit.rank, hit.score) for hit in user_hits
                    ),
                    persona_hits=tuple(
                        RankedRetrievalHit(hit.target_id, hit.rank, hit.score)
                        for hit in persona_hits
                    ),
                    threshold=min(user_threshold, persona_threshold),
                )
            except Exception as error:
                self._disable_backend(error)
                raise EmbeddingUnavailableError("embedding_unavailable") from error

        try:
            return self._vector_runtime.submit(operation, priority=VectorTaskPriority.QUERY)
        except VectorRuntimeClosedError as error:
            self._publish_status("runtime_unavailable")
            return _failed_future(EmbeddingUnavailableError("embedding_unavailable"), error)

    def retry_backend(self) -> Future[bool]:
        """Explicitly retry model construction after a user-requested verification."""

        with self._state_lock:
            if self._closed:
                return _failed_future(EmbeddingUnavailableError("embedding_unavailable"))

        def operation() -> bool:
            previous, self._backend = self._backend, None
            if previous is not None:
                with suppress(Exception):
                    previous.close()
            try:
                backend = self._create_backend()
            except Exception as error:
                with self._state_lock:
                    self._backend_unavailable = True
                raise EmbeddingUnavailableError(_safe_embedding_category(error)) from error
            self._backend = backend
            with self._state_lock:
                self._backend_unavailable = False
            return True

        try:
            future = self._vector_runtime.submit(operation, priority=VectorTaskPriority.QUERY)
        except VectorRuntimeClosedError as error:
            self._publish_status("runtime_unavailable")
            return _failed_future(EmbeddingUnavailableError("embedding_unavailable"), error)

        def completed(result: Future[bool]) -> None:
            try:
                result.result()
            except Exception as error:
                self._publish_status(_safe_embedding_category(error))
            else:
                # A retry can follow a startup failure, before any persisted
                # generation was materialized into the immutable caches.
                # Re-run the normal load path rather than reporting a false
                # ready state with empty caches.
                self.start()

        future.add_done_callback(completed)
        return future

    def close(self) -> Future[None]:
        """Stop new coordinator work and release the backend on its owner thread."""

        with self._state_lock:
            self._closed = True

        def operation() -> None:
            backend, self._backend = self._backend, None
            if backend is not None:
                backend.close()
            self._caches.clear(VectorCorpus.USER_MEMORY)
            self._caches.clear(VectorCorpus.PERSONA_KNOWLEDGE)

        try:
            future = self._vector_runtime.submit(operation, priority=VectorTaskPriority.QUERY)
        except VectorRuntimeClosedError as error:
            future = _failed_future(error)
        self._publish_status("closed")
        return future

    def _load_persisted(self, resource: object) -> _LoadedState:
        repositories = self._repository_resolver(resource)
        repositories.vectors.recover_interrupted_generations()
        user_generation = repositories.vectors.get_active_memory_generation()
        persona_generation = repositories.vectors.get_active_persona_generation(self._persona_id)
        return _LoadedState(
            user=(
                None
                if user_generation is None
                else _LoadedCorpus(user_generation, repositories.vectors.load_memory_vectors())
            ),
            persona=(
                None
                if persona_generation is None
                else _LoadedCorpus(
                    persona_generation,
                    repositories.vectors.load_persona_vectors(self._persona_id),
                )
            ),
        )

    def _on_persisted_loaded(self, value: object) -> None:
        loaded = value
        assert isinstance(loaded, _LoadedState)

        def operation() -> tuple[VectorCacheSnapshot | None, VectorCacheSnapshot | None]:
            self._require_backend()
            return (
                self._snapshot_from_loaded(loaded.user),
                self._snapshot_from_loaded(loaded.persona),
            )

        try:
            future = self._vector_runtime.submit(operation, priority=VectorTaskPriority.REBUILD)
        except VectorRuntimeClosedError:
            self._publish_status("runtime_unavailable")
            return

        def completed(
            result: Future[tuple[VectorCacheSnapshot | None, VectorCacheSnapshot | None]],
        ) -> None:
            try:
                user_snapshot, persona_snapshot = result.result()
            except Exception as error:
                self._disable_backend(error)
                return
            self._replace_caches(user_snapshot, persona_snapshot)
            with self._state_lock:
                if loaded.user is not None:
                    self._thresholds[VectorCorpus.USER_MEMORY] = (
                        loaded.user.generation.calibration_threshold
                    )
                if loaded.persona is not None:
                    self._thresholds[VectorCorpus.PERSONA_KNOWLEDGE] = (
                        loaded.persona.generation.calibration_threshold
                    )
            self._publish_status("ready")

        future.add_done_callback(completed)

    def _begin_incremental(self, resource: object) -> _IncrementalSeed:
        repositories = self._repository_resolver(resource)
        user_generation = repositories.vectors.get_active_memory_generation()
        persona_generation = repositories.vectors.get_active_persona_generation(self._persona_id)
        if user_generation is None or persona_generation is None:
            return _IncrementalSeed(requires_rebuild=True)

        user_documents = repositories.memories.list_active_documents(limit=MAX_INDEX_DOCUMENTS)
        persona_documents = repositories.personas.list_active_documents(
            self._persona_id,
            limit=MAX_INDEX_DOCUMENTS,
        )
        current_user = {
            document.current_version.version_id: document.current_version.content
            for document in user_documents
        }
        current_persona = {
            document.knowledge_id: document.content for document in persona_documents
        }
        try:
            stored_user = {
                vector.target_id for vector in repositories.vectors.load_memory_vectors()
            }
            stored_persona = {
                vector.target_id
                for vector in repositories.vectors.load_persona_vectors(self._persona_id)
            }
        except Exception:
            return _IncrementalSeed(requires_rebuild=True)
        if stored_user - set(current_user) or stored_persona - set(current_persona):
            return _IncrementalSeed(requires_rebuild=True)

        documents = tuple(
            _Document(VectorCorpus.USER_MEMORY, target_id, content)
            for target_id, content in current_user.items()
            if target_id not in stored_user
        ) + tuple(
            _Document(VectorCorpus.PERSONA_KNOWLEDGE, target_id, content)
            for target_id, content in current_persona.items()
            if target_id not in stored_persona
        )
        return _IncrementalSeed(
            requires_rebuild=False,
            user_generation_id=user_generation.generation_id,
            persona_generation_id=persona_generation.generation_id,
            documents=documents,
        )

    def _on_incremental_seeded(self, value: object) -> None:
        seed = value
        assert isinstance(seed, _IncrementalSeed)
        if seed.requires_rebuild:
            with self._state_lock:
                self._incremental_in_progress = False
            self.rebuild()
            return
        if not seed.documents:
            # Even when no embedding is needed, an archive/delete/deactivate
            # can have removed rows from the active corpus. Reload the filtered
            # persisted view so the copy-on-write snapshots drop those rows.
            self._reload_incremental_snapshots()
            return
        self._submit_incremental_batch(_IncrementalContext(seed))

    def _submit_incremental_batch(self, context: _IncrementalContext) -> None:
        if context.offset >= len(context.seed.documents):
            self._persist_incremental(context)
            return
        batch = context.seed.documents[context.offset : context.offset + self._batch_size]

        def operation() -> tuple[tuple[float, ...], ...]:
            backend = self._require_backend()
            vectors = backend.embed_documents(tuple(document.content for document in batch))
            if len(vectors) != len(batch):
                raise ValueError("embedding_count_mismatch")
            return vectors

        try:
            future = self._vector_runtime.submit(
                operation,
                priority=VectorTaskPriority.INCREMENTAL,
            )
        except VectorRuntimeClosedError:
            self._finish_incremental_failure("runtime_unavailable")
            return

        def completed(result: Future[tuple[tuple[float, ...], ...]]) -> None:
            try:
                vectors = result.result()
                for document, vector in zip(batch, vectors, strict=True):
                    target = (
                        context.user_vectors
                        if document.corpus is VectorCorpus.USER_MEMORY
                        else context.persona_vectors
                    )
                    target[document.target_id] = vector
                context.offset += len(batch)
                self._submit_incremental_batch(context)
            except Exception as error:
                self._disable_backend(error, publish=False)
                self._finish_incremental_failure(_safe_embedding_category(error))

        future.add_done_callback(completed)

    def _persist_incremental(self, context: _IncrementalContext) -> None:
        assert context.seed.user_generation_id is not None
        assert context.seed.persona_generation_id is not None

        def operation(resource: object) -> _LoadedState:
            repositories = self._repository_resolver(resource)
            for target_id, vector in context.user_vectors.items():
                repositories.vectors.upsert_memory_vector(
                    context.seed.user_generation_id,
                    target_id,
                    vector,
                )
            for target_id, vector in context.persona_vectors.items():
                repositories.vectors.upsert_persona_vector(
                    context.seed.persona_generation_id,
                    target_id,
                    vector,
                )
            return self._load_persisted(resource)

        request_id = self._data_thread.submit(
            operation,
            priority=DataPriority.BACKGROUND,
            on_success=self._refresh_incremental_snapshots,
            on_failure=lambda _category: self._finish_incremental_failure("storage_error"),
        )
        if request_id is None:
            self._finish_incremental_failure("storage_unavailable")

    def _reload_incremental_snapshots(self) -> None:
        request_id = self._data_thread.submit(
            self._load_persisted,
            priority=DataPriority.BACKGROUND,
            on_success=self._refresh_incremental_snapshots,
            on_failure=lambda _category: self._finish_incremental_failure("storage_error"),
        )
        if request_id is None:
            self._finish_incremental_failure("storage_unavailable")

    def _refresh_incremental_snapshots(self, value: object) -> None:
        loaded = value
        assert isinstance(loaded, _LoadedState)

        def operation() -> tuple[VectorCacheSnapshot | None, VectorCacheSnapshot | None]:
            return (
                self._snapshot_from_loaded(loaded.user),
                self._snapshot_from_loaded(loaded.persona),
            )

        try:
            future = self._vector_runtime.submit(
                operation,
                priority=VectorTaskPriority.INCREMENTAL,
            )
        except VectorRuntimeClosedError:
            self._finish_incremental_failure("runtime_unavailable")
            return

        def completed(
            result: Future[tuple[VectorCacheSnapshot | None, VectorCacheSnapshot | None]],
        ) -> None:
            try:
                user, persona = result.result()
            except Exception:
                self._finish_incremental_failure("invalid_vector")
                return
            self._replace_caches(user, persona)
            self._finish_incremental_success()

        future.add_done_callback(completed)

    def _finish_incremental_success(self) -> None:
        with self._state_lock:
            self._incremental_in_progress = False
            pending = self._incremental_pending
            self._incremental_pending = False
        self._publish_status("ready")
        if pending:
            self.refresh_incremental()

    def _finish_incremental_failure(self, category: str) -> None:
        with self._state_lock:
            self._incremental_in_progress = False
            pending = self._incremental_pending
            self._incremental_pending = False
        self._publish_status(category)
        if pending and not self._backend_unavailable:
            self.refresh_incremental()

    def _begin_rebuild(self, resource: object) -> _RebuildSeed:
        repositories = self._repository_resolver(resource)
        user_documents = repositories.memories.list_active_documents(limit=MAX_INDEX_DOCUMENTS)
        persona_documents = repositories.personas.list_active_documents(
            self._persona_id, limit=MAX_INDEX_DOCUMENTS
        )
        user_generation: EmbeddingGeneration | None = None
        persona_generation: EmbeddingGeneration | None = None
        try:
            user_generation = repositories.vectors.begin_memory_generation(
                model_name=PINNED_MODEL.api_name,
                model_commit=PINNED_MODEL.revision,
                model_sha256=PINNED_MODEL.onnx_sha256,
                calibration_threshold=self._calibration_threshold,
                dimension=PINNED_MODEL.dimension,
            )
            persona_generation = repositories.vectors.begin_persona_generation(
                persona_id=self._persona_id,
                model_name=PINNED_MODEL.api_name,
                model_commit=PINNED_MODEL.revision,
                model_sha256=PINNED_MODEL.onnx_sha256,
                calibration_threshold=self._calibration_threshold,
                dimension=PINNED_MODEL.dimension,
            )
        except Exception:
            if user_generation is not None:
                repositories.vectors.fail_memory_generation(
                    user_generation.generation_id, "rebuild_begin_failed"
                )
            if persona_generation is not None:
                repositories.vectors.fail_persona_generation(
                    persona_generation.generation_id, "rebuild_begin_failed"
                )
            raise
        documents = tuple(
            _Document(
                VectorCorpus.USER_MEMORY,
                document.current_version.version_id,
                document.current_version.content,
            )
            for document in user_documents
        ) + tuple(
            _Document(
                VectorCorpus.PERSONA_KNOWLEDGE,
                document.knowledge_id,
                document.content,
            )
            for document in persona_documents
        )
        return _RebuildSeed(
            user_generation_id=user_generation.generation_id,
            persona_generation_id=persona_generation.generation_id,
            documents=documents,
        )

    def _on_rebuild_seeded(self, value: object) -> None:
        seed = value
        assert isinstance(seed, _RebuildSeed)
        self._submit_rebuild_batch(_RebuildContext(seed))

    def _submit_rebuild_batch(self, context: _RebuildContext) -> None:
        if context.offset >= len(context.seed.documents):
            self._prepare_rebuild_snapshots(context)
            return
        batch = context.seed.documents[context.offset : context.offset + self._batch_size]

        def operation() -> tuple[tuple[float, ...], ...]:
            backend = self._require_backend()
            vectors = backend.embed_documents(tuple(document.content for document in batch))
            if len(vectors) != len(batch):
                raise ValueError("embedding_count_mismatch")
            return vectors

        try:
            future = self._vector_runtime.submit(operation, priority=VectorTaskPriority.REBUILD)
        except VectorRuntimeClosedError:
            self._mark_rebuild_failed(context.seed, "runtime_unavailable")
            return

        def completed(result: Future[tuple[tuple[float, ...], ...]]) -> None:
            try:
                vectors = result.result()
                for document, vector in zip(batch, vectors, strict=True):
                    target = (
                        context.user_vectors
                        if document.corpus is VectorCorpus.USER_MEMORY
                        else context.persona_vectors
                    )
                    target[document.target_id] = vector
                context.offset += len(batch)
                self._submit_rebuild_batch(context)
            except Exception as error:
                self._disable_backend(error, publish=False)
                self._mark_rebuild_failed(context.seed, "embedding_unavailable")

        future.add_done_callback(completed)

    def _prepare_rebuild_snapshots(self, context: _RebuildContext) -> None:
        try:
            context.user_snapshot = VectorCacheSnapshot.build(
                context.seed.user_generation_id,
                (
                    VectorRecord(target_id, vector)
                    for target_id, vector in context.user_vectors.items()
                ),
            )
            context.persona_snapshot = VectorCacheSnapshot.build(
                context.seed.persona_generation_id,
                (
                    VectorRecord(target_id, vector)
                    for target_id, vector in context.persona_vectors.items()
                ),
            )
        except Exception as error:
            self._disable_backend(error, publish=False)
            self._mark_rebuild_failed(context.seed, "embedding_unavailable")
            return

        def activate(resource: object) -> tuple[str, str]:
            repositories = self._repository_resolver(resource)
            current_user = {
                document.current_version.version_id
                for document in repositories.memories.list_active_documents(
                    limit=MAX_INDEX_DOCUMENTS
                )
            }
            current_persona = {
                document.knowledge_id
                for document in repositories.personas.list_active_documents(
                    self._persona_id, limit=MAX_INDEX_DOCUMENTS
                )
            }
            if current_user != set(context.user_vectors) or current_persona != set(
                context.persona_vectors
            ):
                raise ValueError("rebuild_documents_changed")
            user, persona = repositories.vectors.activate_generations_atomically(
                context.seed.user_generation_id,
                context.user_vectors,
                context.seed.persona_generation_id,
                context.persona_vectors,
            )
            return user.generation_id, persona.generation_id

        request_id = self._data_thread.submit(
            activate,
            priority=DataPriority.BACKGROUND,
            on_success=lambda _value: self._finish_rebuild_success(context),
            on_failure=lambda _category: self._mark_rebuild_failed(context.seed, "storage_error"),
        )
        if request_id is None:
            self._mark_rebuild_failed(context.seed, "storage_unavailable")

    def _finish_rebuild_success(self, context: _RebuildContext) -> None:
        assert context.user_snapshot is not None
        assert context.persona_snapshot is not None
        self._replace_caches(context.user_snapshot, context.persona_snapshot)
        with self._state_lock:
            self._thresholds[VectorCorpus.USER_MEMORY] = self._calibration_threshold
            self._thresholds[VectorCorpus.PERSONA_KNOWLEDGE] = self._calibration_threshold
            self._rebuild_in_progress = False
            pending = self._incremental_pending
            self._incremental_pending = False
        self._publish_status("ready")
        if pending:
            self.refresh_incremental()

    def _mark_rebuild_failed(self, seed: _RebuildSeed, category: str) -> None:
        def operation(resource: object) -> None:
            repositories = self._repository_resolver(resource)
            with suppress(Exception):
                repositories.vectors.fail_memory_generation(
                    seed.user_generation_id, "rebuild_failed"
                )
            with suppress(Exception):
                repositories.vectors.fail_persona_generation(
                    seed.persona_generation_id, "rebuild_failed"
                )

        request_id = self._data_thread.submit(
            operation,
            priority=DataPriority.BACKGROUND,
            on_success=lambda _value: self._finish_rebuild_failure(category),
            on_failure=lambda _failure: self._finish_rebuild_failure(category),
        )
        if request_id is None:
            self._finish_rebuild_failure(category)

    def _finish_rebuild_failure(self, category: str) -> None:
        with self._state_lock:
            self._rebuild_in_progress = False
            pending = self._incremental_pending
            self._incremental_pending = False
        self._publish_status(category)
        if pending and not self._backend_unavailable:
            self.refresh_incremental()

    def _snapshot_from_loaded(self, loaded: _LoadedCorpus | None) -> VectorCacheSnapshot | None:
        if loaded is None:
            return None
        generation = loaded.generation
        if (
            generation.model_name != PINNED_MODEL.api_name
            or generation.model_commit != PINNED_MODEL.revision
            or generation.model_sha256 != PINNED_MODEL.onnx_sha256
            or generation.dimension != PINNED_MODEL.dimension
        ):
            raise ValueError("generation_model_mismatch")
        return VectorCacheSnapshot.build(
            generation.generation_id,
            (VectorRecord(item.target_id, item.vector) for item in loaded.vectors),
            dimension=generation.dimension,
        )

    def _require_backend(self) -> EmbeddingBackend:
        if self._backend_unavailable or self._closed:
            raise EmbeddingUnavailableError("embedding_unavailable")
        if self._backend is None:
            self._backend = self._create_backend()
        return self._backend

    def _create_backend(self) -> EmbeddingBackend:
        backend = self._backend_factory()
        try:
            valid = (
                backend.model_name == PINNED_MODEL.api_name
                and backend.dimension == PINNED_MODEL.dimension
                and backend.provider == CPU_PROVIDER
            )
        except Exception:
            with suppress(Exception):
                backend.close()
            raise
        if not valid:
            with suppress(Exception):
                backend.close()
            raise EmbeddingUnavailableError("embedding_identity_mismatch")
        if self._backend_calibrator is not None:
            try:
                threshold = float(self._backend_calibrator(backend))
                if not -1.0 <= threshold <= 1.0:
                    raise ValueError("calibration_failed")
            except Exception:
                with suppress(Exception):
                    backend.close()
                raise
            with self._state_lock:
                self._calibration_threshold = threshold
                self._thresholds[VectorCorpus.USER_MEMORY] = threshold
                self._thresholds[VectorCorpus.PERSONA_KNOWLEDGE] = threshold
        return backend

    def _disable_backend(self, error: BaseException, *, publish: bool = True) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            with suppress(Exception):
                backend.close()
        with self._state_lock:
            self._backend_unavailable = True
        if publish:
            self._publish_status(_safe_embedding_category(error))

    def _replace_caches(
        self,
        user: VectorCacheSnapshot | None,
        persona: VectorCacheSnapshot | None,
    ) -> None:
        if user is None:
            self._caches.clear(VectorCorpus.USER_MEMORY)
        else:
            self._caches.swap(VectorCorpus.USER_MEMORY, user)
        if persona is None:
            self._caches.clear(VectorCorpus.PERSONA_KNOWLEDGE)
        else:
            self._caches.swap(VectorCorpus.PERSONA_KNOWLEDGE, persona)

    def _publish_status(self, category: str) -> None:
        user = self._caches.snapshot(VectorCorpus.USER_MEMORY)
        persona = self._caches.snapshot(VectorCorpus.PERSONA_KNOWLEDGE)
        with self._state_lock:
            available = (
                not self._closed and not self._backend_unavailable and self._backend is not None
            )
            status = VectorIndexStatus(
                category=category,
                available=available,
                user_generation_id=None if user is None else user.generation_id,
                user_count=0 if user is None else user.count,
                persona_generation_id=None if persona is None else persona.generation_id,
                persona_count=0 if persona is None else persona.count,
            )
            self._status = status
        self.status_changed.emit(status)


T = TypeVar("T")

_SAFE_EMBEDDING_CATEGORIES = frozenset(
    {
        "model_missing",
        "model_corrupt",
        "model_version_mismatch",
        "model_runtime_unavailable",
        "model_inference_failed",
        "generation_model_mismatch",
        "invalid_vector",
    }
)


def _safe_embedding_category(error: BaseException) -> str:
    category = str(error).strip().lower()
    return category if category in _SAFE_EMBEDDING_CATEGORIES else "embedding_unavailable"


def _failed_future(error: BaseException, cause: BaseException | None = None) -> Future[T]:
    if cause is not None:
        error.__cause__ = cause
    future: Future[T] = Future()
    future.set_exception(error)
    return future
