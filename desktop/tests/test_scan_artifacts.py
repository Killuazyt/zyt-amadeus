from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

SCANNER = Path(__file__).resolve().parents[1] / "scripts" / "scan_artifacts.py"


def _scan(path: Path) -> tuple[int, dict[str, object]]:
    completed = subprocess.run(
        (sys.executable, str(SCANNER), "--path", str(path)),
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode, json.loads(completed.stdout)


def test_scanner_opens_archives_nested_below_a_directory(tmp_path) -> None:
    artifact = tmp_path / "package.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("package/.env", "not-a-real-secret")

    exit_code, result = _scan(tmp_path)

    assert exit_code == 1
    assert result["status"] == "failed"
    assert result["archive_members_scanned"] == 1
    assert result["violations_by_category"] == {"dotenv_file": 1}


def test_scanner_allows_explicitly_invalid_test_credentials(tmp_path) -> None:
    source = tmp_path / "test_example.py"
    source.write_text(
        'value = "Authorization: Bearer sk-invalid-test-example-token"\n',
        encoding="utf-8",
    )

    exit_code, result = _scan(source)

    assert exit_code == 0
    assert result["status"] == "passed"
    assert result["violations_by_category"] == {}


def test_scanner_rejects_quoted_json_credentials_and_generic_private_media(tmp_path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"api_key": "a" * 32}),
        encoding="utf-8",
    )
    (tmp_path / "sheet.png").write_bytes(b"synthetic-private-image")

    exit_code, result = _scan(tmp_path)

    assert exit_code == 1
    assert result["violations_by_category"] == {
        "credential_pattern": 1,
        "unauthorized_character_asset": 1,
    }


def test_scanner_rejects_every_embedding_model_file_without_allow_flag(tmp_path) -> None:
    model = tmp_path / "amadeus_desktop" / "resources" / "embedding_model"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")

    exit_code, result = _scan(tmp_path)

    assert exit_code == 1
    assert result["violations_by_category"] == {"unexpected_model_file": 1}


def test_scanner_reports_missing_input_without_path_or_traceback(tmp_path) -> None:
    missing = tmp_path / "private-missing-artifact"
    completed = subprocess.run(
        (sys.executable, str(SCANNER), "--path", str(missing)),
        check=False,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert str(missing) not in completed.stdout
    assert result["violations_by_category"] == {"artifact_missing": 1}
