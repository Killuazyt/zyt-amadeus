"""Qt-safe orchestration for offline vector caches and generation rebuilds."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from PySide6.QtCore import QObject, QTimer, Signal

from amadeus_desktop.data_runtime import DataPriority, SerialDataThread
from amadeus_desktop.deep_memory_store import DeepMemoryStore
from amadeus_desktop.embedding_backend import (
    CPU_PROVIDER,
    EmbeddingBackend,
    EmbeddingUnavailableError,
)
from amadeus_desktop.embedding_model import PINNED_MODEL
from amadeus_desktop.hybrid_retrieval import RankedRetrievalHit
from amadeus_desktop.memory_models import MemoryLayer
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
    deep_memories: DeepMemoryStore | None = None


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
    reflection_generation_id: str | None = None
    reflection_count: int = 0
    persona_impression_generation_id: str | None = None
    persona_impression_count: int = 0

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
    reflection: _LoadedCorpus | None
    persona_impression: _LoadedCorpus | None
    persona: _LoadedCorpus | None


@dataclass(frozen=True, slots=True)
class _Document:
    corpus: VectorCorpus
    target_id: str
    content: str


@dataclass(frozen=True, slots=True)
class _RebuildSeed:
    user_generation_id: str
    reflection_generation_id: str
    persona_impression_generation_id: str
    persona_generation_id: str
    documents: tuple[_Document, ...]


@dataclass(slots=True)
class _RebuildContext:
    seed: _RebuildSeed
    offset: int = 0
    user_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    reflection_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    persona_impression_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    persona_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    user_snapshot: VectorCacheSnapshot | None = None
    reflection_snapshot: VectorCacheSnapshot | None = None
    persona_impression_snapshot: VectorCacheSnapshot | None = None
    persona_snapshot: VectorCacheSnapshot | None = None


@dataclass(frozen=True, slots=True)
class _PersonaRebuildSeed:
    persona_generation_id: str
    documents: tuple[_Document, ...]


@dataclass(slots=True)
class _PersonaRebuildContext:
    seed: _PersonaRebuildSeed
    offset: int = 0
    persona_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    persona_snapshot: VectorCacheSnapshot | None = None


@dataclass(frozen=True, slots=True)
class _IncrementalSeed:
    requires_rebuild: bool
    user_generation_id: str | None = None
    reflection_generation_id: str | None = None
    persona_impression_generation_id: str | None = None
    persona_generation_id: str | None = None
    documents: tuple[_Document, ...] = ()


@dataclass(slots=True)
class _IncrementalContext:
    seed: _IncrementalSeed
    offset: int = 0
    user_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    reflection_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    persona_impression_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
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
        self._persona_rebuild_pending = False
        self._load_in_progress = False
        self._retry_in_progress = False
        self._operation_epoch = 0
        self._thresholds = {
            VectorCorpus.USER_MEMORY: self._calibration_threshold,
            VectorCorpus.MEMORY_REFLECTION: self._calibration_threshold,
            VectorCorpus.MEMORY_PERSONA_IMPRESSION: self._calibration_threshold,
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
            if (
                self._closed
                or self._load_in_progress
                or self._retry_in_progress
                or self._rebuild_in_progress
                or self._incremental_in_progress
            ):
                return False
            self._load_in_progress = True
            self._operation_epoch += 1
            epoch = self._operation_epoch
        request_id = self._data_thread.submit(
            self._load_persisted,
            priority=DataPriority.BACKGROUND,
            on_success=lambda value: self._on_persisted_loaded(value, epoch),
            on_failure=lambda _category: self._finish_persisted_load_failure(
                epoch, "storage_error"
            ),
        )
        if request_id is None:
            self._finish_persisted_load_failure(epoch, "storage_unavailable")
            return False
        self._publish_status("loading")
        return True

    def rebuild(self) -> bool:
        """Start a resumable two-corpus rebuild and return without waiting."""

        with self._state_lock:
            if (
                self._closed
                or self._load_in_progress
                or self._retry_in_progress
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

    def rebuild_persona(self) -> bool:
        """Rebuild only the fixed persona corpus without switching user memory."""

        with self._state_lock:
            if self._closed or self._backend_unavailable:
                return False
            if (
                self._load_in_progress
                or self._retry_in_progress
                or self._rebuild_in_progress
                or self._incremental_in_progress
            ):
                self._persona_rebuild_pending = True
                return True
            self._rebuild_in_progress = True
        request_id = self._data_thread.submit(
            self._begin_persona_rebuild,
            priority=DataPriority.BACKGROUND,
            on_success=self._on_persona_rebuild_seeded,
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
            if self._load_in_progress or self._retry_in_progress:
                self._incremental_pending = True
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
        include_deep: bool = True,
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
                    reflection_threshold = self._thresholds[VectorCorpus.MEMORY_REFLECTION]
                    impression_threshold = self._thresholds[VectorCorpus.MEMORY_PERSONA_IMPRESSION]
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
                reflection_hits = (
                    self._caches.search(
                        VectorCorpus.MEMORY_REFLECTION,
                        vector,
                        limit=user_limit,
                        minimum_score=reflection_threshold,
                    )
                    if include_user and include_deep
                    else ()
                )
                impression_hits = (
                    self._caches.search(
                        VectorCorpus.MEMORY_PERSONA_IMPRESSION,
                        vector,
                        limit=user_limit,
                        minimum_score=impression_threshold,
                    )
                    if include_user and include_deep
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
                    reflection_hits=tuple(
                        RankedRetrievalHit(hit.target_id, hit.rank, hit.score)
                        for hit in reflection_hits
                    ),
                    persona_impression_hits=tuple(
                        RankedRetrievalHit(hit.target_id, hit.rank, hit.score)
                        for hit in impression_hits
                    ),
                    persona_hits=tuple(
                        RankedRetrievalHit(hit.target_id, hit.rank, hit.score)
                        for hit in persona_hits
                    ),
                    threshold=min(
                        user_threshold,
                        reflection_threshold,
                        impression_threshold,
                        persona_threshold,
                    ),
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
            if (
                self._closed
                or self._load_in_progress
                or self._retry_in_progress
                or self._rebuild_in_progress
                or self._incremental_in_progress
            ):
                return _failed_future(EmbeddingUnavailableError("embedding_unavailable"))
            self._retry_in_progress = True

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
            with self._state_lock:
                self._retry_in_progress = False
            self._publish_status("runtime_unavailable")
            return _failed_future(EmbeddingUnavailableError("embedding_unavailable"), error)

        def completed(result: Future[bool]) -> None:
            try:
                result.result()
            except Exception as error:
                with self._state_lock:
                    self._retry_in_progress = False
                self._publish_status(_safe_embedding_category(error))
            else:
                with self._state_lock:
                    self._retry_in_progress = False
                    closed = self._closed
                # A retry can follow a startup failure, before any persisted
                # generation was materialized into the immutable caches.
                # Re-run the normal load path rather than reporting a false
                # ready state with empty caches.
                if not closed:
                    self.start()

        future.add_done_callback(completed)
        return future

    def close(self) -> Future[None]:
        """Stop new coordinator work and release the backend on its owner thread."""

        with self._state_lock:
            self._closed = True
            self._operation_epoch += 1
            self._load_in_progress = False
            self._retry_in_progress = False

        def operation() -> None:
            backend, self._backend = self._backend, None
            if backend is not None:
                backend.close()
            self._caches.clear(VectorCorpus.USER_MEMORY)
            self._caches.clear(VectorCorpus.MEMORY_REFLECTION)
            self._caches.clear(VectorCorpus.MEMORY_PERSONA_IMPRESSION)
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
        reflection_generation = repositories.vectors.get_active_reflection_generation()
        impression_generation = repositories.vectors.get_active_persona_impression_generation()
        persona_generation = repositories.vectors.get_active_persona_generation(self._persona_id)
        return _LoadedState(
            user=(
                None
                if user_generation is None
                else _LoadedCorpus(user_generation, repositories.vectors.load_memory_vectors())
            ),
            reflection=(
                None
                if reflection_generation is None
                else _LoadedCorpus(
                    reflection_generation,
                    repositories.vectors.load_reflection_vectors(),
                )
            ),
            persona_impression=(
                None
                if impression_generation is None
                else _LoadedCorpus(
                    impression_generation,
                    repositories.vectors.load_persona_impression_vectors(),
                )
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

    def _on_persisted_loaded(self, value: object, epoch: int) -> None:
        with self._state_lock:
            if self._closed or not self._load_in_progress or epoch != self._operation_epoch:
                return
        loaded = value
        assert isinstance(loaded, _LoadedState)

        def operation() -> tuple[
            VectorCacheSnapshot | None,
            VectorCacheSnapshot | None,
            VectorCacheSnapshot | None,
            VectorCacheSnapshot | None,
        ]:
            self._require_backend()
            return (
                self._snapshot_from_loaded(loaded.user),
                self._snapshot_from_loaded(loaded.reflection),
                self._snapshot_from_loaded(loaded.persona_impression),
                self._snapshot_from_loaded(loaded.persona),
            )

        try:
            future = self._vector_runtime.submit(operation, priority=VectorTaskPriority.REBUILD)
        except VectorRuntimeClosedError:
            self._finish_persisted_load_failure(epoch, "runtime_unavailable")
            return

        def completed(
            result: Future[
                tuple[
                    VectorCacheSnapshot | None,
                    VectorCacheSnapshot | None,
                    VectorCacheSnapshot | None,
                    VectorCacheSnapshot | None,
                ]
            ],
        ) -> None:
            try:
                (
                    user_snapshot,
                    reflection_snapshot,
                    impression_snapshot,
                    persona_snapshot,
                ) = result.result()
            except Exception as error:
                if _safe_embedding_category(error) == "generation_model_mismatch":
                    if self._finish_persisted_load_failure(
                        epoch,
                        "generation_model_mismatch",
                        run_pending=False,
                    ):
                        QTimer.singleShot(0, self, self.rebuild)
                    return
                if not self._finish_persisted_load_failure(
                    epoch,
                    _safe_embedding_category(error),
                    publish=False,
                    run_pending=False,
                ):
                    return
                self._disable_backend(error)
                return
            with self._state_lock:
                if self._closed or not self._load_in_progress or epoch != self._operation_epoch:
                    return
                self._replace_caches(
                    user_snapshot,
                    reflection_snapshot,
                    impression_snapshot,
                    persona_snapshot,
                )
                if loaded.user is not None:
                    self._thresholds[VectorCorpus.USER_MEMORY] = (
                        loaded.user.generation.calibration_threshold
                    )
                if loaded.persona is not None:
                    self._thresholds[VectorCorpus.PERSONA_KNOWLEDGE] = (
                        loaded.persona.generation.calibration_threshold
                    )
                if loaded.reflection is not None:
                    self._thresholds[VectorCorpus.MEMORY_REFLECTION] = (
                        loaded.reflection.generation.calibration_threshold
                    )
                if loaded.persona_impression is not None:
                    self._thresholds[VectorCorpus.MEMORY_PERSONA_IMPRESSION] = (
                        loaded.persona_impression.generation.calibration_threshold
                    )
                self._load_in_progress = False
                persona_pending = self._persona_rebuild_pending
                self._persona_rebuild_pending = False
                incremental_pending = False if persona_pending else self._incremental_pending
                if not persona_pending:
                    self._incremental_pending = False
            self._publish_status("ready")
            self._run_pending_index_work(persona_pending, incremental_pending)

        future.add_done_callback(completed)

    def _finish_persisted_load_failure(
        self,
        epoch: int,
        category: str,
        *,
        publish: bool = True,
        run_pending: bool = True,
    ) -> bool:
        with self._state_lock:
            if self._closed or not self._load_in_progress or epoch != self._operation_epoch:
                return False
            self._load_in_progress = False
            persona_pending = self._persona_rebuild_pending
            self._persona_rebuild_pending = False
            incremental_pending = False if persona_pending else self._incremental_pending
            if not persona_pending:
                self._incremental_pending = False
        if publish:
            self._publish_status(category)
        if run_pending:
            self._run_pending_index_work(persona_pending, incremental_pending)
        return True

    def _run_pending_index_work(
        self,
        persona_pending: bool,
        incremental_pending: bool,
    ) -> None:
        if persona_pending and not self._backend_unavailable and self.rebuild_persona():
            return
        if incremental_pending and not self._backend_unavailable:
            self.refresh_incremental()

    def _begin_incremental(self, resource: object) -> _IncrementalSeed:
        repositories = self._repository_resolver(resource)
        user_generation = repositories.vectors.get_active_memory_generation()
        reflection_generation = repositories.vectors.get_active_reflection_generation()
        impression_generation = repositories.vectors.get_active_persona_impression_generation()
        persona_generation = repositories.vectors.get_active_persona_generation(self._persona_id)
        if (
            user_generation is None
            or reflection_generation is None
            or impression_generation is None
            or persona_generation is None
        ):
            return _IncrementalSeed(requires_rebuild=True)

        user_documents = repositories.memories.list_active_documents(limit=MAX_INDEX_DOCUMENTS)
        persona_documents = repositories.personas.list_active_documents(
            self._persona_id,
            limit=MAX_INDEX_DOCUMENTS,
        )
        reflection_documents = (
            ()
            if repositories.deep_memories is None
            else repositories.deep_memories.list_active_documents(
                MemoryLayer.REFLECTION,
                limit=MAX_INDEX_DOCUMENTS,
            )
        )
        impression_documents = (
            ()
            if repositories.deep_memories is None
            else repositories.deep_memories.list_active_documents(
                MemoryLayer.PERSONA,
                limit=MAX_INDEX_DOCUMENTS,
            )
        )
        current_user = {
            document.current_version.version_id: document.current_version.content
            for document in user_documents
        }
        current_persona = {
            document.knowledge_id: document.content for document in persona_documents
        }
        current_reflection = {
            document.current_version.version_id: document.current_version.content
            for document in reflection_documents
        }
        current_impression = {
            document.current_version.version_id: document.current_version.content
            for document in impression_documents
        }
        try:
            stored_user = {
                vector.target_id for vector in repositories.vectors.load_memory_vectors()
            }
            stored_persona = {
                vector.target_id
                for vector in repositories.vectors.load_persona_vectors(self._persona_id)
            }
            stored_reflection = {
                vector.target_id for vector in repositories.vectors.load_reflection_vectors()
            }
            stored_impression = {
                vector.target_id
                for vector in repositories.vectors.load_persona_impression_vectors()
            }
        except Exception:
            return _IncrementalSeed(requires_rebuild=True)
        if (
            stored_user - set(current_user)
            or stored_reflection - set(current_reflection)
            or stored_impression - set(current_impression)
            or stored_persona - set(current_persona)
        ):
            return _IncrementalSeed(requires_rebuild=True)

        documents = (
            tuple(
                _Document(VectorCorpus.USER_MEMORY, target_id, content)
                for target_id, content in current_user.items()
                if target_id not in stored_user
            )
            + tuple(
                _Document(VectorCorpus.MEMORY_REFLECTION, target_id, content)
                for target_id, content in current_reflection.items()
                if target_id not in stored_reflection
            )
            + tuple(
                _Document(VectorCorpus.MEMORY_PERSONA_IMPRESSION, target_id, content)
                for target_id, content in current_impression.items()
                if target_id not in stored_impression
            )
            + tuple(
                _Document(VectorCorpus.PERSONA_KNOWLEDGE, target_id, content)
                for target_id, content in current_persona.items()
                if target_id not in stored_persona
            )
        )
        return _IncrementalSeed(
            requires_rebuild=False,
            user_generation_id=user_generation.generation_id,
            reflection_generation_id=reflection_generation.generation_id,
            persona_impression_generation_id=impression_generation.generation_id,
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
                    target = _context_vectors(context, document.corpus)
                    target[document.target_id] = vector
                context.offset += len(batch)
                self._submit_incremental_batch(context)
            except Exception as error:
                self._disable_backend(error, publish=False)
                self._finish_incremental_failure(_safe_embedding_category(error))

        future.add_done_callback(completed)

    def _persist_incremental(self, context: _IncrementalContext) -> None:
        assert context.seed.user_generation_id is not None
        assert context.seed.reflection_generation_id is not None
        assert context.seed.persona_impression_generation_id is not None
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
            for target_id, vector in context.reflection_vectors.items():
                repositories.vectors.upsert_reflection_vector(
                    context.seed.reflection_generation_id,
                    target_id,
                    vector,
                )
            for target_id, vector in context.persona_impression_vectors.items():
                repositories.vectors.upsert_persona_impression_vector(
                    context.seed.persona_impression_generation_id,
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

        def operation() -> tuple[
            VectorCacheSnapshot | None,
            VectorCacheSnapshot | None,
            VectorCacheSnapshot | None,
            VectorCacheSnapshot | None,
        ]:
            return (
                self._snapshot_from_loaded(loaded.user),
                self._snapshot_from_loaded(loaded.reflection),
                self._snapshot_from_loaded(loaded.persona_impression),
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
            result: Future[
                tuple[
                    VectorCacheSnapshot | None,
                    VectorCacheSnapshot | None,
                    VectorCacheSnapshot | None,
                    VectorCacheSnapshot | None,
                ]
            ],
        ) -> None:
            try:
                user, reflection, impression, persona = result.result()
            except Exception:
                self._finish_incremental_failure("invalid_vector")
                return
            self._replace_caches(user, reflection, impression, persona)
            self._finish_incremental_success()

        future.add_done_callback(completed)

    def _finish_incremental_success(self) -> None:
        with self._state_lock:
            self._incremental_in_progress = False
            persona_pending = self._persona_rebuild_pending
            self._persona_rebuild_pending = False
            pending = False if persona_pending else self._incremental_pending
            if not persona_pending:
                self._incremental_pending = False
        self._publish_status("ready")
        if persona_pending and self.rebuild_persona():
            return
        if pending:
            self.refresh_incremental()

    def _finish_incremental_failure(self, category: str) -> None:
        with self._state_lock:
            self._incremental_in_progress = False
            persona_pending = self._persona_rebuild_pending
            self._persona_rebuild_pending = False
            pending = False if persona_pending else self._incremental_pending
            if not persona_pending:
                self._incremental_pending = False
        self._publish_status(category)
        if persona_pending and not self._backend_unavailable and self.rebuild_persona():
            return
        if pending and not self._backend_unavailable:
            self.refresh_incremental()

    def _begin_persona_rebuild(self, resource: object) -> _PersonaRebuildSeed:
        repositories = self._repository_resolver(resource)
        persona_documents = repositories.personas.list_active_documents(
            self._persona_id,
            limit=MAX_INDEX_DOCUMENTS,
        )
        generation = repositories.vectors.begin_persona_generation(
            persona_id=self._persona_id,
            model_name=PINNED_MODEL.api_name,
            model_commit=PINNED_MODEL.revision,
            model_sha256=PINNED_MODEL.onnx_sha256,
            calibration_threshold=self._calibration_threshold,
            dimension=PINNED_MODEL.dimension,
        )
        return _PersonaRebuildSeed(
            persona_generation_id=generation.generation_id,
            documents=tuple(
                _Document(
                    VectorCorpus.PERSONA_KNOWLEDGE,
                    document.knowledge_id,
                    document.content,
                )
                for document in persona_documents
            ),
        )

    def _on_persona_rebuild_seeded(self, value: object) -> None:
        seed = value
        assert isinstance(seed, _PersonaRebuildSeed)
        self._submit_persona_rebuild_batch(_PersonaRebuildContext(seed))

    def _submit_persona_rebuild_batch(self, context: _PersonaRebuildContext) -> None:
        if context.offset >= len(context.seed.documents):
            self._prepare_persona_rebuild_snapshot(context)
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
            self._mark_persona_rebuild_failed(context.seed, "runtime_unavailable")
            return

        def completed(result: Future[tuple[tuple[float, ...], ...]]) -> None:
            try:
                vectors = result.result()
                for document, vector in zip(batch, vectors, strict=True):
                    context.persona_vectors[document.target_id] = vector
                context.offset += len(batch)
                self._submit_persona_rebuild_batch(context)
            except Exception as error:
                self._disable_backend(error, publish=False)
                self._mark_persona_rebuild_failed(context.seed, "embedding_unavailable")

        future.add_done_callback(completed)

    def _prepare_persona_rebuild_snapshot(self, context: _PersonaRebuildContext) -> None:
        try:
            context.persona_snapshot = VectorCacheSnapshot.build(
                context.seed.persona_generation_id,
                (
                    VectorRecord(target_id, vector)
                    for target_id, vector in context.persona_vectors.items()
                ),
            )
        except Exception as error:
            self._disable_backend(error, publish=False)
            self._mark_persona_rebuild_failed(context.seed, "embedding_unavailable")
            return

        def activate(resource: object) -> str:
            repositories = self._repository_resolver(resource)
            current_persona = {
                document.knowledge_id
                for document in repositories.personas.list_active_documents(
                    self._persona_id,
                    limit=MAX_INDEX_DOCUMENTS,
                )
            }
            if current_persona != set(context.persona_vectors):
                raise ValueError("rebuild_documents_changed")
            generation = repositories.vectors.activate_persona_generation(
                context.seed.persona_generation_id,
                context.persona_vectors,
            )
            return generation.generation_id

        request_id = self._data_thread.submit(
            activate,
            priority=DataPriority.BACKGROUND,
            on_success=lambda _value: self._finish_persona_rebuild_success(context),
            on_failure=lambda _category: self._mark_persona_rebuild_failed(
                context.seed,
                "storage_error",
            ),
        )
        if request_id is None:
            self._mark_persona_rebuild_failed(context.seed, "storage_unavailable")

    def _finish_persona_rebuild_success(self, context: _PersonaRebuildContext) -> None:
        assert context.persona_snapshot is not None
        self._caches.swap(VectorCorpus.PERSONA_KNOWLEDGE, context.persona_snapshot)
        with self._state_lock:
            self._thresholds[VectorCorpus.PERSONA_KNOWLEDGE] = self._calibration_threshold
            self._rebuild_in_progress = False
            persona_pending = self._persona_rebuild_pending
            self._persona_rebuild_pending = False
            pending = False if persona_pending else self._incremental_pending
            if not persona_pending:
                self._incremental_pending = False
        self._publish_status("ready")
        if persona_pending and self.rebuild_persona():
            return
        if pending:
            self.refresh_incremental()

    def _mark_persona_rebuild_failed(
        self,
        seed: _PersonaRebuildSeed,
        category: str,
    ) -> None:
        def operation(resource: object) -> None:
            repositories = self._repository_resolver(resource)
            with suppress(Exception):
                repositories.vectors.fail_persona_generation(
                    seed.persona_generation_id,
                    "rebuild_failed",
                )

        request_id = self._data_thread.submit(
            operation,
            priority=DataPriority.BACKGROUND,
            on_success=lambda _value: self._finish_rebuild_failure(category),
            on_failure=lambda _failure: self._finish_rebuild_failure(category),
        )
        if request_id is None:
            self._finish_rebuild_failure(category)

    def _begin_rebuild(self, resource: object) -> _RebuildSeed:
        repositories = self._repository_resolver(resource)
        user_documents = repositories.memories.list_active_documents(limit=MAX_INDEX_DOCUMENTS)
        persona_documents = repositories.personas.list_active_documents(
            self._persona_id, limit=MAX_INDEX_DOCUMENTS
        )
        reflection_documents = (
            ()
            if repositories.deep_memories is None
            else repositories.deep_memories.list_active_documents(
                MemoryLayer.REFLECTION,
                limit=MAX_INDEX_DOCUMENTS,
            )
        )
        impression_documents = (
            ()
            if repositories.deep_memories is None
            else repositories.deep_memories.list_active_documents(
                MemoryLayer.PERSONA,
                limit=MAX_INDEX_DOCUMENTS,
            )
        )
        user_generation: EmbeddingGeneration | None = None
        reflection_generation: EmbeddingGeneration | None = None
        impression_generation: EmbeddingGeneration | None = None
        persona_generation: EmbeddingGeneration | None = None
        try:
            user_generation = repositories.vectors.begin_memory_generation(
                model_name=PINNED_MODEL.api_name,
                model_commit=PINNED_MODEL.revision,
                model_sha256=PINNED_MODEL.onnx_sha256,
                calibration_threshold=self._calibration_threshold,
                dimension=PINNED_MODEL.dimension,
            )
            reflection_generation = repositories.vectors.begin_reflection_generation(
                model_name=PINNED_MODEL.api_name,
                model_commit=PINNED_MODEL.revision,
                model_sha256=PINNED_MODEL.onnx_sha256,
                calibration_threshold=self._calibration_threshold,
                dimension=PINNED_MODEL.dimension,
            )
            impression_generation = repositories.vectors.begin_persona_impression_generation(
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
            if reflection_generation is not None:
                repositories.vectors.fail_reflection_generation(
                    reflection_generation.generation_id,
                    "rebuild_begin_failed",
                )
            if impression_generation is not None:
                repositories.vectors.fail_persona_impression_generation(
                    impression_generation.generation_id,
                    "rebuild_begin_failed",
                )
            raise
        documents = (
            tuple(
                _Document(
                    VectorCorpus.USER_MEMORY,
                    document.current_version.version_id,
                    document.current_version.content,
                )
                for document in user_documents
            )
            + tuple(
                _Document(
                    VectorCorpus.MEMORY_REFLECTION,
                    document.current_version.version_id,
                    document.current_version.content,
                )
                for document in reflection_documents
            )
            + tuple(
                _Document(
                    VectorCorpus.MEMORY_PERSONA_IMPRESSION,
                    document.current_version.version_id,
                    document.current_version.content,
                )
                for document in impression_documents
            )
            + tuple(
                _Document(
                    VectorCorpus.PERSONA_KNOWLEDGE,
                    document.knowledge_id,
                    document.content,
                )
                for document in persona_documents
            )
        )
        return _RebuildSeed(
            user_generation_id=user_generation.generation_id,
            reflection_generation_id=reflection_generation.generation_id,
            persona_impression_generation_id=impression_generation.generation_id,
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
                    target = _context_vectors(context, document.corpus)
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
            context.reflection_snapshot = VectorCacheSnapshot.build(
                context.seed.reflection_generation_id,
                (
                    VectorRecord(target_id, vector)
                    for target_id, vector in context.reflection_vectors.items()
                ),
            )
            context.persona_impression_snapshot = VectorCacheSnapshot.build(
                context.seed.persona_impression_generation_id,
                (
                    VectorRecord(target_id, vector)
                    for target_id, vector in context.persona_impression_vectors.items()
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

        def activate(resource: object) -> tuple[str, str, str, str]:
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
            current_reflections = (
                set()
                if repositories.deep_memories is None
                else {
                    document.current_version.version_id
                    for document in repositories.deep_memories.list_active_documents(
                        MemoryLayer.REFLECTION,
                        limit=MAX_INDEX_DOCUMENTS,
                    )
                }
            )
            current_impressions = (
                set()
                if repositories.deep_memories is None
                else {
                    document.current_version.version_id
                    for document in repositories.deep_memories.list_active_documents(
                        MemoryLayer.PERSONA,
                        limit=MAX_INDEX_DOCUMENTS,
                    )
                }
            )
            if (
                current_user != set(context.user_vectors)
                or current_reflections != set(context.reflection_vectors)
                or current_impressions != set(context.persona_impression_vectors)
                or current_persona != set(context.persona_vectors)
            ):
                raise ValueError("rebuild_documents_changed")
            reflection = repositories.vectors.activate_reflection_generation(
                context.seed.reflection_generation_id,
                context.reflection_vectors,
            )
            impression = repositories.vectors.activate_persona_impression_generation(
                context.seed.persona_impression_generation_id,
                context.persona_impression_vectors,
            )
            user, persona = repositories.vectors.activate_generations_atomically(
                context.seed.user_generation_id,
                context.user_vectors,
                context.seed.persona_generation_id,
                context.persona_vectors,
            )
            return (
                user.generation_id,
                reflection.generation_id,
                impression.generation_id,
                persona.generation_id,
            )

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
        assert context.reflection_snapshot is not None
        assert context.persona_impression_snapshot is not None
        assert context.persona_snapshot is not None
        self._replace_caches(
            context.user_snapshot,
            context.reflection_snapshot,
            context.persona_impression_snapshot,
            context.persona_snapshot,
        )
        with self._state_lock:
            self._thresholds[VectorCorpus.USER_MEMORY] = self._calibration_threshold
            self._thresholds[VectorCorpus.MEMORY_REFLECTION] = self._calibration_threshold
            self._thresholds[VectorCorpus.MEMORY_PERSONA_IMPRESSION] = self._calibration_threshold
            self._thresholds[VectorCorpus.PERSONA_KNOWLEDGE] = self._calibration_threshold
            self._rebuild_in_progress = False
            persona_pending = self._persona_rebuild_pending
            self._persona_rebuild_pending = False
            pending = False if persona_pending else self._incremental_pending
            if not persona_pending:
                self._incremental_pending = False
        self._publish_status("ready")
        if persona_pending and self.rebuild_persona():
            return
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
            with suppress(Exception):
                repositories.vectors.fail_reflection_generation(
                    seed.reflection_generation_id,
                    "rebuild_failed",
                )
            with suppress(Exception):
                repositories.vectors.fail_persona_impression_generation(
                    seed.persona_impression_generation_id,
                    "rebuild_failed",
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
            persona_pending = self._persona_rebuild_pending
            self._persona_rebuild_pending = False
            pending = False if persona_pending else self._incremental_pending
            if not persona_pending:
                self._incremental_pending = False
        self._publish_status(category)
        if persona_pending and not self._backend_unavailable and self.rebuild_persona():
            return
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
                for corpus in VectorCorpus:
                    self._thresholds[corpus] = threshold
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
        reflection: VectorCacheSnapshot | None,
        persona_impression: VectorCacheSnapshot | None,
        persona: VectorCacheSnapshot | None,
    ) -> None:
        if user is None:
            self._caches.clear(VectorCorpus.USER_MEMORY)
        else:
            self._caches.swap(VectorCorpus.USER_MEMORY, user)
        if reflection is None:
            self._caches.clear(VectorCorpus.MEMORY_REFLECTION)
        else:
            self._caches.swap(VectorCorpus.MEMORY_REFLECTION, reflection)
        if persona_impression is None:
            self._caches.clear(VectorCorpus.MEMORY_PERSONA_IMPRESSION)
        else:
            self._caches.swap(
                VectorCorpus.MEMORY_PERSONA_IMPRESSION,
                persona_impression,
            )
        if persona is None:
            self._caches.clear(VectorCorpus.PERSONA_KNOWLEDGE)
        else:
            self._caches.swap(VectorCorpus.PERSONA_KNOWLEDGE, persona)

    def _publish_status(self, category: str) -> None:
        user = self._caches.snapshot(VectorCorpus.USER_MEMORY)
        reflection = self._caches.snapshot(VectorCorpus.MEMORY_REFLECTION)
        impression = self._caches.snapshot(VectorCorpus.MEMORY_PERSONA_IMPRESSION)
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
                reflection_generation_id=(None if reflection is None else reflection.generation_id),
                reflection_count=0 if reflection is None else reflection.count,
                persona_impression_generation_id=(
                    None if impression is None else impression.generation_id
                ),
                persona_impression_count=0 if impression is None else impression.count,
            )
            self._status = status
        self.status_changed.emit(status)


T = TypeVar("T")


def _context_vectors(
    context: _IncrementalContext | _RebuildContext,
    corpus: VectorCorpus,
) -> dict[str, tuple[float, ...]]:
    if corpus is VectorCorpus.USER_MEMORY:
        return context.user_vectors
    if corpus is VectorCorpus.MEMORY_REFLECTION:
        return context.reflection_vectors
    if corpus is VectorCorpus.MEMORY_PERSONA_IMPRESSION:
        return context.persona_impression_vectors
    if corpus is VectorCorpus.PERSONA_KNOWLEDGE:
        return context.persona_vectors
    raise ValueError("unknown vector corpus")


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
