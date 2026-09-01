from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt

from amadeus_desktop.local_data_service import ReminderListSnapshot
from amadeus_desktop.storage_models import (
    TemporalCommitment,
    TemporalCommitmentKind,
    TemporalCommitmentStatus,
    TemporalCommitmentVersion,
    TemporalSourceKind,
    TemporalVersionOrigin,
)
from amadeus_desktop.temporal_commitments import TemporalDraftSpec
from amadeus_desktop.ui.chat_panel import ChatPanel
from amadeus_desktop.ui.reminders_page import RemindersPage


def _commitment(*, status: TemporalCommitmentStatus = TemporalCommitmentStatus.DRAFT):
    now = datetime.now(UTC)
    due = now + timedelta(hours=2)
    version = TemporalCommitmentVersion(
        version_id="version-1",
        commitment_id="commitment-1",
        version_number=1,
        kind=TemporalCommitmentKind.REMINDER,
        content="交报告",
        due_at_utc=due,
        original_local_time=due.astimezone().isoformat(timespec="minutes"),
        timezone_name=due.astimezone().tzname() or "local",
        utc_offset_minutes=round(
            (due.astimezone().utcoffset() or timedelta()).total_seconds() / 60
        ),
        show_content=False,
        origin=TemporalVersionOrigin.CHAT,
        supersedes_version_id=None,
        created_at=now,
    )
    return TemporalCommitment(
        commitment_id="commitment-1",
        profile_id="default",
        source_kind=TemporalSourceKind.CHAT,
        source_message_id="message-1",
        source_conversation_id="conversation-1",
        live_source_message_id="message-1",
        live_source_conversation_id="conversation-1",
        status=status,
        current_version=version,
        created_at=now,
        updated_at=now,
        confirmed_at=None if status is TemporalCommitmentStatus.DRAFT else now,
        due_detected_at=None,
        surfaced_at=None,
        completed_at=None,
        cancelled_at=None,
    )


def test_reminders_page_edits_and_confirms_selected_draft(qtbot) -> None:
    page = RemindersPage()
    qtbot.addWidget(page)
    page.set_writable(True)
    page.set_snapshot(ReminderListSnapshot((_commitment(),), "", "", 0))
    saved: list[tuple[str, TemporalDraftSpec]] = []
    page.save_requested.connect(lambda identifier, spec: saved.append((identifier, spec)))

    page.content.setPlainText("提交最终报告")
    qtbot.mouseClick(page.save_button, Qt.MouseButton.LeftButton)

    assert len(saved) == 1
    assert saved[0][0] == "commitment-1"
    assert saved[0][1].content == "提交最终报告"
    assert saved[0][1].due_at_utc is not None


def test_reminders_page_outstanding_filter_and_tombstone_source(qtbot) -> None:
    item = _commitment(status=TemporalCommitmentStatus.SURFACED)
    item = replace(
        item,
        live_source_message_id=None,
        live_source_conversation_id=None,
    )
    page = RemindersPage()
    qtbot.addWidget(page)
    page.set_writable(True)
    page.set_snapshot(ReminderListSnapshot((item,), "", "outstanding", 1))

    assert page.table.item(0, 1).text() == "待处理"
    assert page.table.item(0, 4).text() == "原聊天已删除"
    assert "提醒独立保留" in page.source.text()
    assert page.complete_button.isEnabled()
    assert page.snooze_button.isEnabled()


def test_unconfigured_chat_keeps_text_entry_for_local_reminders(qtbot) -> None:
    panel = ChatPanel()
    qtbot.addWidget(panel)

    panel.set_provider_mode("unconfigured")

    assert panel.input.isEnabled()
    assert "本地提醒仍可使用" in panel.provider_banner.text()
