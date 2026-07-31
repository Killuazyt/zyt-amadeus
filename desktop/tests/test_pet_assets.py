from __future__ import annotations

import json
import shutil
import stat
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

import amadeus_desktop.pet_assets as pet_assets
from amadeus_desktop.pet_assets import (
    BUILTIN_PET_ID,
    LEGACY_PROFILE,
    AMadeus_MANIFEST_NAME,
    InvalidPetAssetError,
    PetAlreadyInstalledError,
    PetAssetService,
    builtin_pet_root,
    validate_package,
)


def copy_builtin(destination: Path) -> Path:
    package = destination / "sample.codex-pet"
    shutil.copytree(builtin_pet_root(), package)
    return package


def write_legacy_package(destination: Path) -> Path:
    package = destination / "legacy.codex-pet"
    package.mkdir(parents=True)
    document = {
        "id": "legacy-test",
        "displayName": "Legacy Test",
        "description": "legacy",
        "spritesheetPath": "spritesheet.webp",
        "kind": "person",
    }
    (package / "pet.json").write_text(json.dumps(document), encoding="utf-8")
    image = QImage(1536, 1872, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    assert image.save(str(package / "spritesheet.webp"), "WEBP")
    return package


def test_builtin_pet_is_valid_and_redistributable(qapp) -> None:
    asset = validate_package(builtin_pet_root())

    assert asset.manifest.pet_id == BUILTIN_PET_ID
    assert asset.manifest.license_name == "CC0-1.0"
    assert asset.manifest.spritesheet.columns == 8
    assert asset.manifest.spritesheet.rows == 9
    assert set(asset.manifest.animations) >= {
        "idle",
        "move_left",
        "move_right",
        "greeting",
        "jump",
        "error",
        "responding",
        "waiting",
        "thinking",
    }


def test_verified_legacy_profile_uses_nine_rows(qapp, tmp_path: Path) -> None:
    asset = validate_package(write_legacy_package(tmp_path))

    assert asset.manifest.compatibility_profile == LEGACY_PROFILE
    assert asset.manifest.spritesheet.frame_width == 192
    assert asset.manifest.spritesheet.frame_height == 208
    assert [len(asset.manifest.animations[name].frames) for name in asset.manifest.animations] == [
        6,
        8,
        8,
        4,
        5,
        8,
        6,
        6,
        6,
    ]


def test_directory_import_copies_and_writes_legacy_supplement(qapp, tmp_path: Path) -> None:
    source = write_legacy_package(tmp_path / "source")
    service = PetAssetService(tmp_path / "local" / "pets")

    imported = service.import_package(source)

    assert imported.root == tmp_path / "local" / "pets" / "legacy-test"
    assert (imported.root / AMadeus_MANIFEST_NAME).is_file()
    assert not (source / AMadeus_MANIFEST_NAME).exists()


def test_archive_import_supports_existing_codex_pet_zip(qapp, tmp_path: Path) -> None:
    source = copy_builtin(tmp_path / "source")
    archive = tmp_path / "sample.codex-pet.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for path in source.iterdir():
            if path.is_file():
                handle.write(path, path.name)

    imported = PetAssetService(tmp_path / "pets").import_package(archive)

    assert imported.manifest.pet_id == BUILTIN_PET_ID
    assert imported.spritesheet_path.is_file()


def test_duplicate_import_requires_explicit_replace(qapp, tmp_path: Path) -> None:
    source = copy_builtin(tmp_path / "source")
    service = PetAssetService(tmp_path / "pets")
    service.import_package(source)

    with pytest.raises(PetAlreadyInstalledError):
        service.import_package(source)

    replaced = service.import_package(source, replace=True)
    assert replaced.manifest.pet_id == BUILTIN_PET_ID


def test_missing_active_pet_falls_back_without_overwriting(qapp, tmp_path: Path) -> None:
    asset = PetAssetService(tmp_path / "pets").load_active("missing-pet")

    assert asset.manifest.pet_id == BUILTIN_PET_ID
    assert asset.is_fallback is True


@pytest.mark.parametrize("entry", ["../escape.json", "/absolute.json", "C:/drive.json"])
def test_archive_path_escape_is_rejected(qapp, tmp_path: Path, entry: str) -> None:
    archive = tmp_path / "unsafe.codex-pet.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(entry, "{}")

    with pytest.raises(InvalidPetAssetError, match="relative|unsafe"):
        PetAssetService(tmp_path / "pets").import_package(archive)


def test_unsupported_executable_is_rejected(qapp, tmp_path: Path) -> None:
    source = copy_builtin(tmp_path)
    (source / "payload.exe").write_bytes(b"MZ")

    with pytest.raises(InvalidPetAssetError, match="Unsupported"):
        PetAssetService(tmp_path / "pets").import_package(source)


def test_archive_size_limit_is_enforced(qapp, tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "large.codex-pet.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("pet.json", "{}")
    monkeypatch.setattr(pet_assets, "MAX_SINGLE_FILE_BYTES", 1)

    with pytest.raises(InvalidPetAssetError, match="limits"):
        PetAssetService(tmp_path / "pets").import_package(archive)


def test_directory_file_count_limit_is_enforced(qapp, tmp_path: Path, monkeypatch) -> None:
    source = copy_builtin(tmp_path)
    (source / "extra.txt").write_text("extra", encoding="utf-8")
    monkeypatch.setattr(pet_assets, "MAX_FILE_COUNT", 3)

    with pytest.raises(InvalidPetAssetError, match="files"):
        PetAssetService(tmp_path / "pets").import_package(source)


def test_directory_total_size_limit_is_enforced(qapp, tmp_path: Path, monkeypatch) -> None:
    source = copy_builtin(tmp_path)
    monkeypatch.setattr(pet_assets, "MAX_TOTAL_BYTES", 1)

    with pytest.raises(InvalidPetAssetError, match="total"):
        PetAssetService(tmp_path / "pets").import_package(source)


def test_archive_symbolic_link_is_rejected(qapp, tmp_path: Path) -> None:
    archive = tmp_path / "symlink.codex-pet.zip"
    link = zipfile.ZipInfo("spritesheet.png")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(link, "target.png")

    with pytest.raises(InvalidPetAssetError, match="symbolic links"):
        PetAssetService(tmp_path / "pets").import_package(archive)


def test_invalid_zip_is_rejected(qapp, tmp_path: Path) -> None:
    archive = tmp_path / "broken.codex-pet.zip"
    archive.write_bytes(b"not a zip archive")

    with pytest.raises(InvalidPetAssetError, match="valid ZIP"):
        PetAssetService(tmp_path / "pets").import_package(archive)


def test_wrong_spritesheet_dimensions_are_rejected(qapp, tmp_path: Path) -> None:
    source = copy_builtin(tmp_path)
    image = QImage(32, 32, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    assert image.save(str(source / "spritesheet.png"), "PNG")

    with pytest.raises(InvalidPetAssetError, match="dimensions"):
        validate_package(source)


def test_corrupt_spritesheet_is_rejected(qapp, tmp_path: Path) -> None:
    source = copy_builtin(tmp_path)
    (source / "spritesheet.png").write_bytes(b"not an image")

    with pytest.raises(InvalidPetAssetError, match="cannot be decoded"):
        validate_package(source)


def test_failed_replacement_preserves_installed_pet(qapp, tmp_path: Path) -> None:
    service = PetAssetService(tmp_path / "pets")
    original_source = copy_builtin(tmp_path / "original")
    service.import_package(original_source)
    replacement_source = copy_builtin(tmp_path / "replacement")
    (replacement_source / "spritesheet.png").write_bytes(b"not an image")

    with pytest.raises(InvalidPetAssetError):
        service.import_package(replacement_source, replace=True)

    installed = service.load_active(BUILTIN_PET_ID)
    assert installed.is_fallback is False
    assert installed.manifest.pet_id == BUILTIN_PET_ID


def test_non_string_compatibility_profile_is_rejected(qapp, tmp_path: Path) -> None:
    source = copy_builtin(tmp_path)
    manifest_path = source / AMadeus_MANIFEST_NAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["compatibilityProfile"] = 9
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(InvalidPetAssetError, match="compatibilityProfile"):
        validate_package(source)
