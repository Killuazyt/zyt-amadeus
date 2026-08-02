"""Local application directory boundaries."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


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
        r"""Resolve ``%LOCALAPPDATA%\Amadeus`` without depending on Qt."""

        if local_app_data is None:
            configured = os.environ.get("LOCALAPPDATA")
            local_app_data = Path(configured) if configured else Path.home() / "AppData" / "Local"
        return cls(root=local_app_data / "Amadeus")

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
