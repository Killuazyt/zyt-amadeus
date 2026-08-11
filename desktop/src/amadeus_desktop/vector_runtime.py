"""Immutable vector caches and a single priority inference worker."""

from __future__ import annotations

import itertools
import queue
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, TypeVar, cast

from amadeus_desktop.embedding_backend import normalize_vector, validate_normalized_vector
from amadeus_desktop.embedding_model import MODEL_DIMENSION


class VectorCorpus(StrEnum):
    """Physically and logically separate vector cache regions."""

    USER_MEMORY = "user_memory"
    MEMORY_REFLECTION = "memory_reflection"
    MEMORY_PERSONA_IMPRESSION = "memory_persona_impression"
    PERSONA_KNOWLEDGE = "persona_knowledge"


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """Validated input used to construct an immutable cache generation."""

    target_id: str
    vector: Sequence[float]


@dataclass(frozen=True, slots=True)
class VectorSearchHit:
    """One cosine-ranked target from a cache generation."""

    target_id: str
    score: float
    rank: int


@dataclass(frozen=True, slots=True)
class VectorCacheSnapshot:
    """Copy-on-write C-contiguous float32 matrix for one active generation."""

    generation_id: str
    target_ids: tuple[str, ...]
    _matrix: object = field(repr=False, compare=False)
    dimension: int = MODEL_DIMENSION

    @classmethod
    def build(
        cls,
        generation_id: str,
        records: Iterable[VectorRecord],
        *,
        dimension: int = MODEL_DIMENSION,
    ) -> VectorCacheSnapshot:
        if not generation_id:
            raise ValueError("generation_id must not be empty")
        materialized = tuple(records)
        target_ids = tuple(record.target_id for record in materialized)
        if any(not target_id for target_id in target_ids):
            raise ValueError("target_id must not be empty")
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("target_id must be unique within a generation")
        for record in materialized:
            validate_normalized_vector(record.vector, dimension=dimension)

        np = _require_numpy()
        if materialized:
            matrix = np.array(
                [record.vector for record in materialized],
                dtype=np.float32,
                order="C",
                copy=True,
            )
        else:
            matrix = np.empty((0, dimension), dtype=np.float32, order="C")
        if matrix.shape != (len(materialized), dimension):
            raise ValueError("vector matrix has an invalid shape")
        if not bool(matrix.flags.c_contiguous):
            raise ValueError("vector matrix must be C-contiguous")
        if not bool(np.isfinite(matrix).all()):
            raise ValueError("vector matrix contains non-finite values")
        matrix.setflags(write=False)
        return cls(
            generation_id=generation_id,
            target_ids=target_ids,
            _matrix=matrix,
            dimension=dimension,
        )

    @property
    def count(self) -> int:
        return len(self.target_ids)

    @property
    def byte_size(self) -> int:
        return int(cast(Any, self._matrix).nbytes)

    def search(
        self,
        query: Sequence[float],
        *,
        limit: int = 30,
        minimum_score: float = -1.0,
    ) -> tuple[VectorSearchHit, ...]:
        if limit <= 0 or not self.target_ids:
            return ()
        if not -1.0 <= minimum_score <= 1.0:
            raise ValueError("minimum_score must be between -1 and 1")

        normalized_query = normalize_vector(query, dimension=self.dimension)
        np = _require_numpy()
        query_array = np.asarray(normalized_query, dtype=np.float32)
        scores = self._matrix @ query_array
        if scores.shape != (self.count,) or not bool(np.isfinite(scores).all()):
            raise ValueError("vector scan produced invalid scores")
        eligible = np.flatnonzero(scores >= minimum_score)
        if not len(eligible):
            return ()
        # Stable sorting makes equal-score ordering deterministic by matrix row.
        ordered = eligible[np.argsort(-scores[eligible], kind="stable")][:limit]
        return tuple(
            VectorSearchHit(
                target_id=self.target_ids[int(index)],
                score=float(scores[int(index)]),
                rank=rank,
            )
            for rank, index in enumerate(ordered, start=1)
        )


class VectorCacheSet:
    """Thread-safe holder for independently switched user and persona snapshots."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[VectorCorpus, VectorCacheSnapshot] = {}

    def swap(self, corpus: VectorCorpus, snapshot: VectorCacheSnapshot) -> None:
        with self._lock:
            updated = dict(self._snapshots)
            updated[corpus] = snapshot
            self._snapshots = updated

    def clear(self, corpus: VectorCorpus) -> None:
        with self._lock:
            updated = dict(self._snapshots)
            updated.pop(corpus, None)
            self._snapshots = updated

    def snapshot(self, corpus: VectorCorpus) -> VectorCacheSnapshot | None:
        with self._lock:
            return self._snapshots.get(corpus)

    def search(
        self,
        corpus: VectorCorpus,
        query: Sequence[float],
        *,
        limit: int = 30,
        minimum_score: float = -1.0,
    ) -> tuple[VectorSearchHit, ...]:
        snapshot = self.snapshot(corpus)
        if snapshot is None:
            return ()
        return snapshot.search(query, limit=limit, minimum_score=minimum_score)

    @property
    def byte_size(self) -> int:
        with self._lock:
            return sum(snapshot.byte_size for snapshot in self._snapshots.values())


class VectorTaskPriority(IntEnum):
    """Lower values run first; foreground chat always precedes rebuild work."""

    QUERY = 0
    INCREMENTAL = 10
    REBUILD = 20


class VectorRuntimeClosedError(RuntimeError):
    """Raised when work is submitted after shutdown begins."""


T = TypeVar("T")
_Task = tuple[int, int, Callable[[], Any] | None, Future[Any] | None]


class PriorityVectorRuntime:
    """Single-thread priority executor for model load, inference, and rebuilds."""

    def __init__(self, *, thread_name: str = "amadeus-vector-runtime") -> None:
        self._queue: queue.PriorityQueue[_Task] = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._state_lock = threading.Lock()
        self._accepting = True
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=False)
        self._thread.start()

    @property
    def accepting(self) -> bool:
        with self._state_lock:
            return self._accepting

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def submit(
        self,
        callback: Callable[[], T],
        *,
        priority: VectorTaskPriority = VectorTaskPriority.QUERY,
    ) -> Future[T]:
        future: Future[T] = Future()
        with self._state_lock:
            if not self._accepting or self._closed:
                raise VectorRuntimeClosedError("vector runtime is closing")
            sequence = next(self._sequence)
            self._queue.put((int(priority), sequence, callback, future))
        return future

    def stop_accepting(self) -> None:
        with self._state_lock:
            self._accepting = False

    def cancel_pending(self) -> int:
        """Cancel queued work; the single currently running callback is unaffected."""

        cancelled = 0
        while True:
            try:
                _priority, _sequence, callback, future = self._queue.get_nowait()
            except queue.Empty:
                break
            if callback is not None and future is not None:
                cancelled += int(future.cancel())
        return cancelled

    def close(self, *, timeout: float = 10.0, cancel_pending: bool = True) -> None:
        self.stop_accepting()
        if cancel_pending:
            self.cancel_pending()
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put((int(VectorTaskPriority.REBUILD) + 1, next(self._sequence), None, None))
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("vector runtime did not stop before timeout")

    def _run(self) -> None:
        while True:
            _priority, _sequence, callback, untyped_future = self._queue.get()
            if callback is None:
                return
            future = cast(Future[Any], untyped_future)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = callback()
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)


def _require_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as error:
        raise RuntimeError("numpy is required for vector cache operations") from error
    return np
