"""Deterministic FTS/vector fusion, calibration, and memory decay rules."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

RRF_K = 60
MAX_SOURCE_HITS = 30
MAX_USER_RESULTS = 6
MAX_REFLECTION_RESULTS = 2
MAX_PERSONA_IMPRESSION_RESULTS = 2
MAX_PERSONA_RESULTS = 4
MAX_DERIVED_AND_FACT_RESULTS = 8
PINNED_COEFFICIENT = 1.25
RECENT_RECALL_PENALTY = 0.70
STRONG_VECTOR_RELEVANCE = 0.82
EVENT_HALF_LIFE_DAYS = 30.0
AUTO_ARCHIVE_INACTIVE_DAYS = 90.0
AUTO_ARCHIVE_SCORE = 0.15
MIN_CALIBRATION_GAP = 0.05
CALIBRATION_SAMPLE_COUNT = 24
MAX_CONTINUITY_CONTEXT_CHARS = 600

_CONTINUITY_PATTERN = re.compile(
    r"(?:它|他|她|这(?:个|件|些)?|那(?:个|件|些)?|刚才|之前|上次|继续|然后|后来|"
    r"还是|同样|再说|那个|上述|前面)"
)
_NON_DECAY_KINDS = frozenset({"fact", "preference", "relationship"})
_SUCCESS_TERMINALS = frozenset({"completed", "user_stopped"})


class RetrievalCorpus(StrEnum):
    USER_MEMORY = "user_memory"
    MEMORY_REFLECTION = "memory_reflection"
    MEMORY_PERSONA_IMPRESSION = "memory_persona_impression"
    PERSONA_KNOWLEDGE = "persona_knowledge"


@dataclass(frozen=True, slots=True)
class RankedRetrievalHit:
    """One already-ranked result from FTS or a vector scan."""

    target_id: str
    rank: int
    score: float | None = None


@dataclass(frozen=True, slots=True)
class RetrievalItem:
    """Current metadata required for final scoring and prompt selection."""

    target_id: str
    corpus: RetrievalCorpus
    content: str
    kind: str = ""
    topic_key: str = ""
    importance: float = 1.0
    confidence: float = 1.0
    evidence_score: float = 0.0
    status_weight: float = 1.0
    pinned: bool = False
    successful_recall_count: int = 0
    created_at: datetime | None = None
    last_successful_recall_at: datetime | None = None
    active: bool = True
    current: bool = True


@dataclass(frozen=True, slots=True)
class HybridResult:
    """Auditable fused result with every multiplier kept separately."""

    item: RetrievalItem
    final_score: float
    rrf_score: float
    quality_weight: float
    decay_weight: float
    pinned_weight: float
    success_weight: float
    evidence_weight: float
    repetition_weight: float
    fts_rank: int | None
    vector_rank: int | None
    vector_similarity: float | None


@dataclass(frozen=True, slots=True)
class RetrievalBundle:
    """Separated results ready for independent prompt budget sections."""

    user_memories: tuple[HybridResult, ...] = ()
    reflections: tuple[HybridResult, ...] = ()
    persona_impressions: tuple[HybridResult, ...] = ()
    persona_knowledge: tuple[HybridResult, ...] = ()

    @property
    def memory_version_ids(self) -> tuple[str, ...]:
        return tuple(result.item.target_id for result in self.user_memories)

    @property
    def persona_knowledge_ids(self) -> tuple[str, ...]:
        return tuple(result.item.target_id for result in self.persona_knowledge)

    @property
    def reflection_version_ids(self) -> tuple[str, ...]:
        return tuple(result.item.target_id for result in self.reflections)

    @property
    def persona_impression_version_ids(self) -> tuple[str, ...]:
        return tuple(result.item.target_id for result in self.persona_impressions)


@dataclass(frozen=True, slots=True)
class RetrievalTicket:
    """IDs actually injected for one stable turn/attempt."""

    turn_id: str
    attempt: int
    memory_version_ids: tuple[str, ...] = ()
    persona_knowledge_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.turn_id:
            raise ValueError("turn_id must not be empty")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    threshold: float
    worst_positive: float
    best_negative: float
    gap: float
    positive_count: int
    negative_count: int


class CalibrationError(ValueError):
    """Raised when fixed positive/negative samples do not separate safely."""


def calibrate_threshold(
    positive_scores: Sequence[float],
    negative_scores: Sequence[float],
    *,
    minimum_gap: float = MIN_CALIBRATION_GAP,
    minimum_samples: int = CALIBRATION_SAMPLE_COUNT,
) -> CalibrationResult:
    """Choose the midpoint between the worst positive and best negative."""

    if len(positive_scores) < minimum_samples or len(negative_scores) < minimum_samples:
        raise CalibrationError("calibration_sample_count")
    positives = tuple(_validate_similarity(score) for score in positive_scores)
    negatives = tuple(_validate_similarity(score) for score in negative_scores)
    worst_positive = min(positives)
    best_negative = max(negatives)
    gap = worst_positive - best_negative
    if gap + 1e-12 < minimum_gap:
        raise CalibrationError("calibration_gap")
    return CalibrationResult(
        threshold=(worst_positive + best_negative) / 2.0,
        worst_positive=worst_positive,
        best_negative=best_negative,
        gap=gap,
        positive_count=len(positives),
        negative_count=len(negatives),
    )


def fuse_hybrid_results(
    *,
    fts_hits: Sequence[RankedRetrievalHit],
    vector_hits: Sequence[RankedRetrievalHit],
    items: Mapping[str, RetrievalItem] | Iterable[RetrievalItem],
    vector_threshold: float,
    now: datetime | None = None,
    limit: int = MAX_USER_RESULTS,
    query_text: str = "",
    recently_recalled_ids: Iterable[str] = (),
) -> tuple[HybridResult, ...]:
    """Fuse two top-30 lists with RRF, relevance gating, and quality weights."""

    _validate_similarity(vector_threshold)
    if limit <= 0:
        return ()
    item_map = (
        dict(items) if isinstance(items, Mapping) else {item.target_id: item for item in items}
    )
    fts_ranks = _first_ranks(fts_hits[:MAX_SOURCE_HITS])
    qualified_vector_hits = tuple(
        hit
        for hit in vector_hits[:MAX_SOURCE_HITS]
        if hit.score is not None and _validate_similarity(hit.score) >= vector_threshold
    )
    vector_ranks = _first_ranks(qualified_vector_hits)
    vector_scores = {hit.target_id: float(hit.score) for hit in qualified_vector_hits}
    candidate_ids = set(fts_ranks) | set(vector_ranks)
    reference_time = _as_utc(now or datetime.now(UTC))
    recently_recalled = frozenset(recently_recalled_ids)

    results: list[HybridResult] = []
    for target_id in candidate_ids:
        item = item_map.get(target_id)
        if item is None or not item.active or not item.current:
            continue
        _validate_item(item)
        fts_rank = fts_ranks.get(target_id)
        vector_rank = vector_ranks.get(target_id)
        rrf_score = sum(
            1.0 / (RRF_K + rank) for rank in (fts_rank, vector_rank) if rank is not None
        )
        quality = (item.importance + item.confidence) / 2.0
        decay = decay_weight(item, now=reference_time)
        pinned = PINNED_COEFFICIENT if item.pinned else 1.0
        success = 1.0
        evidence = max(0.25, min(1.25, item.status_weight * (1.0 + item.evidence_score / 8.0)))
        strong_relevance = vector_scores.get(
            target_id, -1.0
        ) >= STRONG_VECTOR_RELEVANCE or _topic_directly_hit(query_text, item.topic_key)
        repetition = (
            RECENT_RECALL_PENALTY
            if target_id in recently_recalled and not strong_relevance
            else 1.0
        )
        results.append(
            HybridResult(
                item=item,
                final_score=(
                    rrf_score * quality * decay * pinned * success * evidence * repetition
                ),
                rrf_score=rrf_score,
                quality_weight=quality,
                decay_weight=decay,
                pinned_weight=pinned,
                success_weight=success,
                evidence_weight=evidence,
                repetition_weight=repetition,
                fts_rank=fts_rank,
                vector_rank=vector_rank,
                vector_similarity=vector_scores.get(target_id),
            )
        )
    results.sort(key=lambda result: (-result.final_score, result.item.target_id))
    return tuple(results[:limit])


def build_retrieval_bundle(
    *,
    user_fts_hits: Sequence[RankedRetrievalHit],
    user_vector_hits: Sequence[RankedRetrievalHit],
    user_items: Mapping[str, RetrievalItem] | Iterable[RetrievalItem],
    reflection_fts_hits: Sequence[RankedRetrievalHit] = (),
    reflection_vector_hits: Sequence[RankedRetrievalHit] = (),
    reflection_items: Mapping[str, RetrievalItem] | Iterable[RetrievalItem] = (),
    persona_impression_fts_hits: Sequence[RankedRetrievalHit] = (),
    persona_impression_vector_hits: Sequence[RankedRetrievalHit] = (),
    persona_impression_items: Mapping[str, RetrievalItem] | Iterable[RetrievalItem] = (),
    persona_fts_hits: Sequence[RankedRetrievalHit],
    persona_vector_hits: Sequence[RankedRetrievalHit],
    persona_items: Mapping[str, RetrievalItem] | Iterable[RetrievalItem],
    vector_threshold: float,
    now: datetime | None = None,
    query_text: str = "",
    recently_recalled: Mapping[RetrievalCorpus, Iterable[str]] | None = None,
) -> RetrievalBundle:
    """Fuse user and persona stores independently and enforce their result caps."""

    recent = recently_recalled or {}
    users = fuse_hybrid_results(
        fts_hits=user_fts_hits,
        vector_hits=user_vector_hits,
        items=user_items,
        vector_threshold=vector_threshold,
        now=now,
        limit=MAX_USER_RESULTS,
        query_text=query_text,
        recently_recalled_ids=recent.get(RetrievalCorpus.USER_MEMORY, ()),
    )
    reflections = fuse_hybrid_results(
        fts_hits=reflection_fts_hits,
        vector_hits=reflection_vector_hits,
        items=reflection_items,
        vector_threshold=vector_threshold,
        now=now,
        limit=MAX_REFLECTION_RESULTS,
        query_text=query_text,
        recently_recalled_ids=recent.get(RetrievalCorpus.MEMORY_REFLECTION, ()),
    )
    impressions = fuse_hybrid_results(
        fts_hits=persona_impression_fts_hits,
        vector_hits=persona_impression_vector_hits,
        items=persona_impression_items,
        vector_threshold=vector_threshold,
        now=now,
        limit=MAX_PERSONA_IMPRESSION_RESULTS,
        query_text=query_text,
        recently_recalled_ids=recent.get(RetrievalCorpus.MEMORY_PERSONA_IMPRESSION, ()),
    )
    personas = fuse_hybrid_results(
        fts_hits=persona_fts_hits,
        vector_hits=persona_vector_hits,
        items=persona_items,
        vector_threshold=vector_threshold,
        now=now,
        limit=MAX_PERSONA_RESULTS,
        query_text=query_text,
        recently_recalled_ids=recent.get(RetrievalCorpus.PERSONA_KNOWLEDGE, ()),
    )
    semantic = sorted(
        (*users, *reflections, *impressions),
        key=lambda result: (-result.final_score, result.item.target_id),
    )[:MAX_DERIVED_AND_FACT_RESULTS]
    return RetrievalBundle(
        user_memories=tuple(
            result for result in semantic if result.item.corpus is RetrievalCorpus.USER_MEMORY
        ),
        reflections=tuple(
            result for result in semantic if result.item.corpus is RetrievalCorpus.MEMORY_REFLECTION
        ),
        persona_impressions=tuple(
            result
            for result in semantic
            if result.item.corpus is RetrievalCorpus.MEMORY_PERSONA_IMPRESSION
        ),
        persona_knowledge=personas,
    )


def decay_weight(item: RetrievalItem, *, now: datetime | None = None) -> float:
    """Return event decay; stable facts/preferences/relationships never decay."""

    if (
        item.kind.lower() != "event"
        or item.kind.lower() in _NON_DECAY_KINDS
        or item.pinned
        or item.importance >= 0.85
    ):
        return 1.0
    inactive_days = _inactive_days(
        item.last_successful_recall_at or item.created_at,
        now=now,
    )
    return 0.5 ** (inactive_days / EVENT_HALF_LIFE_DAYS)


def event_effective_score(item: RetrievalItem, *, now: datetime | None = None) -> float:
    """Return the importance-weighted score used by automatic event archival."""

    _validate_item(item)
    return item.importance * decay_weight(item, now=now)


def should_auto_archive(item: RetrievalItem, *, now: datetime | None = None) -> bool:
    """Archive only low-scoring ordinary events inactive for at least 90 days."""

    _validate_item(item)
    if not item.active or not item.current or item.kind.lower() != "event":
        return False
    if item.pinned or item.importance >= 0.85:
        return False
    inactive_days = _inactive_days(
        item.last_successful_recall_at or item.created_at,
        now=now,
    )
    return (
        inactive_days >= AUTO_ARCHIVE_INACTIVE_DAYS
        and event_effective_score(item, now=now) < AUTO_ARCHIVE_SCORE
    )


def build_retrieval_query(
    current_message: str,
    recent_turn_messages: Sequence[str] = (),
) -> str:
    """Append up to two prior turns only for pronoun/continuity queries."""

    current = current_message.strip()
    if not current:
        raise ValueError("current_message must not be empty")
    if not _CONTINUITY_PATTERN.search(current) or not recent_turn_messages:
        return current

    selected: list[str] = []
    remaining = MAX_CONTINUITY_CONTEXT_CHARS
    for message in reversed(recent_turn_messages[-4:]):
        cleaned = message.strip()
        if not cleaned or remaining <= 0:
            continue
        fragment = cleaned[-remaining:]
        selected.append(fragment)
        remaining -= len(fragment)
    if not selected:
        return current
    context = "\n".join(reversed(selected))
    return f"[当前消息]\n{current}\n[最近上下文]\n{context}"


def recall_is_successful(*, first_chunk_received: bool, terminal_state: str) -> bool:
    """Apply the precise recall-event boundary without depending on Qt/chat enums."""

    return first_chunk_received and terminal_state.strip().lower() in _SUCCESS_TERMINALS


def _first_ranks(hits: Sequence[RankedRetrievalHit]) -> dict[str, int]:
    result: dict[str, int] = {}
    for hit in hits:
        if not hit.target_id:
            raise ValueError("retrieval target_id must not be empty")
        if hit.rank < 1:
            raise ValueError("retrieval rank must be at least 1")
        result.setdefault(hit.target_id, hit.rank)
    return result


def _validate_item(item: RetrievalItem) -> None:
    if not item.target_id or not item.content:
        raise ValueError("retrieval item identity and content must not be empty")
    if not 0.0 <= item.importance <= 1.0 or not math.isfinite(item.importance):
        raise ValueError("importance must be finite and between 0 and 1")
    if not 0.0 <= item.confidence <= 1.0 or not math.isfinite(item.confidence):
        raise ValueError("confidence must be finite and between 0 and 1")
    if item.successful_recall_count < 0:
        raise ValueError("successful_recall_count must not be negative")
    if not math.isfinite(item.evidence_score):
        raise ValueError("evidence_score must be finite")
    if not math.isfinite(item.status_weight) or item.status_weight <= 0.0:
        raise ValueError("status_weight must be finite and positive")


def _topic_directly_hit(query: str, topic_key: str) -> bool:
    normalized_query = " ".join(query.casefold().split())
    normalized_topic = " ".join(topic_key.casefold().split())
    if not normalized_query or not normalized_topic:
        return False
    if normalized_topic in normalized_query:
        return True
    terms = tuple(term for term in re.split(r"[^\w\u3400-\u9fff]+", normalized_topic) if term)
    return bool(terms) and all(term in normalized_query for term in terms)


def _validate_similarity(value: float) -> float:
    score = float(value)
    if not math.isfinite(score) or not -1.0 <= score <= 1.0:
        raise ValueError("similarity must be finite and between -1 and 1")
    return score


def _inactive_days(last_activity_at: datetime | None, *, now: datetime | None) -> float:
    if last_activity_at is None:
        return 0.0
    reference = _as_utc(now or datetime.now(UTC))
    activity = _as_utc(last_activity_at)
    return max(0.0, (reference - activity).total_seconds() / 86_400.0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
