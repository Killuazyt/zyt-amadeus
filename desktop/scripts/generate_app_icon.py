"""Generate the public Windows icon from the verified CC0 icon source."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from PIL import Image

ICON_SOURCE_SHA256 = "2d9795265224b99619d34320e57b070a081ebc1c55df0152fd3041242dbd953e"
FRAME_SIZE = (96, 112)
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
        if sheet.size != (768, 1008):
            raise SystemExit("application icon source dimensions do not match the public asset")
        frame = sheet.convert("RGBA").crop((0, 0, *FRAME_SIZE))

    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    scaled = frame.resize((192, 224), Image.Resampling.LANCZOS)
    canvas.alpha_composite(scaled, ((256 - 192) // 2, (256 - 224) // 2))
    canvas.save(output, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
