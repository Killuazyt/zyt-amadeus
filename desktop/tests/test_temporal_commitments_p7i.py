from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QObject, Signal

from amadeus_desktop.conversation_store import ConversationStore
from amadeus_desktop.data_management import (
    CHAT_EXPORT_FORMAT,
    REMINDER_EXPORT_FORMAT,
    SQLiteExportRepository,
    export_chat_json,
    export_reminders_json,
)
from amadeus_desktop.database import SCHEMA_VERSION, SQLiteDatabase
from amadeus_desktop.presence import PresenceSnapshot
from amadeus_desktop.reminder_scheduler import ReminderScheduler
from amadeus_desktop.storage_models import (
    StorageValidationError,
    TemporalCommitmentKind,
    TemporalCommitmentStatus,
    TemporalVersionOrigin,
)
from amadeus_desktop.temporal_commitments import (
    TemporalCommitmentStore,
    TemporalDraftSpec,
    TemporalDueSnapshot,
)
from amadeus_desktop.temporal_parser import parse_temporal_intent


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.mark.parametrize(
    ("text", "kind", "expected_local", "content"),
    (
        ("明天下午三点提醒我交报告", "reminder", "2026-09-02 15:00", "交报告"),
        (
            "今晚十点到时候问问我论文写完没有",
            "scheduled_followup",
            "2026-09-01 22:00",
            "论文写完没有",
        ),
        ("15 分钟后提醒我关烤箱", "reminder", "2026-09-01 12:15", "关烤箱"),
        (
            "remind me in 15 minutes to turn off the oven",
            "reminder",
            "2026-09-01 12:15",
            "to turn off the oven",
        ),
        ("remind me tomorrow at 3 pm to submit", "reminder", "2026-09-02 15:00", "to submit"),
    ),
)
def test_parser_accepts_explicit_bilingual_time_commands(
    text: str,
    kind: str,
    expected_local: str,
    content: str,
) -> None:
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 9, 1, 12, 0, tzinfo=zone)

    parsed = parse_temporal_intent(text, now=now)

    assert parsed.resolved
    assert parsed.kind is TemporalCommitmentKind(kind)
    assert parsed.due_at_utc is not None
    assert parsed.due_at_utc.astimezone(zone).strftime("%Y-%m-%d %H:%M") == expected_local
    assert parsed.content == content


def test_parser_rejects_event_statement_and_keeps_ambiguous_time_as_draft() -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert not parse_temporal_intent("明天下午三点我要交报告", now=now).matched
    ambiguous = parse_temporal_intent("明天下午三点或四点提醒我交报告", now=now)
    assert ambiguous.matched
    assert not ambiguous.resolved
    assert ambiguous.issue == "ambiguous_time"


def test_parser_refuses_nonexistent_and_duplicated_dst_wall_times() -> None:
    zone = ZoneInfo("America/New_York")

    nonexistent = parse_temporal_intent(
        "remind me 2026-03-08 02:30 to check the clock",
        now=datetime(2026, 3, 1, 12, 0, tzinfo=zone),
    )
    duplicated = parse_temporal_intent(
        "remind me 2026-11-01 01:30 to check the clock",
        now=datetime(2026, 10, 1, 12, 0, tzinfo=zone),
    )

    assert nonexistent.matched and nonexistent.issue == "invalid_time"
    assert duplicated.matched and duplicated.issue == "ambiguous_time"


def test_relative_time_is_an_absolute_duration_across_dst_change() -> None:
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 3, 8, 0, 30, tzinfo=zone)

    parsed = parse_temporal_intent("remind me in 2 hours to check", now=now)

    assert parsed.resolved
    assert parsed.due_at_utc == now.astimezone(UTC) + timedelta(hours=2)
    assert parsed.due_at_utc.astimezone(zone).strftime("%H:%M") == "03:30"


def test_yearless_month_day_uses_the_next_future_calendar_date() -> None:
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 9, 1, 12, 0, tzinfo=zone)

    parsed = parse_temporal_intent("9月1日上午十点提醒我续费", now=now)

    assert parsed.resolved
    assert parsed.due_at_utc is not None
    assert parsed.due_at_utc.astimezone(zone).strftime("%Y-%m-%d %H:%M") == (
        "2027-09-01 10:00"
    )


@pytest.fixture
def temporal_store(tmp_path):
    clock = _Clock(datetime(2026, 9, 1, 4, 0, tzinfo=UTC))
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    conversations = ConversationStore(database, clock=clock)
    conversation = conversations.create_conversation("P7I")
    store = TemporalCommitmentStore(database, clock=clock)
    try:
        yield clock, database, conversations, conversation, store
    finally:
        database.close()


def _spec(
    due: datetime,
    *,
    content: str = "交报告",
    kind: TemporalCommitmentKind = TemporalCommitmentKind.REMINDER,
    show_content: bool = False,
) -> TemporalDraftSpec:
    local = due.astimezone(ZoneInfo("Asia/Shanghai"))
    return TemporalDraftSpec(
        kind,
        content,
        due,
        local.isoformat(timespec="minutes"),
        "Asia/Shanghai",
        480,
        show_content,
    )


def test_unconfirmed_draft_never_becomes_due_and_confirmation_is_versioned(
    temporal_store,
) -> None:
    clock, _database, _conversations, conversation, store = temporal_store
    due = clock.value + timedelta(minutes=10)
    draft = store.create_chat_draft(
        conversation.conversation_id,
        "十分钟后提醒我交报告",
        _spec(due),
    )

    clock.value = due + timedelta(seconds=1)
    assert store.scan_due().due == ()

    clock.value = due - timedelta(minutes=5)
    scheduled = store.confirm(draft.commitment_id, _spec(due, content="提交报告"))
    assert scheduled.status is TemporalCommitmentStatus.SCHEDULED
    assert scheduled.current_version.version_number == 2
    assert scheduled.current_version.origin is TemporalVersionOrigin.EDIT

    clock.value = due + timedelta(seconds=1)
    first = store.scan_due()
    assert [item.commitment_id for item in first.due] == [draft.commitment_id]
    assert [item.commitment_id for item in first.became_due] == [draft.commitment_id]
    assert store.scan_due().became_due == ()
    surfaced = store.mark_surfaced(
        (draft.commitment_id,),
        reason_code="test_notification",
    )
    assert len(surfaced) == 1
    assert surfaced[0].status is TemporalCommitmentStatus.SURFACED
    assert store.mark_surfaced((draft.commitment_id,), reason_code="duplicate") == ()


def test_versions_are_immutable_and_audit_contains_no_body(temporal_store) -> None:
    clock, database, _conversations, _conversation, store = temporal_store
    draft = store.create_manual_draft(_spec(clock.value + timedelta(hours=1)))
    revised = store.confirm(
        draft.commitment_id,
        _spec(clock.value + timedelta(hours=2), content="修改后的正文"),
    )

    assert len(store.versions(draft.commitment_id)) == 2
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute(
            "UPDATE temporal_commitment_versions SET content = 'leak' WHERE id = ?",
            (revised.current_version.version_id,),
        )
    audit = store.audits(draft.commitment_id)
    assert [item.event_type for item in audit] == ["created", "confirmed"]
    assert all(not hasattr(item, "content") for item in audit)


def test_five_year_limit_and_permanent_delete_remove_all_version_text(
    temporal_store,
) -> None:
    clock, database, _conversations, _conversation, store = temporal_store
    draft = store.create_manual_draft(_spec(clock.value + timedelta(hours=1)))
    with pytest.raises(StorageValidationError, match="horizon"):
        store.confirm(
            draft.commitment_id,
            _spec(clock.value + timedelta(days=(5 * 366) + 1)),
        )

    store.delete(draft.commitment_id)

    assert database.connection.execute(
        "SELECT 1 FROM temporal_commitment_versions WHERE commitment_id = ?",
        (draft.commitment_id,),
    ).fetchone() is None
    deleted_audit = database.connection.execute(
        """
        SELECT event_type, reason_code FROM temporal_commitment_audit_events
        WHERE commitment_id = ? ORDER BY occurred_at DESC LIMIT 1
        """,
        (draft.commitment_id,),
    ).fetchone()
    assert tuple(deleted_audit) == ("deleted", "content_and_sources_removed")


def test_chat_source_becomes_content_free_tombstone_after_conversation_delete(
    temporal_store,
) -> None:
    clock, _database, conversations, conversation, store = temporal_store
    draft = store.create_chat_draft(
        conversation.conversation_id,
        "一小时后提醒我喝水",
        _spec(clock.value + timedelta(hours=1), content="喝水"),
    )

    assert conversations.delete_conversation(conversation.conversation_id)
    preserved = store.get(draft.commitment_id)
    assert preserved.source_deleted
    assert preserved.source_message_id is not None
    assert preserved.source_conversation_id == conversation.conversation_id
    assert preserved.live_source_message_id is None
    assert preserved.live_source_conversation_id is None
    assert preserved.current_version.content == "喝水"


def test_scheduled_followup_click_writes_frozen_local_line_and_completes(
    temporal_store,
) -> None:
    clock, database, _conversations, conversation, store = temporal_store
    due = clock.value + timedelta(minutes=1)
    draft = store.create_manual_draft(
        _spec(
            due,
            content="论文写完没有",
            kind=TemporalCommitmentKind.SCHEDULED_FOLLOWUP,
        )
    )
    store.confirm(
        draft.commitment_id,
        _spec(
            due,
            content="论文写完没有",
            kind=TemporalCommitmentKind.SCHEDULED_FOLLOWUP,
        ),
    )
    store.scan_due(now=due + timedelta(seconds=1))

    message_id = store.open_followup_in_chat(
        draft.commitment_id,
        conversation.conversation_id,
    )

    row = database.connection.execute(
        "SELECT content, origin, participates_in_memory FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    assert tuple(row) == (
        "喂，关于“论文写完没有”——所以，做完了吗？",
        "proactive",
        0,
    )
    assert store.get(draft.commitment_id).status is TemporalCommitmentStatus.COMPLETED


def test_exports_use_v4_and_reminder_v1_without_audit_content(tmp_path, temporal_store) -> None:
    clock, database, _conversations, _conversation, store = temporal_store
    store.create_manual_draft(_spec(clock.value + timedelta(hours=1), content="私密正文"))
    repository = SQLiteExportRepository(database.connection)

    chat_path = export_chat_json(tmp_path / "chat.json", repository.load_chat_bundle)
    reminder_path = export_reminders_json(
        tmp_path / "reminders.json",
        repository.load_reminder_bundle,
    )
    chat = json.loads(chat_path.read_text(encoding="utf-8"))
    reminders = json.loads(reminder_path.read_text(encoding="utf-8"))

    assert chat["format"] == CHAT_EXPORT_FORMAT == "amadeus-chat-export/v4"
    assert reminders["format"] == REMINDER_EXPORT_FORMAT == "amadeus-reminder-export/v1"
    assert reminders["database_schema"] == SCHEMA_VERSION == 8
    assert reminders["versions"][0]["content"] == "私密正文"
    assert all("content" not in event for event in reminders["audit_events"])


class _FakeData(QObject):
    temporal_due_scanned = Signal(object)
    temporal_commitment_changed = Signal(object)
    operation_failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.scans = 0
        self.surfaced: list[tuple[str, ...]] = []

    def scan_due_temporal_commitments(self, *, now: datetime | None = None) -> bool:
        del now
        self.scans += 1
        return True

    def mark_temporal_commitments_surfaced(
        self,
        commitment_ids: tuple[str, ...],
        *,
        reason_code: str,
    ) -> bool:
        del reason_code
        self.surfaced.append(commitment_ids)
        return True


class _UnlockedPresence:
    def snapshot(self) -> PresenceSnapshot:
        return PresenceSnapshot(0.0, False, False)


def test_scheduler_deduplicates_repeated_due_snapshots_before_state_commit(
    qapp,
    temporal_store,
) -> None:
    del qapp
    clock, _database, _conversations, _conversation, store = temporal_store
    scheduled_spec = _spec(clock.value + timedelta(minutes=1))
    item = store.create_manual_draft(scheduled_spec)
    item = store.confirm(item.commitment_id, scheduled_spec)
    # Build the due snapshot explicitly; this test exercises scheduler delivery
    # idempotency without starting its real timers.
    due_item = store.scan_due(now=clock.value + timedelta(minutes=2)).due[0]
    data = _FakeData()
    notifications: list[object] = []
    scheduler = ReminderScheduler(
        data,  # type: ignore[arg-type]
        presence_probe=_UnlockedPresence(),  # type: ignore[arg-type]
        notify=lambda _title, _body, payload: notifications.append(payload) is None,
        show_followup=lambda _item: False,
        followup_safe=lambda: True,
        clock=clock,
    )
    scheduler._running = True
    snapshot = TemporalDueSnapshot((due_item,), None, 1)

    scheduler._on_due_scanned(snapshot)
    scheduler._on_due_scanned(snapshot)

    assert len(notifications) == 1
    assert data.surfaced == [(item.commitment_id,)]
