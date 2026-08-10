from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QImageReader

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


def write_modern_package(
    destination: Path,
    *,
    pet_id: str = "sample-test",
    include_logical_dimensions: bool = True,
) -> Path:
    package = destination / "sample.codex-pet"
    package.mkdir(parents=True)
    spritesheet = {
        "path": "spritesheet.webp",
        "frameWidth": 4,
        "frameHeight": 5,
        "columns": 8,
        "rows": 9,
        "defaultScalePercent": 100,
        "alphaThreshold": 8,
        "hitPadding": 2,
    }
    if include_logical_dimensions:
        spritesheet.update({"logicalFrameWidth": 2, "logicalFrameHeight": 3})
    document = {
        "schemaVersion": 1,
        "id": pet_id,
        "displayName": "Synthetic Test Pet",
        "description": "small package used by import tests",
        "kind": "generic",
        "author": "test",
        "source": "generated test fixture",
        "license": "CC0-1.0",
        "spritesheet": spritesheet,
        "animations": {
            "idle": {
                "frames": [[0, 0]],
                "fps": 6,
                "loop": True,
                "fallback": "idle",
            }
        },
    }
    (package / AMadeus_MANIFEST_NAME).write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    image = QImage(32, 45, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    assert image.save(str(package / "spritesheet.webp"), "WEBP")
    return package


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


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


def test_builtin_pet_matches_approved_exact_asset(qapp, tmp_path: Path) -> None:
    asset = PetAssetService(tmp_path / "pets").load_builtin()
    reader = QImageReader(str(asset.spritesheet_path))

    assert asset.manifest.pet_id == BUILTIN_PET_ID
    assert asset.manifest.license_name == "NOASSERTION"
    assert asset.spritesheet_path.name == "spritesheet.webp"
    assert asset.spritesheet_path.stat().st_size == 50_744_436
    assert file_sha256(asset.spritesheet_path) == (
        "cca259ac33ffc7c8170b401a315f9a177a865eb063ba44a4da87c3ab13fa90b7"
    )
    assert reader.canRead()
    assert (reader.size().width(), reader.size().height()) == (6144, 7488)
    assert asset.manifest.spritesheet.frame_width == 768
    assert asset.manifest.spritesheet.frame_height == 832
    assert asset.manifest.spritesheet.logical_frame_width == 192
    assert asset.manifest.spritesheet.logical_frame_height == 208
    assert asset.manifest.spritesheet.columns == 8
    assert asset.manifest.spritesheet.rows == 9
    assert {
        name: len(animation.frames) for name, animation in asset.manifest.animations.items()
    } == {
        "idle": 6,
        "move_right": 8,
        "move_left": 8,
        "greeting": 4,
        "jump": 5,
        "error": 8,
        "responding": 6,
        "waiting": 6,
        "thinking": 6,
    }
    assert {
        name: asset.manifest.animations[name].fps
        for name in ("move_right", "move_left", "greeting", "thinking")
    } == {
        "move_right": 8,
        "move_left": 8,
        "greeting": 4,
        "thinking": 3,
    }


def test_verified_legacy_profile_uses_nine_rows(qapp, tmp_path: Path) -> None:
    asset = validate_package(write_legacy_package(tmp_path))

    assert asset.manifest.compatibility_profile == LEGACY_PROFILE
    assert asset.manifest.spritesheet.frame_width == 192
    assert asset.manifest.spritesheet.frame_height == 208
    assert asset.manifest.spritesheet.logical_frame_width == 192
    assert asset.manifest.spritesheet.logical_frame_height == 208
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


def test_modern_manifest_without_logical_dimensions_uses_source_size(
    qapp,
    tmp_path: Path,
) -> None:
    source = write_modern_package(tmp_path, include_logical_dimensions=False)

    asset = validate_package(source)

    assert asset.manifest.spritesheet.frame_width == 4
    assert asset.manifest.spritesheet.frame_height == 5
    assert asset.manifest.spritesheet.logical_frame_width == 4
    assert asset.manifest.spritesheet.logical_frame_height == 5


def test_modern_manifest_preserves_explicit_logical_dimensions(qapp, tmp_path: Path) -> None:
    asset = validate_package(write_modern_package(tmp_path))

    assert asset.manifest.spritesheet.frame_width == 4
    assert asset.manifest.spritesheet.frame_height == 5
    assert asset.manifest.spritesheet.logical_frame_width == 2
    assert asset.manifest.spritesheet.logical_frame_height == 3


def test_directory_import_copies_and_writes_legacy_supplement(qapp, tmp_path: Path) -> None:
    source = write_legacy_package(tmp_path / "source")
    service = PetAssetService(tmp_path / "local" / "pets")

    imported = service.import_package(source)

    assert imported.root == tmp_path / "local" / "pets" / "legacy-test"
    assert (imported.root / AMadeus_MANIFEST_NAME).is_file()
    assert not (source / AMadeus_MANIFEST_NAME).exists()


def test_archive_import_supports_existing_codex_pet_zip(qapp, tmp_path: Path) -> None:
    source = write_modern_package(tmp_path / "source")
    archive = tmp_path / "sample.codex-pet.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for path in source.iterdir():
            if path.is_file():
                handle.write(path, path.name)

    imported = PetAssetService(tmp_path / "pets").import_package(archive)

    assert imported.manifest.pet_id == "sample-test"
    assert imported.spritesheet_path.is_file()


def test_duplicate_import_requires_explicit_replace(qapp, tmp_path: Path) -> None:
    source = write_modern_package(tmp_path / "source")
    service = PetAssetService(tmp_path / "pets")
    service.import_package(source)

    with pytest.raises(PetAlreadyInstalledError):
        service.import_package(source)

    replaced = service.import_package(source, replace=True)
    assert replaced.manifest.pet_id == "sample-test"


def test_missing_active_pet_falls_back_without_overwriting(qapp, tmp_path: Path) -> None:
    asset = PetAssetService(tmp_path / "pets").load_active("missing-pet")

    assert asset.manifest.pet_id == BUILTIN_PET_ID
    assert asset.is_fallback is True


def test_list_switch_and_safe_remove_keep_bundled_pet(qapp, tmp_path: Path) -> None:
    service = PetAssetService(tmp_path / "pets")
    imported = service.import_package(write_legacy_package(tmp_path / "source"))

    assert [asset.manifest.pet_id for asset in service.list_installed()] == [
        BUILTIN_PET_ID,
        "legacy-test",
    ]
    assert service.load_active("legacy-test").manifest.pet_id == "legacy-test"
    assert service.remove(imported.manifest.pet_id) is True
    assert [asset.manifest.pet_id for asset in service.list_installed()] == [BUILTIN_PET_ID]
    with pytest.raises(InvalidPetAssetError, match="bundled"):
        service.remove(BUILTIN_PET_ID)


@pytest.mark.parametrize("entry", ["../escape.json", "/absolute.json", "C:/drive.json"])
def test_archive_path_escape_is_rejected(qapp, tmp_path: Path, entry: str) -> None:
    archive = tmp_path / "unsafe.codex-pet.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(entry, "{}")

    with pytest.raises(InvalidPetAssetError, match="relative|unsafe"):
        PetAssetService(tmp_path / "pets").import_package(archive)


def test_unsupported_executable_is_rejected(qapp, tmp_path: Path) -> None:
    source = write_modern_package(tmp_path)
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
    source = write_modern_package(tmp_path)
    (source / "extra.txt").write_text("extra", encoding="utf-8")
    monkeypatch.setattr(pet_assets, "MAX_FILE_COUNT", 2)

    with pytest.raises(InvalidPetAssetError, match="files"):
        PetAssetService(tmp_path / "pets").import_package(source)


def test_directory_total_size_limit_is_enforced(qapp, tmp_path: Path, monkeypatch) -> None:
    source = write_modern_package(tmp_path)
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
    source = write_modern_package(tmp_path)
    image = QImage(32, 32, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    assert image.save(str(source / "spritesheet.webp"), "WEBP")

    with pytest.raises(InvalidPetAssetError, match="dimensions"):
        validate_package(source)


def test_corrupt_spritesheet_is_rejected(qapp, tmp_path: Path) -> None:
    source = write_modern_package(tmp_path)
    (source / "spritesheet.webp").write_bytes(b"not an image")

    with pytest.raises(InvalidPetAssetError, match="cannot be decoded"):
        validate_package(source)


def test_failed_replacement_preserves_installed_pet(qapp, tmp_path: Path) -> None:
    service = PetAssetService(tmp_path / "pets")
    original_source = write_modern_package(tmp_path / "original")
    service.import_package(original_source)
    replacement_source = write_modern_package(tmp_path / "replacement")
    (replacement_source / "spritesheet.webp").write_bytes(b"not an image")

    with pytest.raises(InvalidPetAssetError):
        service.import_package(replacement_source, replace=True)

    installed = service.load_active("sample-test")
    assert installed.is_fallback is False
    assert installed.manifest.pet_id == "sample-test"


def test_non_string_compatibility_profile_is_rejected(qapp, tmp_path: Path) -> None:
    source = write_modern_package(tmp_path)
    manifest_path = source / AMadeus_MANIFEST_NAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["compatibilityProfile"] = 9
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(InvalidPetAssetError, match="compatibilityProfile"):
        validate_package(source)


def test_builtin_large_file_exception_does_not_relax_import_limit(
    qapp,
    tmp_path: Path,
) -> None:
    service = PetAssetService(tmp_path / "pets")
    source = write_modern_package(tmp_path / "source")
    with (source / "spritesheet.webp").open("r+b") as handle:
        handle.truncate(pet_assets.MAX_SINGLE_FILE_BYTES + 1)

    assert service.load_builtin().manifest.pet_id == BUILTIN_PET_ID
    with pytest.raises(InvalidPetAssetError, match="exceeds 32 MiB"):
        service.import_package(source)


def test_builtin_directory_is_not_a_general_large_package_exception(qapp) -> None:
    with pytest.raises(InvalidPetAssetError, match="exceeds 32 MiB"):
        validate_package(builtin_pet_root())
