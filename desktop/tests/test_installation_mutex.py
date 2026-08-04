from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from amadeus_desktop.installation_mutex import (
    INSTALLATION_MUTEX_NAME,
    InstallationMutex,
    InstallationMutexError,
    is_installation_mutex_present,
)


@dataclass
class FakeMutexBackend:
    handle: int = 91
    exists: bool = False
    created: list[str] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    inspected: list[str] = field(default_factory=list)

    def create(self, name: str) -> int:
        self.created.append(name)
        return self.handle

    def close(self, handle: int) -> None:
        self.closed.append(handle)

    def present(self, name: str) -> bool:
        self.inspected.append(name)
        return self.exists


def test_application_holds_exact_stable_installer_mutex_until_close() -> None:
    backend = FakeMutexBackend()
    marker = InstallationMutex(backend)

    marker.acquire()
    marker.acquire()

    assert marker.held
    assert backend.created == [INSTALLATION_MUTEX_NAME]
    assert INSTALLATION_MUTEX_NAME == (
        r"Local\AmadeusDesktopPet-8739af85-a7c4-54f5-a318-5d15264178e9"
    )
    marker.close()
    marker.close()
    assert not marker.held
    assert backend.closed == [91]


def test_zero_mutex_handle_fails_closed() -> None:
    with pytest.raises(InstallationMutexError, match="could not start"):
        InstallationMutex(FakeMutexBackend(handle=0)).acquire()


def test_installer_presence_probe_uses_the_same_exact_name() -> None:
    backend = FakeMutexBackend(exists=True)

    assert is_installation_mutex_present(backend)
    assert backend.inspected == [INSTALLATION_MUTEX_NAME]
