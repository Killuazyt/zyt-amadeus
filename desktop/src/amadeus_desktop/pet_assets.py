"""Validation, compatibility, loading, and safe import for desktop-pet packages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from PySide6.QtGui import QImageReader

from amadeus_desktop.pet_models import (
    AnimationSpec,
    FrameCoordinate,
    PetManifest,
    SpriteSheetSpec,
)

CURRENT_PET_SCHEMA_VERSION = 1
AMadeus_MANIFEST_NAME = "pet.amadeus.json"
LEGACY_MANIFEST_NAME = "pet.json"
LEGACY_PROFILE = "legacy-8x9-192x208"
BUILTIN_PET_ID = "builtin-amadeus"
BUILTIN_SPRITESHEET_SHA256 = "cca259ac33ffc7c8170b401a315f9a177a865eb063ba44a4da87c3ab13fa90b7"
BUILTIN_SPRITESHEET_BYTES = 50_744_436

MAX_FILE_COUNT = 256
MAX_SINGLE_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
ALLOWED_SUFFIXES = {".json", ".md", ".png", ".txt", ".webp"}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class PetAssetError(RuntimeError):
    """Base class for actionable pet package failures."""


class InvalidPetAssetError(PetAssetError):
    """Raised when a package is malformed or unsafe."""


class PetAlreadyInstalledError(PetAssetError):
    """Raised when import would replace a package without explicit approval."""


@dataclass(frozen=True, slots=True)
class LoadedPetAsset:
    manifest: PetManifest
    root: Path
    spritesheet_path: Path
    is_fallback: bool = False


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidPetAssetError(f"{label} must be a JSON object.")
    return value


def _require_string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise InvalidPetAssetError(f"{label} must be a non-empty string.")
    return value.strip()


def _require_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidPetAssetError(f"{label} must be between {minimum} and {maximum}.")
    return value


def _relative_parts(value: str, label: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ":" in normalized or not pure.parts:
        raise InvalidPetAssetError(f"{label} must be a package-relative path.")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise InvalidPetAssetError(f"{label} contains an unsafe path component.")
    return tuple(pure.parts)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return _require_mapping(json.load(handle), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidPetAssetError(f"{label} is not valid UTF-8 JSON.") from exc


def _animation_from_document(name: str, value: Any, sheet: SpriteSheetSpec) -> AnimationSpec:
    document = _require_mapping(value, f"animations.{name}")
    raw_frames = document.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise InvalidPetAssetError(f"animations.{name}.frames must be a non-empty array.")
    frames: list[FrameCoordinate] = []
    for index, raw_frame in enumerate(raw_frames):
        if not isinstance(raw_frame, list) or len(raw_frame) != 2:
            raise InvalidPetAssetError(f"animations.{name}.frames[{index}] must be [column, row].")
        column = _require_int(raw_frame[0], f"{name} frame column", 0, sheet.columns - 1)
        row = _require_int(raw_frame[1], f"{name} frame row", 0, sheet.rows - 1)
        frames.append(FrameCoordinate(column, row))

    fps = document.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 1 <= float(fps) <= 60:
        raise InvalidPetAssetError(f"animations.{name}.fps must be between 1 and 60.")
    loop = document.get("loop")
    if not isinstance(loop, bool):
        raise InvalidPetAssetError(f"animations.{name}.loop must be a boolean.")
    fallback = _require_string(document.get("fallback", "idle"), f"animations.{name}.fallback")
    return AnimationSpec(name, tuple(frames), float(fps), loop, fallback)


def manifest_from_document(document: dict[str, Any]) -> PetManifest:
    schema_version = _require_int(
        document.get("schemaVersion"),
        "schemaVersion",
        CURRENT_PET_SCHEMA_VERSION,
        CURRENT_PET_SCHEMA_VERSION,
    )
    pet_id = _require_string(document.get("id"), "id")
    if SAFE_ID.fullmatch(pet_id) is None:
        raise InvalidPetAssetError(
            "id must use lowercase letters, digits, dot, underscore, or dash."
        )

    raw_sheet = _require_mapping(document.get("spritesheet"), "spritesheet")
    path = _require_string(raw_sheet.get("path"), "spritesheet.path")
    _relative_parts(path, "spritesheet.path")
    frame_width = _require_int(raw_sheet.get("frameWidth"), "frameWidth", 1, 8192)
    frame_height = _require_int(raw_sheet.get("frameHeight"), "frameHeight", 1, 8192)
    sheet = SpriteSheetSpec(
        path=path,
        frame_width=frame_width,
        frame_height=frame_height,
        columns=_require_int(raw_sheet.get("columns"), "columns", 1, 256),
        rows=_require_int(raw_sheet.get("rows"), "rows", 1, 256),
        default_scale_percent=_require_int(
            raw_sheet.get("defaultScalePercent", 100), "defaultScalePercent", 50, 200
        ),
        logical_frame_width=_require_int(
            raw_sheet.get("logicalFrameWidth", frame_width),
            "logicalFrameWidth",
            1,
            8192,
        ),
        logical_frame_height=_require_int(
            raw_sheet.get("logicalFrameHeight", frame_height),
            "logicalFrameHeight",
            1,
            8192,
        ),
        alpha_threshold=_require_int(raw_sheet.get("alphaThreshold", 8), "alphaThreshold", 1, 254),
        hit_padding=_require_int(raw_sheet.get("hitPadding", 2), "hitPadding", 0, 16),
    )

    raw_animations = _require_mapping(document.get("animations"), "animations")
    animations = {
        name: _animation_from_document(name, value, sheet)
        for name, value in raw_animations.items()
        if isinstance(name, str) and name
    }
    if "idle" not in animations:
        raise InvalidPetAssetError("animations must include idle.")
    for animation in animations.values():
        if animation.fallback not in animations:
            raise InvalidPetAssetError(
                f"animations.{animation.name}.fallback references missing action "
                f"{animation.fallback}."
            )

    compatibility_profile = document.get("compatibilityProfile")
    if compatibility_profile is not None:
        compatibility_profile = _require_string(
            compatibility_profile,
            "compatibilityProfile",
        )

    return PetManifest(
        schema_version=schema_version,
        pet_id=pet_id,
        display_name=_require_string(document.get("displayName"), "displayName"),
        description=_require_string(
            document.get("description", ""), "description", allow_empty=True
        ),
        kind=_require_string(document.get("kind", "generic"), "kind"),
        author=_require_string(document.get("author", "未声明"), "author"),
        source=_require_string(document.get("source", "未声明"), "source"),
        license_name=_require_string(document.get("license", "未声明"), "license"),
        spritesheet=sheet,
        animations=animations,
        compatibility_profile=compatibility_profile,
    )


def manifest_to_document(manifest: PetManifest) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schemaVersion": manifest.schema_version,
        "id": manifest.pet_id,
        "displayName": manifest.display_name,
        "description": manifest.description,
        "kind": manifest.kind,
        "author": manifest.author,
        "source": manifest.source,
        "license": manifest.license_name,
        "spritesheet": {
            "path": manifest.spritesheet.path,
            "frameWidth": manifest.spritesheet.frame_width,
            "frameHeight": manifest.spritesheet.frame_height,
            "columns": manifest.spritesheet.columns,
            "rows": manifest.spritesheet.rows,
            "defaultScalePercent": manifest.spritesheet.default_scale_percent,
            "logicalFrameWidth": manifest.spritesheet.logical_frame_width,
            "logicalFrameHeight": manifest.spritesheet.logical_frame_height,
            "alphaThreshold": manifest.spritesheet.alpha_threshold,
            "hitPadding": manifest.spritesheet.hit_padding,
        },
        "animations": {
            name: {
                "frames": [[frame.column, frame.row] for frame in animation.frames],
                "fps": animation.fps,
                "loop": animation.loop,
                "fallback": animation.fallback,
            }
            for name, animation in manifest.animations.items()
        },
    }
    if manifest.compatibility_profile:
        document["compatibilityProfile"] = manifest.compatibility_profile
    return document


def _legacy_animation(
    name: str,
    row: int,
    frame_count: int,
    fps: float,
    loop: bool,
) -> AnimationSpec:
    return AnimationSpec(
        name,
        tuple(FrameCoordinate(column, row) for column in range(frame_count)),
        fps,
        loop,
        "idle",
    )


def legacy_manifest_from_document(document: dict[str, Any]) -> PetManifest:
    pet_id = _require_string(document.get("id"), "id")
    if SAFE_ID.fullmatch(pet_id) is None:
        raise InvalidPetAssetError("Legacy id contains unsafe characters.")
    image_path = _require_string(document.get("spritesheetPath"), "spritesheetPath")
    _relative_parts(image_path, "spritesheetPath")
    sheet = SpriteSheetSpec(image_path, 192, 208, 8, 9, 100, 192, 208)
    animations = {
        "idle": _legacy_animation("idle", 0, 6, 6, True),
        "move_right": _legacy_animation("move_right", 1, 8, 10, True),
        "move_left": _legacy_animation("move_left", 2, 8, 10, True),
        "greeting": _legacy_animation("greeting", 3, 4, 8, False),
        "jump": _legacy_animation("jump", 4, 5, 8, False),
        "error": _legacy_animation("error", 5, 8, 8, False),
        "responding": _legacy_animation("responding", 6, 6, 6, True),
        "waiting": _legacy_animation("waiting", 7, 6, 6, True),
        "thinking": _legacy_animation("thinking", 8, 6, 6, True),
    }
    return PetManifest(
        schema_version=1,
        pet_id=pet_id,
        display_name=_require_string(document.get("displayName"), "displayName"),
        description=_require_string(
            document.get("description", ""), "description", allow_empty=True
        ),
        kind=_require_string(document.get("kind", "person"), "kind"),
        author="未声明",
        source="未声明",
        license_name="未声明",
        spritesheet=sheet,
        animations=animations,
        compatibility_profile=LEGACY_PROFILE,
    )


def _validate_files(root: Path, *, allow_exact_builtin_spritesheet: bool = False) -> None:
    count = 0
    total = 0
    root_resolved = root.resolve()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise InvalidPetAssetError("Pet packages cannot contain symbolic links.")
        if not path.is_file():
            continue
        count += 1
        size = path.stat().st_size
        total += size
        if count > MAX_FILE_COUNT:
            raise InvalidPetAssetError(f"Pet package exceeds {MAX_FILE_COUNT} files.")
        relative_path = path.relative_to(root).as_posix()
        allowed_builtin_spritesheet = (
            allow_exact_builtin_spritesheet
            and root_resolved == builtin_pet_root().resolve()
            and relative_path == "spritesheet.webp"
            and size == BUILTIN_SPRITESHEET_BYTES
            and _file_sha256(path) == BUILTIN_SPRITESHEET_SHA256
        )
        if size > MAX_SINGLE_FILE_BYTES and not allowed_builtin_spritesheet:
            raise InvalidPetAssetError("A pet package file exceeds 32 MiB.")
        if total > MAX_TOTAL_BYTES:
            raise InvalidPetAssetError("Pet package exceeds 64 MiB total.")
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise InvalidPetAssetError(f"Unsupported package file type: {path.suffix or '(none)'}.")
        try:
            path.resolve().relative_to(root_resolved)
        except ValueError as exc:
            raise InvalidPetAssetError("Pet package file escapes the package root.") from exc


def _load_manifest(
    root: Path,
    *,
    allow_exact_builtin_spritesheet: bool = False,
) -> PetManifest:
    _validate_files(
        root,
        allow_exact_builtin_spritesheet=allow_exact_builtin_spritesheet,
    )
    modern_path = root / AMadeus_MANIFEST_NAME
    if modern_path.is_file():
        return manifest_from_document(_read_json(modern_path, AMadeus_MANIFEST_NAME))
    legacy_path = root / LEGACY_MANIFEST_NAME
    if legacy_path.is_file():
        return legacy_manifest_from_document(_read_json(legacy_path, LEGACY_MANIFEST_NAME))
    raise InvalidPetAssetError(
        f"Package must contain {AMadeus_MANIFEST_NAME} or {LEGACY_MANIFEST_NAME}."
    )


def load_manifest(root: Path) -> PetManifest:
    return _load_manifest(root)


def _validate_package(
    root: Path,
    *,
    allow_exact_builtin_spritesheet: bool = False,
) -> LoadedPetAsset:
    manifest = _load_manifest(
        root,
        allow_exact_builtin_spritesheet=allow_exact_builtin_spritesheet,
    )
    parts = _relative_parts(manifest.spritesheet.path, "spritesheet.path")
    image_path = root.joinpath(*parts)
    if not image_path.is_file():
        raise InvalidPetAssetError("The declared spritesheet does not exist.")

    reader = QImageReader(os.fspath(image_path))
    reader.setAutoTransform(False)
    if not reader.canRead():
        raise InvalidPetAssetError(f"The spritesheet cannot be decoded: {reader.errorString()}.")
    size = reader.size()
    expected_width = manifest.spritesheet.frame_width * manifest.spritesheet.columns
    expected_height = manifest.spritesheet.frame_height * manifest.spritesheet.rows
    if size.width() != expected_width or size.height() != expected_height:
        raise InvalidPetAssetError(
            "Spritesheet dimensions do not match frame size and grid "
            f"({size.width()}x{size.height()} != {expected_width}x{expected_height})."
        )
    return LoadedPetAsset(manifest, root, image_path)


def validate_package(root: Path) -> LoadedPetAsset:
    return _validate_package(root)


def _validate_source_name(source: Path) -> None:
    name = source.name.lower()
    valid_directory = source.is_dir() and name.endswith(".codex-pet")
    valid_archive = source.is_file() and (
        name.endswith(".codex-pet") or name.endswith(".codex-pet.zip")
    )
    if not (valid_directory or valid_archive):
        raise InvalidPetAssetError(
            "Source must be a .codex-pet directory or .codex-pet(.zip) archive."
        )


def _copy_directory(source: Path, destination: Path) -> None:
    _validate_files(source)
    shutil.copytree(source, destination, symlinks=True)


def _extract_archive(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    seen: set[str] = set()
    count = 0
    total = 0
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InvalidPetAssetError("Pet archive is not a valid ZIP file.") from exc
    with archive:
        for info in archive.infolist():
            parts = _relative_parts(info.filename, "archive entry")
            canonical = "/".join(parts).casefold()
            if canonical in seen:
                raise InvalidPetAssetError("Pet archive contains duplicate file names.")
            seen.add(canonical)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_IFMT(mode) == stat.S_IFLNK:
                raise InvalidPetAssetError("Pet archive cannot contain symbolic links.")
            if info.flag_bits & 0x1:
                raise InvalidPetAssetError("Encrypted pet archives are not supported.")
            if info.is_dir():
                destination.joinpath(*parts).mkdir(parents=True, exist_ok=True)
                continue
            count += 1
            total += info.file_size
            if (
                count > MAX_FILE_COUNT
                or info.file_size > MAX_SINGLE_FILE_BYTES
                or total > MAX_TOTAL_BYTES
            ):
                raise InvalidPetAssetError("Pet archive exceeds the safe extraction limits.")
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source_handle, target.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)


def builtin_pet_root() -> Path:
    return Path(__file__).parent / "resources" / "builtin_pet"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PetAssetService:
    def __init__(self, pets_root: Path) -> None:
        self.pets_root = pets_root

    def load_builtin(self, *, fallback: bool = False) -> LoadedPetAsset:
        root = builtin_pet_root()
        spritesheet_path = root / "spritesheet.webp"
        if (
            not spritesheet_path.is_file()
            or spritesheet_path.stat().st_size != BUILTIN_SPRITESHEET_BYTES
            or _file_sha256(spritesheet_path) != BUILTIN_SPRITESHEET_SHA256
        ):
            raise InvalidPetAssetError("The bundled pet spritesheet identity is invalid.")
        asset = _validate_package(root, allow_exact_builtin_spritesheet=True)
        if asset.spritesheet_path != spritesheet_path:
            raise InvalidPetAssetError("The bundled pet spritesheet path is invalid.")
        return LoadedPetAsset(asset.manifest, asset.root, asset.spritesheet_path, fallback)

    def load_active(self, pet_id: str) -> LoadedPetAsset:
        if pet_id == BUILTIN_PET_ID:
            return self.load_builtin()
        try:
            return validate_package(self.pets_root / pet_id)
        except (OSError, PetAssetError):
            return self.load_builtin(fallback=True)

    def list_installed(self) -> tuple[LoadedPetAsset, ...]:
        """Return the bundled pet plus every valid imported package."""

        assets = [self.load_builtin()]
        if not self.pets_root.exists():
            return tuple(assets)
        for candidate in sorted(self.pets_root.iterdir(), key=lambda path: path.name.lower()):
            if (
                not candidate.is_dir()
                or candidate.name.startswith(".")
                or SAFE_ID.fullmatch(candidate.name) is None
            ):
                continue
            try:
                asset = validate_package(candidate)
            except (OSError, PetAssetError):
                continue
            if asset.manifest.pet_id == candidate.name and asset.manifest.pet_id != BUILTIN_PET_ID:
                assets.append(asset)
        return tuple(assets)

    def remove(self, pet_id: str) -> bool:
        """Remove one validated imported package without accepting a broad path."""

        if pet_id == BUILTIN_PET_ID:
            raise InvalidPetAssetError("The bundled pet cannot be removed.")
        if SAFE_ID.fullmatch(str(pet_id)) is None:
            raise InvalidPetAssetError("Pet id is invalid.")
        root = self.pets_root.resolve()
        target = (self.pets_root / pet_id).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise InvalidPetAssetError("Pet path escaped the import directory.") from exc
        if not target.exists():
            return False
        if not target.is_dir() or target.is_symlink():
            raise InvalidPetAssetError("Installed pet path is unsafe.")
        validate_package(target)
        shutil.rmtree(target)
        return True

    def import_package(self, source: Path, *, replace: bool = False) -> LoadedPetAsset:
        source = source.resolve()
        _validate_source_name(source)
        self.pets_root.mkdir(parents=True, exist_ok=True)
        staging_parent = Path(tempfile.mkdtemp(prefix=".pet-import-", dir=self.pets_root))
        staging = staging_parent / "package"
        backup: Path | None = None
        target: Path | None = None
        try:
            if source.is_dir():
                _copy_directory(source, staging)
            else:
                _extract_archive(source, staging)
            asset = validate_package(staging)
            if (
                asset.manifest.compatibility_profile
                and not (staging / AMadeus_MANIFEST_NAME).exists()
            ):
                with (staging / AMadeus_MANIFEST_NAME).open("w", encoding="utf-8") as handle:
                    json.dump(
                        manifest_to_document(asset.manifest),
                        handle,
                        ensure_ascii=False,
                        indent=2,
                    )
                    handle.write("\n")
            target = self.pets_root / asset.manifest.pet_id
            if target.exists() and not replace:
                raise PetAlreadyInstalledError(
                    f"Pet {asset.manifest.pet_id} is already installed; use explicit replacement."
                )
            if target.exists():
                backup = self.pets_root / f".pet-backup-{asset.manifest.pet_id}-{uuid4().hex}"
                target.replace(backup)
            staging.replace(target)
            if backup is not None:
                shutil.rmtree(backup)
            return validate_package(target)
        except Exception:
            if backup is not None and backup.exists():
                if target is not None and target.exists():
                    shutil.rmtree(target)
                if target is not None:
                    backup.replace(target)
            raise
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)
