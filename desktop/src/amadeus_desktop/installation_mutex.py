"""Stable Windows lifecycle marker used by the application and installer."""

from __future__ import annotations

import ctypes
import os
from typing import Protocol

INSTALLATION_MUTEX_NAME = r"Local\AmadeusDesktopPet-8739af85-a7c4-54f5-a318-5d15264178e9"
_ERROR_FILE_NOT_FOUND = 2
_SYNCHRONIZE = 0x00100000


class InstallationMutexError(RuntimeError):
    """Raised when the lifecycle marker cannot be held safely."""


class MutexBackend(Protocol):
    def create(self, name: str) -> int: ...

    def close(self, handle: int) -> None: ...

    def present(self, name: str) -> bool: ...


class WindowsMutexBackend:
    """Minimal Win32 named-mutex boundary with no import-time side effects."""

    def __init__(self) -> None:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError) as exc:  # pragma: no cover - platform guard
            raise InstallationMutexError("Windows lifecycle coordination is unavailable.") from exc
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.OpenMutexW.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        self._kernel32 = kernel32

    def create(self, name: str) -> int:
        return int(self._kernel32.CreateMutexW(None, False, name) or 0)

    def close(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(handle):
            raise InstallationMutexError("Windows lifecycle coordination could not close.")

    def present(self, name: str) -> bool:
        ctypes.set_last_error(0)
        handle = int(self._kernel32.OpenMutexW(_SYNCHRONIZE, False, name) or 0)
        if handle:
            self.close(handle)
            return True
        if ctypes.get_last_error() == _ERROR_FILE_NOT_FOUND:
            return False
        raise InstallationMutexError("Windows lifecycle state could not be inspected.")


class InstallationMutex:
    """Hold the stable marker for the complete lifetime of a normal app process."""

    def __init__(self, backend: MutexBackend | None = None) -> None:
        self._backend = backend
        self._handle: int | None = None
        self._noop = False

    @property
    def held(self) -> bool:
        return self._handle is not None or self._noop

    def acquire(self) -> None:
        if self.held:
            return
        if self._backend is None and os.name != "nt":
            self._noop = True
            return
        backend = self._backend or WindowsMutexBackend()
        handle = backend.create(INSTALLATION_MUTEX_NAME)
        if not handle:
            raise InstallationMutexError("Windows lifecycle coordination could not start.")
        self._backend = backend
        self._handle = handle

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        self._noop = False
        if handle is not None and self._backend is not None:
            self._backend.close(handle)

    def __enter__(self) -> InstallationMutex:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def is_installation_mutex_present(backend: MutexBackend | None = None) -> bool:
    """Return whether a normal Amadeus process currently publishes its marker."""

    if backend is None and os.name != "nt":
        return False
    return (backend or WindowsMutexBackend()).present(INSTALLATION_MUTEX_NAME)
