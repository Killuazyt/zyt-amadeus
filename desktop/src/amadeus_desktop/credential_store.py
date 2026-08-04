"""Secret storage backed exclusively by Windows Credential Manager."""

from __future__ import annotations

import importlib
import os
import re
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Protocol, runtime_checkable

WINCRED_TARGET_NAME = "Amadeus/DesktopPet/ChatProviderApiKey"
_CREDENTIAL_USER_NAME = "Amadeus Desktop Pet"
_ERROR_NOT_FOUND = 1168
_MAX_CREDENTIAL_BLOB_BYTES = 5 * 512
_ACCEPTANCE_TARGET_PREFIX = "Amadeus/DesktopPet/WinCredAcceptance/"
_ACCEPTANCE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_ACCEPTANCE_SECRET_FIRST = "invalid-fake-amadeus-wincred-acceptance-first"
_ACCEPTANCE_SECRET_REPLACEMENT = "invalid-fake-amadeus-wincred-acceptance-replacement"


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
        return _read_secret(WINCRED_TARGET_NAME)

    def write_secret(self, secret: str) -> None:
        _write_secret(WINCRED_TARGET_NAME, secret)

    def delete_secret(self) -> None:
        _delete_secret(WINCRED_TARGET_NAME)


def run_wincred_acceptance_probe(probe_id: str) -> bool:
    """Exercise one isolated fake target and always remove it before returning."""

    if not isinstance(probe_id, str) or _ACCEPTANCE_ID_PATTERN.fullmatch(probe_id) is None:
        return False
    target_name = f"{_ACCEPTANCE_TARGET_PREFIX}{probe_id}"
    owned = False
    succeeded = False
    cleanup_succeeded = True
    try:
        if _read_secret(target_name) is not None:
            # Never overwrite or delete a pre-existing target, even though a
            # collision with a caller-provided 128-bit random ID is unlikely.
            return False
        owned = True
        _write_secret(target_name, _ACCEPTANCE_SECRET_FIRST)
        if _read_secret(target_name) != _ACCEPTANCE_SECRET_FIRST:
            return False
        _write_secret(target_name, _ACCEPTANCE_SECRET_REPLACEMENT)
        if _read_secret(target_name) != _ACCEPTANCE_SECRET_REPLACEMENT:
            return False
        _delete_secret(target_name)
        if _read_secret(target_name) is not None:
            return False
        succeeded = True
    except CredentialStoreError:
        succeeded = False
    finally:
        if owned:
            try:
                _delete_secret(target_name)
                cleanup_succeeded = _read_secret(target_name) is None
            except CredentialStoreError:
                cleanup_succeeded = False
    return succeeded and cleanup_succeeded


def _read_secret(target_name: str) -> str | None:
    win32cred = _load_win32cred()
    try:
        credential = win32cred.CredRead(
            target_name,
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


def _write_secret(target_name: str, secret: str) -> None:
    validated = _validate_secret(secret)
    win32cred = _load_win32cred()
    credential = {
        "Type": win32cred.CRED_TYPE_GENERIC,
        "TargetName": target_name,
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


def _delete_secret(target_name: str) -> None:
    win32cred = _load_win32cred()
    try:
        win32cred.CredDelete(
            target_name,
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
        pass
    if not bool(getattr(sys, "frozen", False)):
        raise CredentialStoreUnavailableError(
            "Windows Credential Manager support is unavailable."
        ) from None
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not isinstance(bundle_root, str) or not bundle_root:
        raise CredentialStoreUnavailableError(
            "Windows Credential Manager support is unavailable."
        ) from None
    win32_directory = Path(bundle_root) / "win32"
    credential_module = win32_directory / "win32cred.pyd"
    dll_directory = Path(bundle_root) / "pywin32_system32"
    try:
        resolved_win32 = win32_directory.resolve(strict=True)
        resolved_module = credential_module.resolve(strict=True)
        resolved_dlls = dll_directory.resolve(strict=True)
    except OSError:
        raise CredentialStoreUnavailableError(
            "Windows Credential Manager support is unavailable."
        ) from None
    if (
        resolved_module.parent != resolved_win32
        or resolved_module.name.casefold() != "win32cred.pyd"
        or credential_module.is_symlink()
        or win32_directory.is_symlink()
        or dll_directory.is_symlink()
        or not resolved_module.is_file()
        or not resolved_dlls.is_dir()
    ):
        raise CredentialStoreUnavailableError(
            "Windows Credential Manager support is unavailable."
        ) from None
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, os.fspath(resolved_win32))
        with os.add_dll_directory(os.fspath(resolved_dlls)):
            module = importlib.import_module("win32cred")
    except (AttributeError, ImportError, OSError):
        raise CredentialStoreUnavailableError(
            "Windows Credential Manager support is unavailable."
        ) from None
    finally:
        sys.path[:] = previous_path
    module_path = getattr(module, "__file__", None)
    try:
        loaded_path = Path(module_path).resolve(strict=True)
    except (OSError, TypeError):
        raise CredentialStoreUnavailableError(
            "Windows Credential Manager support is unavailable."
        ) from None
    if loaded_path != resolved_module:
        raise CredentialStoreUnavailableError(
            "Windows Credential Manager support is unavailable."
        ) from None
    return module


def _winerror(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    if isinstance(value, int):
        return value
    if exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None
