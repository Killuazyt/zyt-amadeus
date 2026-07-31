"""Measure visible idle CPU and verify hidden animation suspension."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from amadeus_desktop.pet_assets import PetAssetError, PetAssetService, validate_package
from amadeus_desktop.ui.pet_window import PetWindow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure P2 pet idle and hidden behavior.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--visible-seconds", type=float, default=30.0)
    parser.add_argument("--hidden-seconds", type=float, default=10.0)
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
        pet = PetWindow(asset)
    except PetAssetError as exc:
        print(f"运行探针启动失败：{exc}", file=sys.stderr)
        return 1

    frame_count = 0
    visible_start_cpu = 0.0
    visible_start_wall = 0.0
    hidden_start_cpu = 0.0
    hidden_start_wall = 0.0
    hidden_start_frames = 0
    document: dict[str, object] = {}

    def count_frame(_frame) -> None:
        nonlocal frame_count
        frame_count += 1

    def start_visible_sample() -> None:
        nonlocal visible_start_cpu, visible_start_wall
        visible_start_cpu = time.process_time()
        visible_start_wall = time.monotonic()
        QTimer.singleShot(round(arguments.visible_seconds * 1000), finish_visible_sample)

    def finish_visible_sample() -> None:
        nonlocal hidden_start_cpu, hidden_start_wall, hidden_start_frames
        visible_cpu = time.process_time() - visible_start_cpu
        visible_wall = time.monotonic() - visible_start_wall
        document["visible_cpu_percent_of_one_core"] = visible_cpu / visible_wall * 100
        document["visible_frames"] = frame_count
        pet.hide()
        hidden_start_cpu = time.process_time()
        hidden_start_wall = time.monotonic()
        hidden_start_frames = frame_count
        QTimer.singleShot(round(arguments.hidden_seconds * 1000), finish_hidden_sample)

    def finish_hidden_sample() -> None:
        hidden_cpu = time.process_time() - hidden_start_cpu
        hidden_wall = time.monotonic() - hidden_start_wall
        hidden_frames = frame_count - hidden_start_frames
        screen = application.primaryScreen()
        document.update(
            {
                "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "pet_id": asset.manifest.pet_id,
                "visible_seconds": arguments.visible_seconds,
                "hidden_seconds": arguments.hidden_seconds,
                "hidden_cpu_percent_of_one_core": hidden_cpu / hidden_wall * 100,
                "hidden_frames": hidden_frames,
                "cpu_target_percent": 2.0,
                "cpu_target_passed": document["visible_cpu_percent_of_one_core"] < 2.0,
                "hidden_timer_passed": hidden_frames == 0,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "logical_cpu_count": os.cpu_count(),
                "screen": screen.name() if screen else None,
                "screen_device_pixel_ratio": screen.devicePixelRatio() if screen else None,
                "screen_logical_dpi": screen.logicalDotsPerInch() if screen else None,
            }
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        application.quit()

    pet.animation.frame_changed.connect(count_frame)
    pet.show_without_activate()
    QTimer.singleShot(2000, start_visible_sample)
    exit_code = application.exec()
    print(json.dumps(document, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
