from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from amadeus_desktop.autostart import (
    RUN_KEY_PATH,
    RUN_VALUE_NAME,
    AutostartError,
    AutostartManager,
    build_autostart_command,
)


@dataclass
class FakeRegistryBackend:
    value: str | None = None
    writes: list[tuple[str, str, str]] = field(default_factory=list)
    deletes: list[tuple[str, str]] = field(default_factory=list)
    corrupt_next_write: bool = False
    ignore_next_delete: bool = False
    fail_next_write: bool = False
    fail_next_read: bool = False

    def read_value(self, key_path: str, value_name: str) -> str | None:
        assert key_path == RUN_KEY_PATH
        assert value_name == RUN_VALUE_NAME
        if self.fail_next_read:
            self.fail_next_read = False
            raise OSError("synthetic registry read failure")
        return self.value

    def write_value(self, key_path: str, value_name: str, value: str) -> None:
        self.writes.append((key_path, value_name, value))
        if self.fail_next_write:
            self.fail_next_write = False
            raise OSError("synthetic registry write failure")
        self.value = "corrupted-command" if self.corrupt_next_write else value
        self.corrupt_next_write = False

    def delete_value(self, key_path: str, value_name: str) -> None:
        self.deletes.append((key_path, value_name))
        if self.ignore_next_delete:
            self.ignore_next_delete = False
            return
        self.value = None


def test_frozen_command_is_an_explicit_quoted_executable(tmp_path: Path) -> None:
    executable = tmp_path / "Amadeus App" / "Amadeus.exe"

    command = build_autostart_command(executable=executable, frozen=True)

    assert command == f'"{executable.resolve()}"'


def test_development_command_uses_pythonw_module_entrypoint(tmp_path: Path) -> None:
    executable = tmp_path / "venv" / "Scripts" / "python.exe"

    command = build_autostart_command(executable=executable, frozen=False)

    assert command == f'"{executable.resolve().with_name("pythonw.exe")}" -m amadeus_desktop'


def test_enable_writes_fixed_hkcu_run_value_and_verifies_readback() -> None:
    backend = FakeRegistryBackend()
    manager = AutostartManager(backend, command='"C:\\Amadeus.exe"')

    manager.enable()

    assert backend.value == manager.command
    assert backend.writes == [(RUN_KEY_PATH, RUN_VALUE_NAME, manager.command)]
    assert manager.is_enabled()


def test_exact_command_is_required_for_enabled_state() -> None:
    backend = FakeRegistryBackend(value='"C:\\Old-Amadeus.exe"')
    manager = AutostartManager(backend, command='"C:\\Amadeus.exe"')

    assert not manager.is_enabled()


def test_disable_deletes_fixed_value_and_verifies_readback() -> None:
    backend = FakeRegistryBackend(value='"C:\\Amadeus.exe"')
    manager = AutostartManager(backend, command='"C:\\Amadeus.exe"')

    manager.disable()

    assert backend.value is None
    assert backend.deletes == [(RUN_KEY_PATH, RUN_VALUE_NAME)]
    assert not manager.is_enabled()


def test_enable_readback_failure_restores_absent_previous_state() -> None:
    backend = FakeRegistryBackend(corrupt_next_write=True)
    manager = AutostartManager(backend, command='"C:\\Amadeus.exe"')

    with pytest.raises(AutostartError, match="verified"):
        manager.enable()

    assert backend.value is None


def test_enable_write_failure_restores_previous_stale_value() -> None:
    previous = '"C:\\Old-Amadeus.exe"'
    backend = FakeRegistryBackend(value=previous, fail_next_write=True)
    manager = AutostartManager(backend, command='"C:\\Amadeus.exe"')

    with pytest.raises(AutostartError, match="could not be changed"):
        manager.enable()

    assert backend.value == previous


def test_disable_readback_failure_restores_previous_value() -> None:
    previous = '"C:\\Amadeus.exe"'
    backend = FakeRegistryBackend(value=previous, ignore_next_delete=True)
    manager = AutostartManager(backend, command=previous)

    with pytest.raises(AutostartError, match="verified"):
        manager.disable()

    assert backend.value == previous


def test_registry_read_failure_is_redacted_and_does_not_mutate() -> None:
    backend = FakeRegistryBackend(fail_next_read=True)
    manager = AutostartManager(backend, command='"C:\\Amadeus.exe"')

    with pytest.raises(AutostartError, match="could not be read") as caught:
        manager.enable()

    assert "synthetic" not in str(caught.value)
    assert backend.writes == []
    assert backend.deletes == []


@pytest.mark.parametrize("command", ["", "   ", "bad\ncommand", "bad\x00command"])
def test_invalid_command_is_rejected(command: str) -> None:
    with pytest.raises(AutostartError):
        AutostartManager(FakeRegistryBackend(), command=command)


def test_set_enabled_requires_a_real_boolean() -> None:
    manager = AutostartManager(FakeRegistryBackend(), command='"C:\\Amadeus.exe"')

    with pytest.raises(AutostartError):
        manager.set_enabled(1)  # type: ignore[arg-type]
