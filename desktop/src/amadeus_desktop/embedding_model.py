"""Pinned offline embedding-model preparation and verification.

Normal application startup only inspects an already prepared model directory.
Network access is deliberately confined to :func:`prepare_model` and
:func:`repair_model`, which are exposed by the explicit ``amadeus-model`` CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from amadeus_desktop.embedding_backend import EmbeddingBackend


MODEL_API_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_REPOSITORY = "Qdrant/bge-small-zh-v1.5"
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
MODEL_ONNX_FILE = "model_optimized.onnx"
MODEL_ONNX_SHA256 = "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"
MODEL_CONFIG_SHA256 = "9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3"
MODEL_TOKENIZER_SHA256 = "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"
MODEL_TOKENIZER_CONFIG_SHA256 = "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a"
MODEL_SPECIAL_TOKENS_SHA256 = "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3"
MODEL_DIMENSION = 512
MODEL_MANIFEST_FILE = "amadeus-model.json"
MODEL_DIRECTORY_NAME = "bge-small-zh-v1.5"
MODEL_REQUIRED_FILES = (
    MODEL_ONNX_FILE,
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)
MODEL_REQUIRED_FILE_SHA256 = (
    (MODEL_ONNX_FILE, MODEL_ONNX_SHA256),
    ("config.json", MODEL_CONFIG_SHA256),
    ("tokenizer.json", MODEL_TOKENIZER_SHA256),
    ("tokenizer_config.json", MODEL_TOKENIZER_CONFIG_SHA256),
    ("special_tokens_map.json", MODEL_SPECIAL_TOKENS_SHA256),
)
MODEL_BUNDLE_FILES = (*MODEL_REQUIRED_FILES, MODEL_MANIFEST_FILE)


@dataclass(frozen=True, slots=True)
class EmbeddingModelSpec:
    """Immutable identity of the only embedding model accepted by P5B."""

    api_name: str = MODEL_API_NAME
    repository: str = MODEL_REPOSITORY
    revision: str = MODEL_REVISION
    onnx_file: str = MODEL_ONNX_FILE
    onnx_sha256: str = MODEL_ONNX_SHA256
    dimension: int = MODEL_DIMENSION
    required_files: tuple[str, ...] = MODEL_REQUIRED_FILES
    required_file_sha256: tuple[tuple[str, str], ...] = MODEL_REQUIRED_FILE_SHA256


PINNED_MODEL = EmbeddingModelSpec()


class ModelAvailability(StrEnum):
    """Safe, content-free model health states suitable for the settings UI."""

    READY = "ready"
    MISSING = "missing"
    CORRUPT = "corrupt"
    VERSION_MISMATCH = "version_mismatch"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"


@dataclass(frozen=True, slots=True)
class ModelVerification:
    """Result of a structural and optional inference verification."""

    availability: ModelAvailability
    error_category: str | None = None
    dimension: int | None = None
    provider: str | None = None

    @property
    def ready(self) -> bool:
        return self.availability is ModelAvailability.READY


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """Local provenance manifest written only after all files are verified."""

    schema_version: int
    api_name: str
    repository: str
    revision: str
    onnx_file: str
    onnx_sha256: str
    dimension: int
    prepared_at_utc: str
    files: Mapping[str, Mapping[str, str | int]]


class ModelPreparationError(RuntimeError):
    """Raised when a requested prepare/repair operation cannot be completed."""


SnapshotDownloader = Callable[[Path, EmbeddingModelSpec, bool], None]
BackendFactory = Callable[[Path], "EmbeddingBackend"]


def model_directory(models_root: Path, spec: EmbeddingModelSpec = PINNED_MODEL) -> Path:
    """Return the revision-qualified model directory below the app model root."""

    name = MODEL_DIRECTORY_NAME if spec == PINNED_MODEL else _safe_directory_name(spec.api_name)
    return models_root / name / spec.revision


def bundled_model_directory() -> Path:
    """Return the PyInstaller onedir model location beside package resources."""

    return Path(__file__).resolve().parent / "resources" / "embedding_model"


def resolve_runtime_model_directory(
    local_prepared_directory: Path,
    *,
    bundled_directory: Path | None = None,
    spec: EmbeddingModelSpec = PINNED_MODEL,
) -> Path:
    """Prefer a verified onedir model, then the verified LOCALAPPDATA model.

    If neither candidate verifies, return the first existing candidate so the
    caller can surface its precise corrupt/version state; otherwise return the
    local path and report it as missing. No global Hugging Face cache is read.
    """

    bundled = bundled_directory or bundled_model_directory()
    for candidate in (bundled, local_prepared_directory):
        if inspect_model(candidate, spec=spec).ready:
            return candidate
    if bundled.exists():
        return bundled
    return local_prepared_directory


def inspect_model(
    prepared_directory: Path,
    *,
    spec: EmbeddingModelSpec = PINNED_MODEL,
) -> ModelVerification:
    """Inspect files without importing FastEmbed or initiating network access."""

    try:
        _verify_prepared_files(prepared_directory, spec)
    except FileNotFoundError:
        return ModelVerification(ModelAvailability.MISSING, "model_missing")
    except _ModelVersionError:
        return ModelVerification(ModelAvailability.VERSION_MISMATCH, "model_version_mismatch")
    except (OSError, ValueError, json.JSONDecodeError, _ModelCorruptionError):
        return ModelVerification(ModelAvailability.CORRUPT, "model_corrupt")
    return ModelVerification(ModelAvailability.READY, dimension=spec.dimension)


def verify_model(
    prepared_directory: Path,
    *,
    spec: EmbeddingModelSpec = PINNED_MODEL,
    backend_factory: BackendFactory | None = None,
    probe: bool = True,
) -> ModelVerification:
    """Verify provenance and, by default, one finite CPU-only 512-D inference."""

    structural = inspect_model(prepared_directory, spec=spec)
    if not structural.ready or not probe:
        return structural

    backend: EmbeddingBackend | None = None
    try:
        factory = backend_factory or _default_backend_factory
        backend = factory(prepared_directory)
        vector = backend.embed_documents(("离线模型自检",))[0]
        from amadeus_desktop.embedding_backend import validate_normalized_vector

        validate_normalized_vector(vector, dimension=spec.dimension)
        provider = backend.provider
        if provider != "CPUExecutionProvider":
            raise ValueError("embedding provider is not CPUExecutionProvider")
    except Exception:
        return ModelVerification(
            ModelAvailability.RUNTIME_UNAVAILABLE,
            "model_runtime_unavailable",
            dimension=spec.dimension,
        )
    finally:
        if backend is not None:
            with suppress(Exception):
                backend.close()
    return ModelVerification(
        ModelAvailability.READY,
        dimension=spec.dimension,
        provider="CPUExecutionProvider",
    )


def prepare_model(
    models_root: Path,
    *,
    spec: EmbeddingModelSpec = PINNED_MODEL,
    downloader: SnapshotDownloader | None = None,
    backend_factory: BackendFactory | None = None,
    force: bool = False,
) -> ModelVerification:
    """Explicitly download, validate, and atomically activate the pinned model.

    The staging directory is created beside the target so the final rename stays
    on one filesystem. An existing valid installation is reused unless ``force``
    is true. Any failed preparation leaves the prior installation untouched.
    """

    target = model_directory(models_root, spec)
    if not force:
        existing = verify_model(target, spec=spec, backend_factory=backend_factory)
        if existing.ready:
            return existing

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".prepare-", dir=target.parent))
    backup = target.parent / f".{target.name}.previous"
    try:
        (downloader or _download_snapshot)(stage, spec, force)
        cache_directory = stage / ".cache"
        if cache_directory.exists():
            shutil.rmtree(cache_directory)
        _write_manifest(stage, spec)
        structural = inspect_model(stage, spec=spec)
        if not structural.ready:
            raise ModelPreparationError(structural.error_category or "model_corrupt")
        probed = verify_model(stage, spec=spec, backend_factory=backend_factory)
        if not probed.ready:
            raise ModelPreparationError(probed.error_category or "model_runtime_unavailable")

        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.replace(backup)
        try:
            stage.replace(target)
        except Exception:
            if backup.exists() and not target.exists():
                backup.replace(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return probed
    except ModelPreparationError:
        raise
    except Exception as error:
        raise ModelPreparationError("model_prepare_failed") from error
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def repair_model(
    models_root: Path,
    *,
    spec: EmbeddingModelSpec = PINNED_MODEL,
    downloader: SnapshotDownloader | None = None,
    backend_factory: BackendFactory | None = None,
) -> ModelVerification:
    """Explicitly replace an installation through the safe staging path."""

    return prepare_model(
        models_root,
        spec=spec,
        downloader=downloader,
        backend_factory=backend_factory,
        force=True,
    )


def _write_manifest(directory: Path, spec: EmbeddingModelSpec) -> None:
    expected_hashes = _expected_file_hashes(spec)
    _verify_directory_shape(directory, spec, include_manifest=False)
    file_metadata: dict[str, dict[str, str | int]] = {}
    for relative in spec.required_files:
        file_path = directory / relative
        if not file_path.is_file():
            raise ModelPreparationError("model_required_file_missing")
        digest = _sha256_file(file_path)
        if digest != expected_hashes[relative]:
            raise ModelPreparationError("model_file_checksum_mismatch")
        file_metadata[relative] = {
            "size": file_path.stat().st_size,
            "sha256": digest,
        }

    manifest = ModelManifest(
        schema_version=1,
        api_name=spec.api_name,
        repository=spec.repository,
        revision=spec.revision,
        onnx_file=spec.onnx_file,
        onnx_sha256=spec.onnx_sha256,
        dimension=spec.dimension,
        prepared_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        files=file_metadata,
    )
    manifest_path = directory / MODEL_MANIFEST_FILE
    manifest_path.write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _verify_prepared_files(directory: Path, spec: EmbeddingModelSpec) -> None:
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    expected_hashes = _expected_file_hashes(spec)
    _verify_directory_shape(directory, spec, include_manifest=True)
    manifest_path = directory / MODEL_MANIFEST_FILE
    raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise _ModelCorruptionError()

    expected_identity = {
        "schema_version": 1,
        "api_name": spec.api_name,
        "repository": spec.repository,
        "revision": spec.revision,
        "onnx_file": spec.onnx_file,
        "onnx_sha256": spec.onnx_sha256,
        "dimension": spec.dimension,
    }
    if any(raw.get(key) != value for key, value in expected_identity.items()):
        raise _ModelVersionError()

    files = raw.get("files")
    if not isinstance(files, dict) or set(files) != set(spec.required_files):
        raise _ModelCorruptionError()
    root = directory.resolve()
    for relative in spec.required_files:
        metadata = files.get(relative)
        if not isinstance(metadata, dict):
            raise _ModelCorruptionError()
        file_path = directory / relative
        if not file_path.is_file() or file_path.is_symlink():
            raise _ModelCorruptionError()
        resolved = file_path.resolve()
        if root not in resolved.parents:
            raise _ModelCorruptionError()
        if metadata.get("size") != file_path.stat().st_size:
            raise _ModelCorruptionError()
        digest = _sha256_file(file_path)
        if metadata.get("sha256") != digest:
            raise _ModelCorruptionError()
        if digest != expected_hashes[relative]:
            raise _ModelCorruptionError()

    for relative in spec.required_files:
        if relative.endswith(".json"):
            parsed = json.loads((directory / relative).read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise _ModelCorruptionError()


def _download_snapshot(destination: Path, spec: EmbeddingModelSpec, force: bool) -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise ModelPreparationError("model_downloader_unavailable") from error

    try:
        snapshot_download(
            repo_id=spec.repository,
            revision=spec.revision,
            local_dir=destination,
            allow_patterns=list(spec.required_files),
            force_download=force,
            local_files_only=False,
        )
    except Exception as error:
        # Some managed Windows hosts expose their trusted proxy CA only through
        # Schannel. Curl still validates that chain; best-effort revocation keeps
        # hard failures for known revoked certificates while tolerating an
        # unreachable revocation service. Every downloaded file has a pinned
        # digest and this path is reachable only from explicit prepare/repair.
        if os.name != "nt" or not _download_with_windows_curl(destination, spec):
            raise ModelPreparationError("model_download_failed") from error


def _download_with_windows_curl(
    destination: Path,
    spec: EmbeddingModelSpec,
) -> bool:
    executable = shutil.which("curl.exe")
    if executable is None:
        return False
    base = f"https://huggingface.co/{spec.repository}/resolve/{spec.revision}"
    for relative in spec.required_files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"{base}/{quote(relative, safe='/')}"
        completed = subprocess.run(
            (
                executable,
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--ssl-revoke-best-effort",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--max-redirs",
                "10",
                "--connect-timeout",
                "30",
                "--tlsv1.2",
                "--output",
                str(target),
                url,
            ),
            check=False,
            capture_output=True,
            timeout=600,
        )
        if completed.returncode != 0:
            return False
    return True


def _default_backend_factory(directory: Path) -> EmbeddingBackend:
    from amadeus_desktop.embedding_backend import FastEmbedEmbeddingBackend

    return FastEmbedEmbeddingBackend(directory)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_file_hashes(spec: EmbeddingModelSpec) -> dict[str, str]:
    expected = dict(spec.required_file_sha256)
    if set(expected) != set(spec.required_files):
        raise _ModelVersionError()
    if expected.get(spec.onnx_file) != spec.onnx_sha256:
        raise _ModelVersionError()
    for relative, digest in expected.items():
        if Path(relative).name != relative or "/" in relative or "\\" in relative:
            raise _ModelVersionError()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise _ModelVersionError()
    return expected


def _verify_directory_shape(
    directory: Path,
    spec: EmbeddingModelSpec,
    *,
    include_manifest: bool,
) -> None:
    if directory.is_symlink():
        raise _ModelCorruptionError()
    expected_names = set(spec.required_files)
    if include_manifest:
        expected_names.add(MODEL_MANIFEST_FILE)
    entries = tuple(directory.iterdir())
    if {entry.name for entry in entries} != expected_names:
        raise _ModelCorruptionError()
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise _ModelCorruptionError()


def _safe_directory_name(model_name: str) -> str:
    return "-".join(part for part in model_name.replace("/", "-").split("-") if part)


class _ModelVersionError(ValueError):
    pass


class _ModelCorruptionError(ValueError):
    pass
