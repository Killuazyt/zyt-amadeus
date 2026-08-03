"""Transactional Windows per-user launch-at-login registration."""

from __future__ import annotations

import sys
from contextlib import suppress
from pathlib import Path
from typing import Protocol

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "Amadeus"


class AutostartError(RuntimeError):
    """Raised when launch-at-login state cannot be verified or rolled back."""


class RegistryBackend(Protocol):
    """Small injectable registry boundary used by :class:`AutostartManager`."""

    def read_value(self, key_path: str, value_name: str) -> str | None:
        """Return a string value, or ``None`` when the value does not exist."""

    def write_value(self, key_path: str, value_name: str, value: str) -> None:
        """Create or replace a ``REG_SZ`` value."""

    def delete_value(self, key_path: str, value_name: str) -> None:
        """Delete a value, succeeding when it is already absent."""


class WindowsRegistryBackend:
    """HKCU registry backend; importing this module has no registry side effects."""

    @staticmethod
    def _winreg():
        try:
            import winreg
        except ImportError as exc:  # pragma: no cover - Windows-only production guard
            raise OSError("Windows registry is unavailable.") from exc
        return winreg

    def read_value(self, key_path: str, value_name: str) -> str | None:
        winreg = self._winreg()
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                key_path,
                0,
                winreg.KEY_QUERY_VALUE,
            ) as key:
                value, _value_type = winreg.QueryValueEx(key, value_name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else None

    def write_value(self, key_path: str, value_name: str, value: str) -> None:
        winreg = self._winreg()
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            key_path,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, value)

    def delete_value(self, key_path: str, value_name: str) -> None:
        winreg = self._winreg()
        try:
            with (
                winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    key_path,
                    0,
                    winreg.KEY_SET_VALUE,
                ) as key,
                suppress(FileNotFoundError),
            ):
                winreg.DeleteValue(key, value_name)
        except FileNotFoundError:
            pass


def build_autostart_command(
    *,
    executable: str | Path | None = None,
    frozen: bool | None = None,
) -> str:
    """Build the explicit executable command stored in the HKCU Run value."""

    executable_path = Path(sys.executable if executable is None else executable).resolve()
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return _quote_executable(executable_path)

    pythonw_name = "pythonw.exe" if executable_path.suffix.lower() == ".exe" else "pythonw"
    pythonw_path = executable_path.with_name(pythonw_name)
    return f"{_quote_executable(pythonw_path)} -m amadeus_desktop"


def _quote_executable(executable: Path) -> str:
    value = str(executable)
    if not value or '"' in value or "\x00" in value or "\r" in value or "\n" in value:
        raise AutostartError("The launch-at-login executable path is invalid.")
    return f'"{value}"'


class AutostartManager:
    """Manage the fixed Amadeus HKCU Run value with verified rollback."""

    def __init__(
        self,
        registry_backend: RegistryBackend | None = None,
        *,
        command: str | None = None,
    ) -> None:
        self._registry = WindowsRegistryBackend() if registry_backend is None else registry_backend
        self.command = build_autostart_command() if command is None else command
        if (
            not isinstance(self.command, str)
            or not self.command.strip()
            or "\x00" in self.command
            or "\r" in self.command
            or "\n" in self.command
        ):
            raise AutostartError("The launch-at-login command is invalid.")

    def registered_command(self) -> str | None:
        """Read the current Amadeus registration without changing it."""

        try:
            return self._registry.read_value(RUN_KEY_PATH, RUN_VALUE_NAME)
        except Exception as exc:
            raise AutostartError("Launch-at-login state could not be read.") from exc

    def is_enabled(self) -> bool:
        """Return true only when the registry contains this exact command."""

        return self.registered_command() == self.command

    def set_enabled(self, enabled: bool) -> bool:
        """Set and verify state, restoring the exact previous value on failure."""

        if not isinstance(enabled, bool):
            raise AutostartError("Launch-at-login state must be a boolean.")
        previous = self.registered_command()
        if (enabled and previous == self.command) or (not enabled and previous is None):
            return enabled

        try:
            if enabled:
                self._registry.write_value(RUN_KEY_PATH, RUN_VALUE_NAME, self.command)
            else:
                self._registry.delete_value(RUN_KEY_PATH, RUN_VALUE_NAME)
            observed = self._registry.read_value(RUN_KEY_PATH, RUN_VALUE_NAME)
            if (enabled and observed != self.command) or (not enabled and observed is not None):
                raise AutostartError("Launch-at-login change could not be verified.")
        except Exception as exc:
            self._rollback(previous, cause=exc)
        return enabled

    def enable(self) -> None:
        """Enable launch at login."""

        self.set_enabled(True)

    def disable(self) -> None:
        """Disable launch at login."""

        self.set_enabled(False)

    def _rollback(self, previous: str | None, *, cause: Exception) -> None:
        try:
            restored = self._registry.read_value(RUN_KEY_PATH, RUN_VALUE_NAME)
            if restored != previous:
                if previous is None:
                    self._registry.delete_value(RUN_KEY_PATH, RUN_VALUE_NAME)
                else:
                    self._registry.write_value(RUN_KEY_PATH, RUN_VALUE_NAME, previous)
                restored = self._registry.read_value(RUN_KEY_PATH, RUN_VALUE_NAME)
            if restored != previous:
                raise AutostartError("Launch-at-login rollback could not be verified.")
        except Exception as rollback_exc:
            raise AutostartError(
                "Launch-at-login could not be changed and rollback failed."
            ) from rollback_exc
        if isinstance(cause, AutostartError):
            raise cause
        raise AutostartError("Launch-at-login could not be changed.") from cause
