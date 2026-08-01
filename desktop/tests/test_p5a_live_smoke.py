from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path

from amadeus_desktop.chat_models import ChatRequest, GenerationPurpose
from amadeus_desktop.chat_provider import (
    CancellationToken,
    ChatProviderError,
    ProviderErrorCode,
)
from amadeus_desktop.credential_store import InMemoryCredentialStore
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.settings import DEFAULT_SETTINGS, SettingsRepository
from tests.helpers.p5a_live_smoke import execute_smoke, preflight


class SyntheticAcceptanceProvider:
    """Deterministic stand-in for the live provider; it never opens a socket."""

    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        cancellation.raise_if_cancelled()
        if request.options.purpose is GenerationPurpose.MAIN_CONVERSATION:
            yield "合成确认"
            return
        if request.options.purpose is not GenerationPurpose.MEMORY_EXTRACTION:
            raise AssertionError("unexpected generation purpose")
        payload = json.loads(request.messages[-1].content)
        source = payload["sources"][0]
        correction = "更正" in source["content"]
        candidate = {
            "type": "preference",
            "operation": "correct" if correction else "add",
            "content": "合成偏好：无糖茶" if correction else "合成偏好：无糖咖啡",
            "topic_key": "synthetic_beverage",
            "importance": 0.7,
            "confidence": 0.95,
            "source_message_ids": [source["message_id"]],
        }
        yield json.dumps({"candidates": [candidate]}, ensure_ascii=False)


class FailingSyntheticProvider:
    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        del request
        cancellation.raise_if_cancelled()
        raise ChatProviderError(ProviderErrorCode.NETWORK)
        yield ""  # pragma: no cover - preserve the async-iterator contract


def _configured_source(local_app_data: Path) -> SettingsRepository:
    paths = AppPaths.for_current_user(local_app_data)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["provider_enabled"] = True
    repository.save(settings)
    return repository


def test_default_preflight_is_read_only_and_reports_only_safe_checks(tmp_path: Path) -> None:
    repository = _configured_source(tmp_path)
    before = repository.path.read_bytes()
    result = preflight(
        tmp_path,
        credential_store=InMemoryCredentialStore("synthetic-invalid-credential"),
    )

    assert result.ready
    assert repository.path.read_bytes() == before
    public = result.public_report(live_requested=False)
    assert public == {
        "mode": "preflight",
        "status": "ready",
        "category": "ready",
        "checks": {
            "provider_enabled": True,
            "credential_configured": True,
            "deepseek_contract": True,
        },
        "live_execution_requested": False,
        "network_requests_started": 0,
    }
    serialized = json.dumps(public, ensure_ascii=False)
    assert "synthetic-invalid-credential" not in serialized
    assert "base_url" not in serialized
    assert "model" not in serialized


def test_preflight_migrates_legacy_settings_in_memory_without_touching_source(
    tmp_path: Path,
) -> None:
    paths = AppPaths.for_current_user(tmp_path)
    paths.initialize()
    legacy = deepcopy(DEFAULT_SETTINGS)
    legacy["schema_version"] = 3
    legacy.pop("memory")
    legacy["provider_enabled"] = True
    paths.settings_file.write_text(
        json.dumps(legacy, ensure_ascii=False),
        encoding="utf-8",
    )
    before = paths.settings_file.read_bytes()

    result = preflight(
        tmp_path,
        credential_store=InMemoryCredentialStore("synthetic-invalid-credential"),
    )

    assert result.ready
    assert paths.settings_file.read_bytes() == before


def test_missing_configuration_fails_closed_without_provider_work(tmp_path: Path) -> None:
    result = preflight(
        tmp_path,
        credential_store=InMemoryCredentialStore("synthetic-invalid-credential"),
    )

    assert not result.ready
    assert result.category == "settings_missing"
    report = execute_smoke(result)
    assert report["status"] == "blocked"
    assert report["network_requests_started"] == 0


def test_offline_provider_exercises_all_live_smoke_acceptance_checks(
    qapp,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    isolated_parent = tmp_path / "isolated"
    isolated_parent.mkdir()
    _configured_source(source)
    credentials = InMemoryCredentialStore("synthetic-invalid-credential")
    result = preflight(source, credential_store=credentials)

    report = execute_smoke(
        result,
        application=qapp,
        credential_store=credentials,
        provider_override=SyntheticAcceptanceProvider(),
        temporary_parent=isolated_parent,
        turn_timeout_seconds=5.0,
        job_timeout_seconds=5.0,
    )

    assert report["status"] == "passed", report
    assert report["category"] == "none"
    live_model = report["live_model"]
    assert live_model["foreground_send_count"] == 4
    assert live_model["foreground_terminal_count"] == 4
    assert live_model["scenario_status_counts"] == {"passed": 4}
    assert live_model["scenario_checks"] == {
        "do_not_remember": True,
        "exact_duplicate": True,
        "explicit_correction": True,
        "initial": True,
    }
    assert live_model["database_counts"]["memory_groups"] == 1
    assert live_model["database_counts"]["memory_versions"] == 2
    assert live_model["database_counts"]["memory_sources"] == 3
    assert live_model["database_counts"]["memory_fts"] == 1
    assert live_model["database_counts"]["extraction_job_status_counts"] == {"completed": 4}
    assert report["restart"] == {"recovered": True, "turn_count": 4}
    assert report["local_management"] == {
        "status": "passed",
        "category": "none",
        "checks": {
            "archive_persisted": True,
            "delete_cascaded": True,
            "manual_confidence_locked": True,
            "manual_edit_created_version": True,
            "pin_persisted": True,
            "restore_persisted": True,
            "source_context_resolvable": True,
            "source_rows_available": True,
        },
        "source_count": 1,
        "manual_version_delta": 1,
        "operation_failure_category_counts": {},
        "post_delete_counts": {
            "memory_groups": 0,
            "memory_versions": 0,
            "memory_sources": 0,
            "memory_fts": 0,
        },
    }
    assert report["lifecycle_clean"] is True
    assert report["temporary_data_cleaned"] is True
    assert list(isolated_parent.iterdir()) == []

    serialized = json.dumps(report, ensure_ascii=False)
    assert "synthetic-invalid-credential" not in serialized
    assert "source_message" not in serialized
    assert "search_text" not in serialized
    assert "message_id" not in serialized


def test_failed_offline_provider_still_closes_database_and_cleans_temporary_data(
    qapp,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    isolated_parent = tmp_path / "isolated"
    isolated_parent.mkdir()
    _configured_source(source)
    credentials = InMemoryCredentialStore("synthetic-invalid-credential")
    result = preflight(source, credential_store=credentials)

    report = execute_smoke(
        result,
        application=qapp,
        credential_store=credentials,
        provider_override=FailingSyntheticProvider(),
        temporary_parent=isolated_parent,
        turn_timeout_seconds=2.0,
        job_timeout_seconds=2.0,
    )

    assert report["status"] == "failed"
    assert report["category"] == "foreground_network"
    assert report["live_model"]["foreground_send_count"] == 1
    assert report["live_model"]["foreground_terminal_count"] == 1
    assert report["lifecycle_clean"] is True
    assert report["temporary_data_cleaned"] is True
    assert list(isolated_parent.iterdir()) == []
