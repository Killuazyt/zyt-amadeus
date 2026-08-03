"""Versioned exports, consistent backups, and fail-closed restore primitives.

The functions in this module are intentionally independent from Qt and
Windows Credential Manager.  SQLite bundle creation belongs on the existing
data thread; archive I/O and validation belong on a separate worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from amadeus_desktop.database import (
    AMADEUS_APPLICATION_ID,
    SCHEMA_VERSION,
    DatabaseMigrationError,
    validate_database_schema,
)
from amadeus_desktop.paths import AppDirectory, AppPaths
from amadeus_desktop.settings import (
    CURRENT_SCHEMA_VERSION as SETTINGS_SCHEMA_VERSION,
)
from amadeus_desktop.settings import (
    InvalidSettingsError,
    SettingsRepository,
    validate_settings_document,
)

CHAT_EXPORT_FORMAT = "amadeus-chat-export/v1"
MEMORY_EXPORT_FORMAT = "amadeus-memory-export/v1"
BACKUP_FORMAT = "amadeus-backup/v1"

BACKUP_MANIFEST_MEMBER = "manifest.json"
BACKUP_DATABASE_MEMBER = "data/amadeus.sqlite3"
BACKUP_SETTINGS_MEMBER = "config/settings.json"
BACKUP_MEMBERS = frozenset({BACKUP_MANIFEST_MEMBER, BACKUP_DATABASE_MEMBER, BACKUP_SETTINGS_MEMBER})
RESTORE_TRANSACTION_FORMAT = "amadeus-restore-transaction/v1"

_RESTORE_MARKER_NAME = ".restore-transaction.json"
_RESTORE_TRANSACTION_STATES = frozenset(
    {"building", "prepared", "committed", "rolled_back", "committed_cleaning"}
)
_RESTORE_TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")
_RESTORE_TARGET_KEYS = ("database", "database_wal", "database_shm", "settings")

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STAGING_PREFIX = ".amadeus-restore-"
_FORBIDDEN_SETTINGS_KEYS = {
    "api-key",
    "api_key",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}
_FORBIDDEN_SETTINGS_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_password",
    "_secret",
    "_token",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|tp)-[a-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}\b"),
)

_V1_TABLES = {
    "profiles",
    "conversations",
    "messages",
    "conversation_summaries",
    "memory_groups",
    "memory_versions",
    "memory_sources",
    "background_jobs",
    "memory_fts",
}
_V2_TABLES = {
    "memory_embedding_generations",
    "memory_vectors",
    "memory_recall_events",
    "persona_knowledge",
    "persona_fts",
    "persona_embedding_generations",
    "persona_vectors",
    "persona_recall_events",
}
_V3_TABLES = {"proactive_events"}


class DataManagementError(RuntimeError):
    """Base class for privacy-safe data-management failures."""


class ExportError(DataManagementError):
    """Raised when a versioned JSON export cannot be written safely."""


class BackupError(DataManagementError):
    """Raised when a backup archive cannot be created safely."""


class BackupValidationError(DataManagementError):
    """Raised when a backup is malformed, unsafe, corrupt, or unsupported."""


class RestoreError(DataManagementError):
    """Raised when a validated payload cannot be installed atomically."""


class RestoreRollbackError(RestoreError):
    """Raised when both restore installation and exact rollback fail."""


class RestoreCleanupError(RestoreError):
    """Raised after new data committed but obsolete rollback files remain."""


class UnsafeResetPlanError(DataManagementError):
    """Raised when factory-reset targets are broader than one Amadeus root."""


@dataclass(frozen=True, slots=True)
class ChatExportBundle:
    """One synchronous, read-only snapshot prepared on the data thread."""

    database_schema: int
    conversations: tuple[Mapping[str, object], ...]
    messages: tuple[Mapping[str, object], ...]
    summaries: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class MemoryExportBundle:
    """Auditable memory state without vector/index implementation details."""

    database_schema: int
    groups: tuple[Mapping[str, object], ...]
    versions: tuple[Mapping[str, object], ...]
    sources: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class BackupLimits:
    """Fail-closed resource limits for untrusted backup archives."""

    archive_bytes: int = 8 * 1024**3
    database_bytes: int = 8 * 1024**3
    settings_bytes: int = 4 * 1024**2
    manifest_bytes: int = 128 * 1024

    def __post_init__(self) -> None:
        if (
            min(
                self.archive_bytes,
                self.database_bytes,
                self.settings_bytes,
                self.manifest_bytes,
            )
            <= 0
        ):
            raise ValueError("backup limits must be positive")


DEFAULT_BACKUP_LIMITS = BackupLimits()


@dataclass(frozen=True, slots=True)
class BackupMetadata:
    """Validated, non-content metadata from an ``amadeus-backup/v1`` archive."""

    created_at: str
    app_version: str
    database_schema: int
    settings_schema: int
    database_size: int
    database_sha256: str
    settings_size: int
    settings_sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedRestorePayload:
    """Private staged files that have passed all archive and SQLite checks."""

    archive_path: Path
    staging_root: Path
    database_path: Path
    settings_path: Path
    metadata: BackupMetadata
    staged_database_sha256: str
    staged_settings_sha256: str


@dataclass(frozen=True, slots=True)
class FactoryResetPlan:
    """Exact known children eligible for a later, separately confirmed reset."""

    root: Path
    targets: tuple[Path, ...]


ConsistentBackupSource = Path | Callable[[Path], str | Path | None]
ChatBundleLoader = Callable[[], ChatExportBundle]
MemoryBundleLoader = Callable[[], MemoryExportBundle]
ReplaceOperation = Callable[[Path, Path], None]
RestoreCheckpoint = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class _RestoreFileSet:
    target: Path
    rollback: Path
    new: Path | None
    recovery: Path


class SQLiteExportRepository:
    """Build export bundles using only SELECT and read-only PRAGMA statements.

    The repository is synchronous by design.  Production callers must invoke
    it through the existing serialized data-thread callback boundary.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def load_chat_bundle(self) -> ChatExportBundle:
        schema = self._schema_version()
        message_columns = self._table_columns("messages")
        origin_expression = "origin" if "origin" in message_columns else "'conversation' AS origin"
        conversations = self._rows(
            """
            SELECT id, profile_id, title, status, created_at, updated_at, last_activity_at
            FROM conversations
            ORDER BY created_at, id
            """
        )
        messages = self._rows(
            f"""
            SELECT sequence, id, conversation_id, turn_id, role, {origin_expression},
                   content, status, attempt, terminal_reason, provider_name, model_name,
                   failure_code, participates_in_memory, created_at, updated_at, completed_at
            FROM messages
            ORDER BY sequence, id
            """
        )
        summaries = self._rows(
            """
            SELECT id, conversation_id, content, covers_through_sequence, message_count,
                   character_count, created_at
            FROM conversation_summaries
            ORDER BY conversation_id, covers_through_sequence, id
            """
        )
        return ChatExportBundle(schema, conversations, messages, summaries)

    def load_memory_bundle(self) -> MemoryExportBundle:
        schema = self._schema_version()
        groups = self._rows(
            """
            SELECT id, profile_id, kind, topic_key, status, pinned, current_version_id,
                   created_at, updated_at
            FROM memory_groups
            ORDER BY created_at, id
            """
        )
        versions = self._rows(
            """
            SELECT id, memory_id, version_number, content, normalized_content, content_hash,
                   search_text, importance, confidence, origin, operation,
                   supersedes_version_id, created_at
            FROM memory_versions
            ORDER BY memory_id, version_number, id
            """
        )
        sources = self._rows(
            """
            SELECT id, version_id, source_message_id, source_conversation_id,
                   live_message_id, live_conversation_id, extraction_method, created_at
            FROM memory_sources
            ORDER BY version_id, created_at, id
            """
        )
        return MemoryExportBundle(schema, groups, versions, sources)

    def _schema_version(self) -> int:
        row = self._connection.execute("PRAGMA user_version").fetchone()
        if row is None:
            raise ExportError("database schema metadata is unavailable")
        return int(row[0])

    def _table_columns(self, table: str) -> frozenset[str]:
        rows = self._connection.execute(f"PRAGMA table_info({table})")
        return frozenset(str(row[1]) for row in rows)

    def _rows(self, statement: str) -> tuple[Mapping[str, object], ...]:
        cursor = self._connection.execute(statement)
        columns = tuple(str(column[0]) for column in (cursor.description or ()))
        return tuple(dict(zip(columns, row, strict=True)) for row in cursor.fetchall())


def export_chat_json(
    destination: str | Path,
    load_bundle: ChatBundleLoader,
    *,
    exported_at: datetime | None = None,
) -> Path:
    """Load one chat snapshot and atomically write ``amadeus-chat-export/v1``."""

    bundle = load_bundle()
    if not isinstance(bundle, ChatExportBundle):
        raise ExportError("chat export callback returned an invalid bundle")
    payload = {
        "format": CHAT_EXPORT_FORMAT,
        "exported_at": _timestamp(exported_at),
        "database_schema": bundle.database_schema,
        "conversations": list(bundle.conversations),
        "messages": list(bundle.messages),
        "summaries": list(bundle.summaries),
    }
    return _atomic_write_json(Path(destination), payload, error_type=ExportError)


def export_memory_json(
    destination: str | Path,
    load_bundle: MemoryBundleLoader,
    *,
    exported_at: datetime | None = None,
) -> Path:
    """Load one memory snapshot and atomically write ``amadeus-memory-export/v1``."""

    bundle = load_bundle()
    if not isinstance(bundle, MemoryExportBundle):
        raise ExportError("memory export callback returned an invalid bundle")
    payload = {
        "format": MEMORY_EXPORT_FORMAT,
        "exported_at": _timestamp(exported_at),
        "database_schema": bundle.database_schema,
        "groups": list(bundle.groups),
        "versions": list(bundle.versions),
        "sources": list(bundle.sources),
    }
    return _atomic_write_json(Path(destination), payload, error_type=ExportError)


def create_backup_archive(
    destination: str | Path,
    *,
    database_backup: ConsistentBackupSource,
    settings_snapshot: Mapping[str, Any],
    app_version: str,
    created_at: datetime | None = None,
    limits: BackupLimits = DEFAULT_BACKUP_LIMITS,
) -> Path:
    """Create and self-validate one atomic ``amadeus-backup/v1`` ZIP.

    ``database_backup`` must be either an already consistent SQLite backup or
    a callback compatible with ``SQLiteDatabase.create_backup(target)``.  A
    live WAL database path must never be supplied directly.
    """

    target = Path(destination)
    app_version = str(app_version).strip()
    if not app_version or len(app_version) > 128:
        raise BackupError("application version is invalid")
    settings_document = _clone_settings(settings_snapshot, error_type=BackupError)
    settings_schema = _settings_schema(settings_document, error_type=BackupError)
    if settings_schema != SETTINGS_SCHEMA_VERSION:
        raise BackupError("settings snapshot does not use the current schema")
    try:
        validate_settings_document(settings_document)
    except InvalidSettingsError as exc:
        raise BackupError("settings snapshot is invalid") from exc
    settings_bytes = _json_bytes(settings_document)
    if len(settings_bytes) > limits.settings_bytes:
        raise BackupError("settings snapshot exceeds the backup limit")

    temporary_archive: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=target.parent, prefix=".amadeus-backup-build-"
        ) as temporary_directory:
            workspace = Path(temporary_directory)
            database_path = _materialize_consistent_database(database_backup, workspace)
            database_schema = _validate_database_file(
                database_path,
                current_schema=SCHEMA_VERSION,
                maximum_bytes=limits.database_bytes,
            )
            if database_schema != SCHEMA_VERSION:
                raise BackupError("database backup does not use the current schema")
            database_size, database_hash = _file_size_and_sha256(
                database_path, maximum_bytes=limits.database_bytes, error_type=BackupError
            )
            settings_hash = hashlib.sha256(settings_bytes).hexdigest()
            manifest = {
                "format": BACKUP_FORMAT,
                "created_at": _timestamp(created_at),
                "app_version": app_version,
                "schemas": {
                    "database": database_schema,
                    "settings": settings_schema,
                },
                "files": {
                    BACKUP_DATABASE_MEMBER: {
                        "size": database_size,
                        "sha256": database_hash,
                    },
                    BACKUP_SETTINGS_MEMBER: {
                        "size": len(settings_bytes),
                        "sha256": settings_hash,
                    },
                },
            }
            manifest_bytes = _json_bytes(manifest)
            if len(manifest_bytes) > limits.manifest_bytes:
                raise BackupError("backup manifest exceeds the backup limit")

            with tempfile.NamedTemporaryFile(
                "wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_archive = Path(handle.name)
            with zipfile.ZipFile(
                temporary_archive,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as archive:
                archive.writestr(BACKUP_MANIFEST_MEMBER, manifest_bytes)
                archive.write(database_path, BACKUP_DATABASE_MEMBER)
                archive.writestr(BACKUP_SETTINGS_MEMBER, settings_bytes)
            with temporary_archive.open("r+b") as handle:
                os.fsync(handle.fileno())
            if temporary_archive.stat().st_size > limits.archive_bytes:
                raise BackupError("backup archive exceeds the backup limit")

            validation_parent = workspace / "self-check"
            payload = stage_backup_for_restore(
                temporary_archive,
                validation_parent,
                limits=limits,
            )
            discard_staged_restore(payload)
            os.replace(temporary_archive, target)
            temporary_archive = None
    except BackupError:
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise BackupError("backup archive could not be created safely") from exc
    finally:
        if temporary_archive is not None:
            with suppress(OSError):
                temporary_archive.unlink(missing_ok=True)
    return target


def validate_backup_archive(
    archive_path: str | Path,
    *,
    limits: BackupLimits = DEFAULT_BACKUP_LIMITS,
) -> BackupMetadata:
    """Validate an archive without retaining extracted private content."""

    archive = Path(archive_path)
    try:
        with tempfile.TemporaryDirectory(prefix="amadeus-backup-validate-") as parent:
            payload = stage_backup_for_restore(archive, Path(parent), limits=limits)
            metadata = payload.metadata
            discard_staged_restore(payload)
            return metadata
    except BackupValidationError:
        raise
    except OSError as exc:
        raise BackupValidationError("backup validation workspace is unavailable") from exc


def stage_backup_for_restore(
    archive_path: str | Path,
    staging_parent: str | Path,
    *,
    limits: BackupLimits = DEFAULT_BACKUP_LIMITS,
) -> ValidatedRestorePayload:
    """Extract an untrusted archive into a private directory and validate it."""

    archive_path = Path(archive_path)
    staging_parent = Path(staging_parent)
    staging_root: Path | None = None
    try:
        archive_size = archive_path.stat().st_size
        if archive_size <= 0 or archive_size > limits.archive_bytes:
            raise BackupValidationError("backup archive size is invalid")
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging_root = staging_parent / f"{_STAGING_PREFIX}{uuid4().hex}"
        staging_root.mkdir()
        database_path = staging_root / BACKUP_DATABASE_MEMBER
        settings_path = staging_root / BACKUP_SETTINGS_MEMBER
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            infos = archive.infolist()
            _validate_zip_members(infos, limits)
            info_by_name = {info.filename: info for info in infos}
            manifest_bytes = _read_member_bytes(
                archive,
                info_by_name[BACKUP_MANIFEST_MEMBER],
                limits.manifest_bytes,
            )
            manifest = _decode_json_object(manifest_bytes, "backup manifest")
            metadata = _parse_manifest(manifest, limits)

            database_size, database_hash = _extract_member(
                archive,
                info_by_name[BACKUP_DATABASE_MEMBER],
                database_path,
                limits.database_bytes,
            )
            settings_size, settings_hash = _extract_member(
                archive,
                info_by_name[BACKUP_SETTINGS_MEMBER],
                settings_path,
                limits.settings_bytes,
            )
        if (database_size, database_hash) != (
            metadata.database_size,
            metadata.database_sha256,
        ):
            raise BackupValidationError("database backup checksum does not match")
        if (settings_size, settings_hash) != (
            metadata.settings_size,
            metadata.settings_sha256,
        ):
            raise BackupValidationError("settings backup checksum does not match")

        database_schema = _validate_database_file(
            database_path,
            current_schema=SCHEMA_VERSION,
            maximum_bytes=limits.database_bytes,
        )
        if database_schema != metadata.database_schema:
            raise BackupValidationError("database schema does not match the manifest")

        settings_document = _decode_json_object(settings_path.read_bytes(), "settings backup")
        settings_schema = _settings_schema(settings_document, error_type=BackupValidationError)
        if settings_schema != metadata.settings_schema:
            raise BackupValidationError("settings schema does not match the manifest")
        _reject_sensitive_settings(settings_document, error_type=BackupValidationError)
        if settings_schema > SETTINGS_SCHEMA_VERSION:
            raise BackupValidationError("settings backup is newer than this application")
        if settings_schema == SETTINGS_SCHEMA_VERSION:
            try:
                validate_settings_document(settings_document)
            except InvalidSettingsError as exc:
                raise BackupValidationError("settings backup is invalid") from exc
        else:
            try:
                SettingsRepository(settings_path).load()
            except Exception as exc:  # noqa: BLE001 - stable validation category only
                raise BackupValidationError("settings backup cannot be migrated") from exc

        staged_database_hash = _file_size_and_sha256(
            database_path,
            maximum_bytes=limits.database_bytes,
            error_type=BackupValidationError,
        )[1]
        staged_settings_hash = _file_size_and_sha256(
            settings_path,
            maximum_bytes=limits.settings_bytes,
            error_type=BackupValidationError,
        )[1]
        return ValidatedRestorePayload(
            archive_path=archive_path,
            staging_root=staging_root,
            database_path=database_path,
            settings_path=settings_path,
            metadata=metadata,
            staged_database_sha256=staged_database_hash,
            staged_settings_sha256=staged_settings_hash,
        )
    except BackupValidationError:
        if staging_root is not None:
            _remove_staging_root(staging_root, staging_parent)
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as exc:
        if staging_root is not None:
            _remove_staging_root(staging_root, staging_parent)
        raise BackupValidationError("backup archive is invalid") from exc


def apply_validated_restore(
    payload: ValidatedRestorePayload,
    paths: AppPaths,
    *,
    replace_operation: ReplaceOperation | None = None,
    checkpoint: RestoreCheckpoint | None = None,
) -> None:
    """Install staged files through one crash-recoverable restore transaction.

    The owning database, vector runtime, and settings writer must already be
    stopped.  A durable marker is written before any live target changes.  At
    startup, :func:`recover_interrupted_restore` rolls a prepared transaction
    back or finishes cleanup for a committed transaction before settings or
    SQLite are opened.
    """

    if not isinstance(payload, ValidatedRestorePayload):
        raise RestoreError("restore payload is invalid")
    try:
        _validate_payload_paths(payload)
    except DataManagementError:
        raise
    except OSError as exc:
        raise RestoreError("restore staging paths could not be checked safely") from exc
    database_size, database_hash = _file_size_and_sha256(
        payload.database_path,
        maximum_bytes=max(payload.metadata.database_size, 1),
        error_type=RestoreError,
    )
    settings_size, settings_hash = _file_size_and_sha256(
        payload.settings_path,
        maximum_bytes=max(payload.metadata.settings_size, 4 * 1024**2),
        error_type=RestoreError,
    )
    if database_hash != payload.staged_database_sha256 or database_size <= 0:
        raise RestoreError("staged database changed after validation")
    if settings_hash != payload.staged_settings_sha256 or settings_size <= 0:
        raise RestoreError("staged settings changed after validation")
    _validate_database_file(
        payload.database_path,
        current_schema=SCHEMA_VERSION,
        maximum_bytes=max(database_size, 1),
        error_type=RestoreError,
    )
    try:
        settings_bytes = payload.settings_path.read_bytes()
    except OSError as exc:
        raise RestoreError("staged settings could not be read safely") from exc
    settings_document = _decode_json_object(
        settings_bytes, "staged settings", error_type=RestoreError
    )
    try:
        validate_settings_document(settings_document)
    except InvalidSettingsError as exc:
        raise RestoreError("staged settings are invalid") from exc

    try:
        recover_interrupted_restore(paths)
        reset_plan = plan_factory_reset(paths)
        root = reset_plan.root
        database_target = _absolute(paths.database_file)
        settings_target = _absolute(paths.settings_file)
        if database_target.parent != root / AppDirectory.DATA.value:
            raise RestoreError("database restore target is outside the Amadeus data directory")
        if settings_target.parent != root / AppDirectory.CONFIG.value:
            raise RestoreError("settings restore target is outside the Amadeus config directory")
        _reject_file_symlink(database_target)
        _reject_file_symlink(settings_target)

        transaction_id = uuid4().hex
        database_target.parent.mkdir(parents=True, exist_ok=True)
        settings_target.parent.mkdir(parents=True, exist_ok=True)
        marker_path = _restore_marker_path(paths, create_parent=True)
        files = _restore_transaction_files(paths, transaction_id)
    except DataManagementError:
        raise
    except OSError as exc:
        raise RestoreError("restore transaction could not be prepared safely") from exc
    replacer = replace_operation or _durable_replace
    marker: dict[str, Any] = {
        "format": RESTORE_TRANSACTION_FORMAT,
        "transaction_id": transaction_id,
        "state": "building",
    }
    committed = False
    try:
        _write_restore_marker(marker_path, marker)
        _run_restore_checkpoint(checkpoint, "marker_persisted")

        old_metadata: dict[str, Mapping[str, object] | None] = {}
        for key, file_set in files.items():
            old_metadata[key] = _snapshot_restore_target(file_set)
        database_files = files["database"]
        settings_files = files["settings"]
        assert database_files.new is not None
        assert settings_files.new is not None
        _copy_file_fsynced(payload.database_path, database_files.new)
        _copy_file_fsynced(payload.settings_path, settings_files.new)
        new_metadata = {
            "database": _restore_file_metadata(
                database_files.new,
                maximum_bytes=DEFAULT_BACKUP_LIMITS.database_bytes,
            ),
            "settings": _restore_file_metadata(
                settings_files.new,
                maximum_bytes=DEFAULT_BACKUP_LIMITS.settings_bytes,
            ),
        }
        marker = {
            **marker,
            "state": "prepared",
            "old": old_metadata,
            "new": new_metadata,
        }
        _write_restore_marker(marker_path, marker)
        _run_restore_checkpoint(checkpoint, "prepared")

        replacer(database_files.new, database_target)
        _sync_replaced_target(database_target)
        _run_restore_checkpoint(checkpoint, "database_replaced")
        replacer(settings_files.new, settings_target)
        _sync_replaced_target(settings_target)
        _remove_restore_target(files["database_wal"].target)
        _remove_restore_target(files["database_shm"].target)
        _sync_restore_directories(files, marker_path.parent)

        marker = {**marker, "state": "committed"}
        _write_restore_marker(marker_path, marker)
        committed = True
        _run_restore_checkpoint(checkpoint, "commit_persisted")
        _finish_restore_transaction(marker_path, marker, files)
    except Exception as install_error:  # noqa: BLE001 - rollback all filesystem failures
        try:
            recover_interrupted_restore(paths)
        except DataManagementError as recovery_error:
            raise RestoreRollbackError(
                "restore failed and exact rollback could not be completed"
            ) from recovery_error
        if committed:
            raise RestoreCleanupError(
                "restore succeeded but transaction cleanup could not be completed"
            ) from install_error
        raise RestoreError("restore failed; the previous data was restored") from install_error


def recover_interrupted_restore(paths: AppPaths) -> bool:
    """Recover one durable restore transaction before opening settings/SQLite.

    ``prepared`` always returns to the exact old four-file state. ``committed``
    keeps the verified new database/settings and completes cleanup.  Any
    malformed marker or missing rollback evidence fails closed.
    """

    try:
        marker_path = _restore_marker_path(paths, create_parent=False)
        if not _lexists(marker_path):
            return False
        _require_regular_restore_file(marker_path, "restore transaction marker")
        marker = _load_restore_marker(marker_path)
        transaction_id = str(marker["transaction_id"])
        files = _restore_transaction_files(paths, transaction_id)
        state = str(marker["state"])
        if state == "building":
            # No live target is ever changed until the durable ``prepared`` marker
            # exists, so a building transaction needs artifact cleanup only.
            _cleanup_restore_transaction(marker_path, files)
            return True
        if state == "prepared":
            _restore_old_transaction_files(marker_path, marker, files)
            return True
        if state == "committed":
            _verify_committed_restore(marker)
            _verify_new_restore_targets(marker, files)
            _remove_restore_target(files["database_wal"].target)
            _remove_restore_target(files["database_shm"].target)
            _sync_restore_directories(files, marker_path.parent)
            _finish_restore_transaction(marker_path, marker, files)
            return True
        if state == "rolled_back":
            _verify_old_restore_targets(marker, files)
            _cleanup_restore_transaction(marker_path, files)
            return True
        if state == "committed_cleaning":
            _verify_new_restore_targets(marker, files)
            _remove_restore_target(files["database_wal"].target)
            _remove_restore_target(files["database_shm"].target)
            _cleanup_restore_transaction(marker_path, files)
            return True
        raise RestoreError("restore transaction marker state is invalid")
    except DataManagementError:
        raise
    except OSError as exc:
        raise RestoreError("restore recovery filesystem is unavailable") from exc


def _restore_marker_path(paths: AppPaths, *, create_parent: bool) -> Path:
    plan = plan_factory_reset(paths)
    parent = plan.root / AppDirectory.BACKUPS.value
    if create_parent:
        parent.mkdir(parents=True, exist_ok=True)
        plan = plan_factory_reset(paths)
        parent = plan.root / AppDirectory.BACKUPS.value
    if parent.exists() and (not parent.is_dir() or _is_reparse_point(parent)):
        raise RestoreError("restore transaction directory is unsafe")
    marker = parent / _RESTORE_MARKER_NAME
    if marker.parent != parent:
        raise RestoreError("restore transaction marker escaped its directory")
    return marker


def _restore_transaction_files(
    paths: AppPaths,
    transaction_id: str,
) -> dict[str, _RestoreFileSet]:
    if _RESTORE_TRANSACTION_ID.fullmatch(transaction_id) is None:
        raise RestoreError("restore transaction ID is invalid")
    plan = plan_factory_reset(paths)
    database = _absolute(paths.database_file)
    settings = _absolute(paths.settings_file)
    if database.parent != plan.root / AppDirectory.DATA.value:
        raise RestoreError("restore database target escaped the data directory")
    if settings.parent != plan.root / AppDirectory.CONFIG.value:
        raise RestoreError("restore settings target escaped the config directory")
    targets = {
        "database": database,
        "database_wal": Path(f"{database}-wal"),
        "database_shm": Path(f"{database}-shm"),
        "settings": settings,
    }
    result: dict[str, _RestoreFileSet] = {}
    for key, target in targets.items():
        new = (
            target.with_name(f".{target.name}.{transaction_id}.new")
            if key in {"database", "settings"}
            else None
        )
        result[key] = _RestoreFileSet(
            target=target,
            rollback=target.with_name(f".{target.name}.{transaction_id}.rollback"),
            new=new,
            recovery=target.with_name(f".{target.name}.{transaction_id}.recover"),
        )
    return result


def _snapshot_restore_target(file_set: _RestoreFileSet) -> Mapping[str, object] | None:
    target = file_set.target
    if not _lexists(target):
        return None
    _require_regular_restore_file(target, "restore target")
    _copy_file_fsynced(target, file_set.rollback)
    return _restore_file_metadata(
        file_set.rollback,
        maximum_bytes=_restore_maximum_for_target(target),
        allow_empty=_restore_target_allows_empty(target),
    )


def _restore_maximum_for_target(target: Path) -> int:
    if target.name == "settings.json":
        return DEFAULT_BACKUP_LIMITS.settings_bytes
    return DEFAULT_BACKUP_LIMITS.database_bytes


def _restore_target_allows_empty(target: Path) -> bool:
    return target.name.endswith(("-wal", "-shm"))


def _restore_file_metadata(
    path: Path,
    *,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> dict[str, object]:
    size, digest = _file_size_and_sha256(
        path,
        maximum_bytes=maximum_bytes,
        error_type=RestoreError,
        allow_empty=allow_empty,
    )
    return {"size": size, "sha256": digest}


def _write_restore_marker(marker_path: Path, marker: Mapping[str, Any]) -> None:
    validated = _validate_restore_marker_document(marker)
    temporary: Path | None = None
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=marker_path.parent,
            prefix=f".{marker_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(_json_bytes(validated))
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(temporary, marker_path)
        temporary = None
    except (OSError, ValueError, TypeError) as exc:
        raise RestoreError("restore transaction marker could not be persisted") from exc
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _load_restore_marker(marker_path: Path) -> dict[str, Any]:
    try:
        if marker_path.stat().st_size > 64 * 1024:
            raise RestoreError("restore transaction marker is too large")
        document = _decode_json_object(
            marker_path.read_bytes(),
            "restore transaction marker",
            error_type=RestoreError,
        )
    except OSError as exc:
        raise RestoreError("restore transaction marker is unavailable") from exc
    return _validate_restore_marker_document(document)


def _validate_restore_marker_document(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RestoreError("restore transaction marker must be an object")
    state = value.get("state")
    transaction_id = value.get("transaction_id")
    if value.get("format") != RESTORE_TRANSACTION_FORMAT:
        raise RestoreError("restore transaction marker format is unsupported")
    if (
        not isinstance(transaction_id, str)
        or _RESTORE_TRANSACTION_ID.fullmatch(transaction_id) is None
    ):
        raise RestoreError("restore transaction marker ID is invalid")
    if state not in _RESTORE_TRANSACTION_STATES:
        raise RestoreError("restore transaction marker state is invalid")
    if state == "building":
        if set(value) != {"format", "transaction_id", "state"}:
            raise RestoreError("building restore transaction marker fields are invalid")
        return dict(value)
    if set(value) != {"format", "transaction_id", "state", "old", "new"}:
        raise RestoreError("restore transaction marker fields are invalid")
    old = value.get("old")
    new = value.get("new")
    if not isinstance(old, Mapping) or set(old) != set(_RESTORE_TARGET_KEYS):
        raise RestoreError("restore rollback manifest is invalid")
    if not isinstance(new, Mapping) or set(new) != {"database", "settings"}:
        raise RestoreError("restore replacement manifest is invalid")
    for key in _RESTORE_TARGET_KEYS:
        metadata = old[key]
        if metadata is not None:
            _validate_restore_file_metadata(
                metadata,
                maximum_bytes=(
                    DEFAULT_BACKUP_LIMITS.settings_bytes
                    if key == "settings"
                    else DEFAULT_BACKUP_LIMITS.database_bytes
                ),
                allow_empty=key in {"database_wal", "database_shm"},
            )
    _validate_restore_file_metadata(
        new["database"], maximum_bytes=DEFAULT_BACKUP_LIMITS.database_bytes
    )
    _validate_restore_file_metadata(
        new["settings"], maximum_bytes=DEFAULT_BACKUP_LIMITS.settings_bytes
    )
    return json.loads(json.dumps(value, allow_nan=False))


def _validate_restore_file_metadata(
    value: object,
    *,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, Mapping) or set(value) != {"size", "sha256"}:
        raise RestoreError("restore transaction file metadata is invalid")
    size = value.get("size")
    digest = value.get("sha256")
    minimum_size = 0 if allow_empty else 1
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not minimum_size <= size <= maximum_bytes
    ):
        raise RestoreError("restore transaction file size is invalid")
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise RestoreError("restore transaction file checksum is invalid")


def _restore_old_transaction_files(
    marker_path: Path,
    marker: Mapping[str, Any],
    files: Mapping[str, _RestoreFileSet],
) -> None:
    old = marker.get("old")
    if not isinstance(old, Mapping):
        raise RestoreError("restore rollback manifest is unavailable")
    for key, file_set in files.items():
        metadata = old.get(key)
        if metadata is None:
            continue
        _require_regular_restore_file(file_set.rollback, "restore rollback copy")
        _verify_restore_file_metadata(
            file_set.rollback,
            metadata,
            maximum_bytes=_restore_maximum_for_target(file_set.target),
            allow_empty=_restore_target_allows_empty(file_set.target),
        )
    for key, file_set in files.items():
        metadata = old.get(key)
        if metadata is None:
            _remove_restore_target(file_set.target)
            continue
        if _lexists(file_set.recovery):
            _require_regular_restore_file(file_set.recovery, "restore recovery temporary")
            file_set.recovery.unlink()
        _copy_file_fsynced(file_set.rollback, file_set.recovery)
        _durable_replace(file_set.recovery, file_set.target)
        _sync_replaced_target(file_set.target)
    _sync_restore_directories(files, marker_path.parent)
    _finish_restore_transaction(marker_path, marker, files)


def _verify_new_restore_targets(
    marker: Mapping[str, Any],
    files: Mapping[str, _RestoreFileSet],
) -> None:
    new = marker.get("new")
    if not isinstance(new, Mapping):
        raise RestoreError("restore replacement manifest is unavailable")
    for key in ("database", "settings"):
        file_set = files[key]
        _require_regular_restore_file(file_set.target, "committed restore target")
        _verify_restore_file_metadata(
            file_set.target,
            new[key],
            maximum_bytes=_restore_maximum_for_target(file_set.target),
        )


def _verify_old_restore_targets(
    marker: Mapping[str, Any],
    files: Mapping[str, _RestoreFileSet],
) -> None:
    old = marker.get("old")
    if not isinstance(old, Mapping):
        raise RestoreError("restore rollback manifest is unavailable")
    for key, file_set in files.items():
        metadata = old.get(key)
        if metadata is None:
            _remove_restore_target(file_set.target)
            continue
        _require_regular_restore_file(file_set.target, "rolled-back restore target")
        _verify_restore_file_metadata(
            file_set.target,
            metadata,
            maximum_bytes=_restore_maximum_for_target(file_set.target),
            allow_empty=_restore_target_allows_empty(file_set.target),
        )


def _verify_committed_restore(marker: Mapping[str, Any]) -> None:
    if marker.get("state") != "committed":
        raise RestoreError("restore transaction is not committed")


def _verify_restore_file_metadata(
    path: Path,
    metadata: object,
    *,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> None:
    _validate_restore_file_metadata(
        metadata,
        maximum_bytes=maximum_bytes,
        allow_empty=allow_empty,
    )
    assert isinstance(metadata, Mapping)
    actual = _restore_file_metadata(
        path,
        maximum_bytes=maximum_bytes,
        allow_empty=allow_empty,
    )
    if actual != dict(metadata):
        raise RestoreError("restore transaction file checksum does not match")


def _finish_restore_transaction(
    marker_path: Path,
    marker: Mapping[str, Any],
    files: Mapping[str, _RestoreFileSet],
) -> None:
    if marker.get("state") == "prepared":
        terminal_state = "rolled_back"
    elif marker.get("state") == "committed":
        terminal_state = "committed_cleaning"
    else:
        raise RestoreError("restore transaction cannot enter cleanup")
    finished = {**marker, "state": terminal_state}
    _write_restore_marker(marker_path, finished)
    _cleanup_restore_transaction(marker_path, files)


def _cleanup_restore_transaction(
    marker_path: Path,
    files: Mapping[str, _RestoreFileSet],
) -> None:
    for file_set in files.values():
        artifacts = (file_set.rollback, file_set.new, file_set.recovery)
        for artifact in artifacts:
            if artifact is None or not _lexists(artifact):
                continue
            _require_regular_restore_file(artifact, "restore transaction artifact")
            artifact.unlink()
    _sync_restore_directories(files, marker_path.parent)
    if _lexists(marker_path):
        _require_regular_restore_file(marker_path, "restore transaction marker")
        marker_path.unlink()
        _fsync_directory(marker_path.parent)


def _remove_restore_target(target: Path) -> None:
    if not _lexists(target):
        return
    _require_regular_restore_file(target, "restore target")
    target.unlink()
    _fsync_directory(target.parent)


def _require_regular_restore_file(path: Path, label: str) -> None:
    if not _lexists(path) or path.is_dir() or _is_reparse_point(path):
        raise RestoreError(f"{label} is unsafe or unavailable")
    if not path.is_file():
        raise RestoreError(f"{label} is not a regular file")


def _sync_replaced_target(target: Path) -> None:
    _require_regular_restore_file(target, "restored target")
    try:
        # Windows requires a writable handle for ``FlushFileBuffers`` exposed
        # through ``os.fsync``; these are application-owned restore targets.
        with target.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RestoreError("restored target could not be synchronized") from exc
    _fsync_directory(target.parent)


def _sync_restore_directories(
    files: Mapping[str, _RestoreFileSet],
    marker_parent: Path,
) -> None:
    parents = {marker_parent, *(file_set.target.parent for file_set in files.values())}
    for parent in parents:
        _fsync_directory(parent)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError as exc:
        raise RestoreError("restore directory metadata could not be synchronized") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _durable_replace(source: Path, destination: Path) -> None:
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            move_file = kernel32.MoveFileExW
            move_file.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
            move_file.restype = ctypes.c_int
            replace_existing = 0x1
            write_through = 0x8
            if not move_file(
                os.fspath(source),
                os.fspath(destination),
                replace_existing | write_through,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(source, destination)
            _fsync_directory(destination.parent)
    except OSError as exc:
        raise RestoreError("restore file replacement failed") from exc


def _run_restore_checkpoint(checkpoint: RestoreCheckpoint | None, name: str) -> None:
    if checkpoint is not None:
        checkpoint(name)


def discard_staged_restore(payload: ValidatedRestorePayload) -> None:
    """Delete only the generated staging directory represented by ``payload``."""

    if not isinstance(payload, ValidatedRestorePayload):
        raise BackupValidationError("restore payload is invalid")
    _validate_payload_paths(payload)
    _remove_staging_root(payload.staging_root, payload.staging_root.parent)


def plan_factory_reset(paths: AppPaths) -> FactoryResetPlan:
    """Return the seven exact data-region children; never delete them here."""

    try:
        return _plan_factory_reset(paths)
    except UnsafeResetPlanError:
        raise
    except OSError as exc:
        raise UnsafeResetPlanError("factory reset path metadata is unavailable") from exc


def _plan_factory_reset(paths: AppPaths) -> FactoryResetPlan:
    if not isinstance(paths, AppPaths):
        raise UnsafeResetPlanError("factory reset paths are invalid")
    root = _absolute(paths.root)
    if root.name.casefold() != "amadeus" or root == Path(root.anchor):
        raise UnsafeResetPlanError("factory reset root is not one Amadeus directory")
    if _is_reparse_point(paths.root):
        raise UnsafeResetPlanError("factory reset root cannot be a reparse point")
    resolved_root = root.resolve(strict=False)
    targets: list[Path] = []
    for region in AppDirectory:
        candidate = _absolute(paths.directory(region))
        expected = root / region.value
        if candidate != expected or candidate.parent != root:
            raise UnsafeResetPlanError("factory reset target escaped the Amadeus root")
        if candidate.resolve(strict=False) != resolved_root / region.value:
            raise UnsafeResetPlanError("factory reset target resolved outside the Amadeus root")
        if _is_reparse_point(paths.directory(region)):
            raise UnsafeResetPlanError("factory reset target cannot be a reparse point")
        targets.append(candidate)
    if len(set(targets)) != len(AppDirectory):
        raise UnsafeResetPlanError("factory reset targets are not unique")
    return FactoryResetPlan(root=root, targets=tuple(targets))


def _materialize_consistent_database(
    source: ConsistentBackupSource,
    workspace: Path,
) -> Path:
    if callable(source):
        requested = workspace / "amadeus.sqlite3"
        result = source(requested)
        candidate = requested if result is None else Path(result)
    else:
        candidate = Path(source)
    try:
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            raise BackupError("consistent database backup was not created")
    except OSError as exc:
        raise BackupError("consistent database backup is unavailable") from exc
    return candidate


def _parse_manifest(manifest: Mapping[str, Any], limits: BackupLimits) -> BackupMetadata:
    if set(manifest) != {"format", "created_at", "app_version", "schemas", "files"}:
        raise BackupValidationError("backup manifest fields are invalid")
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupValidationError("backup format is unsupported")
    created_at = manifest.get("created_at")
    app_version = manifest.get("app_version")
    if not isinstance(created_at, str) or not _valid_timestamp(created_at):
        raise BackupValidationError("backup timestamp is invalid")
    if not isinstance(app_version, str) or not app_version.strip() or len(app_version) > 128:
        raise BackupValidationError("backup application version is invalid")

    schemas = manifest.get("schemas")
    if not isinstance(schemas, Mapping) or set(schemas) != {"database", "settings"}:
        raise BackupValidationError("backup schema metadata is invalid")
    database_schema = _plain_non_negative_int(schemas.get("database"), "database schema")
    settings_schema = _plain_non_negative_int(schemas.get("settings"), "settings schema")
    if database_schema > SCHEMA_VERSION:
        raise BackupValidationError("database backup is newer than this application")
    if settings_schema > SETTINGS_SCHEMA_VERSION:
        raise BackupValidationError("settings backup is newer than this application")

    files = manifest.get("files")
    expected_files = {BACKUP_DATABASE_MEMBER, BACKUP_SETTINGS_MEMBER}
    if not isinstance(files, Mapping) or set(files) != expected_files:
        raise BackupValidationError("backup file manifest is invalid")
    database_size, database_hash = _parse_file_manifest(
        files[BACKUP_DATABASE_MEMBER], limits.database_bytes
    )
    settings_size, settings_hash = _parse_file_manifest(
        files[BACKUP_SETTINGS_MEMBER], limits.settings_bytes
    )
    return BackupMetadata(
        created_at=created_at,
        app_version=app_version,
        database_schema=database_schema,
        settings_schema=settings_schema,
        database_size=database_size,
        database_sha256=database_hash,
        settings_size=settings_size,
        settings_sha256=settings_hash,
    )


def _parse_file_manifest(value: object, maximum: int) -> tuple[int, str]:
    if not isinstance(value, Mapping) or set(value) != {"size", "sha256"}:
        raise BackupValidationError("backup file metadata is invalid")
    size = _plain_non_negative_int(value.get("size"), "backup member size")
    digest = value.get("sha256")
    if size <= 0 or size > maximum:
        raise BackupValidationError("backup member size is invalid")
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise BackupValidationError("backup member checksum is invalid")
    return size, digest


def _validate_zip_members(infos: Sequence[zipfile.ZipInfo], limits: BackupLimits) -> None:
    if len(infos) != len(BACKUP_MEMBERS):
        raise BackupValidationError("backup archive members are invalid")
    names = [info.filename for info in infos]
    if len(set(names)) != len(names) or set(names) != BACKUP_MEMBERS:
        raise BackupValidationError("backup archive members are invalid")
    member_limits = {
        BACKUP_MANIFEST_MEMBER: limits.manifest_bytes,
        BACKUP_DATABASE_MEMBER: limits.database_bytes,
        BACKUP_SETTINGS_MEMBER: limits.settings_bytes,
    }
    for info in infos:
        path = PurePosixPath(info.filename)
        if (
            path.is_absolute()
            or "\\" in info.filename
            or any(part in {"", ".", ".."} for part in path.parts)
            or info.is_dir()
            or info.flag_bits & 0x1
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        ):
            raise BackupValidationError("backup archive contains an unsafe member")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
            raise BackupValidationError("backup archive contains a symbolic link")
        if info.file_size <= 0 or info.file_size > member_limits[info.filename]:
            raise BackupValidationError("backup archive member size is invalid")
        if info.compress_size < 0:
            raise BackupValidationError("backup archive compressed size is invalid")


def _read_member_bytes(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    maximum_bytes: int,
) -> bytes:
    with archive.open(info, mode="r") as source:
        data = source.read(maximum_bytes + 1)
        if len(data) > maximum_bytes or source.read(1):
            raise BackupValidationError("backup archive member exceeds its limit")
    if len(data) != info.file_size:
        raise BackupValidationError("backup archive member size is inconsistent")
    return data


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    maximum_bytes: int,
) -> tuple[int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    digest = hashlib.sha256()
    with archive.open(info, mode="r") as source, target.open("xb") as destination:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise BackupValidationError("backup archive member exceeds its limit")
            digest.update(chunk)
            destination.write(chunk)
        destination.flush()
        os.fsync(destination.fileno())
    if total != info.file_size:
        raise BackupValidationError("backup archive member size is inconsistent")
    return total, digest.hexdigest()


def _validate_database_file(
    path: Path,
    *,
    current_schema: int,
    maximum_bytes: int,
    error_type: type[DataManagementError] = BackupValidationError,
) -> int:
    _file_size_and_sha256(path, maximum_bytes=maximum_bytes, error_type=error_type)
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if application_id != AMADEUS_APPLICATION_ID:
                raise error_type("database application ID is invalid")
            schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema <= 0:
                raise error_type("database schema is invalid")
            if schema > current_schema:
                raise error_type("database backup is newer than this application")
            if schema == current_schema:
                try:
                    validate_database_schema(connection)
                except DatabaseMigrationError as exc:
                    detail = str(exc)
                    if "foreign-key" in detail:
                        raise error_type("database foreign-key check failed") from exc
                    if "integrity" in detail:
                        raise error_type("database integrity check failed") from exc
                    raise error_type("database schema is invalid") from exc
                return schema
            required_tables = set(_V1_TABLES)
            if schema >= 2:
                required_tables.update(_V2_TABLES)
            if schema >= 3:
                required_tables.update(_V3_TABLES)
            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not required_tables.issubset(available):
                raise error_type("database schema is incomplete")
            quick_rows = connection.execute("PRAGMA quick_check").fetchall()
            if not quick_rows or any(str(row[0]) != "ok" for row in quick_rows):
                raise error_type("database integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise error_type("database foreign-key check failed")
            return schema
        finally:
            connection.close()
    except DataManagementError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise error_type("database backup is invalid") from exc


def _clone_settings(
    settings: Mapping[str, Any],
    *,
    error_type: type[DataManagementError],
) -> dict[str, Any]:
    if not isinstance(settings, Mapping):
        raise error_type("settings snapshot must be an object")
    _reject_sensitive_settings(settings, error_type=error_type)
    try:
        encoded = json.dumps(settings, ensure_ascii=False, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise error_type("settings snapshot is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise error_type("settings snapshot must be an object")
    return decoded


def _reject_sensitive_settings(
    value: object,
    *,
    error_type: type[DataManagementError],
) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            identifier = normalized.replace("-", "_")
            if normalized in _FORBIDDEN_SETTINGS_KEYS or identifier.endswith(
                _FORBIDDEN_SETTINGS_SUFFIXES
            ):
                raise error_type("sensitive values are forbidden in backup settings")
            _reject_sensitive_settings(nested, error_type=error_type)
    elif isinstance(value, list | tuple):
        for nested in value:
            _reject_sensitive_settings(nested, error_type=error_type)
    elif isinstance(value, str) and any(
        pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS
    ):
        raise error_type("sensitive values are forbidden in backup settings")


def _settings_schema(
    settings: Mapping[str, Any],
    *,
    error_type: type[DataManagementError],
) -> int:
    value = settings.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error_type("settings schema is invalid")
    return value


def _atomic_write_json(
    destination: Path,
    payload: Mapping[str, Any],
    *,
    error_type: type[DataManagementError],
) -> Path:
    try:
        data = _json_bytes(payload)
    except (TypeError, ValueError) as exc:
        raise error_type("export payload is not valid JSON") from exc
    temporary: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    except OSError as exc:
        raise error_type("JSON file could not be written atomically") from exc
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return destination


def _json_bytes(value: object) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    return f"{text}\n".encode()


def _decode_json_object(
    data: bytes,
    label: str,
    *,
    error_type: type[DataManagementError] = BackupValidationError,
) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise error_type(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label} must be a JSON object")
    return value


def _file_size_and_sha256(
    path: Path,
    *,
    maximum_bytes: int,
    error_type: type[DataManagementError],
    allow_empty: bool = False,
) -> tuple[int, str]:
    total = 0
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise error_type("file exceeds the configured limit")
                digest.update(chunk)
    except DataManagementError:
        raise
    except OSError as exc:
        raise error_type("file could not be read safely") from exc
    if total <= 0 and not allow_empty:
        raise error_type("file is empty")
    return total, digest.hexdigest()


def _copy_file_fsynced(source: Path, destination: Path) -> None:
    if _lexists(destination):
        raise RestoreError("restore temporary path already exists")
    try:
        with source.open("rb") as input_handle, destination.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
            output_handle.flush()
            os.fsync(output_handle.fileno())
    except OSError as exc:
        with suppress(OSError):
            destination.unlink(missing_ok=True)
        raise RestoreError("restore staging copy failed") from exc


def _validate_payload_paths(payload: ValidatedRestorePayload) -> None:
    root = _absolute(payload.staging_root)
    if not root.name.startswith(_STAGING_PREFIX) or payload.staging_root.is_symlink():
        raise BackupValidationError("restore staging root is unsafe")
    expected_database = root / BACKUP_DATABASE_MEMBER
    expected_settings = root / BACKUP_SETTINGS_MEMBER
    if _absolute(payload.database_path) != expected_database:
        raise BackupValidationError("staged database path is unsafe")
    if _absolute(payload.settings_path) != expected_settings:
        raise BackupValidationError("staged settings path is unsafe")
    if payload.database_path.is_symlink() or payload.settings_path.is_symlink():
        raise BackupValidationError("restore staging files cannot be symbolic links")


def _remove_staging_root(root: Path, expected_parent: Path) -> None:
    absolute_root = _absolute(root)
    absolute_parent = _absolute(expected_parent)
    if (
        absolute_root.parent != absolute_parent
        or not absolute_root.name.startswith(_STAGING_PREFIX)
        or root.is_symlink()
    ):
        raise BackupValidationError("restore staging cleanup target is unsafe")
    try:
        if _lexists(root):
            shutil.rmtree(root)
    except OSError as exc:
        raise BackupValidationError("restore staging cleanup failed") from exc


def _reject_file_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RestoreError("restore target cannot be a symbolic link")


def _plain_non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackupValidationError(f"{label} is invalid")
    return value


def _timestamp(value: datetime | None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return current.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _valid_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _is_reparse_point(path: Path) -> bool:
    """Reject symlinks and Windows junction/mount-point reparse entries."""

    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafeResetPlanError("factory reset path metadata is unavailable") from exc
    attributes = int(getattr(status, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & 0x400)
