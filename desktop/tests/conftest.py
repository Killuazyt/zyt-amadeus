from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


@pytest.fixture(autouse=True)
def _default_tests_are_network_denied(monkeypatch):
    """Deny real external networking without breaking local event-loop IPC.

    Windows' asyncio Proactor loop implements its wake-up socket pair through
    TCP loopback.  Blanket-patching ``socket.connect`` therefore prevents even
    fully mocked HTTPX provider tests from creating an event loop.  Keep the
    default suite offline by rejecting non-loopback Internet destinations while
    allowing loopback and local-domain sockets.
    """

    original_create_connection = socket.create_connection
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_sendto = socket.socket.sendto

    def ensure_local(family: int, address: object) -> None:
        if family == getattr(socket, "AF_UNIX", None):
            return
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("default tests must not open external network sockets")
        if not isinstance(address, tuple) or not address:
            raise AssertionError("default tests must not open external network sockets")
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="strict")
        if not isinstance(host, str) or not _is_loopback_host(host):
            raise AssertionError("default tests must not open external network sockets")

    def guarded_create_connection(address: object, *args: Any, **kwargs: Any):
        family = socket.AF_INET6 if _address_uses_ipv6(address) else socket.AF_INET
        ensure_local(family, address)
        return original_create_connection(address, *args, **kwargs)

    def guarded_connect(current: socket.socket, address: object):
        ensure_local(current.family, address)
        return original_connect(current, address)

    def guarded_connect_ex(current: socket.socket, address: object):
        ensure_local(current.family, address)
        return original_connect_ex(current, address)

    def guarded_getaddrinfo(host: object, *args: Any, **kwargs: Any):
        if host is not None:
            normalized = _host_text(host)
            if normalized is None or not _is_loopback_host(normalized):
                raise AssertionError("default tests must not resolve external network hosts")
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_sendto(current: socket.socket, data: bytes, *args: Any):
        if not args:
            raise TypeError("sendto expected a destination address")
        ensure_local(current.family, args[-1])
        return original_sendto(current, data, *args)

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)


def _address_uses_ipv6(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    return ":" in host


def _host_text(host: object) -> str | None:
    if isinstance(host, str):
        return host
    if isinstance(host, bytes):
        try:
            return host.decode("ascii")
        except UnicodeDecodeError:
            return None
    return None


def _is_loopback_host(host: str) -> bool:
    normalized = host.rstrip(".").casefold()
    if normalized == "localhost":
        return True
    address_text = normalized.split("%", maxsplit=1)[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)
