"""Safely import a local pet package while the main application is stopped."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication

from amadeus_desktop.paths import AppDirectory, AppPaths
from amadeus_desktop.pet_assets import PetAssetError, PetAssetService
from amadeus_desktop.settings import SettingsError, SettingsRepository
from amadeus_desktop.single_instance import DEFAULT_SERVER_NAME, SingleInstance, SingleInstanceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import and select a local Amadeus pet package.")
    parser.add_argument("source", type=Path, help=".codex-pet directory or archive")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an installed package with the same id",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    _application = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    guard = SingleInstance(DEFAULT_SERVER_NAME)
    try:
        if not guard.acquire():
            print("Amadeus 正在运行；请先从托盘彻底退出后再导入桌宠。", file=sys.stderr)
            return 2
        paths = AppPaths.for_current_user()
        paths.initialize()
        pets_root = paths.ensure(AppDirectory.PETS)
        asset = PetAssetService(pets_root).import_package(
            arguments.source,
            replace=arguments.replace,
        )
        repository = SettingsRepository(paths.settings_file)
        settings = repository.load_or_create()
        settings["pet"]["active_pet_id"] = asset.manifest.pet_id
        repository.save(settings)
    except (PetAssetError, SettingsError, SingleInstanceError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    finally:
        guard.close()
    print(f"已导入并选中：{asset.manifest.display_name} ({asset.manifest.pet_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
