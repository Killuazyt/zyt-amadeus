"""Privacy-bounded diagnostic snapshots for the local settings page."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from amadeus_desktop.build_info import DEVELOPMENT_VALUE

_SAFE_ERROR_CATEGORIES = frozenset(
    {
        "",
        "authentication",
        "content_filter",
        "credential",
        "database_read_only",
        "database_unavailable",
        "generation_model_mismatch",
        "index_failed",
        "insufficient_balance",
        "model_corrupt",
        "model_inference_failed",
        "model_missing",
        "model_or_parameter",
        "model_runtime_unavailable",
        "model_version_mismatch",
        "network",
        "not_configured",
        "protocol",
        "provider_authentication",
        "provider_network",
        "provider_rate_limited",
        "provider_timeout",
        "provider_unconfigured",
        "rate_limit",
        "server",
        "storage_error",
        "timeout",
        "unknown",
    }
)


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    app_version: str
    settings_schema: int
    sqlite_schema: int
    data_root: Path
    database_status: str
    model_status: str
    user_index_status: str
    persona_index_status: str
    provider_configured: bool
    last_error_category: str
    commit_sha: str = DEVELOPMENT_VALUE
    build_date_utc: str = DEVELOPMENT_VALUE


class DiagnosticStatusService:
    """Build one content-free snapshot from injectable state readers."""

    def __init__(
        self,
        *,
        app_version: str,
        settings_schema: int,
        sqlite_schema: int,
        data_root: Path,
        database_status: Callable[[], str],
        model_status: Callable[[], str],
        user_index_status: Callable[[], str],
        persona_index_status: Callable[[], str],
        provider_configured: Callable[[], bool],
        last_error_category: Callable[[], str | None],
        commit_sha: str = DEVELOPMENT_VALUE,
        build_date_utc: str = DEVELOPMENT_VALUE,
    ) -> None:
        self._app_version = app_version
        self._settings_schema = settings_schema
        self._sqlite_schema = sqlite_schema
        self._data_root = data_root
        self._database_status = database_status
        self._model_status = model_status
        self._user_index_status = user_index_status
        self._persona_index_status = persona_index_status
        self._provider_configured = provider_configured
        self._last_error_category = last_error_category
        self._commit_sha = commit_sha
        self._build_date_utc = build_date_utc

    def snapshot(self) -> DiagnosticSnapshot:
        raw_category = (self._last_error_category() or "").strip().lower()
        safe_category = raw_category if raw_category in _SAFE_ERROR_CATEGORIES else "unknown"
        return DiagnosticSnapshot(
            app_version=self._app_version,
            settings_schema=self._settings_schema,
            sqlite_schema=self._sqlite_schema,
            data_root=self._data_root,
            database_status=_bounded_status(self._database_status()),
            model_status=_bounded_status(self._model_status()),
            user_index_status=_bounded_status(self._user_index_status()),
            persona_index_status=_bounded_status(self._persona_index_status()),
            provider_configured=bool(self._provider_configured()),
            last_error_category=safe_category,
            commit_sha=_safe_commit_sha(self._commit_sha),
            build_date_utc=_safe_build_date(self._build_date_utc),
        )


def _bounded_status(value: object) -> str:
    normalized = str(value).strip().lower()
    if (
        not normalized
        or len(normalized) > 64
        or not all(
            character.isascii() and (character.isalnum() or character in "_-.")
            for character in normalized
        )
    ):
        return "unknown"
    return normalized


def _safe_commit_sha(value: object) -> str:
    normalized = str(value).strip().lower()
    if normalized == DEVELOPMENT_VALUE or re.fullmatch(r"[0-9a-f]{40}", normalized):
        return normalized
    return "unknown"


def _safe_build_date(value: object) -> str:
    normalized = str(value).strip()
    if normalized == DEVELOPMENT_VALUE:
        return normalized
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized) is None:
        return "unknown"
    try:
        return normalized if date.fromisoformat(normalized).isoformat() == normalized else "unknown"
    except ValueError:
        return "unknown"
