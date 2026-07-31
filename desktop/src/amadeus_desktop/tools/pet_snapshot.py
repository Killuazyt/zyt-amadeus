"""Capture a transparent pet-window snapshot and display metadata."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from amadeus_desktop.pet_assets import PetAssetError, PetAssetService, validate_package
from amadeus_desktop.ui.pet_window import PetWindow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture one P2 pet-window snapshot.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--scale-percent", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    application = QApplication(sys.argv[:1])
    try:
        asset = (
            validate_package(arguments.source)
            if arguments.source
            else PetAssetService(Path()).load_builtin()
        )
        window = PetWindow(asset, scale_percent=arguments.scale_percent)
        window.show_without_activate()
        loop = QEventLoop()
        QTimer.singleShot(250, loop.quit)
        loop.exec()
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        if not window.grab().save(str(arguments.output), "PNG"):
            raise PetAssetError(f"Could not save {arguments.output}.")
        screen = window.screen() or application.primaryScreen()
        metadata = {
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pet_id": asset.manifest.pet_id,
            "scale_percent": window.scale_percent,
            "window_size": [window.width(), window.height()],
            "screen": screen.name() if screen else None,
            "device_pixel_ratio": screen.devicePixelRatio() if screen else None,
            "logical_dpi": screen.logicalDotsPerInch() if screen else None,
            "qt_scale_factor": os.environ.get("QT_SCALE_FACTOR"),
        }
        arguments.output.with_suffix(".json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        window.hide()
        return 0
    except PetAssetError as exc:
        print(f"截图失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
