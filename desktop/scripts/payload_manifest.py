"""Create or verify the immutable file manifest for a PyInstaller onedir payload."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "PAYLOAD-SHA256SUMS.txt"
_HEX_DIGITS = frozenset("0123456789abcdef")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class PayloadManifestError(RuntimeError):
    """Raised when a payload cannot be represented or verified safely."""


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except AttributeError:
        return path.is_symlink()
    return bool(attributes & _REPARSE_POINT)


def _assert_safe_root(root: Path) -> Path:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or _is_reparse_point(root):
        raise PayloadManifestError("payload root must be a real directory")
    return resolved


def _iter_payload_files(root: Path) -> Iterable[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            if entry.is_symlink() or _is_reparse_point(path):
                raise PayloadManifestError(f"payload contains a reparse point: {path}")
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise PayloadManifestError(f"payload contains a non-file entry: {path}")
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST_NAME:
                continue
            if "\n" in relative or "\r" in relative:
                raise PayloadManifestError("payload path contains a line break")
            files.append((relative, path))
    yield from sorted(files, key=lambda value: (value[0].casefold(), value[0]))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_manifest(root: Path) -> int:
    root = _assert_safe_root(root)
    manifest = root / MANIFEST_NAME
    entries = [(relative, _sha256(path)) for relative, path in _iter_payload_files(root)]
    content = "".join(f"{digest}  {relative}\n" for relative, digest in entries)
    temporary = root / f".{MANIFEST_NAME}.tmp"
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, manifest)
    return len(entries)


def _parse_manifest(manifest: Path) -> list[tuple[str, str]]:
    raw = manifest.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or (raw and not raw.endswith(b"\n")):
        raise PayloadManifestError("payload manifest is not canonical UTF-8 text")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PayloadManifestError("payload manifest is not valid UTF-8") from exc
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        if len(line) < 67 or line[64:66] != "  ":
            raise PayloadManifestError("payload manifest line has an invalid format")
        digest = line[:64]
        relative = line[66:]
        if len(digest) != 64 or any(character not in _HEX_DIGITS for character in digest):
            raise PayloadManifestError("payload manifest contains an invalid SHA-256")
        path = PurePosixPath(relative)
        if (
            not relative
            or "\\" in relative
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or relative == MANIFEST_NAME
        ):
            raise PayloadManifestError("payload manifest contains an unsafe path")
        folded = relative.casefold()
        if folded in seen:
            raise PayloadManifestError("payload manifest contains a duplicate path")
        seen.add(folded)
        entries.append((relative, digest))
    canonical = sorted(entries, key=lambda value: (value[0].casefold(), value[0]))
    if entries != canonical:
        raise PayloadManifestError("payload manifest is not deterministically sorted")
    return entries


def verify_manifest(root: Path, *, allow_extra: bool = False) -> int:
    root = _assert_safe_root(root)
    manifest = root / MANIFEST_NAME
    if not manifest.is_file() or _is_reparse_point(manifest):
        raise PayloadManifestError("payload manifest is missing or unsafe")
    entries = _parse_manifest(manifest)
    expected_paths = {relative.casefold() for relative, _digest in entries}
    for relative, expected_digest in entries:
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise PayloadManifestError(f"payload file is missing: {relative}") from exc
        if root not in resolved.parents or not resolved.is_file() or _is_reparse_point(candidate):
            raise PayloadManifestError(f"payload file escaped the root: {relative}")
        if _sha256(resolved) != expected_digest:
            raise PayloadManifestError(f"payload hash differs: {relative}")
    if not allow_extra:
        actual_paths = {relative.casefold() for relative, _path in _iter_payload_files(root)}
        if actual_paths != expected_paths:
            raise PayloadManifestError("payload file set differs from the manifest")
    return len(entries)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "--allow-extra",
        action="store_true",
        help="during verification, permit installer-owned files not listed in the source payload",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "create":
            if arguments.allow_extra:
                raise PayloadManifestError("--allow-extra is valid only for verification")
            count = create_manifest(arguments.root)
            print(f"Payload manifest created: {count} files.")
        else:
            count = verify_manifest(arguments.root, allow_extra=arguments.allow_extra)
            print(f"Payload manifest verified: {count} files.")
    except (OSError, PayloadManifestError) as exc:
        print(f"payload_manifest_error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
