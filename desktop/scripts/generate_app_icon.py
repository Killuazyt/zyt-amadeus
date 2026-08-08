"""Generate the Windows icon from the pinned Kurisu application portrait."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from PIL import Image

ICON_SOURCE_SHA256 = "ded30eeb568f26e3df64998131698e472603bf48d531885b27a7c04293d3b0b5"
ICON_SOURCE_SIZE = (1254, 1254)
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    source = arguments.source.resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise SystemExit("application icon source is unsafe")
    if hashlib.sha256(source.read_bytes()).hexdigest() != ICON_SOURCE_SHA256:
        raise SystemExit("application icon source hash does not match the CC0 asset")

    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as sheet:
        sheet.load()
        if sheet.format != "PNG" or sheet.size != ICON_SOURCE_SIZE:
            raise SystemExit("application icon source metadata does not match the pinned asset")
        icon = sheet.convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)

    icon.save(output, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
