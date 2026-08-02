from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from amadeus_desktop.hybrid_retrieval import (
    PINNED_COEFFICIENT,
    RRF_K,
    CalibrationError,
    RankedRetrievalHit,
    RetrievalCorpus,
    RetrievalItem,
    RetrievalTicket,
    build_retrieval_bundle,
    build_retrieval_query,
    calibrate_threshold,
    decay_weight,
    event_effective_score,
    fuse_hybrid_results,
    recall_is_successful,
    should_auto_archive,
)

NOW = datetime(2026, 8, 2, tzinfo=UTC)


def _item(target_id: str, **changes) -> RetrievalItem:
    values = {
        "target_id": target_id,
        "corpus": RetrievalCorpus.USER_MEMORY,
        "content": f"content-{target_id}",
        "kind": "fact",
    }
    values.update(changes)
    return RetrievalItem(**values)


def test_calibration_requires_fixed_samples_and_safe_gap() -> None:
    result = calibrate_threshold([0.8] * 24, [0.7] * 24)

    assert result.worst_positive == 0.8
    assert result.best_negative == 0.7
    assert result.gap == pytest.approx(0.1)
    assert result.threshold == pytest.approx(0.75)

    with pytest.raises(CalibrationError, match="sample_count"):
        calibrate_threshold([0.8], [0.7])
    with pytest.raises(CalibrationError, match="calibration_gap"):
        calibrate_threshold([0.72] * 24, [0.70] * 24)


def test_rrf_gate_quality_decay_pin_and_success_weights() -> None:
    event = _item(
        "event",
        kind="event",
        importance=0.8,
        confidence=0.6,
        pinned=False,
        successful_recall_count=9,
        created_at=NOW - timedelta(days=30),
    )
    fts_only_pinned = _item("pinned", pinned=True)
    below_threshold_pinned = _item("irrelevant", pinned=True)

    results = fuse_hybrid_results(
        fts_hits=(
            RankedRetrievalHit("event", 1),
            RankedRetrievalHit("pinned", 2),
        ),
        vector_hits=(
            RankedRetrievalHit("event", 2, 0.9),
            RankedRetrievalHit("irrelevant", 1, 0.6),
        ),
        items=(event, fts_only_pinned, below_threshold_pinned),
        vector_threshold=0.75,
        now=NOW,
    )
    by_id = {result.item.target_id: result for result in results}

    assert "irrelevant" not in by_id
    event_result = by_id["event"]
    expected_rrf = 1 / (RRF_K + 1) + 1 / (RRF_K + 2)
    assert event_result.rrf_score == pytest.approx(expected_rrf)
    assert event_result.quality_weight == pytest.approx(0.7)
    assert event_result.decay_weight == pytest.approx(0.5)
    assert event_result.success_weight == pytest.approx(1.05)
    assert by_id["pinned"].pinned_weight == PINNED_COEFFICIENT


def test_current_active_filter_and_separate_result_caps() -> None:
    users = tuple(_item(f"u{index}") for index in range(12))
    personas = tuple(
        _item(
            f"p{index}",
            corpus=RetrievalCorpus.PERSONA_KNOWLEDGE,
        )
        for index in range(7)
    )
    bundle = build_retrieval_bundle(
        user_fts_hits=tuple(
            RankedRetrievalHit(item.target_id, index) for index, item in enumerate(users, 1)
        ),
        user_vector_hits=(),
        user_items=users,
        persona_fts_hits=tuple(
            RankedRetrievalHit(item.target_id, index) for index, item in enumerate(personas, 1)
        ),
        persona_vector_hits=(),
        persona_items=personas,
        vector_threshold=0.75,
        now=NOW,
    )

    assert len(bundle.user_memories) == 8
    assert len(bundle.persona_knowledge) == 4
    assert set(bundle.memory_version_ids).isdisjoint(bundle.persona_knowledge_ids)

    filtered = fuse_hybrid_results(
        fts_hits=(RankedRetrievalHit("old", 1), RankedRetrievalHit("archived", 2)),
        vector_hits=(),
        items=(
            _item("old", current=False),
            _item("archived", active=False),
        ),
        vector_threshold=0.75,
    )
    assert filtered == ()


def test_event_decay_exemptions_and_auto_archive() -> None:
    ordinary = _item(
        "event",
        kind="event",
        importance=0.5,
        created_at=NOW - timedelta(days=120),
    )
    important = _item(
        "important",
        kind="event",
        importance=0.85,
        created_at=NOW - timedelta(days=365),
    )
    fact = _item("fact", kind="fact", created_at=NOW - timedelta(days=365))

    assert decay_weight(ordinary, now=NOW) == pytest.approx(0.5**4)
    assert event_effective_score(ordinary, now=NOW) == pytest.approx(0.03125)
    assert should_auto_archive(ordinary, now=NOW)
    assert decay_weight(important, now=NOW) == 1.0
    assert not should_auto_archive(important, now=NOW)
    assert decay_weight(fact, now=NOW) == 1.0


def test_query_context_and_recall_success_boundary() -> None:
    assert build_retrieval_query("我喜欢咖啡", ("old",)) == "我喜欢咖啡"
    query = build_retrieval_query("那这个呢？", ("第一轮", "回答一", "第二轮", "回答二"))
    assert query.startswith("[当前消息]\n那这个呢？")
    assert "第一轮" in query and "回答二" in query

    assert recall_is_successful(first_chunk_received=True, terminal_state="completed")
    assert recall_is_successful(first_chunk_received=True, terminal_state="USER_STOPPED")
    assert not recall_is_successful(first_chunk_received=False, terminal_state="completed")
    assert not recall_is_successful(first_chunk_received=True, terminal_state="shutdown")


def test_retrieval_ticket_requires_stable_turn_and_attempt() -> None:
    ticket = RetrievalTicket("turn-1", 2, ("version-1",), ("knowledge-1",))
    assert ticket.memory_version_ids == ("version-1",)
    with pytest.raises(ValueError, match="attempt"):
        RetrievalTicket("turn-1", 0)
