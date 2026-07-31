"""Manual 50-click plus 50-drag acceptance counter for P2."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

from amadeus_desktop.pet_assets import PetAssetError, PetAssetService, validate_package
from amadeus_desktop.ui.pet_window import PetWindow


class InteractionAcceptance(QWidget):
    def __init__(self, pet: PetWindow, output: Path | None) -> None:
        super().__init__()
        self.pet = pet
        self.output = output
        self.phase = "click"
        self.clicks = 0
        self.drags = 0
        self.status = QLabel()
        self.status.setWordWrap(True)
        reset = QPushButton("重新开始")
        reset.clicked.connect(self.reset)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(reset)
        self.setWindowTitle("P2 点击/拖动验收")
        self.resize(420, 160)
        pet.clicked.connect(self._clicked)
        pet.drag_finished.connect(self._dragged)
        self.reset()

    def reset(self) -> None:
        self.phase = "click"
        self.clicks = 0
        self.drags = 0
        self._update("请先在宠物主体上连续点击 50 次；发生拖动即整轮重置。")

    def _clicked(self) -> None:
        if self.phase != "click":
            self.reset()
            self._update("拖动阶段检测到点击，整轮已重置。")
            return
        self.clicks += 1
        if self.clicks == 50:
            self.phase = "drag"
            self._update("点击阶段通过。现在连续拖动并松开 50 次；发生点击即整轮重置。")
        else:
            self._update("点击阶段进行中。")

    def _dragged(self) -> None:
        if self.phase != "drag":
            self.reset()
            self._update("点击阶段检测到拖动，整轮已重置。")
            return
        self.drags += 1
        if self.drags == 50:
            self.phase = "complete"
            self._update("通过：50 次点击和 50 次拖动均未互相误触。")
            self._write_result()
        else:
            self._update("拖动阶段进行中。")

    def _update(self, message: str) -> None:
        self.status.setText(f"点击 {self.clicks}/50 · 拖动 {self.drags}/50\n{message}")

    def _write_result(self) -> None:
        if self.output is None:
            return
        self.output.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pet_id": self.pet.asset.manifest.pet_id,
            "clicks": self.clicks,
            "drags": self.drags,
            "passed": True,
        }
        self.output.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the P2 manual input acceptance counter.")
    parser.add_argument("--source", type=Path, help="optional .codex-pet directory")
    parser.add_argument(
        "--output",
        type=Path,
        help="write a result JSON after all 100 operations pass",
    )
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
        counter = InteractionAcceptance(pet, arguments.output)
        counter.show()
        pet.move(counter.geometry().right() + 40, counter.geometry().top())
        pet.show_without_activate()
        exit_code = application.exec()
        pet.hide()
        return exit_code
    except PetAssetError as exc:
        print(f"验收工具启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
