"""Privacy-safe source/build scanner that reports categories and counts only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
import zipfile
from collections import Counter
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

PINNED_ONNX_SHA256 = "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"
PINNED_MODEL_FILE_SHA256 = {
    "model_optimized.onnx": PINNED_ONNX_SHA256,
    "config.json": "9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3",
    "tokenizer.json": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    "tokenizer_config.json": "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
    "special_tokens_map.json": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
}
MODEL_MANIFEST = "amadeus-model.json"
MODEL_BUNDLE_FILES = frozenset((*PINNED_MODEL_FILE_SHA256, MODEL_MANIFEST))
LOCAL_TOOL_CACHE_DIRS = frozenset(
    {".git", ".venv", ".pytest_cache", ".ruff_cache", ".pip-tools-cache", "__pycache__"}
)
PRIVATE_MEDIA_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".wav", ".mp3", ".flac"}
)
APPROVED_MEDIA_SHA256 = {
    "amadeus_desktop/resources/builtin_pet/spritesheet.webp": (
        "cca259ac33ffc7c8170b401a315f9a177a865eb063ba44a4da87c3ab13fa90b7"
    ),
    "amadeus_desktop/resources/app_icon/spritesheet.png": (
        "2d9795265224b99619d34320e57b070a081ebc1c55df0152fd3041242dbd953e"
    ),
    "amadeus_desktop/resources/app_icon/amadeus-kurisu.png": (
        "ded30eeb568f26e3df64998131698e472603bf48d531885b27a7c04293d3b0b5"
    ),
}
SECRET_PATTERNS = (
    re.compile(rb"(?:sk|tp)-[A-Za-z0-9_-]{24,}"),
    re.compile(rb"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._-]{20,}"),
    re.compile(rb"(?i)['\"]?api[-_ ]?key['\"]?\s*[=:]\s*['\"]?[A-Za-z0-9._-]{24,}"),
)
CLEARLY_INVALID_TEST_CREDENTIAL = re.compile(
    rb"(?:sk|tp)-invalid-test-[A-Za-z0-9_-]+",
    re.IGNORECASE,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Amadeus artifacts without printing paths.")
    parser.add_argument("--path", action="append", required=True, type=Path)
    parser.add_argument("--allow-model", action="store_true")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    violations: Counter[str] = Counter()
    files_scanned = 0
    archive_members = 0
    model_files_seen: set[str] = set()
    for root in arguments.path:
        try:
            if not root.exists():
                violations["artifact_missing"] += 1
                continue
            for relative, payload in _iter_payloads(root):
                files_scanned += int(not relative.startswith("archive:"))
                archive_members += int(relative.startswith("archive:"))
                _inspect(
                    relative.removeprefix("archive:"),
                    payload,
                    arguments.allow_model,
                    violations,
                    model_files_seen,
                )
        except Exception:
            violations["artifact_unreadable"] += 1
    if arguments.allow_model and model_files_seen and model_files_seen != MODEL_BUNDLE_FILES:
        violations["model_bundle_incomplete"] += 1
    result = {
        "status": "passed" if not violations else "failed",
        "files_scanned": files_scanned,
        "archive_members_scanned": archive_members,
        "violations_by_category": dict(sorted(violations.items())),
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if not violations else 1


def _iter_payloads(root: Path) -> Iterable[tuple[str, bytes]]:
    if root.is_dir():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if any(part in LOCAL_TOOL_CACHE_DIRS for part in path.parts):
                continue
            relative = path.relative_to(root).as_posix()
            if _is_archive(path):
                for member, payload in _iter_payloads(path):
                    yield f"archive:{relative}!{member.removeprefix('archive:')}", payload
            else:
                yield relative, path.read_bytes()
        return
    suffixes = root.suffixes
    if root.suffix in {".whl", ".zip", ".amadeus-backup", ".codex-pet"}:
        archive_prefix = f"{root.name}!" if root.suffix in {".amadeus-backup", ".codex-pet"} else ""
        with zipfile.ZipFile(root) as archive:
            for name in sorted(archive.namelist()):
                if not name.endswith("/"):
                    yield f"archive:{archive_prefix}{name}", archive.read(name)
        return
    if suffixes[-2:] == [".tar", ".gz"]:
        with tarfile.open(root, "r:gz") as archive:
            for member in sorted(archive.getmembers(), key=lambda value: value.name):
                if member.isfile():
                    handle = archive.extractfile(member)
                    if handle is not None:
                        yield f"archive:{member.name}", handle.read()
        return
    yield root.name, root.read_bytes()


def _is_archive(path: Path) -> bool:
    return path.suffix in {".whl", ".zip", ".amadeus-backup", ".codex-pet"} or path.suffixes[
        -2:
    ] == [".tar", ".gz"]


def _inspect(
    name: str,
    payload: bytes,
    allow_model: bool,
    violations: Counter[str],
    model_files_seen: set[str],
) -> None:
    normalized = name.replace("\\", "/").lower()
    path = PurePosixPath(normalized)
    basename = path.name
    suffix = path.suffix
    if ".amadeus-backup" in normalized:
        violations["local_backup_file"] += 1
    if basename in {
        "amadeus-chat-export.json",
        "amadeus-memory-export.json",
        "amadeus-reminder-export.json",
    } or (suffix == ".json" and _is_local_data_export(payload)):
        violations["local_data_export"] += 1
    if basename == ".env" or basename.startswith(".env."):
        violations["dotenv_file"] += 1
    if suffix in {".sqlite", ".sqlite3", ".db", ".log", ".jsonl"} or basename.endswith(
        ("-wal", "-shm")
    ):
        category = {
            ".log": "log_file",
            ".jsonl": "persona_or_private_jsonl",
        }.get(suffix, "database_file")
        violations[category] += 1
    if "reference/amadeus" in normalized or "克里斯提拉" in normalized:
        violations["private_reference"] += 1
    if "/personas/" in f"/{normalized}":
        violations["private_persona_data"] += 1
    if suffix in PRIVATE_MEDIA_SUFFIXES:
        expected_media_hash = next(
            (
                expected_hash
                for approved_path, expected_hash in APPROVED_MEDIA_SHA256.items()
                if normalized == approved_path
                or normalized.endswith((f"/{approved_path}", f"!{approved_path}"))
            ),
            None,
        )
        if (
            expected_media_hash is None
            or hashlib.sha256(payload).hexdigest() != expected_media_hash
        ):
            violations["unauthorized_character_asset"] += 1
    is_embedding_model_file = "embedding_model" in path.parts
    if is_embedding_model_file:
        model_files_seen.add(basename)
        if not allow_model:
            violations["unexpected_model_file"] += 1
        elif basename not in MODEL_BUNDLE_FILES:
            violations["unverified_model_file"] += 1
        elif basename == MODEL_MANIFEST:
            if not _valid_model_manifest(payload):
                violations["unverified_model_manifest"] += 1
        elif hashlib.sha256(payload).hexdigest() != PINNED_MODEL_FILE_SHA256[basename]:
            violations["unverified_model_file"] += 1
    elif suffix == ".onnx":
        violations["unexpected_model_file"] += 1
    inspected_payload = (
        CLEARLY_INVALID_TEST_CREDENTIAL.sub(b"invalid", payload)
        if b"invalid-test" in payload.lower()
        else payload
    )
    if any(pattern.search(inspected_payload) for pattern in SECRET_PATTERNS):
        violations["credential_pattern"] += 1


def _valid_model_manifest(payload: bytes) -> bool:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    expected = {
        "schema_version": 1,
        "api_name": "BAAI/bge-small-zh-v1.5",
        "repository": "Qdrant/bge-small-zh-v1.5",
        "revision": "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59",
        "onnx_file": "model_optimized.onnx",
        "onnx_sha256": PINNED_ONNX_SHA256,
        "dimension": 512,
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        return False
    files = raw.get("files")
    if not isinstance(files, dict) or set(files) != set(PINNED_MODEL_FILE_SHA256):
        return False
    return all(
        isinstance(files.get(name), dict)
        and files[name].get("sha256") == expected_hash
        and isinstance(files[name].get("size"), int)
        and files[name]["size"] > 0
        for name, expected_hash in PINNED_MODEL_FILE_SHA256.items()
    )


def _is_local_data_export(payload: bytes) -> bool:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(document, dict) and document.get("format") in {
        "amadeus-chat-export/v1",
        "amadeus-chat-export/v2",
        "amadeus-chat-export/v3",
        "amadeus-chat-export/v4",
        "amadeus-memory-export/v1",
        "amadeus-memory-export/v2",
        "amadeus-memory-export/v3",
        "amadeus-reminder-export/v1",
    }


if __name__ == "__main__":
    raise SystemExit(main())
