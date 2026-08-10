"""Local application directory boundaries."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

_FOLDERID_LOCAL_APP_DATA = UUID("f1b32785-6fba-4fcf-9d55-7b8e7f157091")


class AppDirectory(StrEnum):
    """Known application data regions."""

    CONFIG = "config"
    DATA = "data"
    PETS = "pets"
    PERSONAS = "personas"
    MODELS = "models"
    BACKUPS = "backups"
    LOGS = "logs"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved paths for one local Amadeus installation."""

    root: Path

    @classmethod
    def for_current_user(cls, local_app_data: Path | None = None) -> AppPaths:
        r"""Resolve ``%LOCALAPPDATA%\Amadeus`` with an injectable acceptance root."""

        if local_app_data is None:
            configured = os.environ.get("LOCALAPPDATA")
            local_app_data = Path(configured) if configured else _known_local_app_data()
        return cls(root=local_app_data / "Amadeus")

    @classmethod
    def for_trusted_current_user(cls) -> AppPaths:
        """Resolve the destructive-maintenance root only through Windows Known Folder."""

        return cls(root=_known_local_app_data() / "Amadeus")

    def directory(self, region: AppDirectory) -> Path:
        return self.root / region.value

    @property
    def settings_file(self) -> Path:
        return self.directory(AppDirectory.CONFIG) / "settings.json"

    @property
    def log_file(self) -> Path:
        return self.directory(AppDirectory.LOGS) / "amadeus.log"

    @property
    def database_file(self) -> Path:
        """Return the single local SQLite database used by P5 and later phases."""

        return self.directory(AppDirectory.DATA) / "amadeus.sqlite3"

    @property
    def migration_backup_directory(self) -> Path:
        """Return the private directory reserved for automatic migration backups."""

        return self.directory(AppDirectory.BACKUPS) / "migrations"

    @property
    def attachments_directory(self) -> Path:
        """Return the only root allowed to contain managed conversation media."""

        return self.directory(AppDirectory.DATA) / "attachments"

    @property
    def embedding_model_directory(self) -> Path:
        """Return the fixed P5B embedding snapshot directory."""

        from amadeus_desktop.embedding_model import MODEL_REVISION

        return self.directory(AppDirectory.MODELS) / "bge-small-zh-v1.5" / MODEL_REVISION

    @property
    def persona_knowledge_file(self) -> Path:
        """Return the private local knowledge manifest for the active persona."""

        return self.directory(AppDirectory.PERSONAS) / "kurisu" / "knowledge.jsonl"

    def initialize(self) -> None:
        """Create only the directories needed during P1 startup."""

        self.root.mkdir(parents=True, exist_ok=True)
        self.ensure(AppDirectory.CONFIG)
        self.ensure(AppDirectory.LOGS)

    def ensure(self, region: AppDirectory) -> Path:
        """Create a known application region on demand."""

        path = self.directory(region)
        path.mkdir(parents=True, exist_ok=True)
        return path


def _known_local_app_data() -> Path:
    """Read FOLDERID_LocalAppData from the Windows shell, never from the environment."""

    if os.name != "nt":  # pragma: no cover - developer-only portability fallback
        return Path.home() / "AppData" / "Local"
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    except (AttributeError, OSError) as exc:  # pragma: no cover - Windows platform guard
        raise OSError("Windows Known Folder APIs are unavailable.") from exc
    guid_buffer = (ctypes.c_ubyte * 16).from_buffer_copy(_FOLDERID_LOCAL_APP_DATA.bytes_le)
    path_pointer = ctypes.c_void_p()
    shell32.SHGetKnownFolderPath.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole32.CoTaskMemFree.restype = None
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(guid_buffer),
        0,
        None,
        ctypes.byref(path_pointer),
    )
    if result != 0 or not path_pointer.value:
        if path_pointer.value:
            ole32.CoTaskMemFree(path_pointer)
        raise OSError("Windows LocalAppData Known Folder could not be resolved.")
    try:
        value = ctypes.wstring_at(path_pointer.value)
    finally:
        ole32.CoTaskMemFree(path_pointer)
    candidate = Path(value)
    if not value or not candidate.is_absolute():
        raise OSError("Windows LocalAppData Known Folder returned an invalid path.")
    return candidate
