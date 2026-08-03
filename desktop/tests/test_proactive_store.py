from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from PySide6.QtCore import QRect, Qt

from amadeus_desktop.conversation_store import (
    ConversationStore,
    ProactiveInteractionStore,
)
from amadeus_desktop.data_runtime import SerialDataThread
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.local_data_service import (
    ConversationSnapshot,
    LocalDataService,
    _presentation_entries_from_messages,
    _turns_from_messages,
    create_local_data_stores,
)
from amadeus_desktop.presence import PresenceProbe, PresenceSnapshot
from amadeus_desktop.proactive_controller import ProactiveInteractionController
from amadeus_desktop.storage_models import (
    ProactiveDisposition,
    ProactiveTrigger,
    StorageConflictError,
    StorageValidationError,
    StoredMessageOrigin,
    StoredMessageRole,
    StoredMessageStatus,
)
from amadeus_desktop.ui.greeting_bubble import GreetingBubble


@pytest.fixture
def stores(tmp_path):
    now = datetime(2026, 8, 3, 1, 2, 3, tzinfo=UTC)
    database = SQLiteDatabase(tmp_path / "amadeus.sqlite3").open()
    conversations = ConversationStore(database, clock=lambda: now)
    conversations.ensure_default_profile()
    proactive = ProactiveInteractionStore(database, clock=lambda: now)
    try:
        yield database, conversations, proactive, now
    finally:
        database.close()


def test_display_ledger_is_content_free_counted_by_local_date_and_durable(
    stores,
) -> None:
    database, _conversations, proactive, now = stores
    today = date(2026, 8, 3)
    startup = proactive.record_displayed(
        ProactiveTrigger.STARTUP,
        today,
        event_id="startup-event",
    )
    proactive.record_displayed("idle", today, event_id="idle-event")
    proactive.record_displayed("startup", date(2026, 8, 4), event_id="tomorrow-event")

    assert startup.local_date == today
    assert startup.trigger is ProactiveTrigger.STARTUP
    assert startup.displayed_at == now
    assert startup.disposition is ProactiveDisposition.DISPLAYED
    assert startup.message_id is None
    assert proactive.count_displayed_for_date(today) == 2
    assert proactive.count_for_date(date(2026, 8, 4)) == 1
    assert [event.event_id for event in proactive.list_for_date(today)] == [
        "idle-event",
        "startup-event",
    ]

    columns = {
        row["name"] for row in database.connection.execute("PRAGMA table_info(proactive_events)")
    }
    assert columns == {
        "id",
        "profile_id",
        "local_date",
        "trigger_kind",
        "displayed_at",
        "disposition",
        "message_id",
    }
    assert "content" not in columns

    dismissed = proactive.record_dismissed("idle-event")
    assert dismissed.disposition is ProactiveDisposition.DISMISSED
    assert proactive.record_dismissed("idle-event") == dismissed
    with pytest.raises(StorageConflictError):
        proactive.record_clicked("idle-event", "missing", "不会写入")
    with pytest.raises(StorageValidationError):
        proactive.record_displayed("unsupported", today)
    with pytest.raises(StorageValidationError):
        proactive.record_displayed("startup", now)  # type: ignore[arg-type]

    database.close()
    database.open()
    assert proactive.count_displayed_for_date(today) == 2
    assert proactive.get("idle-event").disposition is ProactiveDisposition.DISMISSED


def test_clicked_greeting_is_atomic_standalone_completed_and_not_context_or_memory(
    stores,
) -> None:
    database, conversations, proactive, _now = stores
    conversation = conversations.create_conversation(conversation_id="conversation")
    conversations.save_turn(
        conversation.conversation_id,
        "regular-turn",
        "regular-user",
        "普通问题",
        "regular-assistant",
    )
    conversations.finalize_assistant(
        "regular-assistant",
        "普通回答",
        status="completed",
        terminal_reason="completed",
        attempt=1,
    )
    proactive.record_displayed(
        "startup",
        date(2026, 8, 3),
        event_id="proactive-event",
    )

    event, message = proactive.record_clicked(
        "proactive-event",
        conversation.conversation_id,
        "我在，需要时点我就好。",
        message_id="proactive-message",
    )

    assert event.disposition is ProactiveDisposition.CLICKED
    assert event.message_id == message.message_id == "proactive-message"
    assert message.role is StoredMessageRole.ASSISTANT
    assert message.origin is StoredMessageOrigin.PROACTIVE
    assert message.status is StoredMessageStatus.COMPLETED
    assert message.terminal_reason == "completed"
    assert not message.participates_in_memory
    assert [item.message_id for item in conversations.load_recent_messages("conversation")] == [
        "regular-user",
        "regular-assistant",
        "proactive-message",
    ]
    assert [
        item.message_id for item in conversations.load_recent_valid_messages("conversation")
    ] == ["regular-user", "regular-assistant"]
    assert conversations.summary_progress("conversation").message_count == 2
    assert database.connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 0

    page = conversations.load_message_page("conversation")
    turns = _turns_from_messages(page.items)
    entries = _presentation_entries_from_messages(page.items)
    assert [turn.turn_id for turn in turns] == ["regular-turn"]
    assert [entry.entry_id for entry in entries] == [
        "regular-turn",
        "proactive-message",
    ]
    assert entries[0].user_message is not None
    assert entries[0].assistant_message is not None
    assert entries[1].user_message is None
    assert entries[1].assistant_message is not None
    assert entries[1].assistant_message.content == "我在，需要时点我就好。"

    duplicate_event, duplicate_message = proactive.record_clicked(
        "proactive-event",
        conversation.conversation_id,
        "重复点击不会重复保存",
    )
    assert duplicate_event == event
    assert duplicate_message == message
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE origin = 'proactive'"
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(StorageConflictError):
        conversations.set_message_memory_eligibility("proactive-message", True)
    with pytest.raises(StorageConflictError):
        conversations.begin_assistant_attempt("proactive-message", 2)


def test_failed_click_rolls_back_message_and_keeps_event_displayed(stores) -> None:
    database, conversations, proactive, _now = stores
    conversation = conversations.create_conversation(conversation_id="conversation")
    conversations.save_user_message(
        conversation.conversation_id,
        "regular-turn",
        "taken-message-id",
        "占用 ID",
    )
    proactive.record_displayed(
        "idle",
        date(2026, 8, 3),
        event_id="proactive-event",
    )

    with pytest.raises(StorageConflictError):
        proactive.persist_greeting_on_click(
            "proactive-event",
            conversation.conversation_id,
            "不会留下半条记录",
            message_id="taken-message-id",
        )

    assert proactive.get("proactive-event").disposition is ProactiveDisposition.DISPLAYED
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE origin = 'proactive'"
        ).fetchone()[0]
        == 0
    )


def test_deleted_clicked_message_leaves_content_free_event_tombstone(stores) -> None:
    _database, conversations, proactive, _now = stores
    conversation = conversations.create_conversation(conversation_id="conversation")
    proactive.record_displayed("startup", date(2026, 8, 3), event_id="event")
    proactive.record_clicked(
        "event",
        conversation.conversation_id,
        "短问候",
        message_id="message",
    )

    assert conversations.delete_conversation(conversation.conversation_id)
    event = proactive.get("event")
    assert event.disposition is ProactiveDisposition.CLICKED
    assert event.message_id is None
    assert proactive.count_for_date(date(2026, 8, 3)) == 1


def test_local_data_service_exposes_async_proactive_boundary(qtbot, tmp_path) -> None:
    runtime = SerialDataThread(
        lambda: create_local_data_stores(
            tmp_path / "data" / "amadeus.sqlite3",
            tmp_path / "backups",
        ),
        resource_close=lambda stores: stores.close(),
    )
    service = LocalDataService(runtime)
    displays: list[object] = []
    dismissals: list[object] = []
    counts: list[tuple[str, int]] = []
    persisted: list[tuple[object, object]] = []
    snapshots: list[ConversationSnapshot] = []
    service.proactive_event_displayed.connect(displays.append)
    service.proactive_event_dismissed.connect(dismissals.append)
    service.proactive_count_loaded.connect(lambda day, count: counts.append((day, count)))
    service.proactive_greeting_persisted.connect(
        lambda event, message: persisted.append((event, message))
    )
    service.conversation_loaded.connect(snapshots.append)
    service.start()
    try:
        qtbot.waitUntil(lambda: service.is_writable)
        assert service.current_conversation_id is not None
        assert service.record_proactive_display(
            "startup",
            date(2026, 8, 3),
            event_id="click-event",
        )
        qtbot.waitUntil(lambda: len(displays) == 1)

        assert service.load_proactive_display_count(date(2026, 8, 3))
        qtbot.waitUntil(lambda: counts == [("2026-08-03", 1)])

        assert service.persist_proactive_greeting(
            "click-event",
            "异步保存的问候",
            message_id="click-message",
        )
        qtbot.waitUntil(lambda: len(persisted) == 1 and len(snapshots) == 1)
        assert persisted[0][1].message_id == "click-message"
        assert snapshots[0].turns == ()
        assert [entry.entry_id for entry in snapshots[0].presentation_entries] == ["click-message"]

        assert service.record_proactive_display(
            "idle",
            date(2026, 8, 3),
            event_id="dismiss-event",
        )
        qtbot.waitUntil(lambda: len(displays) == 2)
        assert service.dismiss_proactive_event("dismiss-event")
        qtbot.waitUntil(lambda: len(dismissals) == 1)
        assert dismissals[0].disposition is ProactiveDisposition.DISMISSED
    finally:
        assert service.shutdown()


class _NeverGenerationRunner:
    def start(self, _request, *, on_success, on_failure) -> bool:
        del on_success, on_failure
        raise AssertionError("local-first proactive integration must not start AI")


class _AvailablePresence(PresenceProbe):
    def snapshot(self) -> PresenceSnapshot:
        return PresenceSnapshot(0.0, False, False)


def test_controller_display_click_roundtrip_uses_real_sqlite_and_proactive_origin(
    qtbot,
    tmp_path,
) -> None:
    database_path = tmp_path / "data" / "amadeus.sqlite3"
    runtime = SerialDataThread(
        lambda: create_local_data_stores(database_path, tmp_path / "backups"),
        resource_close=lambda stores: stores.close(),
    )
    service = LocalDataService(runtime)
    bubble = GreetingBubble(always_on_top=False)
    qtbot.addWidget(bubble)
    now = datetime(2026, 8, 3, 10, tzinfo=UTC)
    settings = {
        "proactive": {
            "mode": "restrained",
            "quiet_start_minute": 23 * 60,
            "quiet_end_minute": 8 * 60,
            "daily_limit": 2,
            "paused_local_date": None,
            "ai_greetings_enabled": False,
        }
    }
    displays: list[object] = []
    persisted: list[tuple[object, object]] = []
    snapshots: list[ConversationSnapshot] = []
    opened: list[bool] = []
    service.proactive_event_displayed.connect(displays.append)
    service.proactive_greeting_persisted.connect(
        lambda event, message: persisted.append((event, message))
    )
    service.conversation_loaded.connect(snapshots.append)
    controller = ProactiveInteractionController(
        data=service,
        bubble=bubble,
        generation_runner=_NeverGenerationRunner(),  # type: ignore[arg-type]
        presence_probe=_AvailablePresence(),
        settings_reader=lambda: settings,
        clock=lambda: now,
        pet_visible=lambda: True,
        conversation_active=lambda: False,
        settings_open=lambda: False,
        data_writable=lambda: service.is_writable,
        exiting=lambda: False,
        pet_geometry=lambda: QRect(10, 10, 20, 20),
        work_areas=lambda: [QRect(0, 0, 800, 600)],
        provider_configured=lambda: False,
        provider_metadata=lambda: (None, None),
        acquire_ai_lane=lambda: True,
        release_ai_lane=lambda: None,
        cancel_ai_lane=lambda _wait_ms: True,
        open_chat=lambda: opened.append(True),
        startup_delay_ms=1,
        poll_interval_ms=60_000,
    )
    service.start()
    try:
        qtbot.waitUntil(lambda: service.is_writable)
        controller.start()
        qtbot.waitUntil(lambda: bubble.isVisible() and len(displays) == 1)

        qtbot.mouseClick(bubble, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: len(persisted) == 1 and opened == [True])
        qtbot.waitUntil(lambda: len(snapshots) == 1)

        event, message = persisted[0]
        assert event.disposition is ProactiveDisposition.CLICKED
        assert message.origin is StoredMessageOrigin.PROACTIVE
        assert message.role is StoredMessageRole.ASSISTANT
        assert message.status is StoredMessageStatus.COMPLETED
        assert not message.participates_in_memory
        assert not bubble.isVisible()
        assert snapshots[0].turns == ()
        assert len(snapshots[0].presentation_entries) == 1
        entry = snapshots[0].presentation_entries[0]
        assert entry.origin is StoredMessageOrigin.PROACTIVE
        assert entry.user_message is None
        assert entry.assistant_message is not None
        assert entry.assistant_message.message_id == message.message_id
        assert entry.assistant_message.content == message.content
        assert entry.assistant_message.status.value == message.status.value
    finally:
        controller.stop()
        assert service.shutdown()

    reopened = SQLiteDatabase(database_path).open()
    try:
        durable_event = ProactiveInteractionStore(reopened).get(event.event_id)
        durable_message = ConversationStore(reopened).get_message(message.message_id)
        assert durable_event.disposition is ProactiveDisposition.CLICKED
        assert durable_event.message_id == message.message_id
        assert durable_message.origin is StoredMessageOrigin.PROACTIVE
        assert durable_message.role is StoredMessageRole.ASSISTANT
        assert (
            reopened.connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 0
        )
    finally:
        reopened.close()
