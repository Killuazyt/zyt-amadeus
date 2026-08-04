from pathlib import Path

from amadeus_desktop.diagnostics import DiagnosticStatusService


def test_diagnostics_snapshot_contains_only_bounded_runtime_state(tmp_path: Path) -> None:
    service = DiagnosticStatusService(
        app_version="0.7.0.dev7",
        commit_sha="a" * 40,
        build_date_utc="2026-08-04",
        settings_schema=5,
        sqlite_schema=3,
        data_root=tmp_path / "Amadeus",
        database_status=lambda: "read_write",
        model_status=lambda: "ready",
        user_index_status=lambda: "active",
        persona_index_status=lambda: "active",
        provider_configured=lambda: True,
        last_error_category=lambda: "provider_timeout",
    )

    snapshot = service.snapshot()

    assert snapshot.app_version == "0.7.0.dev7"
    assert snapshot.commit_sha == "a" * 40
    assert snapshot.build_date_utc == "2026-08-04"
    assert snapshot.settings_schema == 5
    assert snapshot.sqlite_schema == 3
    assert snapshot.database_status == "read_write"
    assert snapshot.provider_configured is True
    assert snapshot.last_error_category == "provider_timeout"


def test_diagnostics_redacts_unbounded_status_and_unknown_error(tmp_path: Path) -> None:
    private = "private prompt with spaces and secret-like text"
    service = DiagnosticStatusService(
        app_version="0.7.0.dev7",
        commit_sha=private,
        build_date_utc="2026-02-30",
        settings_schema=5,
        sqlite_schema=3,
        data_root=tmp_path / "Amadeus",
        database_status=lambda: private,
        model_status=lambda: "ready",
        user_index_status=lambda: "active",
        persona_index_status=lambda: "active",
        provider_configured=lambda: False,
        last_error_category=lambda: private,
    )

    snapshot = service.snapshot()

    assert snapshot.database_status == "unknown"
    assert snapshot.last_error_category == "unknown"
    assert snapshot.commit_sha == "unknown"
    assert snapshot.build_date_utc == "unknown"
    assert private not in repr(snapshot)
