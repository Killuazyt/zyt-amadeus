from __future__ import annotations

import subprocess
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
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
    run_wincred_acceptance_probe,
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


def test_wincred_acceptance_probe_is_isolated_replaces_and_cleans_up(
    monkeypatch,
    capsys,
) -> None:
    class NotFound(OSError):
        winerror = 1168

    stored: dict[str, str] = {}
    writes: list[tuple[str, str]] = []
    deletes: list[str] = []

    def cred_read(target: str, _credential_type: int, _flags: int) -> dict[str, object]:
        if target not in stored:
            raise NotFound
        return {"CredentialBlob": stored[target].encode("utf-16-le")}

    def cred_write(credential: dict[str, object], _flags: int) -> None:
        target = str(credential["TargetName"])
        secret = str(credential["CredentialBlob"])
        writes.append((target, secret))
        stored[target] = secret

    def cred_delete(target: str, _credential_type: int, _flags: int) -> None:
        deletes.append(target)
        if target not in stored:
            raise NotFound
        del stored[target]

    fake = SimpleNamespace(
        CRED_TYPE_GENERIC=1,
        CRED_PERSIST_LOCAL_MACHINE=2,
        CredRead=cred_read,
        CredWrite=cred_write,
        CredDelete=cred_delete,
    )
    monkeypatch.setattr(credential_store_module, "_load_win32cred", lambda: fake)

    assert run_wincred_acceptance_probe("0123456789abcdef0123456789abcdef")
    assert stored == {}
    assert len(writes) == 2
    assert writes[0][0] == writes[1][0]
    assert writes[0][0] != WINCRED_TARGET_NAME
    assert writes[0][1] != writes[1][1]
    assert deletes.count(writes[0][0]) >= 2
    assert capsys.readouterr() == ("", "")


def test_wincred_acceptance_probe_rejects_bad_id_without_loading_wincred(monkeypatch) -> None:
    monkeypatch.setattr(
        credential_store_module,
        "_load_win32cred",
        lambda: pytest.fail("invalid probe must not touch WinCred"),
    )

    assert not run_wincred_acceptance_probe("A" * 32)
    assert not run_wincred_acceptance_probe("a" * 31)


def test_wincred_acceptance_probe_finally_cleans_after_replacement_failure(monkeypatch) -> None:
    class NotFound(OSError):
        winerror = 1168

    stored: dict[str, str] = {}
    write_count = 0

    def cred_read(target: str, _credential_type: int, _flags: int) -> dict[str, object]:
        if target not in stored:
            raise NotFound
        return {"CredentialBlob": stored[target]}

    def cred_write(credential: dict[str, object], _flags: int) -> None:
        nonlocal write_count
        write_count += 1
        target = str(credential["TargetName"])
        stored[target] = str(credential["CredentialBlob"])
        if write_count == 2:
            raise OSError("synthetic private failure")

    def cred_delete(target: str, _credential_type: int, _flags: int) -> None:
        if target not in stored:
            raise NotFound
        del stored[target]

    fake = SimpleNamespace(
        CRED_TYPE_GENERIC=1,
        CRED_PERSIST_LOCAL_MACHINE=2,
        CredRead=cred_read,
        CredWrite=cred_write,
        CredDelete=cred_delete,
    )
    monkeypatch.setattr(credential_store_module, "_load_win32cred", lambda: fake)

    assert not run_wincred_acceptance_probe("fedcba9876543210fedcba9876543210")
    assert stored == {}


def test_frozen_wincred_loader_uses_only_the_fixed_bundled_extension_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    win32_directory = tmp_path / "win32"
    dll_directory = tmp_path / "pywin32_system32"
    win32_directory.mkdir()
    dll_directory.mkdir()
    module_path = win32_directory / "win32cred.pyd"
    module_path.write_bytes(b"synthetic-extension-placeholder")
    imported = SimpleNamespace(__file__=str(module_path))
    calls: list[tuple[str, str | None]] = []

    def import_module(name: str):
        calls.append((name, sys.path[0] if sys.path else None))
        if len(calls) == 1:
            raise ImportError
        return imported

    monkeypatch.setattr(credential_store_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(credential_store_module.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(credential_store_module.importlib, "import_module", import_module)
    monkeypatch.setattr(
        credential_store_module.os,
        "add_dll_directory",
        lambda value: nullcontext(value),
    )

    assert credential_store_module._load_win32cred() is imported
    assert calls == [
        ("win32cred", sys.path[0]),
        ("win32cred", str(win32_directory.resolve())),
    ]
    assert str(win32_directory.resolve()) not in sys.path


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
