from __future__ import annotations

import json
import shutil
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


def test_scanner_rejects_bare_token_plan_credentials(tmp_path) -> None:
    source = tmp_path / "accidental_secret.txt"
    source.write_text("tp-" + "a" * 32, encoding="utf-8")

    exit_code, result = _scan(source)

    assert exit_code == 1
    assert result["violations_by_category"] == {"credential_pattern": 1}


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


def test_scanner_only_allows_exact_pinned_pet_and_icon_media(tmp_path) -> None:
    resource_root = Path(__file__).resolve().parents[1] / "src" / "amadeus_desktop" / "resources"
    bundled_pet = tmp_path / "amadeus_desktop" / "resources" / "builtin_pet"
    app_icon = tmp_path / "amadeus_desktop" / "resources" / "app_icon"
    bundled_pet.mkdir(parents=True)
    app_icon.mkdir(parents=True)
    copied_pet = bundled_pet / "spritesheet.webp"
    shutil.copy2(resource_root / "builtin_pet" / "spritesheet.webp", copied_pet)
    shutil.copy2(resource_root / "app_icon" / "spritesheet.png", app_icon / "spritesheet.png")

    exit_code, result = _scan(tmp_path)

    assert exit_code == 0
    assert result["status"] == "passed"
    assert result["violations_by_category"] == {}

    tampered = bytearray(copied_pet.read_bytes())
    tampered[-1] ^= 1
    copied_pet.write_bytes(tampered)

    exit_code, result = _scan(tmp_path)

    assert exit_code == 1
    assert result["status"] == "failed"
    assert result["violations_by_category"] == {"unauthorized_character_asset": 1}


def test_scanner_rejects_approved_media_hash_without_a_path_boundary(tmp_path) -> None:
    resource_root = Path(__file__).resolve().parents[1] / "src" / "amadeus_desktop" / "resources"
    misleading_root = tmp_path / "privateamadeus_desktop" / "resources" / "builtin_pet"
    misleading_root.mkdir(parents=True)
    shutil.copy2(resource_root / "builtin_pet" / "spritesheet.webp", misleading_root)

    exit_code, result = _scan(tmp_path)

    assert exit_code == 1
    assert result["status"] == "failed"
    assert result["violations_by_category"] == {"unauthorized_character_asset": 1}


def test_scanner_allows_pinned_media_at_an_exact_wheel_member_path(tmp_path) -> None:
    resource_root = Path(__file__).resolve().parents[1] / "src" / "amadeus_desktop" / "resources"
    artifact = tmp_path / "package.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.write(
            resource_root / "builtin_pet" / "spritesheet.webp",
            "amadeus_desktop/resources/builtin_pet/spritesheet.webp",
        )

    exit_code, result = _scan(tmp_path)

    assert exit_code == 0
    assert result["status"] == "passed"
    assert result["archive_members_scanned"] == 1
    assert result["violations_by_category"] == {}


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


def test_scanner_opens_p6_backup_and_extension_only_pet_archives(tmp_path) -> None:
    backup = tmp_path / "private.amadeus-backup"
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr("manifest.json", "{}")

    pet = tmp_path / "private.codex-pet"
    with zipfile.ZipFile(pet, "w") as archive:
        archive.writestr("spritesheet.png", b"private-character-image")

    exit_code, result = _scan(tmp_path)

    assert exit_code == 1
    assert result["archive_members_scanned"] == 2
    assert result["violations_by_category"] == {
        "local_backup_file": 1,
        "unauthorized_character_asset": 1,
    }

    backup_exit_code, backup_result = _scan(backup)
    assert backup_exit_code == 1
    assert backup_result["violations_by_category"] == {"local_backup_file": 1}


def test_scanner_rejects_default_exports_and_local_persona_greetings(tmp_path) -> None:
    (tmp_path / "amadeus-chat-export.json").write_text("{}", encoding="utf-8")
    (tmp_path / "amadeus-memory-export.json").write_text("{}", encoding="utf-8")
    (tmp_path / "renamed.json").write_text(
        json.dumps({"format": "amadeus-chat-export/v1", "conversations": []}),
        encoding="utf-8",
    )
    greetings = tmp_path / "personas" / "kurisu" / "greetings.json"
    greetings.parent.mkdir(parents=True)
    greetings.write_text("{}", encoding="utf-8")

    exit_code, result = _scan(tmp_path)

    assert exit_code == 1
    assert result["violations_by_category"] == {
        "local_data_export": 3,
        "private_persona_data": 1,
    }
