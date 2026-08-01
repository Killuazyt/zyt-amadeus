from __future__ import annotations

import pytest

from tests.helpers.tray_acceptance_runner import (
    _REQUIRED_BOOLEAN_CHECKS,
    _acceptance_passed,
)


def passing_result() -> dict[str, object]:
    result: dict[str, object] = {name: True for name in _REQUIRED_BOOLEAN_CHECKS}
    result.update(
        acceptance_timed_out=False,
        chat_open_latency_ms=199.9,
        four_corner_placements=[{"panel_fully_visible": True} for _ in range(4)],
    )
    return result


def test_acceptance_result_requires_all_checks() -> None:
    result = passing_result()

    assert _acceptance_passed(result)

    result["retry_completed"] = False
    assert not _acceptance_passed(result)


@pytest.mark.parametrize("latency", [True, None, 200.1])
def test_acceptance_result_rejects_invalid_or_slow_panel_open(latency: object) -> None:
    result = passing_result()
    result["chat_open_latency_ms"] = latency

    assert not _acceptance_passed(result)


def test_acceptance_result_rejects_timeout_or_bad_corner() -> None:
    result = passing_result()
    result["acceptance_timed_out"] = True
    assert not _acceptance_passed(result)

    result = passing_result()
    result["four_corner_placements"] = [
        {"panel_fully_visible": True},
        {"panel_fully_visible": True},
        {"panel_fully_visible": False},
        {"panel_fully_visible": True},
    ]
    assert not _acceptance_passed(result)
