"""Synthetic real-display smoke for P7H companion authorization UI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel

from amadeus_desktop.ui.chat_panel import ChatPanel
from amadeus_desktop.ui.memory_page import MemoryPage
from amadeus_desktop.ui.proactive_page import ProactivePage


def _save(widget, destination: Path) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    pixmap = widget.grab()
    if not pixmap.save(str(destination), "PNG"):
        raise RuntimeError("screenshot save failed")
    return {
        "device_pixel_ratio": widget.devicePixelRatioF(),
        "logical_size": [widget.width(), widget.height()],
        "physical_size": [pixmap.width(), pixmap.height()],
        "screenshot_bytes": destination.stat().st_size,
    }


def _cue(cue_id: str, status: str) -> dict[str, object]:
    return {
        "memory_id": cue_id,
        "layer": "cue",
        "kind": ("memory_followup" if cue_id == "cue-memory" else "conversation_followup"),
        "status": status,
        "topic_key": f"合成线索主题 · {status}",
        "content": f"用户已审阅的合成待续文本 · {status}",
        "confidence": 0.92,
        "source_label": "已授权记忆" if cue_id == "cue-memory" else "待续话题",
        "reason": "memory_authorized" if cue_id == "cue-memory" else "explicit_return",
        "confirmed_at": "2026-08-18 10:00" if status != "proposed" else "",
        "expires_at": "2026-09-17 10:00" if status != "proposed" else "",
        "surfaced_at": "2026-08-18 11:00" if status == "surfaced" else "",
        "resolved_at": "2026-08-18 12:00" if status == "resolved" else "",
        "created_at": "2026-08-18 09:00",
        "updated_at": "2026-08-18 12:00",
        "sources": (
            {
                "source_kind": ("fact_version" if cue_id == "cue-memory" else "user_message"),
                "source_target_id": "synthetic-source-id",
            },
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    application = QApplication.instance() or QApplication(["p7h-companion-smoke"])
    application.setFont(QFont("Microsoft YaHei UI", 9))
    screen = application.primaryScreen()
    if screen is None:
        raise RuntimeError("primary display unavailable")
    screen_dpr = screen.devicePixelRatio()
    if abs(screen_dpr - 1.25) > 0.01:
        raise RuntimeError(f"expected current 125% display, got DPR {screen_dpr:.3f}")

    memory_page = MemoryPage()
    memory_page.resize(1500, 1080)
    fact = {
        "memory_id": "fact-1",
        "version_id": "fact-v2",
        "layer": "fact",
        "kind": "preference",
        "status": "active",
        "content": "用户偏好先核对证据再做决定。",
        "topic_key": "合成事实主题",
        "importance": 0.8,
        "confidence": 1.0,
        "version_number": 2,
        "created_at": "2026-08-18 09:00",
        "updated_at": "2026-08-18 10:00",
    }
    cues = (
        _cue("cue-proposed", "proposed"),
        _cue("cue-active", "active"),
        _cue("cue-surfaced", "surfaced"),
        _cue("cue-resolved", "resolved"),
        _cue("cue-expired", "expired"),
        _cue("cue-memory", "active"),
    )
    memory_page.set_layer_data(facts=(fact,), cues=cues)
    memory_page.layer_combo.setCurrentIndex(memory_page.layer_combo.findData("cue"))
    memory_page.show()
    application.processEvents()
    cue_counts: dict[str, int] = {}
    for status, expected in (
        ("proposed", 1),
        ("active", 2),
        ("surfaced", 1),
        ("resolved", 1),
        ("expired", 1),
    ):
        memory_page.status_combo.setCurrentIndex(memory_page.status_combo.findData(status))
        application.processEvents()
        count = memory_page.memory_table.rowCount()
        if count != expected:
            raise RuntimeError(f"cue status count mismatch: {status}={count}")
        cue_counts[status] = count
    memory_page.status_combo.setCurrentIndex(memory_page.status_combo.findData("proposed"))
    application.processEvents()
    if memory_page.detail_edit.isReadOnly() or memory_page.confirm_button.isHidden():
        raise RuntimeError("proposed cue review controls unavailable")
    memory_page.status_combo.setCurrentIndex(memory_page.status_combo.findData("active"))
    application.processEvents()
    if not memory_page.detail_edit.isReadOnly():
        raise RuntimeError("confirmed cue text is editable")
    if memory_page.resolve_cue_button.isHidden() or memory_page.open_cue_button.isHidden():
        raise RuntimeError("confirmed cue lifecycle controls unavailable")
    memory_page.status_combo.setCurrentIndex(memory_page.status_combo.findData(""))
    memory_page.search_edit.clear()
    memory_page.select_memory("cue-memory")
    application.processEvents()
    memory_result = _save(memory_page, args.output / "memory-cues-125.png")
    memory_page.layer_combo.setCurrentIndex(memory_page.layer_combo.findData("fact"))
    application.processEvents()
    if memory_page.memory_authorization_check.isHidden():
        raise RuntimeError("exact-version memory authorization control unavailable")

    proactive_page = ProactivePage()
    proactive_page.resize(900, 760)
    proactive_page.apply_settings(
        mode="restrained",
        quiet_start_minute=23 * 60,
        quiet_end_minute=8 * 60,
        daily_limit=2,
        paused_today=False,
        ai_greetings_enabled=False,
        contextual_followups_enabled=False,
    )
    if proactive_page.contextual_followups_enabled.isChecked():
        raise RuntimeError("contextual follow-ups must default off")
    note_text = "\n".join(label.text() for label in proactive_page.findChildren(QLabel))
    for marker in ("默认关闭", "逐条审阅", "只主动展示一次", "30 天", "泛化提示"):
        if marker not in note_text:
            raise RuntimeError(f"missing proactive privacy explanation: {marker}")
    proactive_page.show()
    application.processEvents()
    proactive_result = _save(proactive_page, args.output / "proactive-followups-125.png")

    chat_panel = ChatPanel()
    chat_panel.resize(520, 680)
    chat_panel.set_provider_mode("mock")
    bubble = chat_panel.append_message(
        "synthetic-cue-message",
        "assistant",
        "用户确认后在聊天里展开的合成待续原文。",
        companion_cue_id="cue-active",
        companion_source_label="待续话题",
    )
    chat_panel.show()
    application.processEvents()
    if bubble.source_button.isHidden() or bubble.source_button.text() != "基于：待续话题":
        raise RuntimeError("chat cue source label unavailable")
    chat_result = _save(chat_panel, args.output / "chat-cue-source-125.png")

    result = {
        "status": "passed",
        "screen_dpr": screen_dpr,
        "cue_status_counts": cue_counts,
        "memory": memory_result,
        "proactive": proactive_result,
        "chat": chat_result,
        "contextual_default_off": True,
        "no_provider_requested": True,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    chat_panel.close()
    proactive_page.close()
    memory_page.close()
    application.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
