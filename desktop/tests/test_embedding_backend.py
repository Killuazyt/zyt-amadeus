from __future__ import annotations

import math
import platform
import sys

import pytest

from amadeus_desktop.embedding_backend import (
    CPU_PROVIDER,
    MODEL_DIMENSION,
    QUERY_INSTRUCTION,
    VECTOR_BLOB_BYTES,
    EmbeddingUnavailableError,
    FastEmbedEmbeddingBackend,
    InvalidEmbeddingError,
    _prime_windows_platform_cache_without_subprocess,
    normalize_vector,
    vector_from_blob,
    vector_sha256,
    vector_to_blob,
)


def _unit_vector(index: int = 0) -> tuple[float, ...]:
    values = [0.0] * MODEL_DIMENSION
    values[index] = 1.0
    return tuple(values)


class _Session:
    def get_providers(self) -> list[str]:
        return [CPU_PROVIDER]


class _Implementation:
    def __init__(self) -> None:
        self.model = _Session()


class _Engine:
    def __init__(self) -> None:
        self.model = _Implementation()
        self.calls: list[tuple[list[str], int | None]] = []
        self.invalid = False

    def embed(self, texts: list[str], *, parallel: int | None):
        self.calls.append((texts, parallel))
        if self.invalid:
            yield [math.nan] * MODEL_DIMENSION
            return
        for index, _text in enumerate(texts):
            yield _unit_vector(index % 2)


def test_fastembed_backend_is_local_cpu_only_and_prefixes_queries(tmp_path) -> None:
    captured: dict[str, object] = {}
    engine = _Engine()

    def factory(**kwargs: object) -> _Engine:
        captured.update(kwargs)
        return engine

    backend = FastEmbedEmbeddingBackend(
        tmp_path,
        embedding_factory=factory,
        verify_files=False,
    )

    query = backend.embed_query("咖啡偏好")
    documents = backend.embed_documents(("用户喜欢咖啡", "用户住在上海"))

    assert captured == {
        "model_name": "BAAI/bge-small-zh-v1.5",
        "specific_model_path": str(tmp_path),
        "local_files_only": True,
        "providers": [CPU_PROVIDER],
        "threads": 4,
    }
    assert engine.calls == [
        ([QUERY_INSTRUCTION + "咖啡偏好"], None),
        (["用户喜欢咖啡", "用户住在上海"], None),
    ]
    assert query == _unit_vector(0)
    assert documents == (_unit_vector(0), _unit_vector(1))
    assert backend.provider == CPU_PROVIDER


def test_backend_fails_closed_after_invalid_inference(tmp_path) -> None:
    engine = _Engine()
    backend = FastEmbedEmbeddingBackend(
        tmp_path,
        embedding_factory=lambda **_kwargs: engine,
        verify_files=False,
    )
    engine.invalid = True

    with pytest.raises(EmbeddingUnavailableError, match="model_inference_failed"):
        backend.embed_query("测试")
    with pytest.raises(EmbeddingUnavailableError, match="model_inference_failed"):
        backend.embed_query("不会再次调用模型")
    assert len(engine.calls) == 1


def test_float32_blob_round_trip_and_integrity() -> None:
    vector = normalize_vector([1.0] * MODEL_DIMENSION)
    blob = vector_to_blob(vector)
    decoded = vector_from_blob(blob)

    assert len(blob) == VECTOR_BLOB_BYTES == 2048
    assert len(vector_sha256(blob)) == 64
    assert decoded == vector


@pytest.mark.parametrize(
    "values",
    (
        [1.0] * (MODEL_DIMENSION - 1),
        [0.0] * MODEL_DIMENSION,
        [math.nan] + [0.0] * (MODEL_DIMENSION - 1),
        [math.inf] + [0.0] * (MODEL_DIMENSION - 1),
    ),
)
def test_invalid_vectors_are_rejected(values: list[float]) -> None:
    with pytest.raises(InvalidEmbeddingError):
        normalize_vector(values)


def test_wrong_sized_or_non_normalized_blobs_are_rejected() -> None:
    with pytest.raises(InvalidEmbeddingError):
        vector_from_blob(b"\0" * (VECTOR_BLOB_BYTES - 4))
    with pytest.raises(InvalidEmbeddingError):
        vector_from_blob(b"\0" * VECTOR_BLOB_BYTES)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only subprocess guard")
def test_windows_platform_cache_is_primed_without_command_probe(monkeypatch) -> None:
    calls = []
    expected_version = ".".join(str(component) for component in sys.getwindowsversion()[:3])

    def forbidden_probe(*_arguments: object, **_keywords: object):
        calls.append(True)
        raise AssertionError("command version probe must not run")

    monkeypatch.setattr(platform, "_uname_cache", None)
    monkeypatch.setattr(platform, "_syscmd_ver", forbidden_probe)

    _prime_windows_platform_cache_without_subprocess()

    assert platform.system() == "Windows"
    assert platform.version() == expected_version
    assert calls == []
    assert platform._syscmd_ver is forbidden_probe
