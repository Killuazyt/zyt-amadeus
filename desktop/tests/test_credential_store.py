from __future__ import annotations

import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

from amadeus_desktop import credential_store as credential_store_module
from amadeus_desktop.credential_store import (
    WINCRED_TARGET_NAME,
    CredentialStore,
    CredentialStoreError,
    InMemoryCredentialStore,
    InvalidCredentialError,
    WinCredentialStore,
)

_FAKE_SECRET = "invalid-test-credential-never-authorized"


def test_in_memory_store_matches_protocol_and_replaces_without_delete() -> None:
    store = InMemoryCredentialStore()

    assert isinstance(store, CredentialStore)
    assert store.has_secret() is False
    assert store.read_secret() is None

    store.write_secret(_FAKE_SECRET)
    store.write_secret(f"{_FAKE_SECRET}-replacement")

    assert store.has_secret() is True
    assert store.read_secret() == f"{_FAKE_SECRET}-replacement"
    store.delete_secret()
    store.delete_secret()
    assert store.read_secret() is None


@pytest.mark.parametrize("secret", ["", " padded", "padded ", "bad\x00value", "tp-fake"])
def test_unsafe_credentials_are_rejected_without_echo(secret: str) -> None:
    store = InMemoryCredentialStore()

    with pytest.raises(InvalidCredentialError) as captured:
        store.write_secret(secret)

    if secret:
        assert secret not in str(captured.value)
    assert store.has_secret() is False


def test_wincred_uses_fixed_target_type_persistence_and_no_predelete(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []
    stored: dict[str, object] = {}

    def cred_write(credential: dict[str, object], flags: int) -> None:
        calls.append(("write", credential.copy(), flags))
        stored.clear()
        stored.update(credential)

    def cred_read(target: str, credential_type: int, flags: int) -> dict[str, object]:
        calls.append(("read", target, credential_type, flags))
        return {"CredentialBlob": str(stored["CredentialBlob"]).encode("utf-16-le")}

    fake = SimpleNamespace(
        CRED_TYPE_GENERIC=1,
        CRED_PERSIST_LOCAL_MACHINE=2,
        CredWrite=cred_write,
        CredRead=cred_read,
        CredDelete=lambda *args: calls.append(("delete", *args)),
    )
    monkeypatch.setattr(credential_store_module, "_load_win32cred", lambda: fake)
    store = WinCredentialStore()

    store.write_secret(_FAKE_SECRET)
    assert store.read_secret() == _FAKE_SECRET

    assert [call[0] for call in calls] == ["write", "read"]
    written = calls[0][1]
    assert isinstance(written, dict)
    assert written["TargetName"] == WINCRED_TARGET_NAME
    assert written["Type"] == fake.CRED_TYPE_GENERIC
    assert written["Persist"] == fake.CRED_PERSIST_LOCAL_MACHINE


def test_wincred_errors_are_wrapped_without_secret(monkeypatch) -> None:
    class Failure(RuntimeError):
        pass

    fake = SimpleNamespace(
        CRED_TYPE_GENERIC=1,
        CRED_PERSIST_LOCAL_MACHINE=2,
        CredWrite=lambda *_args: (_ for _ in ()).throw(Failure(_FAKE_SECRET)),
    )
    monkeypatch.setattr(credential_store_module, "_load_win32cred", lambda: fake)

    with pytest.raises(CredentialStoreError) as captured:
        WinCredentialStore().write_secret(_FAKE_SECRET)

    assert _FAKE_SECRET not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_real_wincred_fake_credential_replacement_cross_process_and_cleanup(monkeypatch) -> None:
    pytest.importorskip("win32cred")
    unique_target = f"{WINCRED_TARGET_NAME}/pytest/{uuid.uuid4()}"
    monkeypatch.setattr(credential_store_module, "WINCRED_TARGET_NAME", unique_target)
    store = WinCredentialStore()
    replacement = f"{_FAKE_SECRET}-replacement"
    try:
        store.write_secret(_FAKE_SECRET)
        store.write_secret(replacement)
        code = (
            "import win32cred; "
            f"c=win32cred.CredRead({unique_target!r}, win32cred.CRED_TYPE_GENERIC, 0); "
            "blob=c['CredentialBlob']; "
            "value=blob.decode('utf-16-le') if isinstance(blob, bytes) else blob; "
            f"raise SystemExit(0 if value == {replacement!r} else 2)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0
    finally:
        store.delete_secret()
    assert store.has_secret() is False
