from __future__ import annotations

import pytest

from amadeus_desktop.embedding_backend import CPU_PROVIDER
from amadeus_desktop.embedding_calibration import CALIBRATION_CASES, calibrate_backend
from amadeus_desktop.hybrid_retrieval import CalibrationError

DIMENSION = 512


def _basis(index: int) -> tuple[float, ...]:
    values = [0.0] * DIMENSION
    values[index] = 1.0
    return tuple(values)


class _CalibrationBackend:
    model_name = "synthetic"
    dimension = DIMENSION
    provider = CPU_PROVIDER

    def __init__(self, *, separated: bool = True) -> None:
        self._separated = separated
        self._query_vectors = {
            case.query: _basis(index) for index, case in enumerate(CALIBRATION_CASES)
        }
        self._document_vectors = {}
        for index, case in enumerate(CALIBRATION_CASES):
            self._document_vectors[case.positive_document] = _basis(index)
            self._document_vectors[case.negative_document] = (
                _basis(index + 24) if separated else _basis(index)
            )

    def embed_query(self, query: str) -> tuple[float, ...]:
        return self._query_vectors[query]

    def embed_documents(self, documents):
        return tuple(self._document_vectors[document] for document in documents)

    def close(self) -> None:
        pass


def test_public_calibration_corpus_is_fixed_complete_and_synthetic() -> None:
    assert len(CALIBRATION_CASES) == 24
    assert len({case.query for case in CALIBRATION_CASES}) == 24
    assert len({case.positive_document for case in CALIBRATION_CASES}) == 24
    assert len({case.negative_document for case in CALIBRATION_CASES}) == 24
    assert all(
        case.query and case.positive_document and case.negative_document
        for case in CALIBRATION_CASES
    )


def test_backend_pair_scoring_calls_shared_threshold_calibration() -> None:
    report = calibrate_backend(_CalibrationBackend())

    assert report.positive_scores == (1.0,) * 24
    assert report.negative_scores == (0.0,) * 24
    assert report.calibration.positive_count == 24
    assert report.calibration.negative_count == 24
    assert report.calibration.gap == 1.0
    assert report.calibration.threshold == 0.5


def test_backend_calibration_fails_when_gap_is_below_point_zero_five() -> None:
    with pytest.raises(CalibrationError, match="calibration_gap"):
        calibrate_backend(_CalibrationBackend(separated=False))
