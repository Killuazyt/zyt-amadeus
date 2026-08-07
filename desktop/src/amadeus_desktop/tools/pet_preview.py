"""Interactive per-action preview and private contact-sheet exporter."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QVBoxLayout, QWidget

from amadeus_desktop.pet_assets import (
    LoadedPetAsset,
    PetAssetError,
    PetAssetService,
    builtin_pet_root,
    validate_package,
)


def _load_spritesheet(asset: LoadedPetAsset) -> QImage:
    sheet = QImage(str(asset.spritesheet_path))
    if sheet.isNull():
        raise PetAssetError(f"Could not decode spritesheet {asset.spritesheet_path}.")
    return sheet


def frame_image(
    asset: LoadedPetAsset,
    column: int,
    row: int,
    *,
    sheet: QImage | None = None,
) -> QImage:
    source = sheet if sheet is not None else _load_spritesheet(asset)
    spec = asset.manifest.spritesheet
    return source.copy(
        column * spec.frame_width,
        row * spec.frame_height,
        spec.frame_width,
        spec.frame_height,
    )


def export_contact_sheets(asset: LoadedPetAsset, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    spec = asset.manifest.spritesheet
    sheet = _load_spritesheet(asset)
    for index, (name, animation) in enumerate(asset.manifest.animations.items()):
        output = QImage(
            spec.frame_width * len(animation.frames),
            spec.frame_height,
            QImage.Format.Format_ARGB32,
        )
        output.fill(Qt.GlobalColor.transparent)
        painter = QPainter(output)
        for frame_index, frame in enumerate(animation.frames):
            image = frame_image(
                asset,
                frame.column,
                frame.row,
                sheet=sheet,
            )
            painter.drawImage(frame_index * spec.frame_width, 0, image)
        painter.end()
        path = destination / f"{index:02d}-{name}.png"
        if not output.save(str(path), "PNG"):
            raise PetAssetError(f"Could not save preview image {path}.")
        outputs.append(path)
    return outputs


class PreviewWindow(QWidget):
    def __init__(self, asset: LoadedPetAsset) -> None:
        super().__init__()
        self.asset = asset
        self.sheet = _load_spritesheet(asset)
        self.frame_index = 0
        self.selector = QComboBox()
        self.selector.addItems(list(asset.manifest.animations))
        self.selector.currentTextChanged.connect(self._reset)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        spec = asset.manifest.spritesheet
        self.preview_size = QSize(
            spec.logical_frame_width * 2,
            spec.logical_frame_height * 2,
        )
        self.image.setMinimumSize(self.preview_size)
        self.status = QLabel()
        layout = QVBoxLayout(self)
        layout.addWidget(self.selector)
        layout.addWidget(self.image, 1)
        layout.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance)
        self.setWindowTitle(f"Amadeus Pet Preview - {asset.manifest.display_name}")
        self._reset()

    def _current_animation(self):
        return self.asset.manifest.animations[self.selector.currentText()]

    def _reset(self) -> None:
        self.frame_index = 0
        self._show_frame()
        self.timer.start(round(1000 / self._current_animation().fps))

    def _advance(self) -> None:
        animation = self._current_animation()
        self.frame_index = (self.frame_index + 1) % len(animation.frames)
        self._show_frame()

    def _show_frame(self) -> None:
        animation = self._current_animation()
        frame = animation.frames[self.frame_index]
        pixmap = QPixmap.fromImage(
            frame_image(
                self.asset,
                frame.column,
                frame.row,
                sheet=self.sheet,
            )
        )
        self.image.setPixmap(
            pixmap.scaled(
                self.preview_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.status.setText(
            f"{animation.name} · frame {self.frame_index + 1}/{len(animation.frames)} · "
            f"{animation.fps:g} FPS"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview a .codex-pet directory action by action.")
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--export-dir",
        type=Path,
        help="write contact sheets without opening a window",
    )
    return parser


def _load_preview_asset(source: Path) -> LoadedPetAsset:
    """Use the narrowly pinned large-file exception only for the built-in package."""

    if source.resolve() == builtin_pet_root().resolve():
        return PetAssetService(source.parent).load_builtin()
    return validate_package(source)


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    try:
        asset = _load_preview_asset(arguments.source)
        if arguments.export_dir:
            for path in export_contact_sheets(asset, arguments.export_dir):
                print(path)
            return 0
        window = PreviewWindow(asset)
        window.show()
        return application.exec()
    except PetAssetError as exc:
        print(f"预览失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
