from __future__ import annotations

from datetime import date

from amadeus_desktop.conversation_store import ConversationStore, ProactiveInteractionStore
from amadeus_desktop.database import SQLiteDatabase
from amadeus_desktop.local_data_service import _presentation_entries_from_messages
from amadeus_desktop.storage_models import StoredMessageOrigin


def test_proactive_row_does_not_split_regular_turn_across_restored_pages(tmp_path) -> None:
    database_path = tmp_path / "amadeus.sqlite3"
    database = SQLiteDatabase(database_path).open()
    conversations = ConversationStore(database)
    conversations.ensure_default_profile()
    conversations.create_conversation(conversation_id="conversation")
    for index in range(30):
        conversations.save_turn(
            "conversation",
            f"turn-{index}",
            f"user-{index}",
            f"问题 {index}",
            f"assistant-{index}",
        )
        conversations.finalize_assistant(
            f"assistant-{index}",
            f"回答 {index}",
            status="completed",
            terminal_reason="completed",
            attempt=1,
        )
    proactive = ProactiveInteractionStore(database)
    proactive.record_displayed("startup", date(2026, 8, 3), event_id="event")
    proactive.record_clicked(
        "event",
        "conversation",
        "我在这里。",
        message_id="proactive-message",
    )
    database.close()

    reopened = SQLiteDatabase(database_path).open()
    try:
        restored = ConversationStore(reopened)
        latest = restored.load_message_page("conversation", limit=40)
        assert latest.next_before_sequence is not None
        older = restored.load_message_page(
            "conversation",
            limit=40,
            before_sequence=latest.next_before_sequence,
        )

        assert len(latest.items) == 41
        assert len(older.items) == 20
        entries = (
            *_presentation_entries_from_messages(older.items),
            *_presentation_entries_from_messages(latest.items),
        )
        assert [entry.first_sequence for entry in entries] == sorted(
            entry.first_sequence for entry in entries
        )

        regular = [entry for entry in entries if entry.origin is StoredMessageOrigin.CONVERSATION]
        assert len(regular) == 30
        assert all(entry.user_message is not None for entry in regular)
        assert all(entry.assistant_message is not None for entry in regular)
        rendered_ids = [
            message.message_id
            for entry in entries
            for message in (entry.user_message, entry.assistant_message)
            if message is not None
        ]
        assert len(rendered_ids) == len(set(rendered_ids)) == 61
        assert not any("missing-assistant" in message_id for message_id in rendered_ids)
        assert rendered_ids[-1] == "proactive-message"
    finally:
        reopened.close()
