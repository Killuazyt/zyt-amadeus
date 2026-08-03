from __future__ import annotations

import os
import sys
from contextlib import suppress
from uuid import uuid4

import pytest

from amadeus_desktop.autostart import (
    AutostartManager,
    WindowsRegistryBackend,
)


@pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("AMADEUS_RUN_REAL_HKCU_ACCEPTANCE") != "1",
    reason="real HKCU acceptance is an explicit Windows-only smoke",
)
def test_real_hkcu_roundtrip_uses_unique_temporary_value_and_cleans_up() -> None:
    """Exercise the production registry backend without touching the Amadeus Run value."""

    import winreg

    temporary_key = rf"Software\Amadeus-P6-Acceptance-{uuid4().hex}"
    temporary_value = f"Acceptance-{uuid4().hex}"
    backend = WindowsRegistryBackend()

    class IsolatedRegistryBackend:
        def read_value(self, _key_path: str, _value_name: str) -> str | None:
            return backend.read_value(temporary_key, temporary_value)

        def write_value(self, _key_path: str, _value_name: str, value: str) -> None:
            backend.write_value(temporary_key, temporary_value, value)

        def delete_value(self, _key_path: str, _value_name: str) -> None:
            backend.delete_value(temporary_key, temporary_value)

    manager = AutostartManager(
        IsolatedRegistryBackend(),
        command='"C:\\Program Files\\Amadeus-Test\\Amadeus.exe"',
    )
    try:
        assert not manager.is_enabled()
        assert manager.set_enabled(True)
        assert manager.is_enabled()
        assert manager.set_enabled(False) is False
        assert not manager.is_enabled()
    finally:
        backend.delete_value(temporary_key, temporary_value)
        with suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, temporary_key)

    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, temporary_key, 0, winreg.KEY_QUERY_VALUE)
