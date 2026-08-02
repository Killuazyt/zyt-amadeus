from __future__ import annotations

import asyncio
import os
import socket

import pytest


def test_network_guard_keeps_hugging_face_offline() -> None:
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_network_guard_allows_asyncio_and_loopback_ipc() -> None:
    asyncio.run(asyncio.sleep(0))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1) as client:
            peer, _address = listener.accept()
            with peer:
                client.sendall(b"local")
                assert peer.recv(5) == b"local"


def test_network_guard_rejects_external_resolution_and_connections() -> None:
    with pytest.raises(AssertionError, match="external network hosts"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(AssertionError, match="external network sockets"):
        socket.create_connection(("198.51.100.1", 443), timeout=0.01)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        with pytest.raises(AssertionError, match="external network sockets"):
            client.connect(("198.51.100.1", 443))
        with pytest.raises(AssertionError, match="external network sockets"):
            client.connect_ex(("198.51.100.1", 443))


def test_network_guard_rejects_external_datagrams() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client,
        pytest.raises(AssertionError, match="external network sockets"),
    ):
        client.sendto(b"blocked", ("198.51.100.1", 53))
