from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from amadeus_desktop.ui.memory_page import MemoryPage


@dataclass(frozen=True)
class MemoryView:
    memory_id: str
    memory_type: str
    status: str
    content: str
    topic_key: str
    importance: float
    confidence: float
    pinned: bool
    version: int
    created_at: str
    updated_at: str
    last_recalled_at: str = ""


def make_page(qtbot) -> MemoryPage:
    page = MemoryPage()
    qtbot.addWidget(page)
    return page


def sample_memory(*, archived: bool = False) -> MemoryView:
    return MemoryView(
        memory_id="memory-1",
        memory_type="preference",
        status="archived" if archived else "active",
        content="用户不喜欢太甜的咖啡",
        topic_key="drink.coffee.sweetness",
        importance=0.8,
        confidence=0.91,
        pinned=False,
        version=2,
        created_at="2026-07-01",
        updated_at="2026-08-01",
    )


def test_enabled_and_search_signals_only_reflect_user_actions(qtbot) -> None:
    page = make_page(qtbot)
    enabled: list[bool] = []
    searches: list[tuple[str, str, str, str]] = []
    page.enabled_changed.connect(enabled.append)
    page.search_requested.connect(lambda *values: searches.append(values))
    assert page.status_combo.findData("superseded") == -1
    assert page.sort_combo.findData("recalled_desc") >= 0

    page.set_memory_enabled(False)
    assert enabled == []
    assert "不会提炼" in page.enabled_notice.text()

    page.enabled_check.click()
    page.search_edit.setText(" 咖啡 ")
    page.type_combo.setCurrentIndex(page.type_combo.findData("preference"))
    page.status_combo.setCurrentIndex(page.status_combo.findData("active"))
    page.sort_combo.setCurrentIndex(page.sort_combo.findData("importance_desc"))
    page.search_button.click()

    assert enabled == [True]
    assert searches[-1] == ("咖啡", "preference", "active", "importance_desc")


def test_memory_detail_edit_pin_archive_and_restore_emit_ids(qtbot) -> None:
    page = make_page(qtbot)
    edited: list[tuple[str, str]] = []
    pinned: list[tuple[str, bool]] = []
    archived: list[str] = []
    restored: list[str] = []
    page.edit_requested.connect(lambda memory_id, content: edited.append((memory_id, content)))
    page.pin_requested.connect(lambda memory_id, value: pinned.append((memory_id, value)))
    page.archive_requested.connect(archived.append)
    page.restore_requested.connect(restored.append)

    page.set_memories(iter([sample_memory()]))
    assert page.current_memory_id == "memory-1"
    assert page.detail_edit.toPlainText() == "用户不喜欢太甜的咖啡"
    assert "偏好" in page.detail_title_label.text()
    assert "最近召回：从未" in page.detail_meta_label.text()
    assert page.restore_button.isHidden()

    page.detail_edit.setPlainText("  用户现在喜欢微甜咖啡  ")
    page.save_edit_button.click()
    page.pin_button.click()
    page.archive_button.click()

    assert edited == [("memory-1", "用户现在喜欢微甜咖啡")]
    assert pinned == [("memory-1", True)]
    assert archived == ["memory-1"]

    page.set_memories([sample_memory(archived=True)], selected_id="memory-1")
    assert page.archive_button.isHidden()
    assert not page.restore_button.isHidden()
    page.restore_button.click()
    assert restored == ["memory-1"]


def test_recent_recall_is_visible_and_sortable(qtbot) -> None:
    page = make_page(qtbot)
    searches: list[tuple[str, str, str, str]] = []
    page.search_requested.connect(lambda *values: searches.append(values))
    recalled = MemoryView(
        **{
            **sample_memory().__dict__,
            "last_recalled_at": "2026-08-02 10:30:00",
        }
    )

    page.set_memories([recalled])
    assert page.memory_table.item(0, 6).text() == "2026-08-02 10:30:00"
    assert "最近召回：2026-08-02 10:30:00" in page.detail_meta_label.text()

    page.sort_combo.setCurrentIndex(page.sort_combo.findData("recalled_desc"))
    assert searches[-1][-1] == "recalled_desc"


def test_retrieval_status_and_actions_use_only_safe_metadata(qtbot) -> None:
    page = make_page(qtbot)
    verified: list[bool] = []
    rebuilt: list[bool] = []
    page.verify_model_requested.connect(lambda: verified.append(True))
    page.rebuild_index_requested.connect(lambda: rebuilt.append(True))

    page.set_retrieval_status(
        {
            "model_status": "missing",
            "safe_error_category": "raw secret must never be rendered",
            "user_generation_id": "user-generation-private-long-id",
            "user_generation_status": "active",
            "user_vector_count": 12,
            "persona_generation_id": "persona-generation-private-long-id",
            "persona_generation_status": "failed",
            "persona_vector_count": 7,
            "last_rebuild_status": "failed",
        }
    )

    assert "模型缺失" in page.retrieval_status_label.text()
    assert "raw secret" not in page.retrieval_status_label.text()
    assert "user-generat…" in page.retrieval_index_label.text()
    assert "12 条" in page.retrieval_index_label.text()
    assert not page.rebuild_index_button.isEnabled()
    qtbot.mouseClick(page.verify_model_button, Qt.MouseButton.LeftButton)
    assert verified == [True]

    page.set_retrieval_status(
        {
            "model_status": "ready",
            "user_generation": "generation-1",
            "user_generation_status": "active",
            "user_index_count": 12,
            "persona_generation": "generation-2",
            "persona_generation_status": "active",
            "persona_index_count": 7,
            "last_rebuild_status": "active",
        }
    )
    assert "已就绪" in page.retrieval_status_label.text()
    assert page.rebuild_index_button.isEnabled()
    qtbot.mouseClick(page.rebuild_index_button, Qt.MouseButton.LeftButton)
    assert rebuilt == [True]


def test_sources_show_body_or_tombstone_and_only_live_source_can_jump(qtbot) -> None:
    page = make_page(qtbot)
    page.set_memories([sample_memory()])
    opened: list[tuple[str, str]] = []
    page.source_requested.connect(
        lambda conversation_id, message_id: opened.append((conversation_id, message_id))
    )
    page.set_sources(
        "memory-1",
        [
            {
                "conversation_id": "conversation-1",
                "message_id": "message-1",
                "content": "我不喜欢太甜的咖啡",
                "created_at": "2026-07-01",
                "method": "自动提炼",
            },
            {
                "conversation_id": "conversation-2",
                "message_id": "message-2",
                "deleted": True,
                "content": "不得保留的正文",
            },
            {
                "method": "manual",
                "available": False,
                "version_number": 2,
            },
        ],
    )

    assert page.source_preview.toPlainText() == "我不喜欢太甜的咖啡"
    page.open_source_button.click()
    assert opened == [("conversation-1", "message-1")]

    page.source_list.setCurrentRow(1)
    assert "正文已永久清除" in page.source_preview.toPlainText()
    assert "不得保留" not in page.source_preview.toPlainText()
    assert not page.open_source_button.isEnabled()
    page.source_list.itemDoubleClicked.emit(page.source_list.item(1))
    assert opened == [("conversation-1", "message-1")]

    page.source_list.setCurrentRow(2)
    assert "手工编辑" in page.source_list.item(2).text()
    assert "记忆页手工编辑" in page.source_preview.toPlainText()
    assert not page.open_source_button.isEnabled()


def test_five_layer_boundaries_static_isolation_and_lineage_jump(qtbot) -> None:
    page = make_page(qtbot)
    reflection = {
        "memory_id": "reflection-1",
        "version_id": "reflection-v1",
        "layer": "reflection",
        "kind": "reflection",
        "subject_scope": "relationship",
        "status": "tentative",
        "content": "用户可能通过稳定互动建立信任",
        "topic_key": "稳定互动",
        "importance": 0.8,
        "confidence": 0.8,
        "evidence_score": 0.6,
        "pinned": False,
        "version_number": 1,
    }
    impression = {
        **reflection,
        "memory_id": "persona-1",
        "version_id": "persona-v1",
        "layer": "persona",
        "kind": "persona",
        "status": "active",
        "content": "关系互动通常偏向稳定与可预期",
    }
    static = {
        **impression,
        "memory_id": "static-1",
        "version_id": "static-1",
        "layer": "static_persona",
        "kind": "static_persona",
        "status": "readonly",
        "content": "静态角色资料绝不能被派生流程修改",
    }
    page.set_layer_data(
        working=(
            {
                "memory_id": "working-1",
                "layer": "working",
                "kind": "current_message",
                "status": "readonly",
                "content": "当前消息",
            },
        ),
        facts=(sample_memory(),),
        reflections=(reflection,),
        personas=(impression,),
        static_persona=(static,),
    )

    page.layer_combo.setCurrentIndex(page.layer_combo.findData("working"))
    assert page.detail_edit.isReadOnly()
    assert not page.save_edit_button.isEnabled()

    page.layer_combo.setCurrentIndex(page.layer_combo.findData("reflection"))
    assert not page.detail_edit.isReadOnly()
    assert not page.confirm_button.isHidden()
    assert "证据分：0.60" in page.detail_meta_label.text()
    page.set_layer_sources(
        "reflection",
        "reflection-1",
        (
            {
                "conversation_id": "conversation-1",
                "message_id": "message-1",
                "content": "我重视稳定互动",
                "method": "automatic",
                "parent_layer": "fact",
                "parent_group_id": "memory-1",
                "parent_version_id": "fact-v1",
            },
        ),
    )
    assert page.open_lineage_button.isEnabled()
    page.open_lineage_button.click()
    assert page.current_layer == "fact"
    assert page.current_memory_id == "memory-1"

    page.layer_combo.setCurrentIndex(page.layer_combo.findData("persona"))
    assert page.memory_table.rowCount() == 2
    assert page.select_memory("static-1")
    assert page.detail_edit.isReadOnly()
    assert not page.delete_button.isEnabled()


def test_fact_conflict_and_version_rollback_actions_are_explicit(qtbot) -> None:
    page = make_page(qtbot)
    fact = {
        "memory_id": "fact-1",
        "version_id": "fact-v2",
        "layer": "fact",
        "kind": "fact",
        "status": "active",
        "content": "当前事实",
        "topic_key": "主题",
        "importance": 0.7,
        "confidence": 1.0,
        "pinned": False,
        "version_number": 2,
    }
    page.set_layer_data(
        facts=(fact,),
        conflicts=(
            {
                "conflict_id": "conflict-1",
                "target_layer": "fact",
                "target_group_id": "fact-1",
                "incumbent_version_id": "fact-v2",
                "challenger_version_id": "fact-v3",
                "status": "open",
            },
        ),
    )
    page.set_versions(
        "fact",
        "fact-1",
        (
            {
                "version_id": "fact-v1",
                "version_number": 1,
                "content": "旧事实",
                "operation": "add",
                "created_at": "2026-08-01",
            },
            {
                "version_id": "fact-v2",
                "version_number": 2,
                "content": "当前事实",
                "operation": "manual_edit",
                "created_at": "2026-08-02",
            },
        ),
    )
    resolutions: list[tuple[str, str, str]] = []
    rollbacks: list[tuple[str, str, str]] = []
    page.conflict_resolution_requested.connect(lambda *args: resolutions.append(args))
    page.rollback_requested.connect(lambda *args: rollbacks.append(args))
    assert "停止召回" in page.conflict_notice.text()
    page.keep_conflict_button.click()
    assert resolutions == [("conflict-1", "keep", "")]

    old_index = page.rollback_combo.findData("fact-v1")
    page.rollback_combo.setCurrentIndex(old_index)
    assert page.rollback_button.isEnabled()
    page.rollback_button.click()
    assert rollbacks == [("fact", "fact-1", "fact-v1")]


def test_permanent_memory_delete_requires_confirmation(monkeypatch, qtbot) -> None:
    page = make_page(qtbot)
    page.set_memories([sample_memory()])
    deleted: list[str] = []
    impact_requests: list[tuple[str, str]] = []
    prompts: list[str] = []
    page.delete_requested.connect(deleted.append)
    page.delete_impact_requested.connect(
        lambda layer, memory_id: impact_requests.append((layer, memory_id))
    )

    def decline(*args, **kwargs):
        prompts.append(str(args[2]))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    page.delete_button.click()
    assert impact_requests == [("fact", "memory-1")]
    page.confirm_delete_impact(
        "fact",
        "memory-1",
        {"facts": 1, "reflections": 2, "personas": 1},
    )
    assert deleted == []

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    page.confirm_delete_impact(
        "fact",
        "memory-1",
        {"facts": 1, "reflections": 2, "personas": 1},
    )
    assert deleted == ["memory-1"]
    assert "反思 2" in prompts[0]
    assert "原始聊天不会" in prompts[0]


def test_export_backup_and_clear_all_data_actions(monkeypatch, qtbot) -> None:
    page = make_page(qtbot)
    exported: list[bool] = []
    backed_up: list[bool] = []
    cleared: list[bool] = []
    page.export_requested.connect(lambda: exported.append(True))
    page.backup_requested.connect(lambda: backed_up.append(True))
    page.clear_all_requested.connect(lambda: cleared.append(True))

    page.export_button.click()
    page.backup_button.click()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No,
    )
    page.clear_all_button.click()
    assert exported == [True]
    assert backed_up == [True]
    assert cleared == []

    prompts: list[str] = []

    def accept(*args, **kwargs):
        prompts.append(str(args[2]))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", accept)
    page.clear_all_button.click()

    assert cleared == [True]
    assert "全部长期记忆" in prompts[0]
    assert "原始聊天不会" in prompts[0]


def test_failed_task_retry_uses_stable_task_id(qtbot) -> None:
    page = make_page(qtbot)
    retried: list[str] = []
    page.retry_task_requested.connect(retried.append)

    page.set_failed_tasks(
        [
            {
                "task_id": "job-1",
                "task_type": "memory_extraction",
                "attempt_count": 3,
                "safe_error": "结构校验失败",
            }
        ]
    )
    assert not page.retry_task_button.isEnabled()
    page.failed_task_view.setCurrentItem(page.failed_task_view.topLevelItem(0))
    qtbot.mouseClick(page.retry_task_button, Qt.MouseButton.LeftButton)

    assert retried == ["job-1"]
    assert "结构校验失败" in page.failed_task_view.topLevelItem(0).text(2)
