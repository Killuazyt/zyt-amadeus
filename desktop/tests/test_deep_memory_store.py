from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from amadeus_desktop.conversation_store import ConversationStore
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.deep_memory_store import (
    EVIDENCE_ARCHIVE_DAYS,
    DeepMemoryStore,
)
from amadeus_desktop.memory_models import (
    ConflictResolution,
    DerivedMemoryStatus,
    EvidenceSignalKind,
    MemoryLayer,
)
from amadeus_desktop.memory_store import MemoryStore
from amadeus_desktop.storage_models import (
    StorageConflictError,
    StorageNotFoundError,
    StorageValidationError,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 11, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def deep_fixture(tmp_path):
    clock = MutableClock()
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    conversations = ConversationStore(database, clock=clock)
    facts = MemoryStore(database, clock=clock)
    deep = DeepMemoryStore(database, clock=clock)
    conversation = conversations.create_conversation()
    fact_ids: list[str] = []
    for index in range(5):
        message_id = f"user-{index}"
        conversations.save_user_message(
            conversation.conversation_id,
            f"turn-{index}",
            message_id,
            f"第 {index + 1} 次说明我重视稳定陪伴",
        )
        fact = facts.create_memory(
            "relationship",
            f"陪伴:{index}",
            f"用户第 {index + 1} 次表达重视稳定陪伴",
            importance=0.9 if index == 0 else 0.7,
            confidence=0.9,
            source_message_ids=(message_id,),
        )
        fact_ids.append(fact.current_version.version_id)
        clock.value += timedelta(minutes=1)
    try:
        yield database, conversations, facts, deep, conversation, fact_ids, clock
    finally:
        database.close()


def test_reflection_has_immutable_versions_sources_fts_and_initial_evidence(deep_fixture) -> None:
    database, _conversations, _facts, deep, _conversation, fact_ids, _clock = deep_fixture

    reflection = deep.create_reflection(
        "用户倾向于通过稳定、持续的互动建立信任",
        "关系 信任",
        fact_version_ids=fact_ids,
        subject_scope="relationship",
        importance=0.9,
        confidence=0.8,
    )

    assert reflection.status is DerivedMemoryStatus.TENTATIVE
    assert reflection.evidence_score == pytest.approx(0.6)
    assert len(deep.list_sources(MemoryLayer.REFLECTION, reflection.group_id)) == 5
    assert (
        deep.search(MemoryLayer.REFLECTION, "稳定信任", include_inactive=True)[0][0] == reflection
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        database.connection.execute(
            "UPDATE memory_reflection_versions SET content = 'tampered' WHERE id = ?",
            (reflection.current_version.version_id,),
        )


def test_evidence_combo_confirmation_promotion_and_static_persona_isolation(deep_fixture) -> None:
    database, conversations, facts, deep, conversation, fact_ids, clock = deep_fixture
    reflection = deep.create_reflection(
        "用户把稳定陪伴视为关系中的重要基础",
        "关系 稳定陪伴",
        fact_version_ids=fact_ids,
        subject_scope="relationship",
        importance=0.8,
        confidence=0.8,
    )
    for index in range(3):
        message_id = f"reinforce-{index}"
        conversations.save_user_message(
            conversation.conversation_id,
            f"turn-reinforce-{index}",
            message_id,
            "我仍然觉得稳定陪伴很重要",
        )
        evidence_fact = facts.create_memory(
            "relationship",
            f"强化陪伴:{index}",
            "用户再次明确表示稳定陪伴很重要",
            source_message_ids=(message_id,),
        )
        deep.apply_signal(
            MemoryLayer.REFLECTION,
            reflection.group_id,
            EvidenceSignalKind.INDIRECT_SUPPORT,
            source_message_id=message_id,
            source_fact_version_id=evidence_fact.current_version.version_id,
            correlation_key=f"support:{index}",
        )
        clock.value += timedelta(seconds=1)

    confirmed = deep.get(MemoryLayer.REFLECTION, reflection.group_id)
    assert confirmed.status is DerivedMemoryStatus.CONFIRMED
    assert confirmed.evidence_score > 2.0
    impression = deep.promote_reflection(reflection.group_id)
    assert impression.status is DerivedMemoryStatus.ACTIVE
    assert impression.subject_scope.value == "relationship"
    assert deep.get(MemoryLayer.REFLECTION, reflection.group_id).status is (
        DerivedMemoryStatus.PROMOTED
    )
    assert database.connection.execute("SELECT COUNT(*) FROM persona_knowledge").fetchone()[0] == 0
    assert (
        database.connection.execute("SELECT COUNT(*) FROM memory_persona_impressions").fetchone()[0]
        == 1
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        database.connection.execute(
            "UPDATE memory_persona_impression_versions SET content = 'tampered' WHERE id = ?",
            (impression.current_version.version_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        database.connection.execute("UPDATE memory_evidence_signals SET reinforcement_delta = 99")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        database.connection.execute("UPDATE memory_audit_events SET reason_code = 'tampered'")


def test_ambiguous_fact_conflict_suppresses_recall_and_three_resolutions(deep_fixture) -> None:
    _database, conversations, facts, deep, conversation, _fact_ids, _clock = deep_fixture
    source = conversations.save_user_message(
        conversation.conversation_id,
        "turn-conflict",
        "user-conflict",
        "我好像不再那么看重稳定陪伴了",
    )
    target = facts.list_memories()[0]
    conflict = deep.open_fact_conflict(
        target.memory_id,
        "用户不再看重稳定陪伴",
        source_message_id=source.message_id,
        importance=0.8,
        confidence=0.7,
    )
    assert not any(item.memory.memory_id == target.memory_id for item in facts.search("稳定陪伴"))

    deep.resolve_fact_conflict(conflict.conflict_id, ConflictResolution.ACCEPT)
    assert facts.get(target.memory_id).current_version.content == "用户不再看重稳定陪伴"

    keep_target = facts.list_memories()[1]
    keep_source = conversations.save_user_message(
        conversation.conversation_id,
        "turn-conflict-keep",
        "user-conflict-keep",
        "我似乎有不同想法",
    )
    keep = deep.open_fact_conflict(
        keep_target.memory_id,
        "模糊的新说法",
        source_message_id=keep_source.message_id,
        importance=0.5,
        confidence=0.6,
    )
    incumbent = keep.incumbent_version_id
    deep.resolve_fact_conflict(keep.conflict_id, ConflictResolution.KEEP)
    assert facts.get(keep_target.memory_id).current_version.version_id == incumbent
    edited = facts.edit_memory(keep_target.memory_id, "用户手工确认的后续版本")
    assert edited.current_version.version_number == 3

    merge_target = facts.list_memories()[2]
    merge_source = conversations.save_user_message(
        conversation.conversation_id,
        "turn-conflict-merge",
        "user-conflict-merge",
        "这两种说法需要合并",
    )
    merge = deep.open_fact_conflict(
        merge_target.memory_id,
        "需要合并的新说法",
        source_message_id=merge_source.message_id,
        importance=0.7,
        confidence=0.7,
    )
    deep.resolve_fact_conflict(
        merge.conflict_id,
        ConflictResolution.MERGE,
        merged_content="用户手工合并后的当前事实",
    )
    merged = facts.get(merge_target.memory_id)
    assert merged.current_version.content == "用户手工合并后的当前事实"
    assert merged.current_version.version_number == 3


def test_synthesis_batch_is_atomic_and_idempotent_across_retry(deep_fixture) -> None:
    database, _conversations, _facts, deep, _conversation, fact_ids, _clock = deep_fixture
    valid = {
        "content": "用户长期重视稳定互动",
        "topic_key": "稳定互动",
        "fact_version_ids": fact_ids,
        "subject_scope": "relationship",
        "importance": 0.8,
        "confidence": 0.8,
    }
    invalid = {**valid, "content": "无效来源", "fact_version_ids": (*fact_ids[:4], "missing")}
    with pytest.raises(StorageValidationError):
        deep.create_reflection_batch((valid, invalid), batch_key="batch-atomic")
    assert database.connection.execute("SELECT COUNT(*) FROM memory_reflections").fetchone()[0] == 0

    second = {**valid, "content": "用户也重视可预期的交流节奏", "topic_key": "交流节奏"}
    first_result = deep.create_reflection_batch(
        (valid, second),
        batch_key="batch-stable",
    )
    retry_result = deep.create_reflection_batch(
        (valid, second),
        batch_key="batch-stable",
    )
    assert tuple(item.group_id for item in first_result) == tuple(
        item.group_id for item in retry_result
    )
    assert database.connection.execute("SELECT COUNT(*) FROM memory_reflections").fetchone()[0] == 2


def test_evidence_uses_distinct_half_lives_and_third_support_bonus(deep_fixture) -> None:
    _database, conversations, facts, deep, conversation, fact_ids, clock = deep_fixture
    reflection = deep.create_reflection(
        "用户可能重视可预期的交流",
        "交流",
        fact_version_ids=fact_ids,
        importance=0.5,
        confidence=0.8,
    )
    for index in range(3):
        message = conversations.save_user_message(
            conversation.conversation_id,
            f"turn-support-half-life-{index}",
            f"support-half-life-{index}",
            "我仍然重视可预期的交流",
        )
        evidence_fact = facts.create_memory(
            "fact",
            f"交流强化:{index}",
            "用户再次表示重视可预期的交流",
            source_message_ids=(message.message_id,),
        )
        snapshot = deep.apply_signal(
            MemoryLayer.REFLECTION,
            reflection.group_id,
            EvidenceSignalKind.INDIRECT_SUPPORT,
            source_message_id=message.message_id,
            source_fact_version_id=evidence_fact.current_version.version_id,
            correlation_key=f"half-life-support:{index}",
        )
    assert snapshot.reinforcement == pytest.approx(2.0)
    rebuttal = conversations.save_user_message(
        conversation.conversation_id,
        "turn-half-life-rebuttal",
        "half-life-rebuttal",
        "这并不准确",
    )
    snapshot = deep.apply_signal(
        MemoryLayer.REFLECTION,
        reflection.group_id,
        EvidenceSignalKind.DIRECT_REBUT,
        source_message_id=rebuttal.message_id,
        correlation_key="half-life-rebuttal",
    )
    assert snapshot.disputation == pytest.approx(1.0)

    clock.value += timedelta(days=30)
    decayed = deep.evidence_snapshot(
        MemoryLayer.REFLECTION,
        reflection.current_version.version_id,
    )
    assert decayed.reinforcement == pytest.approx(1.0)
    assert decayed.disputation == pytest.approx(0.5 ** (30 / 180))


def test_delete_impact_and_audit_never_copy_semantic_body(deep_fixture) -> None:
    database, _conversations, facts, deep, _conversation, fact_ids, _clock = deep_fixture
    reflection = deep.create_reflection(
        "这段语义正文不得进入审计事件",
        "审计隔离",
        fact_version_ids=fact_ids,
        importance=1.0,
        confidence=0.9,
    )
    deep.confirm(MemoryLayer.REFLECTION, reflection.group_id)
    deep.confirm(MemoryLayer.REFLECTION, reflection.group_id)
    impression = deep.promote_reflection(reflection.group_id)
    source_fact = facts.list_memories()[0]
    assert deep.deletion_impact(MemoryLayer.FACT, source_fact.memory_id) == {
        "facts": 1,
        "reflections": 1,
        "personas": 1,
    }
    serialized_audit = "\n".join(
        "|".join(str(value) for value in row)
        for row in database.connection.execute(
            """
            SELECT event_type, reason_code, metadata_json
            FROM memory_audit_events WHERE owner_group_id IN (?, ?)
            """,
            (reflection.group_id, impression.group_id),
        ).fetchall()
    )
    assert "这段语义正文" not in serialized_audit


def test_event_timeline_is_projection_of_event_fact_not_a_sixth_copy(deep_fixture) -> None:
    database, conversations, facts, deep, conversation, _fact_ids, clock = deep_fixture
    message = conversations.save_user_message(
        conversation.conversation_id,
        "event-turn",
        "event-user",
        "2026 年 8 月 10 日我完成了阶段复盘",
    )
    occurred_at = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
    event = facts.create_memory(
        "event",
        "阶段复盘",
        "用户完成了阶段复盘",
        source_message_ids=(message.message_id,),
        event_started_at=occurred_at,
        time_confidence=0.95,
    )
    timeline = deep.event_timeline()
    assert len(timeline) == 1
    assert timeline[0].memory_id == event.memory_id
    assert timeline[0].version_id == event.current_version.version_id
    assert timeline[0].occurred_at == occurred_at
    assert timeline[0].occurred_at_is_explicit
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name LIKE '%timeline%'"
        ).fetchone()[0]
        == 0
    )
    assert clock.value >= timeline[0].occurred_at


def test_low_score_archives_only_after_fourteen_days_and_pinned_is_protected(deep_fixture) -> None:
    _database, conversations, _facts, deep, conversation, fact_ids, clock = deep_fixture
    reflection = deep.create_reflection(
        "用户可能偏好固定节奏",
        "固定节奏",
        fact_version_ids=fact_ids,
        importance=0.6,
        confidence=0.7,
    )
    for index in range(3):
        message = conversations.save_user_message(
            conversation.conversation_id,
            f"turn-rebut-{index}",
            f"user-rebut-{index}",
            "这并不准确",
        )
        deep.apply_signal(
            MemoryLayer.REFLECTION,
            reflection.group_id,
            EvidenceSignalKind.DIRECT_REBUT,
            source_message_id=message.message_id,
            correlation_key=f"rebut:{index}",
        )
    deep.run_maintenance()
    clock.value += timedelta(days=EVIDENCE_ARCHIVE_DAYS - 1)
    assert deep.run_maintenance() == 0
    clock.value += timedelta(days=1, seconds=1)
    assert deep.run_maintenance() == 1
    assert deep.get(MemoryLayer.REFLECTION, reflection.group_id).status is (
        DerivedMemoryStatus.ARCHIVED
    )

    pinned = deep.create_reflection(
        "用户可能偏好固定回复时间",
        "固定回复时间",
        fact_version_ids=fact_ids,
        importance=0.5,
        confidence=0.6,
    )
    for index in range(3):
        message = conversations.save_user_message(
            conversation.conversation_id,
            f"turn-pinned-rebut-{index}",
            f"user-pinned-rebut-{index}",
            "固定回复时间这个判断不准确",
        )
        deep.apply_signal(
            MemoryLayer.REFLECTION,
            pinned.group_id,
            EvidenceSignalKind.DIRECT_REBUT,
            source_message_id=message.message_id,
            correlation_key=f"pinned-rebut:{index}",
        )
    deep.set_pinned(MemoryLayer.REFLECTION, pinned.group_id, True)
    clock.value += timedelta(days=30)
    assert deep.run_maintenance() == 0
    assert deep.get(MemoryLayer.REFLECTION, pinned.group_id).status is (
        DerivedMemoryStatus.DISPUTED
    )


def test_cascade_delete_removes_derived_descendants_and_audit(deep_fixture) -> None:
    database, conversations, facts, deep, conversation, fact_ids, _clock = deep_fixture
    reflection = deep.create_reflection(
        "用户重视稳定陪伴",
        "关系",
        fact_version_ids=fact_ids,
        importance=1.0,
        confidence=0.9,
    )
    message = conversations.save_user_message(
        conversation.conversation_id,
        "turn-confirm",
        "user-confirm",
        "对，这很准确",
    )
    for index in range(2):
        deep.apply_signal(
            MemoryLayer.REFLECTION,
            reflection.group_id,
            EvidenceSignalKind.DIRECT_CONFIRM,
            source_message_id=message.message_id,
            correlation_key=f"confirm:{index}",
        )
    impression = deep.promote_reflection(reflection.group_id)
    source_fact = facts.list_memories()[0]

    assert deep.delete_cascade(MemoryLayer.FACT, source_fact.memory_id) >= 3
    with pytest.raises(StorageNotFoundError):
        deep.get(MemoryLayer.REFLECTION, reflection.group_id)
    with pytest.raises(StorageNotFoundError):
        deep.get(MemoryLayer.PERSONA, impression.group_id)
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM memory_audit_events WHERE owner_group_id IN (?, ?)",
            (reflection.group_id, impression.group_id),
        ).fetchone()[0]
        == 0
    )


def test_upstream_archive_suppresses_and_safe_restore_reevaluates_lineage(deep_fixture) -> None:
    _database, _conversations, facts, deep, _conversation, fact_ids, _clock = deep_fixture
    reflection = deep.create_reflection(
        "用户重视稳定陪伴",
        "关系",
        fact_version_ids=fact_ids,
        importance=1.0,
        confidence=0.9,
    )
    deep.confirm(MemoryLayer.REFLECTION, reflection.group_id)
    deep.confirm(MemoryLayer.REFLECTION, reflection.group_id)
    impression = deep.promote_reflection(reflection.group_id)
    source_fact = facts.list_memories()[0]

    facts.archive(source_fact.memory_id)
    deep.suppress_fact_descendants(source_fact.memory_id, reason_code="test_archive")
    assert deep.get(MemoryLayer.PERSONA, impression.group_id).status is (
        DerivedMemoryStatus.DISPUTED
    )
    facts.restore(source_fact.memory_id)
    deep.reevaluate_fact_descendants(source_fact.memory_id)
    assert deep.get(MemoryLayer.PERSONA, impression.group_id).status is DerivedMemoryStatus.ACTIVE

    facts.edit_memory(source_fact.memory_id, "用户现在更重视自主空间")
    deep.suppress_fact_descendants(source_fact.memory_id, reason_code="test_edit")
    deep.reevaluate_fact_descendants(source_fact.memory_id)
    assert deep.get(MemoryLayer.PERSONA, impression.group_id).status is (
        DerivedMemoryStatus.DISPUTED
    )


def test_promotion_failure_does_not_create_fallback_persona(deep_fixture) -> None:
    database, _conversations, _facts, deep, _conversation, fact_ids, _clock = deep_fixture
    reflection = deep.create_reflection(
        "证据不足的反思",
        "证据不足",
        fact_version_ids=fact_ids,
        importance=0.6,
        confidence=0.7,
    )
    with pytest.raises(StorageConflictError):
        deep.promote_reflection(reflection.group_id)
    assert (
        database.connection.execute("SELECT COUNT(*) FROM memory_persona_impressions").fetchone()[0]
        == 0
    )


def test_model_promotion_rejection_is_audited_without_denying_reflection(deep_fixture) -> None:
    database, _conversations, _facts, deep, _conversation, fact_ids, _clock = deep_fixture
    reflection = deep.create_reflection(
        "已确认但不应提升的人格观察",
        "提升拒绝",
        fact_version_ids=fact_ids,
        importance=1.0,
        confidence=0.9,
    )
    deep.confirm(MemoryLayer.REFLECTION, reflection.group_id)
    deep.confirm(MemoryLayer.REFLECTION, reflection.group_id)

    rejected = deep.record_promotion_rejection(reflection.group_id)

    assert rejected.status is DerivedMemoryStatus.CONFIRMED
    assert (
        database.connection.execute("SELECT COUNT(*) FROM memory_persona_impressions").fetchone()[0]
        == 0
    )
    event = database.connection.execute(
        """
        SELECT event_type, reason_code, metadata_json
        FROM memory_audit_events
        WHERE owner_group_id = ? AND event_type = 'reflection.promotion_rejected'
        """,
        (reflection.group_id,),
    ).fetchone()
    assert tuple(event) == ("reflection.promotion_rejected", "model_reject", "{}")
