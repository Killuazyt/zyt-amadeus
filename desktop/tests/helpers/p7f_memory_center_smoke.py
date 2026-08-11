"""Synthetic 125% visual smoke for the P7F five-layer memory center."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from amadeus_desktop.ui.memory_page import MemoryPage


def _row(
    layer: str,
    memory_id: str,
    content: str,
    *,
    kind: str | None = None,
    status: str = "active",
    evidence_score: float = 0.0,
    sources: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "group_id": memory_id,
        "version_id": f"{memory_id}-v1",
        "layer": layer,
        "kind": kind or layer,
        "status": status,
        "content": content,
        "topic_key": "合成验收主题",
        "subject_scope": "relationship" if layer in {"reflection", "persona"} else "user",
        "importance": 0.8,
        "confidence": 0.9,
        "evidence_score": evidence_score,
        "version": 1,
        "pinned": False,
        "created_at": "2026-08-11 10:00",
        "updated_at": "2026-08-11 10:05",
        "last_recalled_at": None,
        "sources": sources,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", required=True, type=Path)
    args = parser.parse_args()
    application = QApplication.instance() or QApplication(["p7f-memory-center-smoke"])
    application.setFont(QFont("Microsoft YaHei UI", 9))
    page = MemoryPage()
    page.resize(1500, 1080)
    page.set_memory_enabled(True)
    page.set_deep_memory_enabled(True)
    page.set_retrieval_status(
        {
            "model_status": "ready",
            "user_generation_status": "active",
            "user_index_count": 10_000,
            "reflection_generation_status": "active",
            "reflection_index_count": 20,
            "persona_impression_generation_status": "active",
            "persona_impression_index_count": 10,
            "persona_generation_status": "active",
            "persona_index_count": 40,
        }
    )
    fact = _row(
        "fact",
        "fact-1",
        "用户偏好在重要决定前核对证据。",
        kind="preference",
        sources=(
            {
                "message_id": "synthetic-user-message",
                "available": True,
                "role": "user",
                "content": "合成来源正文，仅用于界面验收。",
            },
        ),
    )
    reflection = _row(
        "reflection",
        "reflection-1",
        "用户往往通过反复核验来建立稳定信任。",
        status="confirmed",
        evidence_score=1.7,
        sources=(
            {
                "parent_layer": "fact",
                "parent_group_id": "fact-1",
                "parent_version_id": "fact-1-v1",
                "available": True,
                "content": "上游事实版本 fact-1-v1",
            },
        ),
    )
    impression = _row(
        "persona",
        "persona-1",
        "关系印象：彼此适合以清晰、可复核的方式沟通。",
        evidence_score=2.4,
        sources=(
            {
                "parent_layer": "reflection",
                "parent_group_id": "reflection-1",
                "parent_version_id": "reflection-1-v1",
                "available": True,
                "content": "上游反思版本 reflection-1-v1",
            },
        ),
    )
    static_persona = _row(
        "static_persona",
        "static-persona-1",
        "静态角色资料（物理隔离，只读）。",
        kind="static_persona",
        status="readonly",
    )
    page.set_layer_data(
        working=(
            _row(
                "working",
                "working-1",
                "当前查询及其召回评分快照；切换会话即清除。",
                status="readonly",
            ),
        ),
        recent=(
            _row(
                "recent",
                "recent-1",
                "最近消息与滚动摘要（只读）。",
                status="readonly",
            ),
        ),
        facts=(fact,),
        reflections=(reflection,),
        personas=(impression,),
        static_persona=(static_persona,),
        timeline=(
            _row(
                "timeline",
                "timeline-1",
                "2026-08-11 · 事件事实投影，不复制正文。",
                kind="event",
                status="readonly",
            ),
        ),
        audit=(
            _row(
                "audit",
                "audit-1",
                "reflection.confirmed · evidence_threshold · +1.0",
                status="readonly",
            ),
        ),
        conflicts=(
            {
                "conflict_id": "conflict-1",
                "target_layer": "fact",
                "target_group_id": "fact-1",
                "incumbent_version_id": "fact-1-v1",
                "challenger_version_id": "fact-1-v2",
                "status": "open",
            },
        ),
    )
    page.show()
    application.processEvents()

    layer_checks: dict[str, dict[str, object]] = {}
    expected_read_only = {
        "working": True,
        "recent": True,
        "fact": False,
        "reflection": False,
        "persona": False,
        "timeline": True,
        "audit": True,
    }
    for layer, read_only in expected_read_only.items():
        page.layer_combo.setCurrentIndex(page.layer_combo.findData(layer))
        application.processEvents()
        if page.memory_table.rowCount() < 1:
            raise RuntimeError(f"empty layer: {layer}")
        page.memory_table.selectRow(0)
        application.processEvents()
        if page.detail_edit.isReadOnly() is not read_only:
            raise RuntimeError(f"edit boundary mismatch: {layer}")
        layer_checks[layer] = {
            "rows": page.memory_table.rowCount(),
            "read_only": page.detail_edit.isReadOnly(),
        }

    page.layer_combo.setCurrentIndex(page.layer_combo.findData("persona"))
    page.memory_table.selectRow(1)
    application.processEvents()
    if not page.detail_edit.isReadOnly() or page.save_edit_button.isEnabled():
        raise RuntimeError("static persona must remain read-only")
    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
    pixmap = page.grab()
    if not pixmap.save(str(args.screenshot), "PNG"):
        raise RuntimeError("screenshot save failed")
    result = {
        "status": "passed",
        "device_pixel_ratio": page.devicePixelRatioF(),
        "logical_size": [page.width(), page.height()],
        "physical_size": [pixmap.width(), pixmap.height()],
        "layers": layer_checks,
        "static_persona_read_only": True,
        "screenshot_bytes": args.screenshot.stat().st_size,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    page.close()
    application.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
