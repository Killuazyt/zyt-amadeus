from __future__ import annotations

import threading

import pytest

from amadeus_desktop.vector_runtime import (
    PriorityVectorRuntime,
    VectorCacheSet,
    VectorCacheSnapshot,
    VectorCorpus,
    VectorRecord,
    VectorRuntimeClosedError,
    VectorTaskPriority,
)

DIMENSION = 512


def _unit_vector(index: int = 0) -> tuple[float, ...]:
    values = [0.0] * DIMENSION
    values[index] = 1.0
    return tuple(values)


def test_snapshot_is_contiguous_and_scans_by_cosine() -> None:
    snapshot = VectorCacheSnapshot.build(
        "generation-1",
        (
            VectorRecord("a", _unit_vector(0)),
            VectorRecord("b", _unit_vector(1)),
            VectorRecord("c", _unit_vector(0)),
        ),
    )

    hits = snapshot.search(_unit_vector(0), limit=3, minimum_score=0.5)

    assert snapshot.byte_size == 3 * DIMENSION * 4
    assert [(hit.target_id, hit.rank, hit.score) for hit in hits] == [
        ("a", 1, 1.0),
        ("c", 2, 1.0),
    ]


def test_ten_thousand_vector_cache_has_expected_matrix_size() -> None:
    shared_vector = _unit_vector()
    snapshot = VectorCacheSnapshot.build(
        "generation-10k",
        (VectorRecord(str(index), shared_vector) for index in range(10_000)),
    )

    assert snapshot.count == 10_000
    assert snapshot.byte_size == 20_480_000


def test_invalid_and_duplicate_vector_records_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        VectorCacheSnapshot.build(
            "generation",
            (VectorRecord("same", _unit_vector()), VectorRecord("same", _unit_vector())),
        )
    with pytest.raises(ValueError, match="512"):
        VectorCacheSnapshot.build("generation", (VectorRecord("bad", (1.0,)),))


def test_user_and_persona_snapshots_switch_independently() -> None:
    caches = VectorCacheSet()
    user_v1 = VectorCacheSnapshot.build("user-v1", (VectorRecord("u1", _unit_vector()),))
    persona_v1 = VectorCacheSnapshot.build("persona-v1", (VectorRecord("p1", _unit_vector()),))
    user_v2 = VectorCacheSnapshot.build("user-v2", (VectorRecord("u2", _unit_vector()),))
    caches.swap(VectorCorpus.USER_MEMORY, user_v1)
    caches.swap(VectorCorpus.PERSONA_KNOWLEDGE, persona_v1)
    held_snapshot = caches.snapshot(VectorCorpus.USER_MEMORY)

    caches.swap(VectorCorpus.USER_MEMORY, user_v2)

    assert held_snapshot is user_v1
    assert caches.snapshot(VectorCorpus.USER_MEMORY) is user_v2
    assert caches.snapshot(VectorCorpus.PERSONA_KNOWLEDGE) is persona_v1


def test_priority_runtime_runs_chat_query_before_queued_rebuild() -> None:
    runtime = PriorityVectorRuntime(thread_name="test-vector-priority")
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def blocker() -> None:
        started.set()
        assert release.wait(2)
        order.append("blocker")

    first = runtime.submit(blocker)
    assert started.wait(2)
    rebuild = runtime.submit(lambda: order.append("rebuild"), priority=VectorTaskPriority.REBUILD)
    query = runtime.submit(lambda: order.append("query"), priority=VectorTaskPriority.QUERY)
    release.set()
    first.result(timeout=2)
    query.result(timeout=2)
    rebuild.result(timeout=2)
    runtime.close()

    assert order == ["blocker", "query", "rebuild"]
    assert not runtime.alive
    with pytest.raises(VectorRuntimeClosedError):
        runtime.submit(lambda: None)
