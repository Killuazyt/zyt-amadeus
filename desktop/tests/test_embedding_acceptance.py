from __future__ import annotations

import json
import os
import socket

import pytest

from amadeus_desktop.chat_models import PreparedPrompt
from amadeus_desktop.embedding_backend import CPU_PROVIDER
from amadeus_desktop.embedding_calibration import CALIBRATION_CASES
from amadeus_desktop.embedding_model import ModelAvailability, ModelVerification
from amadeus_desktop.tools import embedding_acceptance
from amadeus_desktop.tools.embedding_acceptance import (
    BENCHMARK_QUERIES,
    EXPECTED_MEMORY_MATRIX_BYTES,
    AcceptanceFailure,
    OfflineNetworkAttempt,
    run_benchmark,
    run_offline_smoke,
    run_production_benchmark,
)

DIMENSION = 512


def _basis(index: int) -> tuple[float, ...]:
    values = [0.0] * DIMENSION
    values[index] = 1.0
    return tuple(values)


class _FakeBackend:
    model_name = "BAAI/bge-small-zh-v1.5"
    dimension = DIMENSION
    provider = CPU_PROVIDER

    def __init__(self, _path) -> None:
        self.closed = False
        self._query_vectors = {
            case.query: _basis(index) for index, case in enumerate(CALIBRATION_CASES)
        }
        self._document_vectors = {}
        for index, case in enumerate(CALIBRATION_CASES):
            self._document_vectors[case.positive_document] = _basis(index)
            self._document_vectors[case.negative_document] = _basis(index + 24)

    def embed_query(self, query: str) -> tuple[float, ...]:
        return self._query_vectors.get(query, _basis(sum(map(ord, query)) % DIMENSION))

    def embed_documents(self, documents):
        return tuple(self._document_vectors[document] for document in documents)

    def close(self) -> None:
        self.closed = True


def _ready_verifier(*_args, **_kwargs) -> ModelVerification:
    return ModelVerification(
        ModelAvailability.READY,
        dimension=DIMENSION,
        provider=CPU_PROVIDER,
    )


def test_offline_smoke_reports_only_aggregate_calibration_and_recall(tmp_path) -> None:
    result = run_offline_smoke(
        tmp_path / "private-model-location",
        backend_factory=_FakeBackend,
        verifier=_ready_verifier,
    )
    encoded = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "passed"
    assert result["positive_samples"] == 24
    assert result["negative_samples"] == 24
    assert result["calibration_gap"] == 1.0
    assert result["rewrite_recall_successes"] == 24
    assert "private-model-location" not in encoded
    assert all(case.query not in encoded for case in CALIBRATION_CASES)


def test_offline_guard_rejects_socket_connections() -> None:
    with pytest.raises(OfflineNetworkAttempt), embedding_acceptance._deny_network():
        socket.create_connection(("127.0.0.1", 9))


def test_offline_guard_rejects_dns_and_udp() -> None:
    with embedding_acceptance._deny_network():
        with pytest.raises(OfflineNetworkAttempt):
            socket.getaddrinfo("example.invalid", 443)
        datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(OfflineNetworkAttempt):
                datagram.sendto(b"probe", ("127.0.0.1", 9))
        finally:
            datagram.close()


def test_default_benchmark_builds_exact_10k_matrix_and_runs_100_queries(tmp_path) -> None:
    result = run_benchmark(
        tmp_path,
        backend_factory=_FakeBackend,
    )

    assert result["status"] == "passed"
    assert result["memory_items"] == 10_000
    assert result["memory_matrix_bytes"] == EXPECTED_MEMORY_MATRIX_BYTES == 20_480_000
    assert result["warmup_count"] == 5
    assert result["query_count"] == 100
    assert len(BENCHMARK_QUERIES) == len(set(BENCHMARK_QUERIES)) == 100
    assert result["p95_ms"] <= 300.0
    assert set(result["machine"]) == {
        "os",
        "cpu",
        "physical_memory_bytes",
        "process_rss_bytes",
    }
    if os.name == "nt":
        assert result["machine"]["physical_memory_bytes"] > 0
        assert result["machine"]["process_rss_bytes"] > 0


def test_benchmark_returns_failed_status_when_p95_exceeds_gate(tmp_path) -> None:
    result = run_benchmark(
        tmp_path,
        backend_factory=_FakeBackend,
        memory_items=8,
        persona_items=4,
        warmup_queries=(),
        measured_queries=("查询一", "查询二"),
        p95_limit_ms=0.0,
    )

    assert result["status"] == "failed"
    assert result["error_category"] == "benchmark_p95_exceeded"


def test_production_benchmark_runs_sqlite_vector_fusion_and_prompt_chain(tmp_path, qapp) -> None:
    result = run_production_benchmark(
        tmp_path,
        backend_factory=_FakeBackend,
        memory_items=8,
        persona_items=4,
        warmup_queries=("离线检索预热请求",),
        measured_queries=("请检索公开合成偏好记录编号00000", "编号00001对应什么"),
    )

    assert result["status"] == "passed"
    assert result["memory_items"] == 8
    assert result["persona_items"] == 4
    assert result["memory_matrix_bytes"] == 8 * 512 * 4
    assert result["total_cache_bytes"] == 12 * 512 * 4
    assert result["warmup_count"] == 1
    assert result["query_count"] == 2
    assert result["correct_recall_count"] == 2
    assert result["cross_library_mis_hit_count"] == 0
    assert result["p95_ms"] <= 300.0


def test_user_query_isolation_counts_every_injected_persona_row() -> None:
    prompt = PreparedPrompt(
        messages=(),
        user_memory_version_ids=("benchmark-version-00000",),
        persona_knowledge_ids=("benchmark-persona-00000",),
    )

    assert embedding_acceptance._user_query_cross_library_mis_hits(prompt) == 1


def test_cli_failure_json_never_contains_model_path_or_exception(
    tmp_path, monkeypatch, capsys
) -> None:
    def fail(_path):
        raise AcceptanceFailure("synthetic_safe_category")

    monkeypatch.setattr(embedding_acceptance, "run_offline_smoke", fail)
    private_path = tmp_path / "secret-model-location"

    exit_code = embedding_acceptance.main(("offline-smoke", "--model-directory", str(private_path)))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error_category"] == "synthetic_safe_category"
    assert str(private_path) not in json.dumps(payload)
