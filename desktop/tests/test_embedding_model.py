from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from amadeus_desktop.embedding_backend import CPU_PROVIDER
from amadeus_desktop.embedding_model import (
    MODEL_MANIFEST_FILE,
    EmbeddingModelSpec,
    ModelAvailability,
    ModelPreparationError,
    _download_with_windows_curl,
    inspect_model,
    model_directory,
    prepare_model,
    repair_model,
    resolve_runtime_model_directory,
    verify_model,
)
from amadeus_desktop.tools.model import main


class _ProbeBackend:
    model_name = "test/model"
    dimension = 4
    provider = CPU_PROVIDER

    def embed_query(self, _query: str) -> tuple[float, ...]:
        return (1.0, 0.0, 0.0, 0.0)

    def embed_documents(self, _documents) -> tuple[tuple[float, ...], ...]:
        return ((1.0, 0.0, 0.0, 0.0),)

    def close(self) -> None:
        pass


@pytest.fixture
def model_spec() -> EmbeddingModelSpec:
    onnx = b"fixed-test-onnx"
    json_digest = hashlib.sha256(b"{}").hexdigest()
    return EmbeddingModelSpec(
        api_name="test/model",
        repository="test/repository",
        revision="a" * 40,
        onnx_file="model_optimized.onnx",
        onnx_sha256=hashlib.sha256(onnx).hexdigest(),
        dimension=4,
        required_files=(
            "model_optimized.onnx",
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
        ),
        required_file_sha256=(
            ("model_optimized.onnx", hashlib.sha256(onnx).hexdigest()),
            ("config.json", json_digest),
            ("tokenizer.json", json_digest),
            ("tokenizer_config.json", json_digest),
            ("special_tokens_map.json", json_digest),
        ),
    )


def _downloader(directory: Path, _spec: EmbeddingModelSpec, _force: bool) -> None:
    (directory / "model_optimized.onnx").write_bytes(b"fixed-test-onnx")
    for name in (
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
    ):
        (directory / name).write_text("{}", encoding="utf-8")


def test_prepare_verifies_and_atomically_activates_pinned_snapshot(tmp_path, model_spec) -> None:
    result = prepare_model(
        tmp_path,
        spec=model_spec,
        downloader=_downloader,
        backend_factory=lambda _path: _ProbeBackend(),
    )
    target = model_directory(tmp_path, model_spec)

    assert result.ready
    assert verify_model(
        target,
        spec=model_spec,
        backend_factory=lambda _path: _ProbeBackend(),
    ).ready
    manifest = json.loads((target / MODEL_MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["repository"] == model_spec.repository
    assert manifest["revision"] == model_spec.revision
    assert manifest["onnx_sha256"] == model_spec.onnx_sha256
    assert not list(target.parent.glob(".prepare-*"))


def test_corruption_and_version_mismatch_have_distinct_safe_states(tmp_path, model_spec) -> None:
    prepare_model(
        tmp_path,
        spec=model_spec,
        downloader=_downloader,
        backend_factory=lambda _path: _ProbeBackend(),
    )
    target = model_directory(tmp_path, model_spec)
    (target / "model_optimized.onnx").write_bytes(b"corrupt")
    assert inspect_model(target, spec=model_spec).availability is ModelAvailability.CORRUPT

    _downloader(target, model_spec, False)
    manifest_path = target / MODEL_MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["revision"] = "b" * 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    status = inspect_model(target, spec=model_spec)
    assert status.availability is ModelAvailability.VERSION_MISMATCH


def test_self_consistent_tamper_still_fails_pinned_file_hash(tmp_path, model_spec) -> None:
    prepare_model(
        tmp_path,
        spec=model_spec,
        downloader=_downloader,
        backend_factory=lambda _path: _ProbeBackend(),
    )
    target = model_directory(tmp_path, model_spec)
    config = target / "config.json"
    config.write_text('{"tampered":true}', encoding="utf-8")
    manifest_path = target / MODEL_MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = config.read_bytes()
    manifest["files"]["config.json"] = {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert inspect_model(target, spec=model_spec).availability is ModelAvailability.CORRUPT


@pytest.mark.parametrize("extra_kind", ("file", "directory"))
def test_model_directory_rejects_every_extra_entry(tmp_path, model_spec, extra_kind) -> None:
    prepare_model(
        tmp_path,
        spec=model_spec,
        downloader=_downloader,
        backend_factory=lambda _path: _ProbeBackend(),
    )
    target = model_directory(tmp_path, model_spec)
    extra = target / "private-extra"
    if extra_kind == "file":
        extra.write_text("must not be bundled", encoding="utf-8")
    else:
        extra.mkdir()

    assert inspect_model(target, spec=model_spec).availability is ModelAvailability.CORRUPT


def test_model_directory_rejects_links(tmp_path, model_spec) -> None:
    prepare_model(
        tmp_path,
        spec=model_spec,
        downloader=_downloader,
        backend_factory=lambda _path: _ProbeBackend(),
    )
    target = model_directory(tmp_path, model_spec)
    original = target / "config.json"
    external = tmp_path / "external-config.json"
    external.write_text("{}", encoding="utf-8")
    original.unlink()
    try:
        original.symlink_to(external)
    except OSError:
        pytest.skip("symbolic links are not available on this Windows host")

    assert inspect_model(target, spec=model_spec).availability is ModelAvailability.CORRUPT


def test_failed_repair_preserves_previous_verified_generation(tmp_path, model_spec) -> None:
    prepare_model(
        tmp_path,
        spec=model_spec,
        downloader=_downloader,
        backend_factory=lambda _path: _ProbeBackend(),
    )
    target = model_directory(tmp_path, model_spec)
    original_manifest = (target / MODEL_MANIFEST_FILE).read_bytes()

    def fail_download(_directory: Path, _spec: EmbeddingModelSpec, _force: bool) -> None:
        raise OSError("synthetic failure")

    with pytest.raises(ModelPreparationError, match="model_prepare_failed"):
        repair_model(
            tmp_path,
            spec=model_spec,
            downloader=fail_download,
            backend_factory=lambda _path: _ProbeBackend(),
        )

    assert (target / MODEL_MANIFEST_FILE).read_bytes() == original_manifest
    assert inspect_model(target, spec=model_spec).ready


def test_runtime_resolution_prefers_verified_bundled_model(tmp_path, model_spec) -> None:
    local_root = tmp_path / "local"
    bundle_root = tmp_path / "bundle-root"
    local = model_directory(local_root, model_spec)
    bundled = bundle_root / "embedding_model"
    for target in (local, bundled):
        target.mkdir(parents=True)
        _downloader(target, model_spec, False)
        # Reuse prepare's manifest writer through a dedicated target then copy it.
    prepared_root = tmp_path / "prepared"
    prepare_model(
        prepared_root,
        spec=model_spec,
        downloader=_downloader,
        backend_factory=lambda _path: _ProbeBackend(),
    )
    manifest = (model_directory(prepared_root, model_spec) / MODEL_MANIFEST_FILE).read_bytes()
    (local / MODEL_MANIFEST_FILE).write_bytes(manifest)
    (bundled / MODEL_MANIFEST_FILE).write_bytes(manifest)

    assert (
        resolve_runtime_model_directory(local, bundled_directory=bundled, spec=model_spec)
        == bundled
    )


def test_cli_missing_model_emits_only_safe_metadata(tmp_path, capsys) -> None:
    exit_code = main(("verify", "--models-root", str(tmp_path)))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["status"] == "missing"
    assert payload["error_category"] == "model_missing"
    assert str(tmp_path) not in json.dumps(payload)


def test_windows_curl_fallback_keeps_revocation_and_redirect_guards(
    tmp_path,
    model_spec,
    monkeypatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(arguments, **_kwargs):
        calls.append(tuple(arguments))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("amadeus_desktop.embedding_model.shutil.which", lambda _name: "curl.exe")
    monkeypatch.setattr("amadeus_desktop.embedding_model.subprocess.run", run)

    assert _download_with_windows_curl(tmp_path, model_spec)
    assert len(calls) == len(model_spec.required_files)
    for command in calls:
        assert "--ssl-revoke-best-effort" in command
        assert "--ssl-no-revoke" not in command
        assert command[command.index("--proto") + 1] == "=https"
        assert command[command.index("--proto-redir") + 1] == "=https"
        assert command[command.index("--max-redirs") + 1] == "10"
        assert command[command.index("--connect-timeout") + 1] == "30"


def test_pyinstaller_spec_copies_only_allowlisted_model_files_and_notices() -> None:
    spec_path = Path(__file__).parents[1] / "packaging" / "amadeus-desktop.spec"
    source = spec_path.read_text(encoding="utf-8")

    assert "for model_file in MODEL_BUNDLE_FILES" in source
    assert (
        'datas.append((str(model_directory), "amadeus_desktop/resources/embedding_model"))'
        not in source
    )
    assert "P5B_THIRD_PARTY_NOTICES.txt" in source
