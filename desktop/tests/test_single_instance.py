from __future__ import annotations

from uuid import uuid4

from amadeus_desktop.single_instance import SingleInstance


def test_second_instance_requests_activation(qtbot) -> None:
    server_name = f"amadeus-test-{uuid4().hex}"
    primary = SingleInstance(server_name)
    secondary = SingleInstance(server_name)
    activations: list[bool] = []
    primary.activation_requested.connect(lambda: activations.append(True))

    try:
        assert primary.acquire() is True
        assert secondary.acquire() is False
        qtbot.waitUntil(lambda: activations == [True], timeout=3000)
        assert primary.is_primary is True
        assert secondary.is_primary is False
    finally:
        secondary.close()
        primary.close()


def test_server_can_be_reacquired_after_clean_close() -> None:
    server_name = f"amadeus-test-{uuid4().hex}"
    first = SingleInstance(server_name)
    second = SingleInstance(server_name)

    assert first.acquire() is True
    first.close()
    assert second.acquire() is True
    second.close()
