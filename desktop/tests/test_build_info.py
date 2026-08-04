from __future__ import annotations

import json
from pathlib import Path

import pytest

from amadeus_desktop import __version__
from amadeus_desktop.build_info import (
    BUILD_INFO_RESOURCE,
    BuildInfoError,
    load_build_info,
)


def _write_manifest(path: Path, **overrides: object) -> None:
    payload = {
        "schema_version": 1,
        "version": __version__,
        "commit_sha": "a" * 40,
        "build_date_utc": "2026-08-04",
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_source_tree_without_generated_manifest_uses_explicit_development_values() -> None:
    info = load_build_info(frozen=False)

    assert info.version == __version__
    assert info.commit_sha == "development"
    assert info.build_date_utc == "development"


def test_frozen_build_reads_the_fixed_package_resource(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / BUILD_INFO_RESOURCE
    _write_manifest(manifest)
    monkeypatch.setattr("amadeus_desktop.build_info.sys._MEIPASS", str(tmp_path), raising=False)

    info = load_build_info(frozen=True)

    assert info.schema_version == 1
    assert info.version == "0.7.0.dev7"
    assert info.commit_sha == "a" * 40
    assert info.build_date_utc == "2026-08-04"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema_version": 2}, "schema"),
        ({"version": "0.6.0.dev6"}, "version"),
        ({"commit_sha": "A" * 40}, "commit"),
        ({"commit_sha": "private branch"}, "commit"),
        ({"build_date_utc": "2026-02-30"}, "date"),
    ],
)
def test_invalid_packaged_build_information_fails_closed(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    manifest = tmp_path / "build-info.json"
    _write_manifest(manifest, **overrides)

    with pytest.raises(BuildInfoError, match=message):
        load_build_info(frozen=True, resource_path=manifest)


def test_frozen_build_does_not_use_development_fallback(tmp_path: Path) -> None:
    with pytest.raises(BuildInfoError, match="unavailable"):
        load_build_info(frozen=True, resource_path=tmp_path / "missing.json")
