"""Secret storage backed exclusively by Windows Credential Manager."""

from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Protocol, runtime_checkable

WINCRED_TARGET_NAME = "Amadeus/DesktopPet/ChatProviderApiKey"
_CREDENTIAL_USER_NAME = "Amadeus Desktop Pet"
_ERROR_NOT_FOUND = 1168
_MAX_CREDENTIAL_BLOB_BYTES = 5 * 512


class CredentialStoreError(RuntimeError):
    """Safe base error which never includes credential contents."""


class CredentialStoreUnavailableError(CredentialStoreError):
    """Raised when Windows Credential Manager cannot be used."""


class InvalidCredentialError(CredentialStoreError):
    """Raised before an unsafe or malformed secret can be stored."""


@runtime_checkable
class CredentialStore(Protocol):
    """Fixed-target credential operations used by UI and provider services."""

    def has_secret(self) -> bool: ...

    def read_secret(self) -> str | None: ...

    def write_secret(self, secret: str) -> None: ...

    def delete_secret(self) -> None: ...


class InMemoryCredentialStore:
    """Thread-safe test double; production code must use ``WinCredentialStore``."""

    def __init__(self, initial_secret: str | None = None) -> None:
        self._lock = threading.RLock()
        self._secret: str | None = None
        if initial_secret is not None:
            self.write_secret(initial_secret)

    def has_secret(self) -> bool:
        with self._lock:
            return self._secret is not None

    def read_secret(self) -> str | None:
        with self._lock:
            return self._secret

    def write_secret(self, secret: str) -> None:
        validated = _validate_secret(secret)
        with self._lock:
            self._secret = validated

    def delete_secret(self) -> None:
        with self._lock:
            self._secret = None


class WinCredentialStore:
    """Store one API secret under the code-owned Amadeus WinCred target."""

    target_name = WINCRED_TARGET_NAME

    def has_secret(self) -> bool:
        return self.read_secret() is not None

    def read_secret(self) -> str | None:
        win32cred = _load_win32cred()
        try:
            credential = win32cred.CredRead(
                WINCRED_TARGET_NAME,
                win32cred.CRED_TYPE_GENERIC,
                0,
            )
        except Exception as exc:
            if _winerror(exc) == _ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError(
                "Windows Credential Manager could not read the secret."
            ) from None
        try:
            blob = credential["CredentialBlob"]
            if isinstance(blob, bytes):
                secret = blob.decode("utf-16-le")
            elif isinstance(blob, str):
                secret = blob
            else:
                raise TypeError
            return _validate_secret(secret)
        except (KeyError, TypeError, UnicodeError, InvalidCredentialError):
            raise CredentialStoreError(
                "Windows Credential Manager returned an invalid secret."
            ) from None

    def write_secret(self, secret: str) -> None:
        validated = _validate_secret(secret)
        win32cred = _load_win32cred()
        credential = {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": WINCRED_TARGET_NAME,
            "CredentialBlob": validated,
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
            "UserName": _CREDENTIAL_USER_NAME,
            "Comment": "Amadeus chat provider API credential",
        }
        try:
            # CredWrite replaces an existing credential atomically; never delete first.
            win32cred.CredWrite(credential, 0)
        except Exception:
            raise CredentialStoreError(
                "Windows Credential Manager could not save the secret."
            ) from None

    def delete_secret(self) -> None:
        win32cred = _load_win32cred()
        try:
            win32cred.CredDelete(
                WINCRED_TARGET_NAME,
                win32cred.CRED_TYPE_GENERIC,
                0,
            )
        except Exception as exc:
            if _winerror(exc) == _ERROR_NOT_FOUND:
                return
            raise CredentialStoreError(
                "Windows Credential Manager could not delete the secret."
            ) from None


def _validate_secret(secret: object) -> str:
    if not isinstance(secret, str) or not secret or secret != secret.strip() or "\x00" in secret:
        raise InvalidCredentialError("Credential is empty or malformed.")
    if secret.lower().startswith("tp-"):
        raise InvalidCredentialError("MiMo Token Plan credentials are not supported.")
    if len(secret.encode("utf-16-le")) > _MAX_CREDENTIAL_BLOB_BYTES:
        raise InvalidCredentialError("Credential is too long for Windows Credential Manager.")
    return secret


def _load_win32cred() -> ModuleType:
    try:
        return importlib.import_module("win32cred")
    except (ImportError, OSError):
        raise CredentialStoreUnavailableError(
            "Windows Credential Manager support is unavailable."
        ) from None


def _winerror(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    if isinstance(value, int):
        return value
    if exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None
