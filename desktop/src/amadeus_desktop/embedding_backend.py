"""Injectable, fail-closed embedding boundary for P5B."""

from __future__ import annotations

import hashlib
import math
import platform
import sys
import threading
from array import array
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from amadeus_desktop.embedding_model import (
    MODEL_DIMENSION,
    PINNED_MODEL,
    EmbeddingModelSpec,
    inspect_model,
)

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
CPU_PROVIDER = "CPUExecutionProvider"
ONNX_THREADS = 4
FLOAT32_BYTES = 4
VECTOR_BLOB_BYTES = MODEL_DIMENSION * FLOAT32_BYTES
_NORM_TOLERANCE = 1e-4
_PLATFORM_PROBE_LOCK = threading.Lock()

EmbeddingVector = tuple[float, ...]


class EmbeddingError(RuntimeError):
    """Base class for safe embedding failures."""


class EmbeddingUnavailableError(EmbeddingError):
    """Raised after the local backend becomes unavailable for this session."""


class InvalidEmbeddingError(EmbeddingError, ValueError):
    """Raised for wrong-dimensional, non-finite, or non-normalized vectors."""


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Qt-independent boundary implemented by the pinned local model or a fake."""

    @property
    def model_name(self) -> str:
        """Return the stable FastEmbed API name."""

    @property
    def dimension(self) -> int:
        """Return the vector dimension."""

    @property
    def provider(self) -> str:
        """Return the active ONNX execution provider."""

    def embed_query(self, query: str) -> EmbeddingVector:
        """Embed one retrieval query with the required Chinese instruction."""

    def embed_documents(self, documents: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        """Embed documents without adding a query instruction."""

    def close(self) -> None:
        """Release the inference session without spawning a helper process."""


EmbeddingFactory = Callable[..., object]


class FastEmbedEmbeddingBackend:
    """CPU-only wrapper around FastEmbed that never downloads at runtime.

    ``fastembed`` is imported only while constructing this class, so installations
    that have no prepared model can still start and use the FTS-only path.
    """

    def __init__(
        self,
        prepared_directory: Path,
        *,
        spec: EmbeddingModelSpec = PINNED_MODEL,
        embedding_factory: EmbeddingFactory | None = None,
        verify_files: bool = True,
    ) -> None:
        if verify_files:
            status = inspect_model(prepared_directory, spec=spec)
            if not status.ready:
                raise EmbeddingUnavailableError(status.error_category or "model_unavailable")
        self._prepared_directory = prepared_directory
        self._spec = spec
        self._engine: object | None = None
        self._provider: str | None = None
        self._failure_category: str | None = None
        try:
            factory = embedding_factory or _load_fastembed_factory()
            self._engine = factory(
                model_name=spec.api_name,
                specific_model_path=str(prepared_directory),
                local_files_only=True,
                providers=[CPU_PROVIDER],
                threads=ONNX_THREADS,
            )
            providers = _engine_providers(self._engine)
            if providers != (CPU_PROVIDER,):
                raise EmbeddingUnavailableError("model_provider_mismatch")
            self._provider = CPU_PROVIDER
        except EmbeddingUnavailableError:
            self._engine = None
            self._failure_category = "model_runtime_unavailable"
            raise
        except Exception as error:
            self._engine = None
            self._failure_category = "model_runtime_unavailable"
            raise EmbeddingUnavailableError("model_runtime_unavailable") from error

    @property
    def model_name(self) -> str:
        return self._spec.api_name

    @property
    def dimension(self) -> int:
        return self._spec.dimension

    @property
    def provider(self) -> str:
        if self._provider is None:
            raise EmbeddingUnavailableError(self._failure_category or "model_unavailable")
        return self._provider

    @property
    def available(self) -> bool:
        return self._engine is not None and self._failure_category is None

    @property
    def failure_category(self) -> str | None:
        return self._failure_category

    def embed_query(self, query: str) -> EmbeddingVector:
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("query must not be empty")
        return self._embed((QUERY_INSTRUCTION + cleaned,))[0]

    def embed_documents(self, documents: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        if not documents:
            return ()
        cleaned = tuple(document.strip() for document in documents)
        if any(not document for document in cleaned):
            raise ValueError("documents must not contain empty text")
        return self._embed(cleaned)

    def close(self) -> None:
        self._engine = None
        self._provider = None

    def _embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        if self._engine is None or self._failure_category is not None:
            raise EmbeddingUnavailableError(self._failure_category or "model_unavailable")
        try:
            embed = cast(Any, self._engine).embed
            raw_vectors = tuple(embed(list(texts), parallel=None))
            if len(raw_vectors) != len(texts):
                raise InvalidEmbeddingError("embedding count mismatch")
            return tuple(
                normalize_vector(vector, dimension=self._spec.dimension) for vector in raw_vectors
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            self._failure_category = "model_inference_failed"
            self._engine = None
            self._provider = None
            raise EmbeddingUnavailableError("model_inference_failed") from error


def normalize_vector(
    vector: Iterable[float],
    *,
    dimension: int = MODEL_DIMENSION,
) -> EmbeddingVector:
    """Return a finite, L2-normalized float32 vector of the exact dimension."""

    values = tuple(float(value) for value in vector)
    if len(values) != dimension:
        raise InvalidEmbeddingError(f"expected {dimension} dimensions")
    if not all(math.isfinite(value) for value in values):
        raise InvalidEmbeddingError("embedding contains non-finite values")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise InvalidEmbeddingError("embedding has zero or invalid norm")

    # Round once to the format stored in SQLite, then compensate for float32
    # rounding so a decoded vector passes the same normalization invariant.
    normalized = array("f", (value / norm for value in values))
    rounded_norm = math.sqrt(math.fsum(float(value) ** 2 for value in normalized))
    if not math.isfinite(rounded_norm) or rounded_norm <= 0:
        raise InvalidEmbeddingError("embedding normalization failed")
    normalized = array("f", (float(value) / rounded_norm for value in normalized))
    result = tuple(float(value) for value in normalized)
    validate_normalized_vector(result, dimension=dimension)
    return result


def validate_normalized_vector(
    vector: Sequence[float],
    *,
    dimension: int = MODEL_DIMENSION,
) -> None:
    """Reject invalid persisted or backend vectors without attempting repair."""

    if len(vector) != dimension:
        raise InvalidEmbeddingError(f"expected {dimension} dimensions")
    if not all(math.isfinite(float(value)) for value in vector):
        raise InvalidEmbeddingError("embedding contains non-finite values")
    norm = math.sqrt(math.fsum(float(value) ** 2 for value in vector))
    if not math.isclose(norm, 1.0, rel_tol=_NORM_TOLERANCE, abs_tol=_NORM_TOLERANCE):
        raise InvalidEmbeddingError("embedding is not L2-normalized")


def vector_to_blob(vector: Sequence[float], *, dimension: int = MODEL_DIMENSION) -> bytes:
    """Encode one validated vector as deterministic little-endian float32 bytes."""

    validate_normalized_vector(vector, dimension=dimension)
    values = array("f", (float(value) for value in vector))
    if sys.byteorder != "little":
        values.byteswap()
    blob = values.tobytes()
    expected_bytes = dimension * FLOAT32_BYTES
    if len(blob) != expected_bytes:
        raise InvalidEmbeddingError(f"expected {expected_bytes} vector bytes")
    return blob


def vector_from_blob(blob: bytes, *, dimension: int = MODEL_DIMENSION) -> EmbeddingVector:
    """Decode and validate one little-endian float32 SQLite BLOB."""

    expected_bytes = dimension * FLOAT32_BYTES
    if len(blob) != expected_bytes:
        raise InvalidEmbeddingError(f"expected {expected_bytes} vector bytes")
    values = array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    result = tuple(float(value) for value in values)
    validate_normalized_vector(result, dimension=dimension)
    return result


def vector_sha256(blob: bytes) -> str:
    """Return the integrity identity persisted beside a vector BLOB."""

    return hashlib.sha256(blob).hexdigest()


def _load_fastembed_factory() -> EmbeddingFactory:
    _prime_windows_platform_cache_without_subprocess()
    try:
        from fastembed import TextEmbedding
    except ImportError as error:
        raise EmbeddingUnavailableError("fastembed_not_installed") from error
    return TextEmbedding


def _prime_windows_platform_cache_without_subprocess() -> None:
    """Prevent Python 3.11's Windows version fallback from launching ``cmd.exe``.

    ``onnxruntime`` calls :func:`platform.system` during import. Python 3.11's
    Windows implementation otherwise executes ``cmd.exe /c ver`` even though
    ``sys.getwindowsversion().platform_version`` already exposes the native
    version. Prime the same process-wide cache under a narrowly scoped probe so
    the embedding runtime remains genuinely single-process.
    """

    if sys.platform != "win32" or not hasattr(sys, "getwindowsversion"):
        return
    with _PLATFORM_PROBE_LOCK:
        if getattr(platform, "_uname_cache", None) is not None:
            return
        system_probe = getattr(platform, "_syscmd_ver", None)
        if not callable(system_probe):
            return
        winver = sys.getwindowsversion()
        native_version = winver[:3]
        version_text = ".".join(str(component) for component in native_version)

        def native_probe(*_arguments: object, **_keywords: object) -> tuple[str, str, str]:
            return "Microsoft Windows", "", version_text

        platform._syscmd_ver = native_probe
        try:
            platform.uname()
        finally:
            platform._syscmd_ver = system_probe


def _engine_providers(engine: object) -> tuple[str, ...]:
    implementation = getattr(engine, "model", None)
    session = getattr(implementation, "model", None)
    get_providers = getattr(session, "get_providers", None)
    if not callable(get_providers):
        raise EmbeddingUnavailableError("model_provider_unknown")
    providers = tuple(str(provider) for provider in get_providers())
    if not providers:
        raise EmbeddingUnavailableError("model_provider_unknown")
    return providers
