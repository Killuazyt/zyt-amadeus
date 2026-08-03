from __future__ import annotations

from datetime import date

from PySide6.QtCore import QTime
from PySide6.QtWidgets import QFileDialog

from amadeus_desktop.diagnostics import DiagnosticSnapshot
from amadeus_desktop.ui.diagnostics_page import DiagnosticsPage
from amadeus_desktop.ui.persona_page import PersonaPage
from amadeus_desktop.ui.proactive_page import ProactivePage


def test_persona_page_programmatic_state_is_signal_safe_and_actions_emit(
    monkeypatch,
    qtbot,
) -> None:
    page = PersonaPage()
    qtbot.addWidget(page)
    language_changes: list[bool] = []
    imports: list[str] = []
    rebuilds: list[bool] = []
    page.follow_user_language_changed.connect(language_changes.append)
    page.import_requested.connect(imports.append)
    page.rebuild_index_requested.connect(lambda: rebuilds.append(True))

    page.apply_settings(follow_user_language=True)
    page.set_summary(name="Amadeus", source="本地严格 JSONL", knowledge_count=24)
    page.set_index_status("active", generation="generation-private-long-id", count=24)

    assert language_changes == []
    assert page.follow_user_language.isChecked()
    assert page.name_value.text() == "Amadeus"
    assert page.knowledge_count_value.text() == "24 条"
    assert "generation-p…" in page.index_status_value.text()
    assert "24 条" in page.index_status_value.text()

    page.follow_user_language.click()
    page.rebuild_index_button.click()
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: ("C:/local/persona.jsonl", "JSON Lines"),
    )
    page.import_button.click()

    assert language_changes == [False]
    assert rebuilds == [True]
    assert imports == ["C:/local/persona.jsonl"]


def test_proactive_page_applies_policy_without_feedback_and_emits_user_changes(
    monkeypatch,
    qtbot,
) -> None:
    page = ProactivePage()
    qtbot.addWidget(page)
    modes: list[str] = []
    quiet: list[tuple[int, int]] = []
    limits: list[int] = []
    pauses: list[bool] = []
    ai: list[bool] = []
    files: list[str] = []
    page.mode_changed.connect(modes.append)
    page.quiet_hours_changed.connect(lambda start, end: quiet.append((start, end)))
    page.daily_limit_changed.connect(limits.append)
    page.pause_today_changed.connect(pauses.append)
    page.ai_greetings_enabled_changed.connect(ai.append)
    page.greeting_file_requested.connect(files.append)

    page.apply_settings(
        mode="restrained",
        quiet_start_minute=23 * 60,
        quiet_end_minute=8 * 60,
        daily_limit=2,
        paused_today=False,
        ai_greetings_enabled=False,
    )
    assert modes == [] and quiet == [] and limits == [] and pauses == [] and ai == []
    assert page.quiet_start.time() == QTime(23, 0)
    assert page.quiet_end.time() == QTime(8, 0)
    assert page.daily_limit.value() == 2

    page.mode_combo.setCurrentIndex(page.mode_combo.findData("startup_only"))
    page.quiet_start.setTime(QTime(22, 30))
    page.daily_limit.setValue(1)
    page.pause_today.click()
    page.ai_greetings_enabled.click()
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: ("C:/local/greetings.json", "JSON"),
    )
    page.import_greetings_button.click()

    assert modes == ["startup_only"]
    assert quiet[-1] == (22 * 60 + 30, 8 * 60)
    assert limits == [1]
    assert pauses == [True]
    assert ai == [True]
    assert files == ["C:/local/greetings.json"]

    page.set_paused_local_date("2026-08-03", today=date(2026, 8, 3))
    assert page.pause_today.isChecked()
    assert pauses == [True]


def test_diagnostics_page_renders_allowlisted_metadata_and_redacts_raw_errors(qtbot) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    page.set_diagnostics(
        {
            "version": "0.6.0.dev6",
            "settings_schema": 5,
            "database_schema": 3,
            "data_path": "C:/Users/test/AppData/Local/Amadeus",
            "database_state": "read_write",
            "model_status": "missing",
            "user_index_status": "active",
            "user_index_count": 12,
            "user_generation_id": "user-generation-private-long-id",
            "persona_index_status": "failed",
            "persona_index_count": 7,
            "provider_configured": True,
            "safe_error_categories": [
                "model_missing",
                "raw secret token=must-not-render",
                "provider_timeout",
            ],
        }
    )

    rendered = "\n".join(
        label.text()
        for label in (
            page.version_value,
            page.settings_schema_value,
            page.database_schema_value,
            page.data_path_value,
            page.database_state_value,
            page.model_state_value,
            page.user_index_value,
            page.persona_index_value,
            page.provider_value,
            page.errors_value,
        )
    )
    assert "0.6.0.dev6" in rendered
    assert "C:/Users/test/AppData/Local/Amadeus" in rendered
    assert "12 条" in rendered and "7 条" in rendered
    assert "离线模型缺失" in rendered
    assert "请求超时" in rendered
    assert "raw secret" not in rendered
    assert "must-not-render" not in rendered


def test_diagnostics_page_accepts_canonical_snapshot_without_guessing_counts(
    qtbot,
    tmp_path,
) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    page.set_diagnostics(
        DiagnosticSnapshot(
            app_version="0.6.0.dev6",
            settings_schema=5,
            sqlite_schema=3,
            data_root=tmp_path,
            database_status="read_only",
            model_status="ready",
            user_index_status="active",
            persona_index_status="building",
            provider_configured=False,
            last_error_category="authentication",
        )
    )

    assert page.version_value.text() == "0.6.0.dev6"
    assert page.database_schema_value.text() == "3"
    assert page.data_path_value.text() == str(tmp_path)
    assert page.database_state_value.text() == "只读"
    assert page.user_index_value.text() == "有效"
    assert page.persona_index_value.text() == "构建中"
    assert page.provider_value.text() == "未配置"
    assert page.errors_value.text() == "对话供应商鉴权失败"
